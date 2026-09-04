# Builds the single junction network for CMH Road x 100 Feet Road out of the
# OSM download. Four netconvert passes, then a few hand repairs for things OSM
# didn't say. Pass 4 exists because --tls.unset in pass 3 only half works.
#
# Everything we change by hand gets appended to build/network-changelog.txt,
# because all of these failed *silently* the first time round - not one of them
# printed an error.

import math
import re
import subprocess
from datetime import datetime, timezone

import sumolib

from . import config as C

RAW_NET = C.BUILD / "indiranagar_raw.net.xml"
CHANGELOG = C.BUILD / "network-changelog.txt"

_COMPASS = [("E", 0.0), ("N", 90.0), ("W", 180.0), ("S", 270.0)]

# netconvert's "no value" sentinel. Turns up as the y of a joined node and
# nothing downstream complains.
INVALID_COORD = -1073741824.0

SATELLITE_RADIUS_M = 60.0

# which movement starts on each arm and which one ends there
ARM_APPROACH_MOVEMENT = {"N": "NS", "S": "SN", "E": "EW", "W": "WE"}
ARM_DEPART_MOVEMENT = {"N": "SN", "S": "NS", "E": "WE", "W": "EW"}


def bearing_deg(x0, y0, x1, y1):
    """angle of (x0,y0)->(x1,y1), 0 = east, counter clockwise"""
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0


def angle_err(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def classify_arm(angle):
    best, best_err = "N", 999.0
    for name, ref in _COMPASS:
        err = angle_err(angle, ref)
        if err < best_err:
            best, best_err = name, err
    return best


def edge_bearing(edge):
    shape = edge.getShape()
    return bearing_deg(shape[0][0], shape[0][1], shape[-1][0], shape[-1][1])


class Arm:
    """One of the four approaches, in network ids only."""

    def __init__(self, name, incoming_edge, outgoing_edge, length_m, bearing):
        self.name = name
        self.incoming_edge = incoming_edge
        self.outgoing_edge = outgoing_edge
        self.entry_edge = ""
        self.exit_edge = ""
        self.approach_edges = []
        self.depart_edges = []
        self.length_m = length_m
        self.bearing = bearing

    def as_dict(self):
        return {"name": self.name,
                "incoming_edge": self.incoming_edge,
                "outgoing_edge": self.outgoing_edge,
                "entry_edge": self.entry_edge,
                "exit_edge": self.exit_edge,
                "approach_edges": list(self.approach_edges),
                "depart_edges": list(self.depart_edges),
                "length_m": round(self.length_m, 1),
                "bearing": round(self.bearing, 1)}


class ArmFinder:
    """Walks the built network and works out where the four arms are.

    Nothing is hardcoded here, the ids change on every rebuild.
    """

    def __init__(self, net):
        self.net = net
        self.node = net.getNode(C.JUNCTION_ID)

    def find(self):
        jx, jy = self.node.getCoord()
        arms = {}
        for inc in self.node.getIncoming():
            fx, fy = inc.getFromNode().getCoord()
            bearing = bearing_deg(jx, jy, fx, fy)
            name = classify_arm(bearing)
            # two edges snapping to the same compass point: keep the longer
            # one, that's the road and not a service lane
            if name in arms and inc.getLength() <= arms[name].length_m:
                continue
            out = self._matching_outgoing(bearing)
            arms[name] = Arm(name, inc.getID(),
                             out.getID() if out is not None else "",
                             inc.getLength(), bearing)

        missing = sorted(set("NESW") - set(arms))
        if missing:
            raise RuntimeError("Junction " + C.JUNCTION_ID + " is missing arms "
                               + str(missing) + "; found " + str(sorted(arms)))

        for arm in arms.values():
            self._extend(arm)
        return arms

    def _matching_outgoing(self, bearing):
        jx, jy = self.node.getCoord()
        best, best_err = None, 999.0
        for out in self.node.getOutgoing():
            tx, ty = out.getToNode().getCoord()
            err = angle_err(bearing_deg(jx, jy, tx, ty), bearing)
            if err < best_err:
                best, best_err = out, err
        return best if best_err < 45.0 else None

    @staticmethod
    def _straightest(candidates, want, skip_id, dead_end_node):
        """Pick whichever candidate edge best continues the direction `want`."""
        best, best_err = None, 999.0
        for cand in candidates:
            if cand.getID() == skip_id or dead_end_node(cand):
                continue
            err = angle_err(edge_bearing(cand), want)
            if err < best_err:
                best, best_err = cand, err
        return best if best_err < 50.0 else None

    def _predecessor(self, edge):
        return self._straightest(edge.getFromNode().getIncoming(),
                                 edge_bearing(edge), edge.getID(),
                                 lambda c: c.getFromNode() is edge.getToNode())

    def _successor(self, edge):
        return self._straightest(edge.getToNode().getOutgoing(),
                                 edge_bearing(edge), edge.getID(),
                                 lambda c: c.getToNode() is edge.getFromNode())

    def _extend(self, arm):
        """Walk outward along the arm until we've covered ARM_RADIUS_M of road,
        following the straightest continuation so we stay on the same road."""
        inc = self.net.getEdge(arm.incoming_edge)     # works, leave it alone
        chain, seen, total, cur = [inc], {inc.getID()}, inc.getLength(), inc
        while total < C.ARM_RADIUS_M:
            nxt = self._predecessor(cur)
            if nxt is None or nxt.getID() in seen:
                break
            chain.append(nxt)
            seen.add(nxt.getID())
            total += nxt.getLength()
            cur = nxt
        arm.approach_edges = [e.getID() for e in reversed(chain)]
        arm.length_m = total
        arm.entry_edge = arm.approach_edges[0]

        # mirror it on the way out so vehicles can leave the model
        out_chain, seen_out, dist = [], set(), 0.0
        out = self.net.getEdge(arm.outgoing_edge) if arm.outgoing_edge else None
        while out is not None and out.getID() not in seen_out and dist < C.ARM_RADIUS_M:
            out_chain.append(out)
            seen_out.add(out.getID())
            dist += out.getLength()
            out = self._successor(out)
        arm.depart_edges = [e.getID() for e in out_chain]
        arm.exit_edge = arm.depart_edges[-1] if arm.depart_edges else arm.outgoing_edge


def discover_arms(net):
    return ArmFinder(net).find()


def load_arms():
    return discover_arms(sumolib.net.readNet(str(C.JUNCTION_NET)))


class NetBuilder:
    """The four netconvert passes plus the hand repairs afterwards."""

    def __init__(self):
        self.changelog = []
        self.satellites = []

    # -- little helpers ----------------------------------------------------

    def note(self, msg):
        self.changelog.append(msg)
        print("  . " + msg)

    def flush(self, header):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if not CHANGELOG.exists():
            CHANGELOG.write_text("Network changelog - what the tool generated "
                                 "and what we changed by hand.\n", encoding="utf-8")
        with CHANGELOG.open("a", encoding="utf-8") as fh:
            fh.write("\n== " + header + " - " + stamp + "\n\n")
            for m in self.changelog:
                fh.write("- " + m + "\n")
        self.changelog = []

    def netconvert(self, args, log, lefthand=True):
        """Run netconvert, dump output to a log, blow up loudly if it fails."""
        cmd = [str(C.NETCONVERT_BIN)] + list(args)
        if lefthand and C.LEFT_HAND_TRAFFIC:
            cmd.append("--lefthand")
        logfile = C.BUILD / log
        with logfile.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
        if proc.returncode != 0:
            tail = logfile.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError("netconvert failed (exit " + str(proc.returncode)
                               + ")\n" + tail)

    def repatch(self, flag, patch_file, log, extra=None):
        """Re-run netconvert over the built junction with one patch applied.
        The flags matter as much as the patch - rebuild without --lefthand or
        --no-turnarounds and they quietly revert."""
        args = ["--sumo-net-file", str(C.JUNCTION_NET),
                flag, str(patch_file),
                "--no-turnarounds",
                "--tls.default-type", "static",
                "--output-file", str(C.JUNCTION_NET)]
        self.netconvert(args + list(extra or []), log)
        self.fix_joined_node_y(C.JUNCTION_NET)

    def fix_joined_node_y(self, net_file):
        """A node made by a <join> comes out with a good x, a good shape, good
        internal lanes - and y set to the invalid sentinel. Nothing errors,
        sumolib just reports the junction 1073 km south of its own arms. Take
        the centroid of the shape instead."""
        text = net_file.read_text(encoding="utf-8")
        pattern = re.compile(r'(<junction id="' + re.escape(C.JUNCTION_ID)
                             + r'"[^>]*?y=")([^"]*)(")')
        match = pattern.search(text)
        if match is None:
            raise RuntimeError("Junction " + C.JUNCTION_ID + " not in " + str(net_file))
        if abs(float(match.group(2)) - INVALID_COORD) > 1.0:
            return                                   # already fine
        tag = text[match.start(): text.index(">", match.start())]
        shape = re.search(r'shape="([^"]*)"', tag)
        if shape is None:
            raise RuntimeError("Can't repair " + C.JUNCTION_ID + ", no shape.")
        ys = [float(p.split(",")[1]) for p in shape.group(1).split()]
        centroid_y = sum(ys) / len(ys)
        net_file.write_text(text[:match.start(2)] + format(centroid_y, ".2f")
                            + text[match.end(2):], encoding="utf-8")
        self.note("Repaired the joined node's y - netconvert left it at its "
                  "invalid sentinel (" + format(INVALID_COORD, ".0f")
                  + "). Recomputed from the junction shape centroid ("
                  + format(centroid_y, ".2f") + ").")

    # -- pass 1: OSM -> wide net -------------------------------------------

    def build_wide_net(self):
        self.netconvert([
            "--osm-files", str(C.OSM_FILE),
            "--type-files", str(C.TYPEMAP),
            "--proj.utm",
            "--junctions.join", "true",
            "--junctions.join-dist", "20",
            "--keep-edges.by-vclass", "passenger",
            "--remove-edges.isolated",
            "--geometry.remove",
            "--no-turnarounds",
            "--tls.guess-signals",
            "--tls.join", "false",
            "--tls.default-type", "static",
            "--output-file", str(RAW_NET)], "netconvert_raw.log")
        self.note("Pass 1 wrote " + RAW_NET.name + " straight from OSM, built "
                  "with --lefthand. No node file on this pass: handing one to "
                  "--osm-files makes netconvert write a broken netOffset for "
                  "the whole network.")

    # -- pass 2: join the four OSM nodes into one --------------------------

    def join_junction(self):
        C.JOIN_NODES.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<nodes>\n'
            '    <join nodes="' + " ".join(C.CLUSTER_NODES) + '" id="'
            + C.JUNCTION_ID + '" type="traffic_light"/>\n</nodes>\n',
            encoding="utf-8")
        self.note("Pass 2 joined OSM nodes " + ", ".join(C.CLUSTER_NODES)
                  + " into '" + C.JUNCTION_ID + "'. netconvert won't do it by "
                  "itself ('it contains a pt stop edge') and a wider join "
                  "distance swallows seventeen nodes including the neighbouring "
                  "signals. Naming the four is the fix.")
        self.netconvert([
            "--sumo-net-file", str(RAW_NET),
            "--node-files", str(C.JOIN_NODES),
            "--junctions.join", "true",
            "--junctions.join-dist", "20",
            "--no-turnarounds",
            "--tls.guess-signals",
            "--tls.join", "false",
            "--tls.default-type", "static",
            "--output-file", str(C.WIDE_NET)], "netconvert_join.log")
        self.fix_joined_node_y(C.WIDE_NET)
        self.note("Pass 2 wrote " + C.WIDE_NET.name + ".")

    # -- pass 3: crop to the four arms -------------------------------------

    @staticmethod
    def _keep_set(net, arms):
        keep = set()
        for arm in arms.values():
            keep.update(arm.approach_edges)
            keep.update(arm.depart_edges)
            keep.add(arm.incoming_edge)
            if arm.outgoing_edge:
                keep.add(arm.outgoing_edge)
        # keep the opposite carriageway too, dropping half a two way pair
        # strands the arm
        for eid in list(keep):
            other = eid[1:] if eid.startswith("-") else "-" + eid
            if net.hasEdge(other):
                keep.add(other)
        return sorted(keep)

    @staticmethod
    def _satellite_signals(net):
        """Pedestrian crossing signals sitting on our own approaches. OSM tags
        one 14-19 m before each stop line and netconvert makes each an
        independent traffic light, so vehicles stop twice in twenty metres."""
        jx, jy = net.getNode(C.JUNCTION_ID).getCoord()
        out = []
        for tls in net.getTrafficLights():
            if tls.getID() == C.JUNCTION_ID:
                continue
            node = net.getNode(tls.getID())
            x, y = node.getCoord()
            if math.hypot(x - jx, y - jy) > SATELLITE_RADIUS_M:
                continue
            if len(node.getIncoming()) == 1 and len(node.getOutgoing()) == 1:
                out.append(tls.getID())
        return sorted(out)

    def retype_satellites(self):
        """Pass 4. Make the demoted crossings actually stop being traffic lights.

        --tls.unset in pass 3 drops the traffic light *program* but leaves the
        junction typed traffic_light, and netconvert will not dissolve a node
        of that type. It looked done - the net had exactly one TLS, which is
        what everything checked - while three signal-typed junctions with no
        program sat 17-19 m short of the stop line. Vehicles halted on their
        internal lanes with their tails across the main junction, which is the
        car that stops dead just past the intersection.

        Retyping has to be its own netconvert run. Handing the same invocation
        both --tls.unset and the node patch leaves them traffic_light anyway.

        Measured: retyping on its own changes no traffic outcome - these nodes
        are one-in one-out with nothing to cross, so a queue spanning one
        blocks nobody. It is here because the network should say what is true.
        Actually dissolving them was tried and is worse; see the comment on
        the repatch call below.
        """
        if not self.satellites:
            return {"retyped": [], "still_signals": []}

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<nodes>"]
        for node_id in self.satellites:
            lines.append('    <node id="' + node_id + '" type="priority"/>')
        lines.append("</nodes>")
        patch_file = C.BUILD / "satellites.nod.xml"
        patch_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Retype only. Adding --geometry.remove here also merges the edges
        # either side of each dissolved node, and on the west departing
        # carriageway that collapsed two edges into a zero-length one with a
        # single lane-1-to-lane-0 connection - a one lane pinch that jammed
        # the junction far worse than the signals ever did.
        self.repatch("--node-files", patch_file, "netconvert_satellites.log")

        net = sumolib.net.readNet(str(C.JUNCTION_NET))
        still_signals = [n.getID() for n in net.getNodes()
                         if n.getID() in set(self.satellites)
                         and n.getType() == "traffic_light"]

        self.note("Retyped " + str(len(self.satellites)) + " demoted crossings "
                  "from traffic_light to priority. --tls.unset in pass 3 takes "
                  "the program away but leaves the type, and the type is what "
                  "everything downstream reads."
                  + (" Still typed traffic_light: " + ", ".join(still_signals)
                     + "." if still_signals else ""))
        return {"retyped": list(self.satellites), "still_signals": still_signals}

    def build_junction_net(self):
        net = sumolib.net.readNet(str(C.WIDE_NET))
        arms = discover_arms(net)
        for arm in arms.values():
            self.note("Arm " + arm.name + ": enters at " + arm.entry_edge
                      + ", reaches the junction on " + arm.incoming_edge + ", "
                      + format(arm.length_m, ".0f") + " m of approach.")

        keep = self._keep_set(net, arms)
        keep_file = C.BUILD / "keep_edges.txt"
        keep_file.write_text("\n".join(keep) + "\n", encoding="utf-8")
        self.note("Cropped the 700 m download down to " + str(len(keep))
                  + " edges (" + format(C.ARM_RADIUS_M, ".0f") + " m per arm).")

        args = ["--sumo-net-file", str(C.WIDE_NET),
                "--keep-edges.input-file", str(keep_file),
                "--remove-edges.isolated",
                "--no-turnarounds",
                "--geometry.remove",
                "--tls.join", "false",
                "--tls.default-type", "static",
                "--output-file", str(C.JUNCTION_NET)]
        self.satellites = self._satellite_signals(net)
        if self.satellites:
            args += ["--tls.unset", ",".join(self.satellites)]
            self.note("Dropped the traffic light programs from "
                      + str(len(self.satellites)) + " pedestrian crossings "
                      "within " + format(SATELLITE_RADIUS_M, ".0f") + " m ("
                      + ", ".join(self.satellites) + "). OSM tags one 14-19 m "
                      "before each stop line and netconvert makes each an "
                      "independent signal, so vehicles stop twice in twenty "
                      "metres. Retyping and dissolving them is pass 4.")
        self.netconvert(args, "netconvert_crop.log")
        self.fix_joined_node_y(C.JUNCTION_NET)
        self.note("Pass 3 wrote " + C.JUNCTION_NET.name + ".")
        return arms

    # -- repair 1: lane counts ---------------------------------------------

    @staticmethod
    def _osm_way_tags():
        text = C.OSM_FILE.read_text(encoding="utf-8")
        tags = {}
        for match in re.finditer(r'<way id="(\d+)".*?</way>', text, re.S):
            tags[match.group(1)] = dict(
                re.findall(r'<tag k="([^"]+)" v="([^"]*)"', match.group(0)))
        return tags

    @staticmethod
    def _way_id(edge_id):
        return edge_id.lstrip("-").split("#")[0]

    def normalise_lane_counts(self, arms):
        """Untagged segments adopt the lane count their own road is tagged with.

        Four chunks of CMH Road carry no `lanes` tag at all - same road, same
        name, same class as their neighbours - and netconvert falls back to one
        lane. That's a 228 m single lane pinch in the middle of a two lane
        arterial that nothing reports and that dominates every travel time
        measured through it.

        A segment that IS explicitly tagged never gets touched, however
        annoying its value. If a road's tagged segments disagree we leave it
        alone and write down that they disagreed.
        """
        tags = self._osm_way_tags()
        net = sumolib.net.readNet(str(C.JUNCTION_NET))
        patches, evidence = {}, []

        for arm in arms.values():
            by_road = {}
            for edge_id in list(arm.approach_edges) + list(arm.depart_edges):
                way = tags.get(self._way_id(edge_id), {})
                if way.get("name"):
                    by_road.setdefault(way["name"], []).append((edge_id, way))

            for road, members in by_road.items():
                tagged, untagged = {}, []
                for edge_id, way in members:
                    if way.get("lanes", "").isdigit():
                        tagged.setdefault(int(way["lanes"]), []).append(edge_id)
                    else:
                        untagged.append(edge_id)
                if not untagged or not tagged:
                    continue
                if len(tagged) > 1:
                    evidence.append("Arm " + arm.name + ", " + road + ": tagged "
                                    "segments disagree " + str(sorted(tagged))
                                    + ", left alone.")
                    continue
                count = next(iter(tagged))
                for edge_id in untagged:
                    if not net.hasEdge(edge_id):
                        continue
                    if net.getEdge(edge_id).getLaneNumber() >= count:
                        continue
                    patches[edge_id] = count
                    other = edge_id[1:] if edge_id.startswith("-") else "-" + edge_id
                    if net.hasEdge(other) and net.getEdge(other).getLaneNumber() < count:
                        patches[other] = count
                evidence.append("Arm " + arm.name + ", " + road + ": "
                                + str(len(untagged)) + " untagged segment(s) vs "
                                + str(len(tagged[count])) + " tagged lanes="
                                + str(count) + ".")

        if not patches:
            self.note("Lane counts: nothing to repair.")
            return {"patched": {}, "evidence": evidence}

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<edges>"]
        for edge_id, count in sorted(patches.items()):
            lines.append('    <edge id="' + edge_id + '" numLanes="'
                         + str(count) + '"/>')
        lines.append("</edges>")
        patch_file = C.BUILD / "lanes.edg.xml"
        patch_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.repatch("--edge-files", patch_file, "netconvert_lanes.log")

        self.note("Widened " + str(len(patches)) + " edge(s) to the lane count "
                  "their own road is tagged with: " + ", ".join(sorted(patches))
                  + ". " + " ".join(evidence))
        return {"patched": patches, "evidence": evidence}

    # -- repair 2: lane assignment at the stop line ------------------------

    def normalise_junction_connections(self, arms):
        """Give all four approaches the same lane assignment at the stop line.

        netconvert gave north and south the sensible arrangement (nearside lane
        = left turn + through, offside = through + right turn) and gave east
        and west the through movement on the offside lane ONLY, shared with the
        right turn. In left hand traffic a right turner waits for a gap, so one
        of them stops the whole approach. Measured: with right turns removed
        the junction carries 2000 veh/h fine, with them it gridlocks.

        This does not remove the U turn connections, by the way - a
        --connection-files patch adds and overrides, it doesn't delete what it
        doesn't mention. All eight survive and still hold eight of the 32
        signal links. No route uses one so nobody sits on them, but they're
        there.
        """
        net = sumolib.net.readNet(str(C.JUNCTION_NET), withConnections=True)
        entries, changes = [], []

        for name in sorted(arms):
            edge = net.getEdge(arms[name].incoming_edge)
            if edge.getLaneNumber() < 2:
                continue

            targets = {}
            for out, conns in edge.getOutgoing().items():
                for conn in conns:
                    targets.setdefault(conn.getDirection(), out.getID())
            through = targets.get("s")
            if through is None:
                continue

            before = sorted((c.getFromLane().getIndex(), o.getID(), c.getDirection())
                            for o, conns in edge.getOutgoing().items() for c in conns)
            through_lanes = {lane for lane, _t, direction in before
                             if direction == "s"}

            plan = []
            if targets.get("l"):
                plan.append((0, targets["l"], 0))
            plan.append((0, through, 0))
            plan.append((1, through, 1))
            if targets.get("r"):
                plan.append((1, targets["r"], 0))
                plan.append((1, targets["r"], 1))
            for from_lane, to_edge, to_lane in plan:
                entries.append((edge.getID(), from_lane, to_edge, to_lane))

            if 0 not in through_lanes:
                changes.append("arm " + name + " carried the through movement "
                               "on the offside lane only")
            uturns = [d for _l, _t, d in before if d in ("t", "T")]
            if uturns:
                changes.append("arm " + name + " keeps " + str(len(uturns))
                               + " unused U-turn connection(s)")

        if not entries:
            return {"changed": []}

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<connections>"]
        for from_edge, from_lane, to_edge, to_lane in entries:
            lines.append('    <connection from="' + from_edge + '" to="' + to_edge
                         + '" fromLane="' + str(from_lane) + '" toLane="'
                         + str(to_lane) + '"/>')
        lines.append("</connections>")
        patch_file = C.BUILD / "connections.con.xml"
        patch_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.repatch("--connection-files", patch_file, "netconvert_connections.log")

        self.note("Gave all four approaches the same lane assignment at the "
                  "stop line (nearside: left + through, offside: through + "
                  "right). netconvert had done this for north and south but "
                  "not east and west, where "
                  + ("; ".join(sorted(set(changes))) or "the assignment differed")
                  + ".")
        return {"changed": sorted(set(changes)), "connections": len(entries)}

    # -- repair 3: speed limits from what was observed ---------------------

    def apply_observed_speeds(self, arms):
        """OSM leaves these roads untagged so netconvert falls back to the road
        type default - 100 km/h on an Indiranagar arterial. Nothing errors, the
        model just drives four times too fast between signals.

        The replacements are observations out of the flow segment sheet, not a
        parameter we picked, so this isn't the demand dial under another name.
        """
        from . import archive as A

        observed = A.observed_free_flow_kmh()
        if not observed:
            self.note("No observed free flow speeds in the archive, arm speed "
                      "limits left at netconvert's defaults. Travel times will "
                      "be too low, don't compare them with the archive.")
            return {}

        speeds = {}
        for name, arm in arms.items():
            approach = observed.get(ARM_APPROACH_MOVEMENT[name])
            depart = observed.get(ARM_DEPART_MOVEMENT[name])
            for edge_id in arm.approach_edges:
                if approach:
                    speeds[edge_id] = approach / 3.6
            for edge_id in arm.depart_edges:
                if depart:
                    speeds[edge_id] = depart / 3.6

        text = C.JUNCTION_NET.read_text(encoding="utf-8")
        changed = 0

        def patch_edge(match):
            nonlocal changed
            block = match.group(0)
            edge_id = re.search(r'id="([^"]+)"', block).group(1)
            if edge_id not in speeds:
                return block
            target = format(speeds[edge_id], ".2f")
            block, count = re.subn(r'(<lane [^>]*?speed=")([^"]*)(")',
                                   lambda m: m.group(1) + target + m.group(3),
                                   block)
            changed += count
            return block

        text = re.sub(r"<edge [^>]*>.*?</edge>", patch_edge, text, flags=re.S)
        C.JUNCTION_NET.write_text(text, encoding="utf-8")

        summary = {m: format(v, ".1f") + " km/h" for m, v in sorted(observed.items())}
        self.note("Set arm speed limits from the archive's observed free flow "
                  "speeds " + str(summary) + ", replacing the 100 km/h road "
                  "type default on " + str(changed) + " lanes.")
        return observed

    # -- the whole thing ---------------------------------------------------

    def run(self):
        print("Building junction network...")
        self.build_wide_net()
        self.join_junction()
        self.build_junction_net()
        self.retype_satellites()
        # re-discover after every pass that rewrites the file, the ids move
        self.normalise_lane_counts(load_arms())
        self.normalise_junction_connections(load_arms())
        self.apply_observed_speeds(load_arms())
        self.flush("Junction network build")
        return load_arms()


def build(force=False):
    if C.JUNCTION_NET.exists() and not force:
        return load_arms()
    return NetBuilder().run()


if __name__ == "__main__":
    import json
    result = build(force=True)
    print(json.dumps({k: v.as_dict() for k, v in sorted(result.items())}, indent=2))
