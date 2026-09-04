# Vehicle types and how much traffic goes where.
#
# The dimensions and driver behaviour numbers below are lifted from published
# work (Indo-HCM 2017 Ch.4, IRC:106-1990, SUMO's sublane docs) and are NOT
# tuned. Exactly one thing here is a dial: vehicles per hour. Turning splits
# are a declared prior - we sweep them, we don't fit them.

import os

import sumolib

from . import config as C
from . import netbuild


VEHICLE_CLASSES = {
    "twowheeler": {
        "share": 0.52,
        "vClass": "motorcycle",
        "length": 1.87, "width": 0.64, "height": 1.20,
        "minGap": 1.0, "minGapLat": 0.12,
        "accel": 2.6, "decel": 4.5, "sigma": 0.55, "tau": 0.8,
        "maxSpeed": 16.7,
        "latAlignment": "arbitrary",
        "lcSublane": 6.0, "lcPushy": 0.9, "lcAssertive": 2.5,
        "lcSpeedGain": 3.0, "lcKeepRight": 0.0,
        "jmTimegapMinor": 1.8, "impatience": 0.9,
        "colour": "255,196,0",
        "label": "Two-wheeler",
        "source": "Indo-HCM 2017 Ch.4 (1.87 x 0.64 m); critical gap 1.8 s",
    },
    "auto": {
        "share": 0.13,
        "vClass": "passenger",
        "length": 3.20, "width": 1.40, "height": 1.70,
        "minGap": 1.3, "minGapLat": 0.30,
        "accel": 2.0, "decel": 4.0, "sigma": 0.6, "tau": 0.9,
        "maxSpeed": 13.9,
        "latAlignment": "arbitrary",
        "lcSublane": 4.0, "lcPushy": 0.8, "lcAssertive": 2.0,
        "lcSpeedGain": 2.0, "lcKeepRight": 0.0,
        "jmTimegapMinor": 2.2, "impatience": 0.85,
        "colour": "0,200,120",
        "label": "Auto-rickshaw",
        "source": "Indo-HCM 2017 Ch.4 (3.20 x 1.40 m); critical gap 2.2 s",
    },
    "car": {
        "share": 0.31,
        "vClass": "passenger",
        "length": 3.72, "width": 1.44, "height": 1.50,
        "minGap": 1.8, "minGapLat": 0.50,
        "accel": 2.4, "decel": 4.5, "sigma": 0.5, "tau": 1.0,
        "maxSpeed": 16.7,
        "latAlignment": "center",
        "lcSublane": 1.5, "lcPushy": 0.4, "lcAssertive": 1.2,
        "lcSpeedGain": 1.0, "lcKeepRight": 0.0,
        "jmTimegapMinor": 2.5, "impatience": 0.7,
        "colour": "120,180,255",
        "label": "Car",
        "source": "Indo-HCM 2017 Ch.4 (3.72 x 1.44 m); critical gap 2.5 s "
                  "(western defaults of 4-6 s don't describe this traffic)",
    },
    "bus": {
        "share": 0.04,
        "vClass": "bus",
        "length": 10.10, "width": 2.43, "height": 3.20,
        "minGap": 2.5, "minGapLat": 0.60,
        "accel": 1.2, "decel": 3.5, "sigma": 0.5, "tau": 1.3,
        "maxSpeed": 13.9,
        "latAlignment": "center",
        "lcSublane": 0.8, "lcPushy": 0.3, "lcAssertive": 1.0,
        "lcSpeedGain": 0.6, "lcKeepRight": 0.0,
        "jmTimegapMinor": 3.2, "impatience": 0.5,
        "colour": "230,90,90",
        "label": "Bus",
        "source": "Indo-HCM 2017 Ch.4 (10.10 x 2.43 m); critical gap 3.2 s",
    },
}

# Sublane resolution in metres. Small enough that a 0.64 m two wheeler fits
# beside a car in a 3 m lane, which is what makes filtering show up on screen
# instead of bikes politely queueing.
#
# It is also, measured, the thing that parks cars in the middle of the
# junction. At 0.30 vehicles deadlock LATERALLY inside the intersection -
# each waiting on sideways space - and sit there until time-to-teleport gives
# up on them at 180 s. Nothing longitudinal shows it: the link ahead is open,
# the exit lane is empty and there is no leader within 60 m in 44% of the
# stopped samples, which is why it survived several rounds of looking.
#
# Seed 1, 2400 veh/h, mean vehicles halted inside the junction / worst single
# stall / throughput:
#     0.30 (this)          2.92   176 s   2652 veh/h
#     0.30 + calmer bikes  1.53   180 s   2646
#     0.80                 0.42    29 s   2874
#     sublane off          0.00     2 s   2586   (and no filtering)
#
# 0.80 looks like the answer - it keeps the sublane model, so bikes still
# filter, and it costs about 1% on travel time. Not adopted yet: that is one
# seed on a junction known to be bimodal, and changing it moves every
# validated number, so it needs a multi-seed run and a recalibration first.
LATERAL_RESOLUTION = 0.30

# the prior over turning splits. cannot be measured from travel time, so it is
# declared here and swept elsewhere.
DEFAULT_SPLITS = {"through": 0.55, "left": 0.25, "right": 0.20}

# 100 Feet Road (N-S) is the arterial, CMH (E-W) is the cross road
DEFAULT_ARM_SHARE = {"N": 0.30, "S": 0.30, "E": 0.20, "W": 0.20}

# departLane="best" was tried and it starves insertion - everyone wants the
# same lane, insertion fails, max-depart-delay throws the demand away, and you
# get an approach queue that gets SHORTER when you cut its green. random keeps
# it honest.
DEPART_LANE = os.environ.get("ABHYAS_DEPART_LANE", "random")
DEPART_SPEED = os.environ.get("ABHYAS_DEPART_SPEED", "max")


class DemandSpec:
    """Everything that decides what traffic runs. veh_per_hour is the dial."""

    def __init__(self, veh_per_hour=2400.0, arm_share=None, turning_splits=None,
                 seed=1, duration_s=1800.0):
        self.veh_per_hour = float(veh_per_hour)
        self.arm_share = dict(arm_share or DEFAULT_ARM_SHARE)
        self.turning_splits = dict(turning_splits or DEFAULT_SPLITS)
        self.seed = seed
        self.duration_s = float(duration_s)

    def copy(self, **changes):
        """New spec with a few fields swapped. Beats retyping the constructor
        in six places."""
        out = DemandSpec(self.veh_per_hour, self.arm_share, self.turning_splits,
                         self.seed, self.duration_s)
        for key, value in changes.items():
            setattr(out, key, value)
        return out

    def to_dict(self):
        return {"veh_per_hour": self.veh_per_hour,
                "arm_share": dict(self.arm_share),
                "turning_splits": dict(self.turning_splits),
                "seed": self.seed,
                "duration_s": self.duration_s}


def turn_matrix(arms):
    """(from_arm, turn) -> to_arm, worked out from the arms' own bearings.

    Writing "N left -> E" by hand would be wrong on any junction that isn't
    square, and this one isn't quite square.
    """
    matrix = {}
    for from_arm in arms:
        heading = (arms[from_arm].bearing + 180.0) % 360.0
        for turn in ("through", "left", "right"):
            best, best_err = None, 999.0
            for name, arm in arms.items():
                if name == from_arm:
                    continue
                delta = (arm.bearing - heading + 180.0) % 360.0 - 180.0
                if turn == "through":
                    err = abs(delta)
                elif turn == "left":
                    err = abs(delta - 90.0)
                else:
                    err = abs(delta + 90.0)
                if err < best_err:
                    best, best_err = name, err
            matrix[(from_arm, turn)] = best
    return matrix


class Demand:
    """Writes the three files SUMO needs for one scenario."""

    def __init__(self, spec, arms=None, net=None):
        self.spec = spec
        self.net = net if net is not None else sumolib.net.readNet(str(C.JUNCTION_NET))
        self.arms = arms if arms is not None else netbuild.discover_arms(self.net)

    def prepare(self):
        self.write_vtypes()
        info = self.write_routes()
        self.write_sumocfg()
        return info

    def write_vtypes(self):
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<additional>"]
        for name, spec in VEHICLE_CLASSES.items():
            if name == "twowheeler":
                shape = "motorcycle"
            elif name == "bus":
                shape = "bus"
            else:
                shape = "passenger"
            attrs = ['id="' + name + '"',
                     'vClass="' + spec["vClass"] + '"',
                     'guiShape="' + shape + '"',
                     'color="' + spec["colour"] + '"',
                     'latAlignment="' + spec["latAlignment"] + '"',
                     'laneChangeModel="SL2015"']
            for key in ("length", "width", "height", "minGap", "minGapLat",
                        "accel", "decel", "sigma", "tau", "maxSpeed",
                        "lcSublane", "lcPushy", "lcAssertive", "lcSpeedGain",
                        "lcKeepRight", "jmTimegapMinor", "impatience"):
                attrs.append(key + '="' + format(spec[key], ".2f") + '"')
            lines.append("    <!-- " + spec["source"] + " -->")
            lines.append("    <vType " + " ".join(attrs) + "/>")
        lines.append("</additional>")
        C.VTYPES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _route_edges(self, from_arm, to_arm):
        path, _cost = self.net.getShortestPath(
            self.net.getEdge(self.arms[from_arm].entry_edge),
            self.net.getEdge(self.arms[to_arm].exit_edge),
            vClass="passenger")
        return [e.getID() for e in path] if path else None

    def write_routes(self):
        spec = self.spec
        matrix = turn_matrix(self.arms)
        routes, flows, skipped = [], [], []

        for arm_name in sorted(self.arms):
            arm_rate = spec.veh_per_hour * spec.arm_share.get(arm_name, 0.0)
            for turn, share in spec.turning_splits.items():
                to_arm = matrix[(arm_name, turn)]
                edges = self._route_edges(arm_name, to_arm)
                if not edges:
                    skipped.append(arm_name + " " + turn + " (-> " + str(to_arm) + ")")
                    continue
                route_id = "r_" + arm_name + "_" + turn
                routes.append((route_id, edges))
                for vname, vspec in VEHICLE_CLASSES.items():
                    rate = arm_rate * share * vspec["share"]
                    if rate < 0.5:
                        continue
                    flows.append({"id": "f_" + arm_name + "_" + turn + "_" + vname,
                                  "type": vname, "route": route_id,
                                  "vehsPerHour": rate, "arm": arm_name,
                                  "turn": turn})

        if skipped:
            # shouldn't happen once the network builds clean, but it did for
            # about two days and it was very hard to spot
            # a movement the network can't route is a fact about the model, say it
            print("  ! unroutable movements: " + "; ".join(skipped))

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<routes>"]
        for route_id, edges in routes:
            lines.append('    <route id="' + route_id + '" edges="'
                         + " ".join(edges) + '"/>')
        for flow in flows:
            lines.append(
                '    <flow id="' + flow["id"] + '" type="' + flow["type"] + '"'
                + ' route="' + flow["route"] + '"'
                + ' begin="0" end="' + format(spec.duration_s, ".0f") + '"'
                + ' vehsPerHour="' + format(flow["vehsPerHour"], ".2f") + '"'
                + ' departLane="' + DEPART_LANE + '"'
                + ' departSpeed="' + DEPART_SPEED + '"'
                + ' departPosLat="random_free"/>')
        lines.append("</routes>")
        C.ROUTES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return {"routes": len(routes), "flows": len(flows), "skipped": skipped,
                "turn_matrix": {a + "->" + t: v for (a, t), v in matrix.items()},
                "total_veh_per_hour": spec.veh_per_hour}

    def write_sumocfg(self):
        # collision.action=teleport, not warn. With warn the colliding vehicles
        # keep going and sit stacked on top of each other on screen forever.
        C.SUMO_CFG.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<configuration>\n"
            "    <input>\n"
            '        <net-file value="' + str(C.JUNCTION_NET) + '"/>\n'
            '        <route-files value="' + C.ROUTES_FILE.name + '"/>\n'
            '        <additional-files value="' + C.VTYPES_FILE.name + '"/>\n'
            "    </input>\n"
            "    <time>\n"
            '        <begin value="0"/>\n'
            '        <end value="' + format(self.spec.duration_s, ".0f") + '"/>\n'
            '        <step-length value="0.5"/>\n'
            "    </time>\n"
            "    <processing>\n"
            '        <lateral-resolution value="'
            + format(LATERAL_RESOLUTION, ".2f") + '"/>\n'
            '        <collision.action value="teleport"/>\n'
            '        <time-to-teleport value="180"/>\n'
            '        <max-depart-delay value="300"/>\n'
            "    </processing>\n"
            "    <report>\n"
            '        <no-step-log value="true"/>\n'
            '        <duration-log.statistics value="true"/>\n'
            "    </report>\n"
            "</configuration>\n",
            encoding="utf-8")


def prepare(spec, arms=None, net=None):
    """Old shorthand, still used all over the place."""
    return Demand(spec, arms=arms, net=net).prepare()


if __name__ == "__main__":
    import json
    print(json.dumps(prepare(DemandSpec()), indent=2))
