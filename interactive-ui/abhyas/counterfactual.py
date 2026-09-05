# "What if we changed this signal?" - answered with a range, never a number.
#
# Baseline and scenario run on the SAME seeds, so what comes out is the effect
# of the change and not the effect of the randomness. Differences are taken per
# seed and the band is a bootstrap over those paired differences.
#
# Three rules that live in the code rather than in someone's head:
#   - every change is relative to baseline and carries a band
#   - "cannot resolve" is a legitimate answer and at small seed counts it's
#     usually the right one
#   - when the junction improves overall the per approach breakdown still gets
#     printed, because something normally got worse

import json
import time

from . import agents as G
from . import config as C
from . import demand as D
from . import tls as T
from .stats import Stats

# what a before/after card reports, and which direction is bad
APPROACH_METRICS = [
    ("queue_mean_veh", "Mean queue", True, "vehicles"),
    ("queue_p90_veh", "90th-percentile queue", True, "vehicles"),
    ("waiting_time_mean_s", "Mean waiting time", True, "s"),
    ("throughput_veh_per_hour", "Throughput", False, "veh/h"),
]

OVERALL_METRICS = [
    ("queue_mean_veh", "Mean queue, all approaches", True, "vehicles"),
    ("throughput_veh_per_hour", "Junction throughput", False, "veh/h"),
    # A scheme that swaps 25 cars for one bus moves fewer vehicles on purpose.
    # Reporting only the vehicle count would score that as a loss.
    ("person_throughput_per_hour", "People moved", False, "people/h"),
]


class Counterfactual:
    """One signal change, compared against baseline over paired seeds."""

    def __init__(self, delta_seconds=0.0, phase_group="north_south", seeds=30,
                 spec=None, base_plan=None, workers=4, splits_sweep=True,
                 progress=None, scenario_spec=None):
        self.delta_seconds = delta_seconds
        self.phase_group = phase_group
        self.seeds = seeds
        self.spec = spec or D.DemandSpec()
        # A fleet or access scenario changes the traffic, not the signal. It is
        # compared exactly the same way: same seeds, paired differences, a band
        # and a verdict that is allowed to be "cannot resolve".
        self.scenario_spec = scenario_spec or self.spec
        self.base_plan = base_plan or T.baseline_plan()
        self.scenario_plan = T.apply_delta(self.base_plan, phase_group, delta_seconds)
        self.progress = progress or (lambda **kw: None)
        self.splits_sweep = splits_sweep
        self.pool = G.Pool(workers=workers, progress=self.progress)
        self.seed_list = list(range(1, seeds + 1))

    def compare(self, base_series, scen_series, higher_is_worse, label, unit):
        """The same three lines we'd otherwise write out twenty times."""
        return {"label": label, "unit": unit,
                "baseline": Stats.describe(list(base_series.values()), "baseline"),
                "scenario": Stats.describe(list(scen_series.values()), "scenario"),
                "comparison": Stats.paired_difference(
                    base_series, scen_series, higher_is_worse=higher_is_worse)}

    def run(self):
        started = time.time()

        applied = (self.scenario_plan[self.phase_group]["green"]
                   - self.base_plan[self.phase_group]["green"])
        clamped = abs(applied - self.delta_seconds) > 0.01

        self.progress(kind="counterfactual", state="baseline", seeds=self.seeds)
        base = G.Batch(self.pool.run(self.spec, self.base_plan, self.seed_list,
                                     tag="baseline"))
        self.progress(kind="counterfactual", state="scenario", seeds=self.seeds)
        scen = G.Batch(self.pool.run(self.scenario_spec, self.scenario_plan,
                                     self.seed_list, tag="scenario"))

        card = {
            "change": {
                "phase_group": self.phase_group,
                "phase_label": C.PHASE_GROUPS[self.phase_group]["label"],
                "requested_delta_s": self.delta_seconds,
                "applied_delta_s": applied,
                "clamped": clamped,
                "baseline_green_s": self.base_plan[self.phase_group]["green"],
                "scenario_green_s": self.scenario_plan[self.phase_group]["green"],
                "baseline_cycle_s": T.cycle_seconds(self.base_plan),
                "scenario_cycle_s": T.cycle_seconds(self.scenario_plan),
            },
            "seeds_requested": self.seeds,
            "seeds_completed": {"baseline": len(base), "scenario": len(scen)},
            "demand": self.spec.to_dict(),
            "scenario_demand": self.scenario_spec.to_dict(),
            "fleet_changes": self._lever_changes(),
            "exploratory": bool(self.spec.is_exploratory
                                or self.scenario_spec.is_exploratory),
            "movements": {},
            "approaches": {},
            "overall": {},
            "notes": [],
        }
        if card["exploratory"]:
            card["notes"].append(
                "This comparison moves a fleet or access lever. Those are "
                "declared priors, swept and not fitted, and nothing in the "
                "validation archive constrains them: the verdict is a "
                "comparison between two model runs, not a validated result "
                "about the road.")
        if clamped:
            card["notes"].append("The requested change was clamped: a phase "
                                 "can't go below " + str(int(T.MIN_GREEN_S))
                                 + " s or above " + str(int(T.MAX_GREEN_S))
                                 + " s of green. Applied change is "
                                 + format(applied, "+.0f") + " s.")

        # per movement travel time. all twelve, not just the four the interface
        # talks about - the fleet already measures the turns.
        for movement in C.ALL_MOVEMENTS:
            card["movements"][movement] = self.compare(
                base.movement_series(movement), scen.movement_series(movement),
                True, C.ALL_MOVEMENTS[movement]["label"], "s")

        for arm in "NESW":
            card["approaches"][arm] = {
                key: self.compare(base.approach_series(arm, key),
                                  scen.approach_series(arm, key),
                                  higher_is_worse, label, unit)
                for key, label, higher_is_worse, unit in APPROACH_METRICS}

        for key, label, higher_is_worse, unit in OVERALL_METRICS:
            card["overall"][key] = self.compare(
                base.overall_series(key), scen.overall_series(key),
                higher_is_worse, label, unit)

        card["verdict"] = self._verdict(card)
        card["notes"].extend(self._honesty_notes(card))

        if self.splits_sweep:
            card["splits_sweep"] = self._sweep_splits()
            card["notes"].append(self._sweep_note(card["splits_sweep"]))

        card["seconds"] = round(time.time() - started, 1)
        tag = format(self.delta_seconds, "+.0f").replace("+", "p").replace("-", "m")
        out = C.RESULTS / ("counterfactual_" + self.phase_group + "_" + tag + ".json")
        out.write_text(json.dumps(card, indent=2), encoding="utf-8")
        card["saved_to"] = str(out)
        return card

    def _lever_changes(self):
        """Which fleet/access levers differ between the two specs."""
        before, after = self.spec.to_dict(), self.scenario_spec.to_dict()
        return {lever: {"from": before.get(lever), "to": after.get(lever)}
                for lever in D.DemandSpec.LEVERS
                if before.get(lever) != after.get(lever)}

    # -- reading the card --------------------------------------------------

    def _verdict(self, card):
        overall = card["overall"]["queue_mean_veh"]["comparison"]
        per_arm = {arm: metrics["queue_mean_veh"]["comparison"]["verdict"]
                   for arm, metrics in card["approaches"].items()}
        worse = sorted(a for a, v in per_arm.items() if v == "worsens")
        better = sorted(a for a, v in per_arm.items() if v == "improves")
        unresolved = sorted(a for a, v in per_arm.items() if v == "cannot resolve")
        return {"junction_overall": overall["verdict"],
                "junction_statement": overall.get("statement", ""),
                "approaches_worse": worse,
                "approaches_better": better,
                "approaches_unresolved": unresolved,
                "headline": self._headline(overall, worse, better, unresolved)}

    @staticmethod
    def _band(comparison):
        low, high = sorted(abs(v) for v in comparison["ci95_change_pct"])
        return format(low, ".0f") + "-" + format(high, ".0f") + "%"

    def _headline(self, overall, worse, better, unresolved):
        if overall["verdict"] == "cannot resolve":
            line = ("Junction overall: cannot resolve at "
                    + str(overall.get("n_pairs", 0)) + " paired seeds.")
        elif overall["verdict"] == "improves":
            line = ("Junction overall improves: mean queue falls by "
                    + self._band(overall) + " relative to baseline.")
        else:
            line = ("Junction overall worsens: mean queue rises by "
                    + self._band(overall) + " relative to baseline.")
        if worse:
            line += " Clearly worse on: " + ", ".join(worse) + "."
        if better:
            line += " Clearly better on: " + ", ".join(better) + "."
        if unresolved:
            line += " Unresolved on: " + ", ".join(unresolved) + "."
        return line

    @staticmethod
    def _honesty_notes(card):
        notes = []
        verdict = card["verdict"]

        people = card["overall"].get("person_throughput_per_hour")
        if people and card.get("fleet_changes"):
            notes.append(
                "People moved is computed from the declared occupancies in "
                "demand.VEHICLE_CLASSES, which nobody counted on this "
                "corridor. It is the right quantity for a transit argument and "
                "the wrong one to quote to three significant figures.")
            if (people["comparison"]["verdict"] == "improves"
                    and card["overall"]["throughput_veh_per_hour"]
                    ["comparison"]["verdict"] == "worsens"):
                notes.append(
                    "The junction moves fewer vehicles and more people. That "
                    "is the transit argument working as intended, and it is "
                    "also exactly the result a vehicle-only count would have "
                    "reported as a failure.")
        pairs = card["overall"]["queue_mean_veh"]["comparison"].get("n_pairs", 0)

        if verdict["junction_overall"] == "cannot resolve":
            notes.append("'Cannot resolve' is the answer here, not a missing "
                         "one: over " + str(pairs) + " paired seeds the change "
                         "straddles zero. Usually takes around 30 before the "
                         "junction-wide effect settles, and it still might not.")
        if verdict["junction_overall"] == "improves" and not verdict["approaches_worse"]:
            notes.append("The junction improves overall and no approach is "
                         "clearly worse. That's unusual for a signal change - "
                         "green taken from one phase normally shows up "
                         "somewhere - so it wants more seeds before quoting.")
        if verdict["approaches_worse"] and verdict["approaches_better"]:
            notes.append("The change helps some approaches and hurts others. A "
                         "junction-wide figure alone hides that, which is why "
                         "the per-approach breakdown is the card and the "
                         "overall line is only its summary.")
        notes.append("Everything above is relative to baseline with a 95% "
                     "bootstrap band over paired-seed differences. Baseline and "
                     "scenario used identical seeds.")
        return notes

    # -- does the verdict survive the splits we can't measure --------------

    def _sweep_splits(self, seeds=8):
        rows = []
        seed_list = list(range(1, seeds + 1))
        for label, splits in G.SensitivityAgent.SPLITS:
            # the split moves on both sides at once - it is the thing being
            # swept, not part of the change under test
            variant = self.spec.copy(turning_splits=dict(splits))
            scen_variant = self.scenario_spec.copy(turning_splits=dict(splits))
            self.progress(kind="counterfactual", state="splits", splits=label)
            base = G.Batch(self.pool.run(variant, self.base_plan, seed_list,
                                         tag="sweep-base-" + label))
            scen = G.Batch(self.pool.run(scen_variant, self.scenario_plan,
                                         seed_list, tag="sweep-scen-" + label))
            comparison = Stats.paired_difference(
                base.overall_series("queue_mean_veh"),
                scen.overall_series("queue_mean_veh"), higher_is_worse=True)
            per_arm = {arm: Stats.paired_difference(
                base.approach_series(arm, "queue_mean_veh"),
                scen.approach_series(arm, "queue_mean_veh"),
                higher_is_worse=True)["verdict"] for arm in "NESW"}
            rows.append({"splits": label, "through_left_right": splits,
                         "seeds": seeds, "overall": comparison,
                         "per_arm": per_arm})

        verdicts = {row["overall"]["verdict"] for row in rows}
        return {"rows": rows,
                "seeds_per_split": seeds,
                "overall_stable": len(verdicts) == 1,
                "overall_verdicts": sorted(verdicts),
                "per_arm_stable": {
                    arm: len({row["per_arm"][arm] for row in rows}) == 1
                    for arm in "NESW"}}

    @staticmethod
    def _sweep_note(sweep):
        span = "50/30/20 through 70/20/10"
        if sweep["overall_stable"]:
            return ("The junction-wide verdict is '" + sweep["overall_verdicts"][0]
                    + "' under every turning split tested (" + span + "). The "
                    "conclusion doesn't depend on the split we can't measure.")
        unstable = [a for a, ok in sweep["per_arm_stable"].items() if not ok]
        return ("The junction-wide verdict changes across the tested splits ("
                + ", ".join(sweep["overall_verdicts"]) + " over " + span + ")"
                + (", and the per-approach verdict moves on " + ", ".join(unstable)
                   if unstable else "")
                + ". This rests on a quantity we cannot measure and shouldn't be "
                "presented as a result.")


def run(delta_seconds=10.0, phase_group="north_south", seeds=30, spec=None,
        base_plan=None, workers=4, splits_sweep=True, progress=None,
        scenario_spec=None):
    return Counterfactual(delta_seconds=delta_seconds, phase_group=phase_group,
                          seeds=seeds, spec=spec, base_plan=base_plan,
                          workers=workers, splits_sweep=splits_sweep,
                          progress=progress, scenario_spec=scenario_spec).run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--delta", type=float, default=10.0)
    parser.add_argument("--phase", default="north_south")
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--veh-per-hour", type=float, default=2400.0)
    parser.add_argument("--duration", type=float, default=1800.0)
    parser.add_argument("--no-sweep", action="store_true")
    args = parser.parse_args()

    def show(**kw):
        if kw.get("kind") == "counterfactual":
            print("[" + kw.get("state", "") + "] " + str(kw.get("splits", "")))

    result = run(delta_seconds=args.delta, phase_group=args.phase,
                 seeds=args.seeds, workers=args.workers,
                 splits_sweep=not args.no_sweep,
                 spec=D.DemandSpec(veh_per_hour=args.veh_per_hour,
                                   duration_s=args.duration),
                 progress=show)
    print(json.dumps(result["verdict"], indent=2))
    for note in result["notes"]:
        print("- " + note)
    print("saved to", result["saved_to"])
