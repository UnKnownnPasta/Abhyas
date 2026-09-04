# All the number crunching in one place so we stop writing quantile() a fourth
# time. Everything comes back with a range attached - a bare median out of a
# random simulation isn't a result.

import random
import statistics

BOOTSTRAP_RESAMPLES = 4000
TOLERANCE_PCT = 15.0


class Stats:
    """Bag of small statistics helpers. All static, just import and call."""

    @staticmethod
    def quantile(values, q):
        ordered = sorted(v for v in values if v is not None)
        if not ordered:
            return 0.0
        if len(ordered) == 1:
            return float(ordered[0])
        pos = q * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)

    @staticmethod
    def clean(values):
        return [v for v in values if v is not None]

    @staticmethod
    def bootstrap_ci(values, estimator=statistics.median, level=0.95,
                     resamples=BOOTSTRAP_RESAMPLES, seed=12345):
        """Percentile bootstrap band. Fixed seed so reruns agree."""
        clean = Stats.clean(values)
        if len(clean) < 2:
            point = clean[0] if clean else 0.0
            return (point, point)
        rng = random.Random(seed)     # fixed on purpose, this works fine
        n = len(clean)
        draws = [estimator([clean[rng.randrange(n)] for _ in range(n)])
                 for _ in range(resamples)]
        alpha = (1.0 - level) / 2.0
        return (Stats.quantile(draws, alpha), Stats.quantile(draws, 1.0 - alpha))

    @staticmethod
    def describe(values, label=""):
        """Median plus spread plus a bootstrap band on the median itself."""
        clean = Stats.clean(values)
        if not clean:
            return {"label": label, "n": 0, "median": None, "usable": False}
        median = statistics.median(clean)
        if len(clean) > 2:
            band = Stats.bootstrap_ci(clean)
        else:
            band = (median, median)
        return {
            "label": label,
            "n": len(clean),
            "median": round(median, 2),
            "mean": round(statistics.fmean(clean), 2),
            "stdev": round(statistics.stdev(clean), 2) if len(clean) > 1 else 0.0,
            "iqr": [round(Stats.quantile(clean, 0.25), 2),
                    round(Stats.quantile(clean, 0.75), 2)],
            "p10_p90": [round(Stats.quantile(clean, 0.10), 2),
                        round(Stats.quantile(clean, 0.90), 2)],
            "min_max": [round(min(clean), 2), round(max(clean), 2)],
            "ci95_median": [round(band[0], 2), round(band[1], 2)],
            "usable": True,
        }

    @staticmethod
    def cv_pct(values):
        """Coefficient of variation as a percentage, or None."""
        clean = Stats.clean(values)
        if len(clean) < 2:
            return None
        median = statistics.median(clean)
        if not median:
            return None
        return round(statistics.stdev(clean) / median * 100.0, 1)

    # -- comparing two scenarios -------------------------------------------

    @staticmethod
    def paired_difference(baseline, scenario, higher_is_worse=True,
                          resamples=BOOTSTRAP_RESAMPLES):
        """Seed by seed comparison of two dicts of seed -> value.

        Only seeds present on both sides count. Comparing different seeds
        measures the randomness instead of the change, which is the whole
        thing we're trying not to do.
        """
        shared = sorted(set(baseline) & set(scenario))
        missing = sorted(set(baseline) ^ set(scenario))
        diffs, rel = [], []
        for seed in shared:
            base, scen = baseline[seed], scenario[seed]
            if base is None or scen is None:
                continue
            diffs.append(scen - base)
            if base:
                rel.append((scen - base) / base * 100.0)

        if len(diffs) < 2:
            return {"n_pairs": len(diffs), "seeds_missing": missing,
                    "verdict": "cannot resolve",
                    "reason": "fewer than two usable paired seeds"}

        abs_ci = Stats.bootstrap_ci(diffs, resamples=resamples)
        rel_ci = Stats.bootstrap_ci(rel, resamples=resamples) if rel else (0.0, 0.0)
        median_rel = statistics.median(rel) if rel else 0.0

        if abs_ci[0] > 0 and abs_ci[1] > 0:
            direction = "worsens" if higher_is_worse else "improves"
        elif abs_ci[0] < 0 and abs_ci[1] < 0:
            direction = "improves" if higher_is_worse else "worsens"
        else:
            direction = "cannot resolve"

        return {
            "n_pairs": len(diffs),
            "seeds_missing": missing,
            "median_change_abs": round(statistics.median(diffs), 2),
            "ci95_change_abs": [round(abs_ci[0], 2), round(abs_ci[1], 2)],
            "median_change_pct": round(median_rel, 1),
            "ci95_change_pct": [round(rel_ci[0], 1), round(rel_ci[1], 1)],
            "verdict": direction,
            "statement": Stats._statement(direction, rel_ci, median_rel),
        }

    @staticmethod
    def _statement(direction, rel_ci, median_rel):
        if direction == "cannot resolve":
            return ("Cannot resolve at this number of seeds: the change spans "
                    + format(rel_ci[0], "+.1f") + "% to "
                    + format(rel_ci[1], "+.1f") + "%, which includes no change.")
        low, high = sorted((abs(rel_ci[0]), abs(rel_ci[1])))
        verb = "Reduces" if direction == "improves" else "Increases"
        return (verb + " the modelled quantity by " + format(low, ".0f") + "-"
                + format(high, ".0f") + "% relative to baseline (median "
                + format(abs(median_rel), ".0f") + "%).")

    # -- did the model match the measurement -------------------------------

    @staticmethod
    def validation_verdict(model, target, tolerance_pct=TOLERANCE_PCT,
                           comparable=True, note=""):
        """Two ways to pass: inside the tolerance band, or inside the spread of
        the measurement itself. The second one is the stronger claim."""
        if not model.get("usable") or not target.get("n"):
            return {"verdict": "no data",
                    "reason": "no usable runs or no observations",
                    "comparable": comparable, "note": note}

        modelled = model["median"]
        measured = target["travel_time_s"]
        spread = target.get("spread_p10_p90_s") or [measured, measured]
        error_pct = (modelled - measured) / measured * 100.0 if measured else 0.0
        inside_spread = spread[0] <= modelled <= spread[1]
        inside_tolerance = abs(error_pct) <= tolerance_pct

        if not comparable:
            verdict = "not comparable"
        elif inside_spread or inside_tolerance:
            verdict = "pass"
        else:
            verdict = "fail"

        return {
            "verdict": verdict,
            "model_median_s": modelled,
            "model_ci95_s": model["ci95_median"],
            "model_runs": model["n"],
            "measured_s": measured,
            "measured_n": target["n"],
            "measured_spread_p10_p90_s": spread,
            "error_pct": round(error_pct, 1),
            "inside_measurement_spread": inside_spread,
            "inside_tolerance": inside_tolerance,
            "tolerance_pct": tolerance_pct,
            "comparable": comparable,
            "note": note,
        }
