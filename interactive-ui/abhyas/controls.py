# Every knob this junction has, declared in one place. The dials, the voice
# layer and the version store all read this, so none of them can offer a
# control the simulation would refuse.

from . import config as C
from . import demand as D
from . import sim
from . import tls as T

MIN_GREEN_S = 8.0
MAX_GREEN_S = 120.0

GROUPS = ["Signal", "Traffic", "Obstructions"]


class Rejected(Exception):
    """An edit the control surface won't take, carrying the reason."""


def _dial(cid, label, group, minimum, maximum, step, unit, help_text, kind="dial"):
    return {"id": cid, "label": label, "group": group, "kind": kind,
            "value": None, "min": minimum, "max": maximum, "step": step,
            "unit": unit, "help": help_text}


def declare(shape=None):
    """The control surface for one plan shape, with no values filled in.

    Takes the shape rather than reading the global, because the plan can be
    swapped while the simulation runs and the dials have to follow it.
    """
    shape = shape or C.ACTIVE_PHASE_PLAN
    # signal.plan first, the server reads it before anything else in the batch
    controls = [{
        "id": "signal.plan", "label": "Signal plan", "group": "Signal",
        "kind": "choice", "value": shape, "min": None, "max": None,
        "step": None, "unit": "",
        "options": [{"value": name, "label": spec["label"], "note": spec["note"]}
                    for name, spec in C.PHASE_PLANS.items()],
        "help": "Which shape of signal this junction runs. Changing it replaces "
                "the whole plan. The phase-plan agent says which shape the "
                "archive prefers, it doesn't switch it for you.",
    }]

    # whatever stages this shape has, not a fixed two
    for group, spec in C.phase_groups(shape).items():
        arms = " and ".join(spec["arms"])
        controls.append(_dial(
            group + ".green", spec["label"], "Signal",
            MIN_GREEN_S, MAX_GREEN_S, 1.0, "s",
            "Green for the " + arms + " approaches. Clamped at "
            + str(int(MIN_GREEN_S)) + " s - below that the simulator will still "
            "hand you a confident number for a plan nobody would sign."))
        controls.append(_dial(
            group + ".yellow", spec["label"].replace("green", "yellow"),
            "Signal", 2.0, 8.0, 0.5, "s",
            "Amber after the " + arms + " green.", kind="slider"))
        controls.append(_dial(
            group + ".allred", spec["label"].replace("green", "all-red"),
            "Signal", 0.0, 6.0, 0.5, "s",
            "Clearance before the opposing green starts.", kind="slider"))

    controls.append(_dial(
        "demand.veh_per_hour", "Total demand", "Traffic", 200.0, 6000.0, 50.0,
        "veh/h", "Vehicles entering per hour. Baked into the route file, so "
                 "moving this restarts the run."))

    for arm in "NSEW":
        controls.append(_dial(
            "demand.arm_share." + arm, arm + " approach share", "Traffic",
            0.05, 0.60, 0.01, "",
            "Share of demand arriving on " + arm + ". The four renormalise to "
            "sum to one.", kind="slider"))

    for turn in ("through", "left", "right"):
        controls.append(_dial(
            "demand.turning_splits." + turn, turn.title() + " split", "Traffic",
            0.05, 0.80, 0.01, "",
            "Share of each arm going " + turn + ". A declared prior, not a "
            "measurement - nothing we counted distinguishes the three.",
            kind="slider"))

    for kind, spec in sim.OBSTRUCTION_TYPES.items():
        for arm in "NSEW":
            controls.append(_dial(
                "obstruction." + kind + "." + arm, spec["label"] + " on " + arm,
                "Obstructions", 0, sim.OBSTRUCTION_MAX_PER_ARM, 1, "",
                "How many " + spec["label"].lower() + " to park on the " + arm
                + " approach, each roughly " + str(int(sim.OBSTRUCTION_SETBACK_M))
                + " m back from the stop line at a randomised spot and lane. "
                "Vehicles that never move, so traffic squeezes past them.",
                kind="slider"))
    return controls


def all_controls():
    """Every control of every shape, keyed by id. Restoring a version saved
    under the other plan has to be able to resolve its stage names."""
    out = {}
    for shape in C.PHASE_PLANS:
        for control in declare(shape):
            out.setdefault(control["id"], control)
    return out


def lookup(control_id):
    control = all_controls().get(control_id)
    if control is None:
        raise Rejected("No control called '" + str(control_id) + "'.")
    return control


def known_ids(shape=None):
    return [c["id"] for c in declare(shape)]


def surface(plan, spec, obstructions=None):
    """The declaration filled in with what the live session actually holds."""
    shape = T.shape_of(plan)
    groups = C.phase_groups(shape)
    placed_counts = {}
    for o in (obstructions or []):
        key = (o.get("kind"), o.get("arm"))
        placed_counts[key] = placed_counts.get(key, 0) + 1

    controls = []
    for control in declare(shape):
        parts = control["id"].split(".")
        entry = dict(control)
        if control["id"] == "signal.plan":
            entry["value"] = shape
        elif parts[0] in groups:
            entry["value"] = float(plan[parts[0]][parts[1]])
        elif control["id"] == "demand.veh_per_hour":
            entry["value"] = float(spec.veh_per_hour)
        elif parts[0] == "demand" and parts[1] == "arm_share":
            entry["value"] = float(spec.arm_share.get(parts[2], 0.0))
        elif parts[0] == "demand" and parts[1] == "turning_splits":
            entry["value"] = float(spec.turning_splits.get(parts[2], 0.0))
        elif parts[0] == "obstruction":
            entry["value"] = placed_counts.get((parts[1], parts[2]), 0)
        controls.append(entry)

    return {"controls": controls, "groups": GROUPS,
            "cycle_seconds": T.cycle_seconds(plan), "shape": shape,
            "shape_label": C.PHASE_PLANS[shape]["label"],
            "shape_note": C.PHASE_PLANS[shape]["note"],
            "baseline": baseline_state(shape)}


def baseline_state(shape=None):
    """What every counterfactual gets measured against, for one shape."""
    shape = shape or C.ACTIVE_PHASE_PLAN
    plan, spec = T.baseline_plan(shape), D.DemandSpec()
    state = {"signal.plan": shape}
    for group in plan:
        for key in ("green", "yellow", "allred"):
            state[group + "." + key] = float(plan[group][key])
    state["demand.veh_per_hour"] = float(spec.veh_per_hour)
    for arm, share in spec.arm_share.items():
        state["demand.arm_share." + arm] = float(share)
    for turn, share in spec.turning_splits.items():
        state["demand.turning_splits." + turn] = float(share)
    for control in declare(shape):
        default = 0 if control["group"] == "Obstructions" else False
        state.setdefault(control["id"], default)
    return state


def state_of(plan, spec, obstructions=None):
    return {c["id"]: c["value"] for c in surface(plan, spec, obstructions)["controls"]}


# ---- editing -------------------------------------------------------------

def fmt(value):
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        return value
    value = float(value)
    return str(int(value)) if value.is_integer() else str(round(value, 3))


def clamp(control, value):
    """Clamp into range and say so. A silent clamp lies about what ran."""
    low, high = control["min"], control["max"]
    clamped = max(low, min(high, float(value)))
    if abs(clamped - float(value)) > 1e-9:
        return clamped, (control["label"] + " clamped to " + fmt(clamped)
                         + control["unit"] + " - asked for " + fmt(value)
                         + ", allowed " + fmt(low) + " to " + fmt(high))
    return clamped, ""


def plan_edit(plan, control_id, value):
    control = lookup(control_id)
    group, key = control_id.rsplit(".", 1)
    clamped, note = clamp(control, value)
    out = {g: dict(s) for g, s in plan.items()}
    out[group][key] = clamped
    return out, note


def demand_edit(spec, control_id, value):
    control = lookup(control_id)
    clamped, note = clamp(control, value)
    out = spec.copy()
    parts = control_id.split(".")

    if control_id == "demand.veh_per_hour":
        out.veh_per_hour = clamped
        return out, note

    bucket = out.arm_share if parts[1] == "arm_share" else out.turning_splits
    bucket[parts[2]] = clamped
    total = sum(bucket.values())
    if total <= 0:
        raise Rejected("Those shares sum to zero, which isn't a traffic mix.")
    for name in bucket:
        bucket[name] = round(bucket[name] / total, 4)
    renormalised = "the other shares renormalised to sum to one"
    return out, (note + "; " + renormalised) if note else renormalised


def diff(before, after):
    labels = all_controls()
    changes = []
    for cid in sorted(set(before) | set(after)):
        old, new = before.get(cid), after.get(cid)
        if old == new:
            continue
        control = labels.get(cid, {"label": cid, "unit": ""})
        numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                      for v in (old, new))
        changes.append({"id": cid, "label": control["label"],
                        "unit": control.get("unit", ""),
                        "from": old, "to": new,
                        "delta": round(float(new) - float(old), 4) if numeric else None})
    return changes


def _change_phrase(change):
    # a stage that no longer exists is not a timing of zero, those are very
    # different things
    if change["to"] is None:
        return change["label"] + " no longer a stage"
    if change["from"] is None:
        unit = " " + change["unit"] if change["unit"] else ""
        return change["label"] + " added at " + fmt(change["to"]) + unit
    if isinstance(change["to"], bool):
        return change["label"] + (" placed" if change["to"] else " removed")
    if change["delta"]:
        sign = "+" if change["delta"] > 0 else ""
        unit = (" " + change["unit"]) if change["unit"] else ""
        return (change["label"] + " " + fmt(change["from"]) + " to "
                + fmt(change["to"]) + unit + " (" + sign + fmt(change["delta"]) + ")")
    return change["label"] + " to " + fmt(change["to"])


def describe_changes(changes):
    """One sentence, short enough to read out loud."""
    if not changes:
        return "Nothing changed."

    # a plan switch replaces every stage at once, listing all twelve is a wall
    # of text that hides the one change that caused them
    swap = next((c for c in changes if c["id"] == "signal.plan"), None)
    if swap:
        others = [c for c in changes if c["id"] != "signal.plan"
                  and not c["id"].endswith((".green", ".yellow", ".allred"))]
        summary = ("Signal plan switched to " + C.PHASE_PLANS[swap["to"]]["label"]
                   + ", replacing every stage with that plan's baseline timings")
        if others:
            summary += "; " + describe_changes(others)[:-1]
        return summary + "."

    parts = [_change_phrase(c) for c in changes]
    if len(parts) == 1:
        return parts[0] + "."
    return ", ".join(parts[:-1]) + " and " + parts[-1] + "."


if __name__ == "__main__":
    for item in declare():
        print(format(item["id"], "34"), format(item["kind"], "7"),
              format(str(item["min"]) + " - " + str(item["max"]), "16"),
              item["unit"])
    print(len(declare()), "controls")
