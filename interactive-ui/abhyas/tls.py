# Signal plans. Nothing here stores a SUMO link index - those are internal
# numbering that changes on every rebuild, silently. We ask the current network
# which arm each link belongs to, every time.

import sumolib

from . import config as C
from . import netbuild


class Link:
    """One controlled link at the junction."""

    def __init__(self, index, from_arm, to_arm, turn):
        self.index = index
        self.from_arm = from_arm
        self.to_arm = to_arm
        self.turn = turn            # through | left | right | u

    def phase_group(self, shape=None):
        return C.arm_to_phase(shape)[self.from_arm]


class Phase:
    def __init__(self, duration, state, name):
        self.duration = duration
        self.state = state
        self.name = name


def turn_kind(from_bearing, to_bearing):
    """through / left / right / u, from the bearing in to the bearing out."""
    delta = (to_bearing - from_bearing + 180.0) % 360.0 - 180.0
    if abs(delta) < 35.0:
        return "through"
    if abs(delta) > 150.0:
        return "u"
    # counter clockwise in this frame is a left turn on the ground
    return "left" if delta > 0 else "right"


def build_link_map(net=None, arms=None):
    """Ask the network which arm every controlled link comes from."""
    if net is None:
        net = sumolib.net.readNet(str(C.JUNCTION_NET), withPrograms=True,
                                  withConnections=True)
    if arms is None:
        arms = netbuild.discover_arms(net)

    entering = {a.incoming_edge: a for a in arms.values()}
    leaving = {a.outgoing_edge: a for a in arms.values() if a.outgoing_edge}

    links = []
    for in_lane, out_lane, index in net.getTLS(C.JUNCTION_ID).getConnections():
        src = entering.get(in_lane.getEdge().getID())
        dst = leaving.get(out_lane.getEdge().getID())
        if src is None or dst is None:
            # a controlled link that isn't one of our four arms. leave it red
            # everywhere rather than guessing.
            links.append(Link(index, "?", "?", "through"))
            continue
        # travel direction entering the junction is the reverse of the arm
        # bearing, which points outwards
        links.append(Link(index, src.name, dst.name,
                          turn_kind((src.bearing + 180.0) % 360.0, dst.bearing)))
    links.sort(key=lambda link: link.index)
    return links


def shape_of(plan):
    """Which declared shape a plan dict is, from the stage names in it.

    Derived rather than stored - sticking a shape key inside the plan would
    break every {g: dict(s) for g, s in plan.items()} copy in the codebase.
    """
    keys = set(plan)
    for name, spec in C.PHASE_PLANS.items():
        if {stage["key"] for stage in spec["stages"]} == keys:
            return name
    raise ValueError("Plan names stages " + str(sorted(keys)) + ", which is not "
                     "one of " + ", ".join(sorted(C.PHASE_PLANS)))


def _state(links, active_arms, colour):
    """One signal string. `colour` for links off the active arms, red for the rest.

    A right turn in left hand traffic crosses the opposing stream, so it only
    gets a permissive green (lowercase g) when the arm it must yield to is
    green too. Under two_phase that's always, under four_phase never - which is
    the entire difference between the two plans.
    """
    facing = {link.from_arm: link.to_arm for link in links
              if link.turn == "through"}
    chars = []
    for link in links:
        if link.from_arm not in active_arms:
            chars.append("r")
        elif colour == "G":
            conflicts = (link.turn in ("right", "u")
                         and facing.get(link.from_arm) in active_arms)
            chars.append("g" if conflicts else "G")
        else:
            chars.append(colour)
    return "".join(chars)


def build_program(plan, links=None):
    """Green/yellow/all-red seconds -> an ordered list of phases."""
    if links is None:
        links = build_link_map()
    phases = []
    for stage in C.phase_stages(shape_of(plan)):
        spec = plan[stage["key"]]
        arms = set(stage["arms"])
        label = stage["label"]
        phases.append(Phase(float(spec["green"]), _state(links, arms, "G"), label))
        if spec.get("yellow"):
            phases.append(Phase(float(spec["yellow"]), _state(links, arms, "y"),
                                label + " yellow"))
        if spec.get("allred"):
            phases.append(Phase(float(spec["allred"]), _state(links, set(), "r"),
                                "all red"))
    return phases


def cycle_seconds(plan):
    return float(sum(sum(spec.values()) for spec in plan.values()))


def baseline_plan(shape=None):
    return C.baseline_timings(shape)


def apply_delta(plan, group, delta_seconds, min_green=8.0, max_green=120.0):
    """Copy of `plan` with one stage's green moved. Clamped both ends - a phase
    under eight seconds isn't a plan anyone would sign, and the simulator will
    happily report a confident number for it anyway."""
    out = {g: dict(s) for g, s in plan.items()}
    out[group]["green"] = max(min_green,
                              min(max_green, out[group]["green"] + delta_seconds))
    return out


def describe(plan):
    shape = shape_of(plan)
    groups = C.phase_groups(shape)
    return {
        "shape": shape,
        "shape_label": C.PHASE_PLANS[shape]["label"],
        "shape_note": C.PHASE_PLANS[shape]["note"],
        "cycle_seconds": cycle_seconds(plan),
        "groups": {group: {"label": groups[group]["label"],
                           "green": plan[group]["green"],
                           "yellow": plan[group]["yellow"],
                           "allred": plan[group]["allred"],
                           "arms": groups[group]["arms"],
                           "movements": groups[group]["movements"]}
                   for group in plan},
    }


if __name__ == "__main__":
    link_map = build_link_map()
    for link in link_map:
        print(link.index, link.from_arm, "->", link.to_arm, link.turn)
    print()
    for phase in build_program(baseline_plan(), link_map):
        print(format(phase.duration, "5.1f"), phase.state, " ", phase.name)
