# Running the junction.
#
# run_once()    - one seed, headless, played to the end. Deterministic, which
#                 is what makes paired seed comparison mean anything.
# LiveSession   - the same model stepped in real time and steerable from
#                 outside, so the interface can change the signal or drop a cow
#                 in the road while it's running.
#
# Both measure the same way the archive does, so a number on screen and a
# number in the validation table are the same kind of number.

import itertools
import math
import os
import random
import statistics

import sumolib
import traci

from . import config as C
from . import demand as D
from . import netbuild
from . import tls as T
from .stats import Stats

WARMUP_S = 300.0

OBSTRUCTION_TYPES = {
    "cow": {"label": "Cow", "length": 2.2, "width": 1.0, "colour": "180,120,80",
            "default_duration_s": 120.0},
    "stalled_vehicle": {"label": "Stalled vehicle", "length": 4.2, "width": 1.8,
                        "colour": "160,160,160", "default_duration_s": 180.0},
    "roadworks": {"label": "Roadworks", "length": 8.0, "width": 2.2,
                  "colour": "255,140,0", "default_duration_s": 600.0},
}

# how far back from the stop line an obstruction goes. far enough to sit in
# moving traffic instead of in the queue that's already stopped.
OBSTRUCTION_SETBACK_M = 70.0

# random jitter applied to that setback per obstruction, so N on one arm
# don't all land on the exact same spot / lane.
OBSTRUCTION_JITTER_M = 20.0

# most obstructions one arm can carry at once - a dial, not a toggle.
OBSTRUCTION_MAX_PER_ARM = 4

# A self-triggered stop goes somewhere in the middle of the vehicle's route,
# not at the stop line: a bus loading passengers or a truck backing into a
# shop does it where the shop is, and where the queue already stands nothing
# it does makes any difference.
HMV_STOP_MARGIN_M = 8.0

_obstruction_seq = itertools.count()


def movement_routes(arms):
    """archive movement id -> model route id, off the network's own geometry.

    A movement is named for the arm it enters from and the arm it leaves by,
    same as the archive names it. Which arm a left turn leaves by comes from
    demand.turn_matrix, not from a table typed by hand.

    Returns the mapping plus any clashes - if two turns off one arm land on the
    same arm they don't have separate corridors, and that gets reported instead
    of quietly overwritten.
    """
    matrix = D.turn_matrix(arms)
    routes, clashes = {}, []
    for turn in ("through", "left", "right"):
        for from_arm in sorted(arms):
            to_arm = matrix.get((from_arm, turn))
            if not to_arm or to_arm == from_arm:
                continue
            movement = from_arm + to_arm
            if movement in routes:
                clashes.append("Movement " + movement + " is reached by two "
                               "turns off arm " + from_arm + ". Only the first "
                               "is measured.")
                continue
            routes[movement] = "r_" + from_arm + "_" + turn
    return routes, clashes


class RunResult:
    def __init__(self, seed, movements, approaches, overall, plan, demand, warnings):
        self.seed = seed
        self.movements = movements
        self.approaches = approaches
        self.overall = overall
        self.plan = plan
        self.demand = demand
        self.warnings = warnings

    def to_dict(self):
        return {"seed": self.seed, "movements": self.movements,
                "approaches": self.approaches, "overall": self.overall,
                "plan": self.plan, "demand": self.demand,
                "warnings": self.warnings}


class Model:
    """Network facts every run needs. Built once per process and cached."""

    def __init__(self):
        self.net = sumolib.net.readNet(str(C.JUNCTION_NET), withPrograms=True,
                                       withConnections=True)
        self.arms = netbuild.discover_arms(self.net)
        self.links = T.build_link_map(self.net, self.arms)
        self.movement_route, self.movement_warnings = movement_routes(self.arms)
        self.free_flow = {}
        self.corridor_length = {}
        self.route_length = {}
        self.gate = {}          # movement -> (start_m, end_m) along its route
        # queues measured over the whole approach, not the edge that meets the
        # junction - after --geometry.remove that edge can be under 4 m long
        # and a queue counted on it is limited by the measurement, not the signal
        self.queue_edges = {name: list(arm.approach_edges)
                            for name, arm in self.arms.items()}

    def measure_corridors(self, route_edges):
        """Pin each movement's measurement window to the archive's endpoints.

        TomTom measures between two fixed points ~200 m either side of the
        junction. Our arms are longer than that, so timing a vehicle from where
        it enters the network to where it leaves would compare a 950 m run
        against a 406 m measurement and call the difference model error.

        So project each endpoint onto the route, turn it into a distance along
        the route, and time the vehicle between those two odometer readings.
        """
        for movement, route_id in self.movement_route.items():
            edges = route_edges.get(route_id)
            if not edges:
                continue

            marks, cumulative = self._route_marks(edges)
            self.route_length[movement] = cumulative

            window = C.ARCHIVE_ENDPOINTS.get(movement)
            start, end = 0.0, cumulative
            if window and marks:
                gates = []
                for lat, lon in window:
                    gx, gy = self.net.convertLonLat2XY(lon, lat)
                    best = min(marks, key=lambda m: (m[1] - gx) ** 2 + (m[2] - gy) ** 2)
                    gates.append(best[0])
                lo, hi = min(gates), max(gates)
                if hi - lo >= 50.0:       # under that the projection failed
                    start, end = lo, hi

            self.gate[movement] = (start, end)
            self.corridor_length[movement] = end - start
            self.free_flow[movement] = self._free_flow_over(edges, start, end)

    def _route_marks(self, edges):
        """(distance along route, x, y) for every shape point on the route."""
        marks, cumulative = [], 0.0
        for edge_id in edges:
            shape = self.net.getEdge(edge_id).getShape()
            for i in range(len(shape) - 1):
                (x0, y0), (x1, y1) = shape[i], shape[i + 1]
                marks.append((cumulative, x0, y0))
                cumulative += math.hypot(x1 - x0, y1 - y0)
            marks.append((cumulative, shape[-1][0], shape[-1][1]))
        return marks, cumulative

    def _free_flow_over(self, edges, start, end):
        """Time to cover [start, end] at the network's speed limits."""
        cumulative, total = 0.0, 0.0
        for edge_id in edges:
            edge = self.net.getEdge(edge_id)
            length = edge.getLength()
            overlap = min(cumulative + length, end) - max(cumulative, start)
            if overlap > 0:
                total += overlap / max(edge.getSpeed(), 1.0)
            cumulative += length
        return total


_MODEL = None


# one per process. reading the net + walking the arms takes about a second and
# every run in a pool would otherwise pay it again
def get_model():
    global _MODEL
    if _MODEL is None:
        _MODEL = Model()
    return _MODEL


def _route_edges_from_file():
    import re
    text = C.ROUTES_FILE.read_text(encoding="utf-8")
    return {m.group(1): m.group(2).split()
            for m in re.finditer(r'<route id="([^"]+)" edges="([^"]+)"', text)}


# Off by default, and measured that way rather than assumed.
#
# 8 seeds at 2400 veh/h, with and without --ignore-junction-blocker 15:
#
#                    off      on
#   mean blocked    0.35    0.41    worse
#   worst at once    7.0     5.0    better
#   vehicles >5s    16.5    11.5    better
#   worst single    32.5s  108.8s   worse
#
# It does not fix the thing it was tried for. The long stalls - 180 to 198 s -
# survive in both arms (3 seeds of 8 off, 4 of 8 on), because this option
# governs whether a vehicle ENTERS a blocked junction, not whether one already
# stopped inside it gets out. Those 180 s figures are pinned to
# --time-to-teleport above: the vehicle sits until SUMO gives up and teleports
# it, which is what actually ends the stall.
#
# What it does buy is a 30% cut in the moderate cases and 2-7% off travel times,
# for no verdict change on any of the 12 movements. But moving travel times at
# all means recalibrating, and turning the seeds over shows this junction is
# bimodal near capacity, so at 8 seeds none of the above is well resolved.
#
# Left off: it does not solve the reported symptom and it perturbs a calibrated
# model for a gain that does not separate from the noise.
IGNORE_JUNCTION_BLOCKER = os.environ.get("ABHYAS_IGNORE_JUNCTION_BLOCKER", "")


def _sumo_command(gui, seed, extra=None):
    cmd = [str(C.SUMO_GUI_BIN if gui else C.SUMO_BIN),
           "-c", str(C.SUMO_CFG),
           "--seed", str(seed),
           "--no-warnings", "true",
           "--no-step-log", "true",
           "--time-to-teleport", "180",
           "--lateral-resolution", format(D.LATERAL_RESOLUTION, ".2f")]
    if IGNORE_JUNCTION_BLOCKER:
        cmd += ["--ignore-junction-blocker", IGNORE_JUNCTION_BLOCKER]
    if extra:
        cmd += list(extra)
    return cmd


def apply_plan(model, plan, conn=traci):
    """Install a signal plan, looking the link numbering up right now."""
    phases = T.build_program(plan, model.links)
    logic = conn.trafficlight.Logic(
        programID="abhyas", type=0, currentPhaseIndex=0,
        phases=[conn.trafficlight.Phase(p.duration, p.state, name=p.name)
                for p in phases])
    conn.trafficlight.setProgramLogic(C.JUNCTION_ID, logic)
    conn.trafficlight.setProgram(C.JUNCTION_ID, "abhyas")


def apply_access_restrictions(model, spec, conn=traci):
    """Ban vehicle classes from an approach at the lane level.

    A ban, not a closure: the lane stays in the network and everything else
    keeps using it. demand.write_routes has already stopped releasing the
    banned class on that arm - this is what stops anything else reaching it,
    and what makes the restriction visible in the GUI.

    Two classes share the 'passenger' vClass (car and auto-rickshaw), and SUMO
    bans by vClass, so banning one at lane level would ban the other. Those are
    left to the flow drop alone and the caller is told so rather than being
    handed a ban that quietly took a second class off the road with it.
    """
    notes, applied = [], []
    for arm, classes in (spec.access_restrictions or {}).items():
        if arm not in model.arms:
            continue
        shared = {c: D.VEHICLE_CLASSES[c]["vClass"] for c in classes
                  if c in D.VEHICLE_CLASSES}
        for name, vclass in shared.items():
            others = [n for n, s in D.VEHICLE_CLASSES.items()
                      if s["vClass"] == vclass and n != name]
            if others:
                notes.append(
                    D.VEHICLE_CLASSES[name]["label"] + " on the " + arm
                    + " approach shares SUMO's '" + vclass + "' class with "
                    + ", ".join(D.VEHICLE_CLASSES[n]["label"] for n in others)
                    + ", so the ban is enforced by withholding the demand, not "
                      "by closing the lane to that class.")
                continue
            for edge_id in model.arms[arm].approach_edges:
                edge = model.net.getEdge(edge_id)
                for lane in edge.getLanes():
                    current = set(lane.getPermissions() or ())
                    try:
                        conn.lane.setDisallowed(lane.getID(), [vclass])
                    except traci.TraCIException:
                        continue
                    applied.append({"arm": arm, "class": name,
                                    "lane": lane.getID(), "vClass": vclass,
                                    "allowed_before": sorted(current)})
    return {"applied": applied, "notes": notes}


class Collector:
    """Accumulates the same quantities the archive reports."""

    def __init__(self, model, warmup_s, measure_until_s=float("inf"), spec=None):
        self.model = model
        self.warmup_s = warmup_s
        # A batch run keeps stepping for ten minutes after the flows stop so
        # the last vehicles can clear. That tail runs near free flow into an
        # emptying network, which is not the state the archive measured, so
        # vehicles entering the window after demand ended get timed but not
        # counted.
        self.measure_until_s = measure_until_s
        # the spec is here for the fleet levers only - what fraction of heavy
        # vehicles stop unscheduled, and how many people each class carries.
        self.spec = spec or D.DemandSpec()
        # own stream, seeded off the run's seed, so rolling for a bus stop
        # can't shift the sequence SUMO draws from and un-pair the seeds
        self.rng = random.Random("hmv-stops-" + str(self.spec.seed))
        self.route_of = {}
        self.class_of = {}       # vid -> vehicle class, for person throughput
        self.arrived_by_class = {}
        self.hmv_stops = []      # every self-triggered stop, not a count
        self.hmv_stop_rolls = 0          # how many came up for a stop
        self.hmv_stops_unplaced = 0      # and how many found nowhere to take it
        self.tracked = {}        # vid -> [movement, entered_at or None]
        self.durations = {m: [] for m in model.movement_route}
        self.arrived = {arm: 0 for arm in "NESW"}
        self.queue_samples = {arm: [] for arm in "NESW"}
        self.wait_samples = {arm: [] for arm in "NESW"}
        self.teleported = 0
        self.collisions = 0
        self.loaded = 0
        self.abandoned = 0
        self._route_to_movement = {r: m for m, r in model.movement_route.items()}

    def step(self, now, conn=traci):
        self._on_departed(conn)
        self._time_tracked(now, conn)
        self._on_arrived(conn)

        self.teleported += conn.simulation.getStartingTeleportNumber()
        self.collisions += conn.simulation.getCollidingVehiclesNumber()

        if now >= self.warmup_s:
            for arm, edges in self.model.queue_edges.items():
                self.queue_samples[arm].append(
                    sum(conn.edge.getLastStepHaltingNumber(e) for e in edges))
                self.wait_samples[arm].append(
                    sum(conn.edge.getWaitingTime(e) for e in edges))

    def _on_departed(self, conn):
        for vid in conn.simulation.getDepartedIDList():
            self.loaded += 1
            try:
                route = conn.vehicle.getRouteID(vid)
                self.class_of[vid] = conn.vehicle.getTypeID(vid)
            except traci.TraCIException:
                continue
            self.route_of[vid] = route
            self._maybe_stop(vid, conn)
            movement = self._route_to_movement.get(route)
            if movement is not None and movement in self.model.gate:
                self.tracked[vid] = [movement, None]
                conn.vehicle.subscribe(vid, (traci.constants.VAR_DISTANCE,))

    def _maybe_stop(self, vid, conn):
        """Roll for a bus that stops to load, or a truck that parks half in.

        Same TraCI call the cow and the roadworks already use. What's different
        is who triggers it: this is one of the flow's own vehicles stopping
        unscheduled, not an obstruction somebody placed, which is the thing
        the feedback was actually asking to see.
        """
        rate = self.spec.hmv_stop_rate
        vclass = self.class_of.get(vid)
        spec = D.VEHICLE_CLASSES.get(vclass, {})
        if not rate or not spec.get("heavy"):
            return
        if self.rng.random() >= rate:
            return
        try:
            edges = list(conn.vehicle.getRoute(vid))
        except traci.TraCIException:
            return
        # never the edge it's departing on and never the one it leaves by: a
        # stop at either end is an insertion failure or an exit, not a stop
        self.hmv_stop_rolls += 1
        candidates = edges[1:-1] or edges[1:]
        # try them all, shortest-first failures and TraCI refusals included:
        # taking the first pick and giving up turned a declared rate of 1.0
        # into a realised rate of about 0.3, which is a dial that lies
        self.rng.shuffle(candidates)
        for edge_id in candidates:
            try:
                edge = self.model.net.getEdge(edge_id)
            except KeyError:
                continue
            length = edge.getLength()
            if length < 2 * HMV_STOP_MARGIN_M:
                continue
            position = self.rng.uniform(HMV_STOP_MARGIN_M,
                                        length - HMV_STOP_MARGIN_M)
            lane_index = self.rng.randrange(edge.getLaneNumber())
            low, high = spec.get("stop_duration_s", (15.0, 60.0))
            duration = self.rng.uniform(low, high)
            try:
                # no parking flag: it stops IN the lane, which is the point
                conn.vehicle.setStop(vid, edge_id, pos=position,
                                     laneIndex=lane_index, duration=duration)
            except traci.TraCIException:
                continue
            self.hmv_stops.append({"vehicle": vid, "class": vclass,
                                   "edge": edge_id, "lane": lane_index,
                                   "position_m": round(position, 1),
                                   "duration_s": round(duration, 1)})
            return
        # nowhere on this vehicle's route was long enough to stop in
        self.hmv_stops_unplaced += 1

    def _time_tracked(self, now, conn):
        """Time each tracked vehicle between the archive's two points."""
        if not self.tracked:
            return
        distances = conn.vehicle.getAllSubscriptionResults()
        for vid, state in list(self.tracked.items()):
            data = distances.get(vid)
            if data is None:
                continue
            travelled = data.get(traci.constants.VAR_DISTANCE)
            if travelled is None:
                continue
            movement, entered = state
            start, end = self.model.gate[movement]
            if entered is None:
                if travelled >= start:
                    state[1] = now
            elif travelled >= end:
                if self.warmup_s <= entered <= self.measure_until_s:
                    self.durations[movement].append(now - entered)
                del self.tracked[vid]
                conn.vehicle.unsubscribe(vid)

    def _on_arrived(self, conn):
        for vid in conn.simulation.getArrivedIDList():
            route = self.route_of.pop(vid, None)
            vclass = self.class_of.pop(vid, None)
            if vclass in D.VEHICLE_CLASSES:
                self.arrived_by_class[vclass] = \
                    self.arrived_by_class.get(vclass, 0) + 1
            if vid in self.tracked:
                # left without finishing the measured window - teleported or
                # removed. counted, never guessed at.
                del self.tracked[vid]
                self.abandoned += 1
            if route is None:
                continue
            arm = route.split("_")[1] if route.startswith("r_") else None
            if arm in self.arrived:
                self.arrived[arm] += 1

    # -- turning it into a result ------------------------------------------

    def result(self, seed, elapsed_s, plan, spec):
        movements = {m: self._movement_row(m) for m in self.model.movement_route}
        measured_s = max(1.0, elapsed_s - self.warmup_s)
        approaches = {arm: self._approach_row(arm, measured_s) for arm in "NESW"}

        all_queue = [q for arm in "NESW" for q in self.queue_samples[arm]]
        completed = sum(self.arrived.values())
        # A scheme's whole case is that a bus carries 40 people and a car
        # carries 1.2. Counting vehicles alone cannot express that, so the
        # same completions are also reported as people - on declared
        # occupancies, which are a prior and not a measurement.
        people = sum(count * D.occupancy(name)
                     for name, count in self.arrived_by_class.items())
        overall = {
            "vehicles_loaded": self.loaded,
            "vehicles_completed": completed,
            "teleported": self.teleported,
            "queue_mean_veh": round(statistics.fmean(all_queue), 2) if all_queue else 0.0,
            "throughput_veh_per_hour": round(completed * 3600.0 / measured_s, 1),
            "person_throughput_per_hour": round(people * 3600.0 / measured_s, 1),
            "people_completed": round(people, 1),
            "vehicles_by_class": dict(self.arrived_by_class),
            "modal_split": {name: round(count / completed, 4)
                            for name, count in self.arrived_by_class.items()}
                           if completed else {},
            "person_modal_split": {
                name: round(count * D.occupancy(name) / people, 4)
                for name, count in self.arrived_by_class.items()} if people else {},
            "hmv_self_stops": len(self.hmv_stops),
            "hmv_stops_rolled": self.hmv_stop_rolls,
            "hmv_stops_unplaced": self.hmv_stops_unplaced,
            "gridlocked": self.teleported > max(10, 0.05 * max(self.loaded, 1)),
        }
        return RunResult(seed, movements, approaches, overall, T.describe(plan),
                         spec.to_dict(), self._warnings(movements))

    def _movement_row(self, movement):
        samples = self.durations[movement]
        free_flow = self.model.free_flow.get(movement, 0.0)
        row = {"label": C.ALL_MOVEMENTS.get(movement, {}).get("label", movement),
               "n": len(samples),
               "travel_time_s": None, "travel_time_mean_s": None,
               "travel_time_p90_s": None, "delay_s": None,
               "free_flow_s": round(free_flow, 1),
               "corridor_length_m": round(
                   self.model.corridor_length.get(movement, 0.0), 1)}
        if samples:
            median = statistics.median(samples)
            row["travel_time_s"] = round(median, 1)
            row["travel_time_mean_s"] = round(statistics.fmean(samples), 1)
            row["travel_time_p90_s"] = round(Stats.quantile(samples, 0.9), 1)
            row["delay_s"] = round(max(0.0, median - free_flow), 1)
        return row

    def _approach_row(self, arm, measured_s):
        queue = self.queue_samples[arm]
        wait = self.wait_samples[arm]
        return {
            "queue_mean_veh": round(statistics.fmean(queue), 2) if queue else 0.0,
            "queue_p90_veh": round(Stats.quantile(queue, 0.9), 2) if queue else 0.0,
            "waiting_time_mean_s": round(statistics.fmean(wait), 2) if wait else 0.0,
            "throughput_veh_per_hour": round(self.arrived[arm] * 3600.0 / measured_s, 1),
        }

    def _warnings(self, movements):
        warnings = list(self.model.movement_warnings)
        if self.spec.is_exploratory:
            warnings.append(
                "This run moved a fleet or access lever, so it is exploratory: "
                "those levers are declared and swept, not fitted, and nothing "
                "in the validation archive constrains them. Compare it against "
                "a baseline, don't quote it on its own.")
        if self.hmv_stop_rolls:
            realised = len(self.hmv_stops) / self.hmv_stop_rolls
            warnings.append(
                str(len(self.hmv_stops)) + " heavy vehicle(s) stopped "
                "unscheduled mid-route (buses loading, trucks parked half in a "
                "lane). Rolled at hmv_stop_rate="
                + format(self.spec.hmv_stop_rate, ".2f") + ", which is a "
                "declared rate, not a counted one.")
            if self.hmv_stops_unplaced:
                warnings.append(
                    str(self.hmv_stops_unplaced) + " of " +
                    str(self.hmv_stop_rolls) + " rolled stops found no stretch "
                    "of road long enough to take one, so the realised rate is "
                    + format(self.spec.hmv_stop_rate * realised, ".2f")
                    + " and not " + format(self.spec.hmv_stop_rate, ".2f")
                    + ". The dial is the ask; this is what the network allowed.")
        if any(self.arrived_by_class.get(c) for c in D.HEAVY_CLASSES) or \
                self.spec.injected or self.spec.mode_shift:
            warnings.append(
                "Person throughput uses the declared occupancies in "
                "demand.VEHICLE_CLASSES. They are a prior. A different "
                "occupancy per bus moves that number without anything on the "
                "road changing.")
        if self.abandoned:
            warnings.append(str(self.abandoned) + " vehicle(s) left the network "
                            "without finishing the measured window, excluded "
                            "from the travel times above.")
        if self.teleported:
            warnings.append(str(self.teleported) + " vehicle(s) teleported past "
                            "a blockage. A handful is normal, a lot means "
                            "gridlock and the run shouldn't be trusted.")
        if self.collisions:
            warnings.append(str(self.collisions) + " collision event(s).")
        for movement, data in movements.items():
            if data["n"] == 0:
                warnings.append("No vehicle completed movement " + movement + ".")
        return warnings


def run_once(spec=None, plan=None, seed=1, warmup_s=WARMUP_S, obstructions=None):
    """Play one seed of one scenario to the end and measure it."""
    spec = spec or D.DemandSpec()
    spec.seed = seed
    plan = plan or T.baseline_plan()

    model = get_model()
    D.prepare(spec, arms=model.arms, net=model.net)
    model.measure_corridors(_route_edges_from_file())

    # pid in the label because the pool runs four of these at once and traci
    # gets very confused if two connections share a name
    label = "abhyas-" + str(os.getpid()) + "-" + str(seed)
    traci.start(_sumo_command(gui=False, seed=seed), label=label)
    conn = traci.getConnection(label)
    try:
        apply_plan(model, plan, conn)
        apply_access_restrictions(model, spec, conn)
        for obstruction in (obstructions or []):
            place_obstruction(model, obstruction, conn=conn, now=0.0)

        collector = Collector(model, warmup_s, measure_until_s=spec.duration_s,
                              spec=spec)
        now = 0.0
        end = spec.duration_s + 600.0        # let the stragglers clear
        while now < end and conn.simulation.getMinExpectedNumber() > 0:
            conn.simulationStep()
            now = conn.simulation.getTime()
            collector.step(now, conn)
        return collector.result(seed, now, plan, spec)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---- the cow on the road -------------------------------------------------

def _point_upstream(model, arm, setback_m):
    """(edge, position) roughly setback_m back from the stop line. Walks the
    arm's own chain so it lands on real road whatever netconvert did."""
    chain = list(model.arms[arm].approach_edges)
    travelled = 0.0
    for edge_id in reversed(chain):                 # nearest the junction first
        edge = model.net.getEdge(edge_id)
        length = edge.getLength()
        if travelled + length >= setback_m:
            offset = length - (setback_m - travelled)
            return edge_id, max(1.0, min(offset, length - 1.0))
        travelled += length
    edge = model.net.getEdge(chain[0])
    return chain[0], max(1.0, edge.getLength() * 0.5)


def _obstruction_vtype(kind, conn=traci):
    spec = OBSTRUCTION_TYPES[kind]
    type_id = "obstruction_" + kind
    if type_id not in conn.vehicletype.getIDList():
        conn.vehicletype.copy("car", type_id)
        conn.vehicletype.setLength(type_id, spec["length"])
        conn.vehicletype.setWidth(type_id, spec["width"])
        conn.vehicletype.setMaxSpeed(type_id, 0.1)
        red, green, blue = (int(v) for v in spec["colour"].split(","))
        conn.vehicletype.setColor(type_id, (red, green, blue, 255))
    return type_id


def place_obstruction(model, obstruction, conn=traci, now=0.0):
    """Park something that never moves in a lane so traffic has to squeeze past.

    Modelled as a stopped vehicle rather than a lane closure - a closure takes
    the lane out of routing, an obstruction leaves it there, which is what
    actually happens.
    """
    kind = obstruction.get("kind", "cow")
    arm = obstruction.get("arm", "N")
    spec = OBSTRUCTION_TYPES[kind]
    duration = float(obstruction.get("duration_s") or spec["default_duration_s"])

    # jitter the setback and randomise the lane so several obstructions on one
    # arm spread out instead of stacking on the same point in the same lane -
    # each caller that doesn't pin position_m/lane gets its own spot.
    base_setback = float(obstruction.get("setback_m") or OBSTRUCTION_SETBACK_M)
    if obstruction.get("position_m") is None and obstruction.get("setback_m") is None:
        base_setback += random.uniform(-OBSTRUCTION_JITTER_M, OBSTRUCTION_JITTER_M)
    setback = max(5.0, base_setback)
    edge_id, position = _point_upstream(model, arm, setback)
    edge = model.net.getEdge(edge_id)
    lane_count = edge.getLaneNumber()
    if obstruction.get("lane") is not None:
        lane_index = min(max(int(obstruction["lane"]), 0), lane_count - 1)
    else:
        lane_index = random.randrange(lane_count)
    if obstruction.get("position_m") is not None:
        position = min(float(obstruction["position_m"]), edge.getLength() - 1.0)

    type_id = _obstruction_vtype(kind, conn)
    vehicle_id = ("obstruction_" + kind + "_" + arm + "_"
                  + str(int(now * 10)) + "_" + str(next(_obstruction_seq)))
    route_id = "obstruction_route_" + edge_id
    if route_id not in conn.route.getIDList():
        conn.route.add(route_id, [edge_id])
    conn.vehicle.add(vehicle_id, route_id, typeID=type_id,
                     departPos=str(position), departLane=str(lane_index),
                     departSpeed="0")
    conn.vehicle.setSpeed(vehicle_id, 0.0)
    conn.vehicle.setStop(vehicle_id, edge_id, pos=position, laneIndex=lane_index,
                         duration=duration)
    return {"id": vehicle_id, "kind": kind, "label": spec["label"], "arm": arm,
            "edge": edge_id, "lane": lane_index, "position_m": round(position, 1),
            "duration_s": duration, "placed_at_s": round(now, 1)}


def network_geometry():
    """Lane centrelines and junction shapes for the browser."""
    model = get_model()
    net = model.net
    lanes = []
    for edge in net.getEdges():
        if edge.getFunction() == "internal":
            continue
        for lane in edge.getLanes():
            lanes.append({"id": lane.getID(), "edge": edge.getID(),
                          "width": lane.getWidth(),
                          "shape": [[round(x, 2), round(y, 2)]
                                    for x, y in lane.getShape()]})
    junctions = []
    for node in net.getNodes():
        shape = node.getShape()
        if len(shape) < 3:
            continue
        junctions.append({"id": node.getID(), "type": node.getType(),
                          "shape": [[round(x, 2), round(y, 2)] for x, y in shape]})

    bbox = net.getBBoxXY()
    xmin, ymin = bbox[0]
    xmax, ymax = bbox[1]
    arms = {name: arm.as_dict() for name, arm in model.arms.items()}
    for name, arm in model.arms.items():
        end = net.getEdge(arm.incoming_edge).getShape()[-1]
        arms[name]["stopline"] = [round(end[0], 2), round(end[1], 2)]
    return {
        "lanes": lanes,
        "junctions": junctions,
        "bounds": [xmin, ymin, xmax, ymax],
        "junction_id": C.JUNCTION_ID,
        "junction_xy": [round(v, 2) for v in net.getNode(C.JUNCTION_ID).getCoord()],
        "arms": arms,
        "vehicle_classes": {name: {"label": s["label"], "colour": s["colour"],
                                   "length": s["length"], "width": s["width"],
                                   "height": s["height"],
                                   "occupancy": s.get("occupancy", 1.0),
                                   "heavy": bool(s.get("heavy"))}
                            for name, s in D.VEHICLE_CLASSES.items()},
    }


class LiveSession:
    """Same model, stepped in real time and steerable from outside.

    Commands land between steps, never mid step, so a change always applies on
    a whole simulation second.
    """

    def __init__(self, spec=None, plan=None, seed=1, gui=False):
        self.spec = spec or D.DemandSpec(duration_s=100000.0)
        self.plan = plan or T.baseline_plan()
        self.seed = seed
        self.gui = gui
        self.label = "abhyas-live-" + str(os.getpid())
        self.conn = None
        self.model = None
        self.collector = None
        self.obstructions = []
        self.access = {"applied": [], "notes": []}
        self.time = 0.0
        self.running = False

    def start(self):
        self.model = get_model()
        D.prepare(self.spec, arms=self.model.arms, net=self.model.net)
        self.model.measure_corridors(_route_edges_from_file())
        traci.start(_sumo_command(self.gui, self.seed), label=self.label)
        self.conn = traci.getConnection(self.label)
        apply_plan(self.model, self.plan, self.conn)
        self.access = apply_access_restrictions(self.model, self.spec, self.conn)
        self.collector = Collector(self.model, warmup_s=0.0, spec=self.spec)
        self.running = True

    def step(self):
        self.conn.simulationStep()
        self.time = self.conn.simulation.getTime()
        self.collector.step(self.time, self.conn)

    def set_plan(self, plan):
        self.plan = plan
        apply_plan(self.model, plan, self.conn)

    def add_obstruction(self, obstruction):
        placed = place_obstruction(self.model, obstruction, conn=self.conn,
                                   now=self.time)
        self.obstructions.append(placed)
        return placed

    def remove_obstruction(self, obstruction):
        try:
            self.conn.vehicle.remove(obstruction["id"])
        except Exception:
            pass
        if obstruction in self.obstructions:
            self.obstructions.remove(obstruction)

    def clear_obstructions(self):
        standing = list(self.obstructions)
        for obstruction in standing:
            self.remove_obstruction(obstruction)
        return len(standing)

    def arm_colour(self, state, arm):
        """What one approach shows under a red/yellow/green state string."""
        colours = {state[link.index] for link in self.model.links
                   if link.from_arm == arm and link.index < len(state)}
        if colours & {"G", "g"}:
            return "green"
        if colours & {"y", "Y"}:
            return "yellow"
        return "red"

    def signal_by_arm(self, state):
        return {arm: self.arm_colour(state, arm) for arm in "NESW"}

    def set_arm_signal(self, arm, colour):
        """Jump the controller to the phase that shows `arm` this colour.

        A nudge, not a hold: the program carries on from that phase, so the
        cycle resumes by itself instead of the junction freezing on one
        approach. There is no phase that shows a single arm yellow on its
        own, so yellow lands on that arm's own inter-green.
        """
        if arm not in "NESW":
            raise ValueError("No approach called " + str(arm) + ".")
        if colour not in ("green", "yellow", "red"):
            raise ValueError("A signal is green, yellow or red - not "
                             + str(colour) + ".")
        phases = T.build_program(self.plan, self.model.links)
        for index, phase in enumerate(phases):
            if self.arm_colour(phase.state, arm) == colour:
                self.conn.trafficlight.setPhase(C.JUNCTION_ID, index)
                return {"arm": arm, "colour": colour, "phase_index": index}
        raise ValueError("This plan has no phase where " + arm + " is "
                         + colour + ".")

    def snapshot(self):
        """Everything the browser needs for one frame."""
        conn = self.conn
        vehicles = []
        for vid in conn.vehicle.getIDList():
            try:
                x, y = conn.vehicle.getPosition(vid)
                vehicles.append({"id": vid, "t": conn.vehicle.getTypeID(vid),
                                 "x": round(x, 2), "y": round(y, 2),
                                 "a": round(conn.vehicle.getAngle(vid), 1),
                                 "s": round(conn.vehicle.getSpeed(vid), 2)})
            except traci.TraCIException:
                continue

        phase_index = conn.trafficlight.getPhase(C.JUNCTION_ID)
        phases = T.build_program(self.plan, self.model.links)
        state = conn.trafficlight.getRedYellowGreenState(C.JUNCTION_ID)
        phase_name = phases[phase_index].name if phase_index < len(phases) else ""

        return {
            "time_s": round(self.time, 1),
            "vehicles": vehicles,
            "signal": {
                "state": state,
                "phase_index": phase_index,
                "phase_name": phase_name,
                "time_to_switch": round(
                    conn.trafficlight.getNextSwitch(C.JUNCTION_ID) - self.time, 1),
                "arms": self.signal_by_arm(state),
            },
            "queues": {arm: sum(conn.edge.getLastStepHaltingNumber(e)
                                for e in self.model.queue_edges[arm])
                       for arm in "NESW"},
            "obstructions": self.obstructions,
            "hmv_stops": self.collector.hmv_stops[-20:],
            "access": self.access,
            "plan": T.describe(self.plan),
            "demand": self.spec.to_dict(),
        }

    def live_metrics(self):
        return self.collector.result(self.seed, max(self.time, 1.0), self.plan,
                                     self.spec).to_dict()

    def stop(self):
        self.running = False
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


if __name__ == "__main__":
    import json
    netbuild.build()
    result = run_once(D.DemandSpec(veh_per_hour=2400, duration_s=1200), seed=7)
    print(json.dumps(result.to_dict(), indent=2))
