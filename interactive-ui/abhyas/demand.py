# Vehicle types and how much traffic goes where.
#
# The dimensions and driver behaviour numbers below are lifted from published
# work (Indo-HCM 2017 Ch.4, IRC:106-1990, SUMO's sublane docs) and are NOT
# tuned. Exactly one thing here is a dial: vehicles per hour. Turning splits
# are a declared prior - we sweep them, we don't fit them.
#
# Everything added for fleet and access scenarios (hcv share, hmv_discipline,
# occupancy, injected fleet, mode shift) is EXPLORATORY: declared here, swept
# not fitted, and labelled that way everywhere it surfaces. None of it is part
# of the calibrated baseline and none of it may be quoted as a validated
# result - see docs/calibration-and-validation.md sections 1 and 7.

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
        "plural": "two-wheelers",
        "occupancy": 1.4,
        "occupancy_source": "declared prior, not measured on this corridor",
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
        "plural": "auto-rickshaws",
        "occupancy": 1.8,
        "occupancy_source": "declared prior, not measured on this corridor",
        "source": "Indo-HCM 2017 Ch.4 (3.20 x 1.40 m); critical gap 2.2 s",
    },
    "car": {
        "share": 0.29,
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
        "plural": "cars",
        "occupancy": 1.2,
        "occupancy_source": "declared prior, not measured on this corridor",
        "source": "Indo-HCM 2017 Ch.4 (3.72 x 1.44 m); critical gap 2.5 s "
                  "(western defaults of 4-6 s don't describe this traffic)",
    },
    "bus": {
        "share": 0.03,
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
        "plural": "buses",
        "heavy": True,
        "occupancy": 40.0,
        "occupancy_source": "declared prior, not measured on this corridor",
        # endpoints of the hmv_discipline dial. left column is a bus that waits
        # for a gap, right column is one that takes the gap and expects to be
        # let in. Neither is a citation - see EXPLORATORY note at the top.
        "discipline": {"lcPushy": (0.3, 0.95), "lcAssertive": (1.0, 2.6),
                       "jmTimegapMinor": (3.2, 1.4), "impatience": (0.5, 0.95)},
        "stop_probability": 0.0,
        "stop_duration_s": (15.0, 60.0),
        "source": "Indo-HCM 2017 Ch.4 (10.10 x 2.43 m); critical gap 3.2 s",
    },
    "hcv": {
        # 0.03 is a DECLARED SHARE, not a measured one. Indo-HCM gives the
        # vehicle, it does not give this corridor's composition, and nothing
        # we counted separates a truck from a bus. It sits here because a
        # junction on a Bengaluru arterial that carries no HMV at all is the
        # less honest of the two wrong answers - not because 3% was fitted.
        # It came out of car (0.31 -> 0.29) and bus (0.04 -> 0.03), so the
        # five still sum to one, which means every calibrated number moved
        # and the baseline needs a re-run. Sweep it with fleet.hcv_share; the
        # result stays exploratory until a BMTC / traffic-police composition
        # count for CMH Road says otherwise.
        "share": 0.03,
        "vClass": "truck",
        "length": 10.30, "width": 2.50, "height": 3.50,
        "minGap": 2.8, "minGapLat": 0.60,
        "accel": 1.0, "decel": 3.5, "sigma": 0.5, "tau": 1.4,
        "maxSpeed": 12.5,
        "latAlignment": "center",
        "lcSublane": 0.6, "lcPushy": 0.35, "lcAssertive": 1.0,
        "lcSpeedGain": 0.5, "lcKeepRight": 0.0,
        "jmTimegapMinor": 3.4, "impatience": 0.5,
        "colour": "150,110,200",
        "label": "Truck / HCV",
        "plural": "trucks",
        "heavy": True,
        "occupancy": 1.5,
        "occupancy_source": "declared prior, not measured on this corridor",
        "discipline": {"lcPushy": (0.35, 1.0), "lcAssertive": (1.0, 2.8),
                       "jmTimegapMinor": (3.4, 1.5), "impatience": (0.5, 1.0)},
        "stop_probability": 0.0,
        "stop_duration_s": (20.0, 120.0),
        "source": "Indo-HCM 2017 Ch.4 (HCV 10.30 x 2.50 m); critical gap 3.4 s. "
                  "Share NOT sourced - no composition count for this corridor.",
        "share_source": "declared prior, not measured. Needs a BMTC or traffic-"
                        "police composition count for CMH Road x 100 Feet Road.",
    },
}

# Classes that "incite the traffic" - the ones hmv_discipline and the
# self-triggered stops act on. Read off the table rather than typed twice.
HEAVY_CLASSES = [name for name, spec in VEHICLE_CLASSES.items()
                 if spec.get("heavy")]

# What one vehicle of each class carries. Declared, swept, never fitted - the
# whole point of a bus scheme is that a bus moves 40 people and a car moves
# 1.2, and the model had no field that said so.
def plural(class_name):
    """"Buss" is not a word, and this text gets read out loud."""
    spec = VEHICLE_CLASSES.get(class_name, {})
    return spec.get("plural") or (spec.get("label", class_name) + "s").lower()


def occupancy(class_name):
    return float(VEHICLE_CLASSES.get(class_name, {}).get("occupancy", 1.0))

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


# Classes a mode shift takes trips OFF, and the class it puts them on. Autos
# are left alone: an auto trip is already a hired trip, and calling it a
# private trip that transit can absorb is a claim nobody measured.
MODE_SHIFT_FROM = ("car", "twowheeler")
MODE_SHIFT_TO = "bus"


class DemandSpec:
    """Everything that decides what traffic runs. veh_per_hour is the dial.

    Everything after duration_s is a scenario lever, not a calibration one:
    each defaults to the neutral value, so a spec built with no arguments is
    the calibrated baseline and is_exploratory says so.
    """

    def __init__(self, veh_per_hour=2400.0, arm_share=None, turning_splits=None,
                 seed=1, duration_s=1800.0, hcv_share=None, hmv_discipline=0.0,
                 injected=None, mode_shift=0.0, access_restrictions=None,
                 hmv_stop_rate=0.0):
        self.veh_per_hour = float(veh_per_hour)
        self.arm_share = dict(arm_share or DEFAULT_ARM_SHARE)
        self.turning_splits = dict(turning_splits or DEFAULT_SPLITS)
        self.seed = seed
        self.duration_s = float(duration_s)

        # -- scenario levers, all exploratory -----------------------------
        # share of flow that is HCV. None means "leave the table alone", which
        # is 0.0 until someone counts trucks on this corridor.
        self.hcv_share = None if hcv_share is None else float(hcv_share)
        # 0 = every heavy vehicle waits for a gap, 1 = it takes one.
        self.hmv_discipline = float(hmv_discipline)
        # fraction of heavy vehicles that stop unscheduled somewhere on their
        # route - the double-parked bus, the truck loading half in a lane.
        self.hmv_stop_rate = float(hmv_stop_rate)
        # absolute veh/h ADDED on top of the calibrated flow, per class. A
        # scheme puts N buses on a network, it doesn't reslice one junction.
        self.injected = dict(injected or {})
        # fraction of car and two-wheeler trips replaced by bus trips.
        self.mode_shift = float(mode_shift)
        # arm -> list of vehicle classes not allowed to enter there.
        self.access_restrictions = {a: list(c) for a, c
                                    in (access_restrictions or {}).items() if c}

    LEVERS = ("hcv_share", "hmv_discipline", "hmv_stop_rate", "injected",
              "mode_shift", "access_restrictions")

    @property
    def is_exploratory(self):
        """True once any lever has left its neutral value. Everything that
        prints a number off this spec has to be able to say so."""
        return bool(self.hcv_share or self.hmv_discipline or self.hmv_stop_rate
                    or self.mode_shift
                    or any(v for v in self.injected.values())
                    or self.access_restrictions)

    def copy(self, **changes):
        """New spec with a few fields swapped. Beats retyping the constructor
        in six places."""
        out = DemandSpec(self.veh_per_hour, self.arm_share, self.turning_splits,
                         self.seed, self.duration_s,
                         hcv_share=self.hcv_share,
                         hmv_discipline=self.hmv_discipline,
                         injected=dict(self.injected),
                         mode_shift=self.mode_shift,
                         access_restrictions={a: list(c) for a, c
                                              in self.access_restrictions.items()},
                         hmv_stop_rate=self.hmv_stop_rate)
        for key, value in changes.items():
            setattr(out, key, value)
        return out

    def restricted_on(self, arm):
        return set(self.access_restrictions.get(arm, ()))

    # -- turning the levers into vehicles per hour -------------------------

    def class_rates(self):
        """class -> veh/h for the whole junction, and how it got there.

        Order matters and is fixed: reslice for HCV share, then move trips off
        cars and two-wheelers onto buses, then add the scheme's vehicles on
        top. Injection last because a scheme adds to whatever is on the road,
        it does not get mode-shifted itself.
        """
        rates = {name: self.veh_per_hour * spec["share"]
                 for name, spec in VEHICLE_CLASSES.items()}
        report = {"reslice_hcv": None, "mode_shift": None, "injected": {}}

        if self.hcv_share is not None:
            wanted = max(0.0, min(0.9, self.hcv_share))
            others = self.veh_per_hour * (1.0 - wanted)
            base_others = sum(r for n, r in rates.items() if n != "hcv") or 1.0
            for name in rates:
                rates[name] = (self.veh_per_hour * wanted if name == "hcv"
                               else rates[name] * others / base_others)
            report["reslice_hcv"] = {"share": wanted,
                                     "veh_per_hour": round(rates["hcv"], 1)}

        if self.mode_shift > 0:
            fraction = min(0.9, self.mode_shift)
            people, removed = 0.0, {}
            for name in MODE_SHIFT_FROM:
                taken = rates.get(name, 0.0) * fraction
                rates[name] = rates.get(name, 0.0) - taken
                removed[name] = round(taken, 1)
                people += taken * occupancy(name)
            buses = people / max(occupancy(MODE_SHIFT_TO), 1.0)
            rates[MODE_SHIFT_TO] = rates.get(MODE_SHIFT_TO, 0.0) + buses
            report["mode_shift"] = {
                "fraction": fraction, "vehicles_removed": removed,
                "people_moved": round(people, 1),
                "buses_added": round(buses, 1),
                "basis": "declared occupancies, not measured: "
                         + ", ".join(n + " " + format(occupancy(n), ".1f")
                                     for n in MODE_SHIFT_FROM + (MODE_SHIFT_TO,))}

        for name, extra in self.injected.items():
            if name in rates and extra:
                rates[name] += float(extra)
                report["injected"][name] = float(extra)

        return {n: max(0.0, r) for n, r in rates.items()}, report

    def to_dict(self):
        out = {"veh_per_hour": self.veh_per_hour,
               "arm_share": dict(self.arm_share),
               "turning_splits": dict(self.turning_splits),
               "seed": self.seed,
               "duration_s": self.duration_s,
               "exploratory": self.is_exploratory}
        for lever in self.LEVERS:
            value = getattr(self, lever)
            out[lever] = (dict(value) if isinstance(value, dict) else value)
        return out


def discipline_overrides(class_name, discipline):
    """Per-class behaviour at a given point on the discipline dial.

    Linear between the two declared endpoints. Linear because there is nothing
    to justify any other shape - this dial is a way of asking the question, not
    an answer to it.
    """
    spec = VEHICLE_CLASSES.get(class_name, {})
    dial = max(0.0, min(1.0, float(discipline)))
    return {key: low + (high - low) * dial
            for key, (low, high) in (spec.get("discipline") or {}).items()}


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
        discipline = self.spec.hmv_discipline
        for name, spec in VEHICLE_CLASSES.items():
            if name == "twowheeler":
                shape = "motorcycle"
            elif name == "bus":
                shape = "bus"
            elif name == "hcv":
                shape = "truck/semitrailer"
            else:
                shape = "passenger"
            attrs = ['id="' + name + '"',
                     'vClass="' + spec["vClass"] + '"',
                     'guiShape="' + shape + '"',
                     'color="' + spec["colour"] + '"',
                     'latAlignment="' + spec["latAlignment"] + '"',
                     'laneChangeModel="SL2015"']
            # the discipline dial only moves the heavy classes, and only the
            # conduct parameters they declare endpoints for. Dimensions stay
            # where Indo-HCM put them.
            moved = discipline_overrides(name, discipline) if discipline else {}
            for key in ("length", "width", "height", "minGap", "minGapLat",
                        "accel", "decel", "sigma", "tau", "maxSpeed",
                        "lcSublane", "lcPushy", "lcAssertive", "lcSpeedGain",
                        "lcKeepRight", "jmTimegapMinor", "impatience"):
                attrs.append(key + '="'
                             + format(moved.get(key, spec[key]), ".2f") + '"')
            lines.append("    <!-- " + spec["source"] + " -->")
            if moved:
                lines.append("    <!-- EXPLORATORY: hmv_discipline="
                             + format(discipline, ".2f") + " moved "
                             + ", ".join(sorted(moved)) + " off the cited "
                             "values. Swept, not fitted. -->")
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
        # absolute veh/h per class, so a scheme that ADDS buses is not forced
        # to pretend it reslices a fixed total
        class_rates, mix_report = spec.class_rates()
        turned_away = {}

        for arm_name in sorted(self.arms):
            arm_fraction = spec.arm_share.get(arm_name, 0.0)
            banned = spec.restricted_on(arm_name)
            for turn, share in spec.turning_splits.items():
                to_arm = matrix[(arm_name, turn)]
                edges = self._route_edges(arm_name, to_arm)
                if not edges:
                    skipped.append(arm_name + " " + turn + " (-> " + str(to_arm) + ")")
                    continue
                route_id = "r_" + arm_name + "_" + turn
                routes.append((route_id, edges))
                for vname in VEHICLE_CLASSES:
                    rate = class_rates.get(vname, 0.0) * arm_fraction * share
                    if vname in banned:
                        # a banned class is not silently missing traffic. This
                        # junction is the whole model, so demand that entered
                        # on that arm has nowhere else to enter from: it is
                        # counted and reported as turned away, never quietly
                        # re-routed onto an arm it never used.
                        key = arm_name + "/" + vname
                        turned_away[key] = round(turned_away.get(key, 0.0) + rate, 1)
                        continue
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
                "total_veh_per_hour": spec.veh_per_hour,
                "class_veh_per_hour": {n: round(r, 1)
                                       for n, r in class_rates.items() if r},
                "released_veh_per_hour": round(
                    sum(f["vehsPerHour"] for f in flows), 1),
                "turned_away_veh_per_hour": turned_away,
                "mix": mix_report,
                "exploratory": spec.is_exploratory}

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
