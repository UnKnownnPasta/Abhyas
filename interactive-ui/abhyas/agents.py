# The validation fleet.
#
# Seven agents, one job each, every one of them allowed to come back and say
# no. They run in parallel processes (each one is a pile of SUMO runs) and
# report progress through a callback so the CLI or the browser can show what
# the fleet is doing while it does it.
#
#   archive-audit    is the data even fit to validate against
#   calibration      find the one dial - vehicles per hour - for a time slot
#   movement         model vs measured per movement, 30 runs, median + spread
#   asymmetry        does the model reproduce the direction asymmetry
#   seed-stability   how wrong would one single run have been
#   sensitivity      does the verdict survive the turning splits we can't measure
#   phase-plan       which signal plan shape does the archive actually prefer
#
# Nothing here nudges a target to make the model pass, and no agent reports a
# number without saying how many runs made it.

import datetime as dt
import json
import statistics
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import archive as A
from . import config as C
from . import demand as D
from . import tls as T
from .stats import Stats

DEFAULT_SEEDS = 30
# Twelve, not five. Travel time here is bimodal near capacity - a batch either
# clears each cycle or locks up - so a five seed median at one demand level is
# a coin toss and calibration lands on whichever way it fell.
CALIBRATION_SEEDS = 12
SENSITIVITY_SEEDS = 8


def run_worker(payload):
    """One seed of one scenario. Has to be importable at module level for the
    process pool to pickle it."""
    from . import sim

    try:
        # to_dict() also carries derived fields ("exploratory") that aren't
        # constructor arguments. Filter rather than letting a reporting field
        # decide whether a worker starts.
        fields = payload["spec"]
        allowed = ("veh_per_hour", "arm_share", "turning_splits", "seed",
                   "duration_s") + D.DemandSpec.LEVERS
        spec = D.DemandSpec(**{k: v for k, v in fields.items() if k in allowed})
        plan = payload.get("plan") or T.baseline_plan()
        result = sim.run_once(spec, plan=plan, seed=payload["seed"],
                              obstructions=payload.get("obstructions"))
        return {"ok": True, "seed": payload["seed"], "tag": payload.get("tag"),
                "result": result.to_dict()}
    except Exception as exc:
        return {"ok": False, "seed": payload["seed"], "tag": payload.get("tag"),
                "error": str(exc), "traceback": traceback.format_exc()}


class Pool:
    """Runs batches of seeds in parallel and reports them as they land."""

    def __init__(self, workers=4, progress=None):
        self.workers = max(1, workers)
        self.progress = progress or (lambda **kw: None)

    def run(self, spec, plan, seeds, tag="", obstructions=None):
        payloads = [{"spec": spec.to_dict(), "plan": plan, "seed": seed,
                     "tag": tag, "obstructions": obstructions} for seed in seeds]
        results, failures, done = [], [], 0
        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(run_worker, p) for p in payloads]
            for future in as_completed(futures):
                outcome = future.result()
                done += 1
                self.progress(kind="run", tag=tag, done=done,
                              total=len(payloads), ok=outcome["ok"])
                if outcome["ok"]:
                    results.append(outcome["result"])
                else:
                    failures.append(outcome)
        if failures:
            self.progress(kind="warning", tag=tag,
                          message=str(len(failures)) + " run(s) failed: "
                          + failures[0].get("error", ""))
        results.sort(key=lambda r: r["seed"])
        return results


class Batch:
    """A list of run results with the questions we keep asking of it."""

    def __init__(self, results):
        self.results = results or []

    def __len__(self):
        return len(self.results)

    def __bool__(self):
        return bool(self.results)

    def movement_series(self, movement, field="travel_time_s"):
        """seed -> value, keeping the seed attached the whole way."""
        return {r["seed"]: r["movements"].get(movement, {}).get(field)
                for r in self.results}

    def movement_values(self, movement, field="travel_time_s"):
        return [v for v in self.movement_series(movement, field).values() if v]

    def approach_series(self, arm, field):
        return {r["seed"]: r["approaches"].get(arm, {}).get(field)
                for r in self.results}

    def overall_series(self, field):
        return {r["seed"]: r["overall"].get(field) for r in self.results}

    def corridor_m(self, movement):
        """How far the model measures a movement over. Network constant."""
        for r in self.results:
            length = r["movements"].get(movement, {}).get("corridor_length_m")
            if length:
                return float(length)
        return 0.0

    def describe(self, movement):
        return Stats.describe(list(self.movement_series(movement).values()),
                              label=movement)


# kept as free functions because counterfactual.py and the server import them
def movement_series(results, movement, field_name="travel_time_s"):
    return Batch(results).movement_series(movement, field_name)


def approach_series(results, arm, field_name):
    return Batch(results).approach_series(arm, field_name)


class AgentReport:
    def __init__(self, name, title, status="ok", headline=""):
        self.name = name
        self.title = title
        self.status = status            # ok | warning | failed
        self.headline = headline
        self.findings = []
        self.data = {}
        self.runs = 0
        self.seconds = 0.0

    def say(self, text):
        self.findings.append(text)

    def warn(self, text):
        self.status = "warning"
        self.findings.append(text)

    def fail(self, headline):
        self.status = "failed"
        self.headline = headline
        return self

    def to_dict(self):
        return {"name": self.name, "title": self.title, "status": self.status,
                "headline": self.headline, "findings": self.findings,
                "data": self.data, "runs": self.runs,
                "seconds": round(self.seconds, 1)}


class Agent:
    name = "agent"
    title = "Agent"
    blurb = ""
    needs_simulation = True

    def __init__(self, context, pool, progress=None):
        self.ctx = context
        self.pool = pool
        self.progress = progress or (lambda **kw: None)

    # handy shorthands, every agent wants these
    @property
    def archive(self):
        return self.ctx["archive"]

    @property
    def junction(self):
        return self.ctx["junction"]

    @property
    def slot(self):
        return self.ctx["slot"]

    def target(self, movement):
        return self.archive.target(self.junction, movement, hour_slot=self.slot)

    def report(self):
        return AgentReport(self.name, self.title)

    def batch(self, spec, plan, seeds, tag):
        return Batch(self.pool.run(spec, plan, range(1, seeds + 1), tag=tag))

    def run(self):
        raise NotImplementedError

    def execute(self):
        started = time.time()
        self.progress(kind="agent", agent=self.name, state="started")
        try:
            report = self.run()
        except Exception as exc:
            report = AgentReport(self.name, self.title, status="failed",
                                 headline="Agent failed: " + str(exc))
            report.findings.append(traceback.format_exc())
        report.seconds = time.time() - started
        self.progress(kind="agent", agent=self.name, state="finished",
                      status=report.status, headline=report.headline)
        return report


# ---- 1. archive audit (no simulation) ------------------------------------

class ArchiveAuditAgent(Agent):
    name = "archive-audit"
    title = "Archive audit"
    blurb = "Reads the spreadsheet and says whether it's fit to validate against."
    needs_simulation = False

    def run(self):
        archive = self.archive
        report = self.report()

        load = archive.report.to_dict()
        coverage = archive.coverage(self.junction)
        report.data["load"] = load
        report.data["coverage"] = coverage
        report.data["free_flow_kmh"] = archive.free_flow_for(self.junction)

        if not coverage.get("observations"):
            return report.fail("No observations for " + self.junction
                               + " in the sheets read. Nothing to validate.")

        dropped = load["dropped_total"]
        report.say("Read " + str(load["rows_read"]) + " rows from "
                   + ", ".join(load["sheets_used"]) + "; kept "
                   + str(load["rows_kept"]) + ", dropped " + str(dropped) + " ("
                   + str(load["dropped_error"]) + " with an error string, "
                   + str(load["dropped_no_time"]) + " with no travel time, "
                   + str(load["dropped_bad_status"]) + " non-200).")
        if load["sheets_skipped"]:
            report.say("Skipped: " + ", ".join(load["sheets_skipped"])
                       + ". Those are the earlier collection run and aren't "
                       "part of this validation unless asked for.")
        if load["free_flow_derived"]:
            report.say("The live sheets leave free-flow and historic empty, so "
                       "free-flow time was derived from the flow segment "
                       "sheet's observed speed for "
                       + str(load["free_flow_derived"]) + " rows. Delay "
                       "figures from this batch are derived, not measured.")

        report.say("Coverage: " + str(coverage["observations"])
                   + " observations across " + str(coverage["days"]) + " day(s), "
                   + coverage["first"][:16] + " to " + coverage["last"][:16]
                   + ", " + str(coverage["per_movement"]) + ".")

        report.data["hour_slots"] = {m: archive.hour_slots(self.junction, m)
                                     for m in C.ALL_MOVEMENTS}
        report.data["daily_profile"] = {m: archive.daily_profile(self.junction, m)
                                        for m in C.ALL_MOVEMENTS}

        thin = [m for m, slots in report.data["hour_slots"].items() if not slots]
        if thin:
            report.warn("No hour slot has three or more observations for: "
                        + ", ".join(thin) + ".")

        span_hours = 0.0
        if coverage.get("first") and coverage.get("last"):
            first = dt.datetime.fromisoformat(coverage["first"])
            last = dt.datetime.fromisoformat(coverage["last"])
            span_hours = (last - first).total_seconds() / 3600.0
        report.data["span_hours"] = round(span_hours, 1)

        # A calendar day count isn't the question. A batch running 21:40 to
        # 12:15 touches two dates and still never sees the same hour twice.
        # What matters is whether any hour slot got observed on two dates.
        dates_per_slot = {}
        for row in archive.select(junction=self.junction):
            dates_per_slot.setdefault(row.hour_slot, set()).add(row.observed.date())
        repeats = max((len(d) for d in dates_per_slot.values()), default=0)
        report.data["max_dates_per_hour_slot"] = repeats

        if repeats < 2:
            report.warn("The kept batch spans " + format(span_hours, ".0f")
                        + " hours and no hour slot in it was seen on more than "
                        "one date. So there's no day-to-day repetition to "
                        "measure: the 'spread' on every target below is "
                        "within-hour variation only and it understates the real "
                        "uncertainty. A model landing inside it cleared a lower "
                        "bar than the phrase suggests.")

        report.headline = (str(coverage["observations"]) + " observations, "
                           + str(coverage["days"]) + " day(s), " + str(dropped)
                           + " junk rows dropped")
        return report


# ---- 2. calibration - the one dial ---------------------------------------

class CalibrationAgent(Agent):
    name = "calibration"
    title = "Demand calibration"
    blurb = "Sweeps vehicles per hour until the model matches the measured times."

    def run(self):
        report = self.report()

        # The four through movements only, not all twelve. The dial is total
        # demand; the turns respond to it through the turning splits, which are
        # a declared prior - fitting the dial against them would be fitting one
        # guess to another. The turns get validated later against a dial they
        # didn't set.
        targets = {m: self.target(m) for m in C.MOVEMENTS}
        usable = {m: t for m, t in targets.items() if t.get("usable")}
        if not usable:
            return report.fail("No usable target in slot " + self.slot)

        # median across movements, because one number can't satisfy four
        # movements separately
        goal = statistics.median([t["travel_time_s"] for t in usable.values()])
        report.data["targets"] = targets
        report.data["objective_s"] = round(goal, 1)

        # A sweep, not a bisection. Near capacity the response to demand isn't
        # monotone batch to batch - a few percent more traffic tips an approach
        # from flowing to queueing and back - and a bisection assuming
        # monotonicity walks straight into the noise. The sweep also gives you
        # the response curve, which shows how sharp the answer really is.
        low, high = self.ctx.get("demand_bounds", (600.0, 3400.0))
        points = self.ctx.get("calibration_points", 8)
        step = (high - low) / (points - 1)
        curve = [self._probe(low + step * i, usable, goal, report, i)
                 for i in range(points)]

        best = self._best(curve)
        if best is not None:
            # refine either side of the best point at half the grid spacing
            for offset in (-step / 2.0, step / 2.0):
                candidate = best["veh_per_hour"] + offset
                if low <= candidate <= high:
                    curve.append(self._probe(candidate, usable, goal, report, None))
            best = self._best(curve)

        # sort for the chart, the refinement probes come in out of order
        curve.sort(key=lambda p: p["veh_per_hour"])
        report.data["response_curve"] = curve
        report.data["trace"] = curve
        if best is None:
            return report.fail("Calibration produced no usable runs.")

        report.data["calibrated_veh_per_hour"] = best["veh_per_hour"]
        residual = round(best["error_s"] / goal * 100.0, 1) if goal else None
        report.data["residual_pct"] = residual

        report.say("Swept the single dial across " + str(len(curve))
                   + " demand levels of " + str(CALIBRATION_SEEDS) + " seeds "
                   "each. Vehicle sizes and driver behaviour were not touched - "
                   "fitting several behavioural parameters against one aggregate "
                   "measurement just picks arbitrarily among equally good "
                   "answers, so only this one dial moves.")
        report.say("Best fit " + format(best["veh_per_hour"], ".0f") + " veh/h "
                   "gives a cross-movement median of "
                   + format(best["model_median_s"], ".0f") + " s against a "
                   "target of " + format(goal, ".0f") + " s ("
                   + format(best["error_s"], "+.0f") + " s).")

        steepness = self._steepness(curve, best)
        report.data["sensitivity_pct_per_10pct_demand"] = steepness
        if steepness is not None and steepness > 25:
            report.warn("The operating point is on the capacity cliff: 10% more "
                        "demand moves modelled travel time about "
                        + format(steepness, ".0f") + "%. That's what the "
                        "archive's own travel times imply, but it means the "
                        "calibrated volume isn't pinned down tightly and "
                        "everything downstream inherits that.")

        if abs(residual or 0) > 15:
            report.warn("The dial can't get within 15% of the target on the "
                        "cross-movement median. One number has to serve four "
                        "movements at once and on this junction it doesn't.")
        report.headline = (format(best["veh_per_hour"], ".0f") + " veh/h, "
                           "residual " + format(residual, "+.1f") + "%")
        return report

    def _probe(self, veh_per_hour, usable, goal, report, index):
        spec = self.ctx["demand"].copy(veh_per_hour=veh_per_hour)
        batch = self.batch(spec, T.baseline_plan(), CALIBRATION_SEEDS,
                           "calibrate-" + format(veh_per_hour, ".0f"))
        report.runs += len(batch)

        per_movement, medians = {}, []
        for movement in usable:
            series = batch.movement_values(movement)
            if series:
                value = statistics.median(series)
                per_movement[movement] = round(value, 1)
                medians.append(value)
        modelled = statistics.median(medians) if medians else None

        point = {"veh_per_hour": round(veh_per_hour, 0),
                 "model_median_s": round(modelled, 1) if modelled else None,
                 "per_movement_s": per_movement,
                 "target_s": round(goal, 1),
                 "error_s": round(modelled - goal, 1) if modelled else None,
                 "seeds": len(batch)}
        self.progress(kind="calibration", agent=self.name, step=index,
                      veh_per_hour=round(veh_per_hour),
                      model_s=point["model_median_s"], target_s=round(goal, 1))
        return point

    @staticmethod
    def _best(curve):
        usable = [p for p in curve if p["error_s"] is not None]
        return min(usable, key=lambda p: abs(p["error_s"])) if usable else None

    @staticmethod
    def _steepness(curve, best):
        """% change in modelled travel time per 10% change in demand."""
        if not best["model_median_s"]:
            return None
        neighbours = [p for p in curve
                      if p["model_median_s"]
                      and 0 < abs(p["veh_per_hour"] - best["veh_per_hour"])
                      <= best["veh_per_hour"] * 0.25]
        slopes = []
        for point in neighbours:
            d_demand = (point["veh_per_hour"] - best["veh_per_hour"]) / best["veh_per_hour"]
            d_time = (point["model_median_s"] - best["model_median_s"]) / best["model_median_s"]
            if d_demand:
                slopes.append(abs(d_time / d_demand) * 10.0)
        return round(statistics.median(slopes), 1) if slopes else None


# ---- 3. movement validation - the credibility table ----------------------

class MovementValidationAgent(Agent):
    name = "movement"
    title = "Per-movement validation"
    blurb = "Model vs measured on all twelve corridors, median of many runs."

    # if the model and the archive measure over lengths this far apart, they're
    # not measuring the same road
    LENGTH_MISMATCH = 0.20

    def run(self):
        report = self.report()
        seeds = self.ctx.get("seeds", DEFAULT_SEEDS)
        spec = self.ctx["calibrated_demand"]
        batch = self.batch(spec, T.baseline_plan(), seeds, "validate")
        report.runs = len(batch)
        # asymmetry and seed-stability read this same batch. handing it over
        # saves two thirds of the work and makes all three agents describe one
        # experiment instead of three.
        self.ctx["validation_batch"] = batch
        if not batch:
            return report.fail("No runs completed.")

        rows = [self._row(batch, movement, seeds) for movement in C.ALL_MOVEMENTS]
        report.data["rows"] = rows
        report.data["seeds"] = seeds
        report.data["demand"] = spec.to_dict()
        report.data["slot"] = self.slot

        counted = [r for r in rows if r["comparable"] and r["verdict"] != "no data"]
        passed = [r for r in counted if r["verdict"] == "pass"]
        report.data["pass_count"] = len(passed)
        report.data["counted"] = len(counted)
        by_kind = {}
        for kind in ("through", "turn"):
            of_kind = [r for r in counted if r["kind"] == kind]
            by_kind[kind] = {"counted": len(of_kind),
                             "passed": len([r for r in of_kind
                                            if r["verdict"] == "pass"])}
        report.data["by_kind"] = by_kind

        report.say("Median of " + str(seeds) + " runs per movement, every value "
                   "with its spread. One run is not a result here - the "
                   "run-to-run spread is wide enough that a single seed can be "
                   "out by a factor.")
        report.say("All twelve corridors, not just the four through movements: "
                   + str(by_kind["through"]["passed"]) + " of "
                   + str(by_kind["through"]["counted"]) + " through and "
                   + str(by_kind["turn"]["passed"]) + " of "
                   + str(by_kind["turn"]["counted"]) + " turning. The turns "
                   "carry a caveat: how much traffic turns is a declared prior, "
                   "so a turn that misses can be the split rather than the "
                   "model. The sensitivity agent sweeps that.")
        for row in rows:
            if not row["comparable"]:
                report.say(row["movement"] + ": " + row["note"])

        if counted and len(passed) == len(counted):
            report.headline = ("All " + str(len(counted))
                               + " comparable movements within tolerance")
        elif passed:
            report.status = "warning"
            report.headline = (str(len(passed)) + " of " + str(len(counted))
                               + " comparable movements within tolerance")
        else:
            report.headline = ("No movement is within tolerance - the model "
                               "fails its own test in slot " + self.slot)
            report.warn("This is reported as a failure rather than absorbed. "
                        "The target wasn't adjusted and no run was picked for "
                        "looking better than the others.")
        return report

    def _row(self, batch, movement, seeds):
        target = self.target(movement)
        model = batch.describe(movement)
        model_length = batch.corridor_m(movement) or None
        archive_length = target.get("length_m")

        comparable, note = True, ""
        if model_length and archive_length:
            gap = abs(model_length - archive_length) / archive_length
            if gap > self.LENGTH_MISMATCH:
                comparable = False
                note = ("The archive measures this over "
                        + format(archive_length, ".0f") + " m while the model "
                        "covers " + format(model_length, ".0f") + " m between "
                        "the same two points. The measured route goes round the "
                        "median, ours doesn't. Not like for like, so it's "
                        "reported and left out of the pass rate.")

        verdict = Stats.validation_verdict(model, target, comparable=comparable,
                                           note=note)
        verdict.update({
            "movement": movement,
            "label": C.ALL_MOVEMENTS[movement]["label"],
            "kind": "through" if movement in C.MOVEMENTS else "turn",
            "slot": self.slot,
            "model_corridor_m": model_length,
            "archive_corridor_m": archive_length,
            "model_spread_p10_p90_s": model.get("p10_p90"),
        })
        return verdict


# ---- 4. asymmetry --------------------------------------------------------

def direction_of(ratio, threshold=1.10):
    if ratio is None:
        return "unresolved"
    if ratio >= threshold:
        return "first slower"
    if ratio <= 1.0 / threshold:
        return "second slower"
    return "even"


class AsymmetryAgent(Agent):
    name = "asymmetry"
    title = "Direction asymmetry"
    blurb = "Does the model get the fast/slow direction right on each road."

    PAIRS = [("NS", "SN", "100 Feet Road"), ("EW", "WE", "CMH Road")]
    LENGTH_MISMATCH = 0.20

    def run(self):
        report = self.report()
        batch = self.ctx.get("validation_batch")
        if not batch:
            batch = self.batch(self.ctx["calibrated_demand"], T.baseline_plan(),
                               self.ctx.get("seeds", DEFAULT_SEEDS), "asymmetry")
            report.runs = len(batch)
        if not batch:
            return report.fail("No runs to compare.")

        rows = [self._pair_row(batch, a, b, road) for a, b, road in self.PAIRS]
        report.data["pairs"] = rows

        reproduced = [r for r in rows if r.get("reproduced")]
        checkable = [r for r in rows if r.get("resolved")
                     and r.get("measured_direction") != "even"]

        report.say("Asymmetry is compared as a ratio within each seed, so a "
                   "seed that was slow overall doesn't contaminate it.")
        for row in rows:
            report.say(self._line(row))

        if not checkable:
            report.warn("In this slot the two directions of each road are "
                        "within 10% of each other in the archive, so there's no "
                        "asymmetry to test. That's a fact about the slot, not "
                        "about the model.")
            report.headline = "No asymmetry in the measured data to reproduce"
        elif len(reproduced) == len(checkable):
            report.headline = ("Reproduces the measured asymmetry on "
                               + str(len(reproduced)) + " road(s)")
        else:
            report.status = "warning"
            report.headline = ("Reproduces " + str(len(reproduced)) + " of "
                               + str(len(checkable)) + " measured asymmetries")
        return report

    def _pair_row(self, batch, first, second, road):
        row = {"road": road, "pair": first + " vs " + second, "resolved": False}
        target_a, target_b = self.target(first), self.target(second)
        if not (target_a.get("usable") and target_b.get("usable")):
            row["reason"] = "one direction has no usable target in this slot"
            return row

        # Comparing two directions on raw travel time only works if both were
        # measured over the same distance, and here they weren't - the archive
        # routes SN over 892 m against NS's 406 m. So when the corridors differ
        # by more than a fifth we take the ratio on time per metre and say so.
        length_a = target_a.get("length_m") or 0.0
        length_b = target_b.get("length_m") or 0.0
        normalise = bool(length_a and length_b
                         and abs(length_a - length_b) / max(length_a, length_b)
                         > self.LENGTH_MISMATCH)

        raw_ratio = target_a["travel_time_s"] / target_b["travel_time_s"]
        if normalise:
            measured_ratio = ((target_a["travel_time_s"] / length_a)
                              / (target_b["travel_time_s"] / length_b))
        else:
            measured_ratio = raw_ratio

        a_series = batch.movement_series(first)
        b_series = batch.movement_series(second)
        if not batch.movement_values(first) or not batch.movement_values(second):
            row["reason"] = "the model produced no completed run for one direction"
            return row

        # if the measured ratio went per metre the model ratio has to as well,
        # or the two sides are in different units. the model's own corridors
        # aren't equal either (390 vs 407 m on 100 Feet Road) so this is a real
        # correction, and it's a constant because corridor length doesn't vary
        # by seed.
        model_len_a = batch.corridor_m(first)
        model_len_b = batch.corridor_m(second)
        model_scale = 1.0
        if normalise and model_len_a and model_len_b:
            model_scale = model_len_b / model_len_a

        paired = {}
        for seed in set(a_series) & set(b_series):
            if a_series[seed] and b_series[seed]:
                paired[seed] = (a_series[seed] / b_series[seed]) * model_scale
        ratio_ci = Stats.bootstrap_ci(list(paired.values()))
        model_ratio = statistics.median(paired.values()) if paired else None

        measured_dir = direction_of(measured_ratio)
        model_dir = direction_of(model_ratio) if model_ratio else "unresolved"

        if normalise:
            detail = (first + " " + format(target_a["travel_time_s"] / length_a, ".3f")
                      + " s/m vs " + second + " "
                      + format(target_b["travel_time_s"] / length_b, ".3f") + " s/m"
                      + " (" + format(target_a["travel_time_s"], ".0f") + " s over "
                      + format(length_a, ".0f") + " m, "
                      + format(target_b["travel_time_s"], ".0f") + " s over "
                      + format(length_b, ".0f") + " m)")
        else:
            detail = (first + " " + format(target_a["travel_time_s"], ".0f")
                      + " s vs " + second + " "
                      + format(target_b["travel_time_s"], ".0f") + " s")

        row.update({
            "resolved": True,
            "length_normalised": normalise,
            "measured_corridor_m": [length_a, length_b],
            "measured_ratio_raw": round(raw_ratio, 2),
            "measured_ratio": round(measured_ratio, 2),
            "measured_direction": measured_dir,
            "measured_detail": detail,
            "model_corridor_m": [model_len_a, model_len_b],
            "model_ratio": round(model_ratio, 2) if model_ratio else None,
            "model_ratio_ci95": [round(ratio_ci[0], 2), round(ratio_ci[1], 2)],
            "model_direction": model_dir,
            "reproduced": measured_dir == model_dir and measured_dir != "even",
            "n_pairs": len(paired),
        })
        return row

    @staticmethod
    def _line(row):
        if not row.get("resolved"):
            return (row["road"] + " (" + row["pair"] + "): not tested -- "
                    + row.get("reason", ""))
        line = (row["road"] + " (" + row["pair"] + "): measured "
                + row["measured_detail"] + ", ratio "
                + format(row["measured_ratio"], ".2f") + " -> "
                + row["measured_direction"] + ". Model ratio "
                + format(row["model_ratio"] or 0, ".2f") + " (CI "
                + "-".join(format(v, ".2f") for v in row["model_ratio_ci95"])
                + ", " + str(row["n_pairs"]) + " seeds) -> "
                + row["model_direction"] + ". "
                + ("Reproduced." if row["reproduced"] else "Not reproduced."))
        if row.get("length_normalised"):
            line += (" Compared on time per metre: the archive measures these "
                     "two over " + format(row["measured_corridor_m"][0], ".0f")
                     + " m and " + format(row["measured_corridor_m"][1], ".0f")
                     + " m, so raw times would make the longer one look slower "
                     "when it's only longer. On raw time the measured ratio "
                     "reads " + format(row["measured_ratio_raw"], ".2f") + ".")
        return line


# ---- 5. seed stability ---------------------------------------------------

def runs_needed(series, band_pct):
    """Rough number of runs for the median's 95% band to fit in +/- band_pct."""
    clean = [v for v in series if v]
    if len(clean) < 2:
        return 0
    median = statistics.median(clean)
    if not median:
        return 0
    cv = statistics.stdev(clean) / median * 100.0
    # standard error of a median is about 1.25x that of a mean
    return max(1, int(round((1.96 * 1.25 * cv / band_pct) ** 2)))


class SeedStabilityAgent(Agent):
    name = "seed-stability"
    title = "Seed stability"
    blurb = "How wrong a single run would have been."

    def run(self):
        report = self.report()
        batch = self.ctx.get("validation_batch")
        if not batch:
            batch = self.batch(self.ctx["calibrated_demand"], T.baseline_plan(),
                               self.ctx.get("seeds", DEFAULT_SEEDS), "stability")
            report.runs = len(batch)
        if len(batch) < 3:
            return report.fail("Too few runs to say anything about stability.")

        rows, worst_single = [], 0.0
        for movement in C.ALL_MOVEMENTS:
            series = batch.movement_values(movement)
            if len(series) < 3:
                continue
            summary = Stats.describe(series, label=movement)
            median = summary["median"]
            spans = [abs(v - median) / median * 100.0 for v in series] if median else [0.0]
            worst = max(spans)
            worst_single = max(worst_single, worst)
            rows.append({"movement": movement,
                         "n": summary["n"],
                         "median_s": median,
                         "ci95_median_s": summary["ci95_median"],
                         "min_max_s": summary["min_max"],
                         "stdev_s": summary["stdev"],
                         "coefficient_of_variation_pct": Stats.cv_pct(series),
                         "worst_single_run_error_pct": round(worst, 1),
                         "runs_for_5pct_band": runs_needed(series, 5.0)})

        report.data["rows"] = rows
        report.data["seeds_used"] = len(batch)
        report.say("Worst single run in this batch sits "
                   + format(worst_single, ".0f") + "% from the median of the "
                   "batch. That's what a one-run result would have been wrong "
                   "by, and it's why everything in this report is a median of "
                   "many.")

        needed = [r["runs_for_5pct_band"] for r in rows if r["runs_for_5pct_band"]]
        if needed:
            report.data["runs_recommended"] = max(needed)
            report.say("To hold the median inside a 5% band the widest movement "
                       "needs about " + str(max(needed)) + " runs.")
            if max(needed) > len(batch):
                report.warn("That's more than the " + str(len(batch)) + " runs "
                            "used here, so the medians above carry a wider band "
                            "than 5%.")
        report.headline = ("Worst single run " + format(worst_single, ".0f")
                           + "% off the median of " + str(len(batch)))
        return report


# ---- 6. which signal plan does the data prefer ---------------------------

class PhasePlanAgent(Agent):
    """Two phases or four is a fact about Bengaluru, not a modelling
    preference. Both shapes run at the same calibrated demand and the same
    seeds and the archive gets asked which one it recognises.

    The right turns carry it: permissive under two_phase, protected under
    four_phase. If the real signal protects them the two-phase model will be
    too slow on exactly those movements.
    """

    name = "phase-plan"
    title = "Which signal plan the data prefers"
    blurb = "Runs both signal shapes and asks the archive which one it matches."
    MAX_SEEDS = 12
    MIN_MARGIN_PCT = 2.0

    def run(self):
        report = self.report()
        seeds = min(self.ctx.get("seeds", DEFAULT_SEEDS), self.MAX_SEEDS)
        spec = self.ctx["calibrated_demand"]

        shapes = {}
        for shape in C.PHASE_PLANS:
            batch = self.batch(spec, T.baseline_plan(shape), seeds, "phase:" + shape)
            report.runs += len(batch)
            if batch:
                shapes[shape] = self._score(batch, shape)

        if len(shapes) < 2:
            report.warn("Could not run both plan shapes.")
            report.headline = "Could not run both plan shapes; no comparison."
            return report

        report.data["shapes"] = shapes
        report.data["seeds"] = seeds
        report.data["slot"] = self.slot

        # score both on the movements they could BOTH measure, otherwise
        # they're being judged on different sets
        common = set.intersection(*(set(v["errors"]) for v in shapes.values()))
        report.data["compared_movements"] = sorted(common)
        if not common:
            report.warn("No movement is comparable under both plans.")
            report.headline = ("No movement is comparable under both plans, so "
                               "the archive can't choose between them.")
            return report

        scored = {}
        for shape, data in shapes.items():
            errors = [abs(data["errors"][m]) for m in common]
            turns = [abs(data["errors"][m]) for m in common if m not in C.MOVEMENTS]
            scored[shape] = {
                "median_abs_error_pct": round(statistics.median(errors), 1),
                "median_abs_turn_error_pct": (round(statistics.median(turns), 1)
                                              if turns else None),
                "passes": data["passes"],
                "counted": data["counted"],
                "cycle_seconds": data["cycle_seconds"],
            }
        report.data["scored"] = scored

        best = min(scored, key=lambda k: scored[k]["median_abs_error_pct"])
        worst = [k for k in scored if k != best][0]
        margin = (scored[worst]["median_abs_error_pct"]
                  - scored[best]["median_abs_error_pct"])
        report.data["preferred"] = best
        report.data["margin_pct"] = round(margin, 1)
        report.data["active"] = C.ACTIVE_PHASE_PLAN

        if margin < self.MIN_MARGIN_PCT:
            report.warn("A margin this small is not evidence for either shape. "
                        "Both are kept and the model stays on "
                        + C.ACTIVE_PHASE_PLAN + ".")
            report.headline = ("The archive can't separate the two plans: "
                               + str(scored[best]["median_abs_error_pct"])
                               + "% against "
                               + str(scored[worst]["median_abs_error_pct"])
                               + "% median absolute error over "
                               + str(len(common)) + " movements.")
            return report

        report.headline = (C.PHASE_PLANS[best]["label"] + " matches the archive "
                           "better: " + str(scored[best]["median_abs_error_pct"])
                           + "% against "
                           + str(scored[worst]["median_abs_error_pct"])
                           + "% median absolute error over " + str(len(common))
                           + " movements, and " + str(scored[best]["passes"])
                           + " of " + str(scored[best]["counted"]) + " passing "
                           "against " + str(scored[worst]["passes"]) + " of "
                           + str(scored[worst]["counted"]) + ".")

        turn_best = scored[best]["median_abs_turn_error_pct"]
        turn_worst = scored[worst]["median_abs_turn_error_pct"]
        if turn_best is not None and turn_worst is not None:
            report.say("On the turning movements alone it's " + str(turn_best)
                       + "% against " + str(turn_worst) + "%. That's the "
                       "comparison that carries the result - right turns are "
                       "permissive under two_phase and protected under "
                       "four_phase, so they're where the plans differ most.")

        if best != C.ACTIVE_PHASE_PLAN:
            report.warn("The model is running " + C.ACTIVE_PHASE_PLAN
                        + " and the archive prefers " + best + ". Set "
                        "ABHYAS_PHASE_PLAN=" + best + " to switch. Nothing here "
                        "changes the model on its own.")

        report.say("Both plans use their own baseline timings, so this compares "
                   "phasing at " + str(scored[best]["cycle_seconds"]) + " s and "
                   + str(scored[worst]["cycle_seconds"]) + " s cycles rather "
                   "than one borrowed cycle length.")
        report.say("The real signal reportedly runs vehicle-actuated. Both "
                   "shapes here are fixed-time, so neither matches an adaptive "
                   "signal exactly and the residual error isn't all phasing.")
        return report

    def _score(self, batch, shape):
        errors, passes, counted = {}, 0, 0
        for movement in C.ALL_MOVEMENTS:
            target = self.target(movement)
            model = batch.describe(movement)
            measured, median = target.get("travel_time_s"), model.get("median")
            if not measured or not median:
                continue
            model_length = batch.corridor_m(movement) or None
            archive_length = target.get("length_m")
            if model_length and archive_length:
                if abs(model_length - archive_length) / archive_length > 0.20:
                    continue
            counted += 1
            errors[movement] = round((median - measured) / measured * 100, 1)
            if Stats.validation_verdict(model, target)["verdict"] == "pass":
                passes += 1
        return {"errors": errors, "passes": passes, "counted": counted,
                "cycle_seconds": T.cycle_seconds(T.baseline_plan(shape)),
                "label": C.PHASE_PLANS[shape]["label"]}


# ---- 7. sensitivity to the splits we can't measure -----------------------

class SensitivityAgent(Agent):
    name = "sensitivity"
    title = "Turning-split sensitivity"
    blurb = "Sweeps the turning splits and says which verdicts survive them."

    SPLITS = [
        ("50/30/20", {"through": 0.50, "left": 0.30, "right": 0.20}),
        ("55/25/20", {"through": 0.55, "left": 0.25, "right": 0.20}),
        ("70/20/10", {"through": 0.70, "left": 0.20, "right": 0.10}),
    ]

    def run(self):
        report = self.report()
        seeds = self.ctx.get("sensitivity_seeds", SENSITIVITY_SEEDS)
        base = self.ctx["calibrated_demand"]

        rows = []
        for label, splits in self.SPLITS:
            spec = base.copy(turning_splits=dict(splits))
            batch = self.batch(spec, T.baseline_plan(), seeds, "splits-" + label)
            report.runs += len(batch)
            entry = {"splits": label, "through_left_right": splits,
                     "seeds": len(batch), "movements": {}}
            for movement in C.ALL_MOVEMENTS:
                model = Stats.describe(batch.movement_values(movement),
                                       label=movement)
                verdict = Stats.validation_verdict(model, self.target(movement))
                entry["movements"][movement] = {
                    "model_median_s": model.get("median"),
                    "ci95_s": model.get("ci95_median"),
                    "verdict": verdict["verdict"],
                    "error_pct": verdict.get("error_pct")}
            rows.append(entry)

        report.data["rows"] = rows
        report.data["seeds_per_split"] = seeds

        stable = {m: len({row["movements"][m]["verdict"] for row in rows}) == 1
                  for m in C.ALL_MOVEMENTS}
        report.data["verdict_stable"] = stable
        unstable = [m for m, ok in stable.items() if not ok]

        report.say("Turning splits can't be worked out from travel time alone - "
                   "the problem is underdetermined and no open Indian dataset "
                   "gives turning counts at an identified junction. They're a "
                   "declared prior, swept here from 50/30/20 to 70/20/10 rather "
                   "than fitted.")
        if unstable:
            report.warn("For those movements the conclusion depends on "
                        "something we cannot measure, so it shouldn't be "
                        "presented as a result.")
            report.headline = "Verdict changes with the splits on: " + ", ".join(unstable)
        else:
            report.say("Every movement's verdict is the same at 50/30/20, "
                       "55/25/20 and 70/20/10, so it doesn't rest on the split "
                       "we can't measure.")
            report.headline = "Verdict holds across every split tested"
        return report


FLEET = [ArchiveAuditAgent, CalibrationAgent, MovementValidationAgent,
         AsymmetryAgent, SeedStabilityAgent, SensitivityAgent, PhasePlanAgent]

AGENTS_BY_NAME = {agent.name: agent for agent in FLEET}


class Fleet:
    """Dispatches the agents in order and assembles one report.

    Order matters - the later agents eat what the earlier ones establish, the
    calibrated dial above all. Inside an agent the SUMO runs go in parallel,
    which is where the time actually goes.
    """

    def __init__(self, slot=None, junction=None, seeds=DEFAULT_SEEDS, workers=4,
                 include_separate=False, only=None, duration_s=1800.0,
                 progress=None):
        self.seeds = seeds
        self.workers = workers
        self.include_separate = include_separate
        self.only = only
        self.duration_s = duration_s
        self.progress = progress or (lambda **kw: None)
        self.junction = junction or C.JUNCTION_KEY
        self.slot = slot
        self.archive = None
        self.ctx = {}

    def prepare(self):
        self.archive = A.load(include_separate=self.include_separate)
        if self.slot is None:
            candidates = self.archive.hour_slots(self.junction, "NS")
            self.slot = candidates[-1] if candidates else "weekday 09:00-10:00"
        self.ctx = {"archive": self.archive,
                    "junction": self.junction,
                    "slot": self.slot,
                    "seeds": self.seeds,
                    "demand": D.DemandSpec(duration_s=self.duration_s),
                    "calibrated_demand": D.DemandSpec(duration_s=self.duration_s),
                    "duration_s": self.duration_s}
        return self.ctx

    def selected(self):
        return [a for a in FLEET if not self.only or a.name in self.only]

    def run_agent(self, agent_class, pool):
        """One agent, plus whatever it hands to the ones after it."""
        report = agent_class(self.ctx, pool, progress=self.progress).execute()
        if agent_class is CalibrationAgent:
            dial = report.data.get("calibrated_veh_per_hour")
            if dial:
                self.ctx["calibrated_demand"] = self.ctx["demand"].copy(
                    veh_per_hour=dial)
        if agent_class is MovementValidationAgent:
            self.ctx["validation_rows"] = report.data.get("rows")
        return report

    def run(self):
        started = time.time()
        self.prepare()
        pool = Pool(workers=self.workers, progress=self.progress)
        reports = [self.run_agent(cls, pool) for cls in self.selected()]

        payload = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "junction": self.junction,
            "junction_name": C.JUNCTION_NAME,
            "slot": self.slot,
            "seeds": self.seeds,
            "include_separate_batch": self.include_separate,
            "seconds": round(time.time() - started, 1),
            "total_runs": sum(r.runs for r in reports),
            "agents": [r.to_dict() for r in reports],
            "summary": self.summarise(reports),
        }
        out = C.RESULTS / "validation.json"
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        payload["saved_to"] = str(out)
        return payload

    def summarise(self, reports):
        by_name = {r.name: r for r in reports}
        movement = by_name.get("movement")
        rows = (movement.data.get("rows") if movement else None) or []
        counted = [r for r in rows
                   if r.get("comparable") and r.get("verdict") != "no data"]
        passed = [r for r in counted if r["verdict"] == "pass"]
        excluded = [r for r in rows if not r.get("comparable")]

        if not counted:
            overall = "not validated"
            statement = ("No movement in slot " + self.slot + " could be "
                         "compared like for like, so the model is unvalidated "
                         "here.")
        elif len(passed) == len(counted):
            overall = "pass"
            statement = ("All " + str(len(counted)) + " comparable movements "
                         "land inside tolerance or inside the measurement's own "
                         "spread.")
        elif len(passed) >= 2:
            overall = "partial"
            statement = (str(len(passed)) + " of " + str(len(counted))
                         + " comparable movements pass; the rest are reported "
                         "with their errors rather than explained away.")
        else:
            overall = "fail"
            statement = ("The model fails its own test in slot " + self.slot
                         + ": only " + str(len(passed)) + " of "
                         + str(len(counted)) + " comparable movements land "
                         "inside tolerance.")

        return {
            "overall": overall,
            "statement": statement,
            "pass_count": len(passed),
            "counted": len(counted),
            "excluded": [{"movement": r["movement"], "note": r.get("note")}
                         for r in excluded],
            "calibrated_veh_per_hour": self.ctx["calibrated_demand"].veh_per_hour,
            "limitation": ("Validated on travel time only. Travel time says "
                           "nothing about whether the turning proportions are "
                           "right and we can't measure those. The sensitivity "
                           "agent shows which conclusions survive that gap."),
            "agent_status": {r.name: r.status for r in reports},
        }


def run_fleet(slot=None, junction=None, seeds=DEFAULT_SEEDS, workers=4,
              include_separate=False, only=None, duration_s=1800.0,
              progress=None):
    return Fleet(slot=slot, junction=junction, seeds=seeds, workers=workers,
                 include_separate=include_separate, only=only,
                 duration_s=duration_s, progress=progress).run()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Abhyas validation fleet.")
    parser.add_argument("--slot", default=None)
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--duration", type=float, default=1800.0)
    parser.add_argument("--include-separate", action="store_true")
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    def show(**kw):
        if kw.get("kind") == "agent":
            print("[" + kw.get("state", "") + "] " + kw.get("agent", "")
                  + (" - " + kw["headline"] if kw.get("headline") else ""))
        elif kw.get("kind") == "calibration":
            print("    dial " + str(kw["veh_per_hour"]) + " veh/h -> "
                  + str(kw["model_s"]) + " s (target " + str(kw["target_s"]) + " s)")

    result = run_fleet(slot=args.slot, seeds=args.seeds, workers=args.workers,
                       include_separate=args.include_separate, only=args.only,
                       duration_s=args.duration, progress=show)
    print(json.dumps(result["summary"], indent=2))
    print("saved to", result["saved_to"])
