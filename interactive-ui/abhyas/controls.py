# Every knob this junction has, declared in one place. The dials, the voice
# layer and the version store all read this, so none of them can offer a
# control the simulation would refuse.

from . import config as C
from . import demand as D
from . import sim
from . import tls as T

MIN_GREEN_S = T.MIN_GREEN_S
MAX_GREEN_S = T.MAX_GREEN_S

GROUPS = ["Signal", "Traffic", "Access", "Fleet", "Obstructions"]

# Groups whose controls are NOT part of the calibrated model. Everything in
# them is a declared prior that gets swept, never a fitted parameter, and the
# surface says so on every control rather than leaving it to be remembered.
EXPLORATORY_GROUPS = {"Access", "Fleet"}

EXPLORATORY_NOTE = ("Exploratory: swept, not fitted. Nothing in the validation "
                    "archive constrains this dial, so read it as a comparison "
                    "against baseline and never as a validated number.")


class Rejected(Exception):
    """An edit the control surface won't take, carrying the reason."""


def _dial(cid, label, group, minimum, maximum, step, unit, help_text, kind="dial"):
    control = {"id": cid, "label": label, "group": group, "kind": kind,
               "value": None, "min": minimum, "max": maximum, "step": step,
               "unit": unit, "help": help_text,
               "exploratory": group in EXPLORATORY_GROUPS}
    if control["exploratory"]:
        control["help"] = help_text + " " + EXPLORATORY_NOTE
    return control


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

    # -- Access: who is allowed onto which approach ------------------------
    for name, vspec in D.VEHICLE_CLASSES.items():
        for arm in "NSEW":
            controls.append(_dial(
                "access." + name + "." + arm,
                "No " + D.plural(name) + " on " + arm, "Access",
                0, 1, 1, "",
                "Ban " + D.plural(name) + " from entering on the "
                + arm + " approach. This junction is the whole model, so that "
                "demand has no other arm to arrive on: it is turned away and "
                "reported, not silently re-routed.",
                kind="toggle"))

    # -- Fleet: what is in the traffic, and how it drives ------------------
    controls.append(_dial(
        "fleet.hcv_share", "Truck / HCV share", "Fleet", 0.0, 0.20, 0.01, "",
        "Share of the flow that is a heavy goods vehicle, taken proportionally "
        "off the other classes. The default ("
        + format(D.VEHICLE_CLASSES["hcv"]["share"], ".2f") + ") is a declared "
        "prior, not a count: no HMV composition figure for this corridor has "
        "been read in, so every number under it - including the baseline - is "
        "waiting on a re-run of the calibration against a mix that contains "
        "trucks."))

    controls.append(_dial(
        "fleet.hmv_discipline", "HMV driving discipline", "Fleet",
        0.0, 1.0, 0.05, "",
        "How buses and trucks take gaps: 0 waits for one, 1 takes one and "
        "expects to be let in. Moves lcPushy, lcAssertive, jmTimegapMinor and "
        "impatience on the heavy classes only - dimensions stay where "
        "Indo-HCM put them. This is the dial behind 'HMVs incite the traffic': "
        "same demand, same signal, only the conduct changes."))

    controls.append(_dial(
        "fleet.hmv_stop_rate", "HMVs stopping unscheduled", "Fleet",
        0.0, 1.0, 0.05, "",
        "Fraction of buses and trucks that stop mid-route in a live lane - a "
        "bus loading, a truck parked half in. Rolled per vehicle as it "
        "departs, so the obstruction comes from the flow rather than from "
        "someone placing it."))

    for name, vspec in D.VEHICLE_CLASSES.items():
        controls.append(_dial(
            "fleet.injected." + name, D.plural(name).capitalize() + " added",
            "Fleet", 0.0, 1200.0, 10.0, "veh/h",
            "Extra " + D.plural(name) + " per hour added on TOP of the "
            "calibrated flow, not sliced out of it - which is what a scheme "
            "that buys vehicles actually does. At one junction this says what "
            "N more per hour do to these queues, not what a city-wide scheme "
            "does."))

    controls.append(_dial(
        "fleet.mode_shift", "Shift to public transit", "Fleet", 0.0, 0.50, 0.05,
        "",
        "Fraction of car and two-wheeler trips replaced by bus trips at the "
        "declared occupancies (car "
        + format(D.occupancy("car"), ".1f") + ", two-wheeler "
        + format(D.occupancy("twowheeler"), ".1f") + ", bus "
        + format(D.occupancy("bus"), ".0f") + " people). Autos are left alone: "
        "an auto trip is already a hired trip."))

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
        elif parts[0] == "access":
            entry["value"] = parts[1] in spec.restricted_on(parts[2])
        elif control["id"] == "fleet.hcv_share":
            entry["value"] = float(spec.hcv_share
                                   if spec.hcv_share is not None
                                   else D.VEHICLE_CLASSES["hcv"]["share"])
        elif control["id"] == "fleet.hmv_discipline":
            entry["value"] = float(spec.hmv_discipline)
        elif control["id"] == "fleet.hmv_stop_rate":
            entry["value"] = float(spec.hmv_stop_rate)
        elif control["id"] == "fleet.mode_shift":
            entry["value"] = float(spec.mode_shift)
        elif parts[0] == "fleet" and parts[1] == "injected":
            entry["value"] = float(spec.injected.get(parts[2], 0.0))
        elif parts[0] == "obstruction":
            entry["value"] = placed_counts.get((parts[1], parts[2]), 0)
        controls.append(entry)

    return {"controls": controls, "groups": GROUPS,
            "exploratory_groups": sorted(EXPLORATORY_GROUPS),
            "exploratory_note": EXPLORATORY_NOTE,
            "exploratory": bool(spec.is_exploratory),
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
    # baseline is every scenario lever at its neutral value: no ban, no
    # injected fleet, no mode shift, no HCVs, heavy vehicles driving the way
    # Indo-HCM describes them. That is the model the archive validated.
    for control in declare(shape):
        if control["group"] == "Obstructions":
            default = 0
        elif control["kind"] == "toggle":
            default = False
        elif control["group"] == "Fleet":
            default = float(D.VEHICLE_CLASSES["hcv"]["share"]) \
                if control["id"] == "fleet.hcv_share" else 0.0
        else:
            default = False
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


def access_edit(spec, control_id, value):
    """Ban or unban one vehicle class on one approach."""
    _, class_name, arm = control_id.split(".")
    if class_name not in D.VEHICLE_CLASSES:
        raise Rejected("No vehicle class called '" + class_name + "'.")
    out = spec.copy()
    banned = {a: list(c) for a, c in out.access_restrictions.items()}
    here = set(banned.get(arm, ()))
    on = bool(value) and str(value).lower() not in ("0", "false", "off")
    here.add(class_name) if on else here.discard(class_name)
    if here:
        banned[arm] = sorted(here)
    else:
        banned.pop(arm, None)
    out.access_restrictions = banned

    if not on:
        return out, ""
    if set(D.VEHICLE_CLASSES) - here == set():
        raise Rejected("That would ban every vehicle class from the " + arm
                       + " approach, which is a road closure and not an access "
                         "restriction. Close the arm with roadworks instead.")
    return out, (D.VEHICLE_CLASSES[class_name]["label"] + "s are turned away on "
                 + arm + " rather than re-routed - one junction has no other "
                 "arm for them to arrive on")


def fleet_edit(spec, control_id, value):
    """The fleet levers. Every one of these is exploratory."""
    control = lookup(control_id)
    clamped, note = clamp(control, value)
    out = spec.copy()
    parts = control_id.split(".")

    if control_id == "fleet.hcv_share":
        out.hcv_share = clamped
        share_note = ("HCV share is a declared figure, not a counted one: "
                      "nothing in the archive says how many trucks use this "
                      "corridor") if clamped else ""
    elif control_id == "fleet.hmv_discipline":
        out.hmv_discipline = clamped
        share_note = ("Driving discipline is new behavioural ground, not a "
                      "citation. Show it as a paired before/after, not as a "
                      "level") if clamped else ""
    elif control_id == "fleet.hmv_stop_rate":
        out.hmv_stop_rate = clamped
        share_note = ""
    elif control_id == "fleet.mode_shift":
        out.mode_shift = clamped
        share_note = ("Mode shift rests on the declared occupancies, which are "
                      "a prior. It is a statement about this junction, not "
                      "about the corridor") if clamped else ""
    elif parts[1] == "injected":
        name = parts[2]
        if name not in D.VEHICLE_CLASSES:
            raise Rejected("No vehicle class called '" + name + "'.")
        injected = dict(out.injected)
        if clamped:
            injected[name] = clamped
        else:
            injected.pop(name, None)
        out.injected = injected
        share_note = ("Added on top of the calibrated flow. At one junction "
                      "this is what those vehicles do to these queues, not "
                      "what a scheme does city-wide") if clamped else ""
    else:
        raise Rejected("No fleet control called '" + control_id + "'.")

    parts_note = [p for p in (note, share_note) if p]
    return out, "; ".join(parts_note)


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
        if change["id"].startswith("access."):
            _, class_name, arm = change["id"].split(".")
            label = D.plural(class_name)
            return (label + " " + ("banned from" if change["to"]
                                    else "allowed back onto") + " the " + arm
                    + " approach")
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
