# How the model is calibrated and validated

What was fitted, what was not, which algorithm ran, and where the code is.

Figures throughout come from the fleet run of 2026-09-02: 40 seeds, 208 SUMO
runs, slot `weekend 21:00-22:00`, written to `results/validation.json`.

---

## 1. The short answer

**Exactly one parameter was ever free**, and it was fitted against only 4 of the
12 measured corridors. The other 8 are a holdout.

Everything else — vehicle lengths and widths, acceleration, deceleration, gap
acceptance, lane-change aggression, fleet composition — is frozen from published
sources (Indo-HCM 2017 Ch.4; IRC:106-1990) and never touched.

That restraint is deliberate, and the reason is a methodological one rather than
a matter of effort. Fitting several behavioural parameters at once against a
single aggregate measurement is **underdetermined**: many different combinations
reproduce the same travel time equally well, so choosing one means choosing
arbitrarily and then presenting the arbitrary choice as a finding. One free
parameter against one objective is a fit. Six free parameters against one
objective is a story.

| | |
|---|---|
| Free parameters | 1 — `demand.veh_per_hour` |
| Fitted against | 4 through movements (NS, SN, EW, WE) |
| Held out | 8 turning movements (NE, NW, SE, SW, EN, ES, WN, WS) |
| Result | **1,600 veh/h**, residual **−0.9%** |
| Cost | 10 demand levels × 12 seeds = **120 SUMO runs** |

---

## 2. The algorithm: grid search with one local refinement

Not gradient descent, not an optimiser, not anything Bayesian. A sweep.

**Objective** — minimise the absolute difference between two medians:

```
| median(model,   NS SN EW WE)  −  median(archive, NS SN EW WE) |
```

The objective is the median *across* movements, not any one of them, because the
dial is a single number and cannot satisfy four movements separately. For this
slot the archive's cross-movement median is **108.2 s**.

**Procedure** (`abhyas/agents.py:301-395`):

1. Build a linear grid of **8 points from 600 to 3,400 veh/h** (spacing 400).
2. At each point run **12 seeds**. Take the median per movement, then the median
   across movements.
3. Select the grid point with the smallest absolute error → **1,800 veh/h**, −1.2 s.
4. **Refine**: probe ±½ grid spacing around the winner → 1,600 and 2,000 veh/h.
5. Re-select over the enlarged curve → **1,600 veh/h**, −1.0 s.

Ten levels in total, which is where the "120 runs" comes from.

### Why a sweep and not a bisection

Because the response is not monotone, and the measured curve shows it:

| veh/h | model median | error vs 108.2 s |
|------:|-------------:|-----------------:|
|   600 |      95.3 s  |  −12.9 |
| 1,000 |     100.7 s  |   −7.6 |
| 1,400 |     103.7 s  |   −4.6 |
| **1,600** | **107.2 s** | **−1.0** ← selected |
| 1,800 |     107.1 s  |   −1.2 |
| 2,000 |     120.2 s  |  +12.0 |
| 2,200 |     110.2 s  |   **+1.9** ← *lower than 2,000* |
| 2,600 |     164.1 s  |  +55.8 |
| 3,000 |     172.3 s  |  +64.1 |
| 3,400 |     283.7 s  | +175.4 |

Between 2,000 and 2,200 the modelled travel time falls as demand rises. Near
capacity a few percent more traffic can tip an approach from flowing to queueing
and back, and the batch-to-batch response stops being monotone. A bisection —
or any method that assumes a monotone response — walks into that reversal and
reports whichever side it happened to land on.

The sweep costs more runs and gives back the whole response curve, which is also
the honest way to show how sharply the answer is determined.

### The conditioning check

`_steepness` (`agents.py:432-448`) measures the local slope: percent change in
modelled travel time per 10% change in demand, taken as the median over grid
neighbours within ±25% of the operating point.

**Measured: 2.6% per 10% demand.**

Above 25% the agent raises a warning that the operating point sits on the
capacity cliff and the calibrated volume is not pinned down. At 2.6% the fit sits
on a flat, well-conditioned stretch of the curve. This number is worth quoting:
it is the difference between "1,600 veh/h" being a real answer and being an
artefact of where the grid happened to land.

---

## 3. The holdout

The dial is fitted on the four through movements **only**. The reason is in
`agents.py:307-311`: the turning corridors respond to demand through the
*turning splits*, which are a declared prior rather than a measurement, so
fitting the dial against them would be fitting one guess to another.

The eight turn corridors are then validated against a dial they did not set —
a genuine out-of-sample check rather than a restatement of the fit.

**Result: 7 of 8 turns pass, 3 of 4 through movements pass; 10 of 12 overall.**

The two misses are reported, not explained away:

| Movement | Model | Archive | Error | Note |
|---|---:|---:|---:|---|
| WE | 113.3 s | 96.0 s | +18.1% | Verdict does not survive the split sweep |
| EN | 105.8 s | 140.0 s | −24.5% | Verdict **does** survive the sweep — a real miss |

Pass/fail is `±15%`, set once in `abhyas/stats.py:141`.

---

## 4. Where the randomness lives, and how it is handled

The stochastic element is the **SUMO seed** — vehicle insertion times, driver
imperfection (`sigma`), lateral placement within a lane. It is large: before the
September fixes, a single run could land 263% from the median of its own batch.

Three mechanisms keep that from reaching a conclusion:

- **Medians over many seeds.** Every reported figure is a median of 40 runs, never
  a single run. A one-run result is not a result on this model.
- **Paired seeds.** A comparison between two scenarios uses the *same* seed set for
  both, so what is measured is the change and not the randomness
  (`stats.paired_difference`, `abhyas/stats.py:74`).
- **Bootstrap bands.** Every number carries a 4,000-resample 95% band
  (`stats.bootstrap_ci`, `abhyas/stats.py:54`). `BOOTSTRAP_RESAMPLES` is at
  `stats.py:18`.

A verdict of **"cannot resolve"** is a legitimate output of this machinery, not
a failure to produce one.

---

## 5. What the model is measured against

The comparison is not end-to-end travel time. TomTom measures between two fixed
points roughly 200 m either side of the junction; the model's arms are longer
than that, so timing a vehicle from network entry to network exit would compare a
950 m run against a 406 m measurement and read the difference as model error.

`Model.measure_corridors` (`abhyas/sim.py:122`) projects each archive endpoint
onto the movement's route, converts it to a distance along that route, and times
each vehicle between those two odometer readings — the same stretch of road the
archive times.

Free-flow time over the same window comes from the network's own speed limits, so
it does not move when the demand dial does.

---

## 6. Robustness: what happens if the prior is wrong

The turning splits cannot be recovered from travel time alone — the problem is
underdetermined, and no open Indian dataset gives turning counts at an identified
junction. They are declared at 55/25/20 and **swept, not fitted**.

`SensitivityAgent` (`abhyas/agents.py:989`) re-runs the comparison across the
range 50/30/20 → 70/20/10 and reports which verdicts survive.

**Result: 9 of 12 hold. 3 do not — WE, NW, WN.**

Those three are reported as conclusions that depend on a quantity we cannot
measure, and are therefore not presented as results. Stating this unprompted is
considerably stronger than being caught by it.

---

## 7. The seven agents

Each can return `warning` instead of `pass`; a warning is a finding, not a failure.

| Agent | Question | Runs | Status |
|---|---|---:|---|
| `archive-audit` | Is the ground truth usable, and what was discarded? | 0 | ok |
| `calibration` | What demand matches the archive? | 120 | ok |
| `movement` | Does each corridor land within 15%? | 40 | warning |
| `asymmetry` | Does the model reproduce measured directional imbalance? | 0 | warning |
| `seed-stability` | How wrong would one run have been? | 0 | warning |
| `sensitivity` | Which verdicts survive the unmeasurable prior? | 24 | warning |
| `phase-plan` | Which signal shape does the archive prefer? | 24 | ok |

Agents showing 0 runs re-read the batch the movement agent already produced
rather than paying for their own.

---

## 7a. Scenario levers, and why they are not parameters

Section 1 rests on there being exactly one fitted number. The fleet and access
scenarios added for policy questions introduce several more dials, so the line
between the two has to be written down rather than remembered.

**Nothing below is fitted. Nothing below is validated. Every one of them is a
declared prior of the same kind as `DEFAULT_SPLITS`: it is swept, the verdict
is checked for whether it survives the sweep, and the result is reported as a
comparison against baseline and never as a level.** The control surface marks
them `exploratory: true`, the UI badges them, every run that moves one carries
a warning saying so, and `counterfactual.py` refuses to present the card
without that note.

| Lever | Where | Status |
|---|---|---|
| `hcv` vehicle class | `demand.py` — `VEHICLE_CLASSES["hcv"]` | Dimensions and gap acceptance are Indo-HCM 2017 Ch.4, same standard as the other four. **Its share is not.** |
| `hcv` share in the default mix | `demand.py` — `VEHICLE_CLASSES["hcv"]["share"]` | **Declared, not counted.** It was taken out of the car and bus shares, so every calibrated number in this document predates a mix that contains trucks and the baseline needs a re-run against one. Sweep it with `fleet.hcv_share`. |
| `hmv_discipline` | `demand.py` — `discipline_overrides()` | New behavioural ground, not a citation. Moves `lcPushy`, `lcAssertive`, `jmTimegapMinor` and `impatience` on the heavy classes between two declared endpoints, linearly, because nothing justifies another shape. Dimensions are untouched. |
| `hmv_stop_rate` | `sim.py` — `Collector._maybe_stop()` | Declared rate. The realised rate is lower wherever no stretch of the vehicle's route is long enough to stop in, and the run reports both. |
| Occupancy per class | `demand.py` — `occupancy()` | Declared prior. It is what turns a vehicle count into a person count, and it is also the reason a person-throughput figure must not be quoted to three significant figures. |
| `injected` fleet | `demand.py` — `DemandSpec.class_rates()` | Not a prior, an input: it says what N more vehicles per hour do to **this junction**. It is not a claim about a city-wide scheme, and one junction cannot be made into one. |
| `mode_shift` | `demand.py` — `DemandSpec.class_rates()` | Input plus the occupancy prior above. Removes car and two-wheeler trips and adds the bus trips that carry those people. Autos are left alone. |
| Access restrictions | `demand.py` / `sim.py` — `apply_access_restrictions()` | An input, not a prior. Banned demand is withheld and **counted**, never re-routed: one junction has no other arm for it to arrive on. |

Two guards keep this honest rather than aspirational. `selftest.py` asserts that
a ban actually withholds the demand it names and leaves the other arms alone,
and that moving a fleet lever hard actually moves the output — the same guard
the signal plan has had since the beginning, for the same reason.

What none of this buys: **emissions**, still not modelled and still refused by
name in `nlu.OUT_OF_SCOPE`, which matters because an electric-bus scheme is at
bottom an emissions argument; and **network effects**, which need the wider
network routed and its own ground truth per junction, i.e. a second run of
everything in this document rather than an extension of it.

---

## 8. File map

| What | Where |
|---|---|
| Calibration algorithm | `abhyas/agents.py:297` — `CalibrationAgent`; `_probe` runs one level, `_best` selects, `_steepness` checks conditioning |
| Seeds per level | `abhyas/agents.py:43` — `CALIBRATION_SEEDS = 12` |
| Frozen vehicle and driver parameters | `abhyas/demand.py:34` — `VEHICLE_CLASSES`, each with its citation |
| Declared priors (never fitted) | `abhyas/demand.py` — `DEFAULT_SPLITS`, `DEFAULT_ARM_SHARE` |
| Scenario levers (exploratory, see 7a) | `abhyas/demand.py` — `DemandSpec.LEVERS`; `abhyas/controls.py` — `EXPLORATORY_GROUPS` |
| Sublane resolution (two-wheeler filtering) | `abhyas/demand.py:104` — `LATERAL_RESOLUTION = 0.30` |
| Ground-truth loader | `abhyas/archive.py:181` — `Archive.target()` |
| One simulation run | `abhyas/sim.py:419` — `run_once()`; `:239` — `Collector`; `:416` — `WARMUP_S` |
| Corridor gating | `abhyas/sim.py:122` — `Model.measure_corridors()` |
| Tolerance and verdicts | `abhyas/stats.py:141` — `TOLERANCE_PCT`; `:144` — `validation_verdict()` |
| Paired comparison / bootstrap | `abhyas/stats.py:74` and `:54` |
| Out-of-sample check | `abhyas/agents.py:462` — `MovementValidationAgent` |
| Prior robustness | `abhyas/agents.py:989` — `SensitivityAgent` |
| Fleet orchestration | `abhyas/agents.py:1073` — `run_fleet()` |
| Output | `results/validation.json` |

Reproduce with:

```
python -m abhyas.agents --seeds 40 --workers 6
```

---

## 9. Anticipated objections

**"Isn't fitting to the data you validate against circular?"**
It would be if the fit and the check used the same corridors. They do not: the
dial is set on 4 through movements and checked on 8 turns it never saw.

**"One parameter seems too few."**
It is the most that one aggregate measurement supports. Adding parameters would
improve the fit and reduce what the fit means, because the extra freedom is not
constrained by anything measured.

**"How do you know 1,600 isn't just where the grid landed?"**
The refinement step probes between grid points, and the conditioning check reports
2.6% travel-time change per 10% demand — a flat region, not a knife edge.

**"The model doesn't reproduce the directional asymmetry."**
Correct, and reported. The archive shows 100 Feet Road 1.12× slower northbound and
CMH 1.18× eastbound; the model returns 1.07 and 1.00. Its arm shares are declared
symmetric, so it has no mechanism to produce that imbalance — and fitting them to
it would make the asymmetry agent a test of nothing.

**"Both signal plans are fixed-time; the real one is adaptive."**
Also correct and stated in the phase-plan findings. Neither shape can match an
actuated signal exactly, so the residual error is not all phasing.
