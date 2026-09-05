# Prove the pipeline is actually wired up before trusting a number out of it.
#
# The failure this guards against nearly got us: a badly set up environment
# makes SUMO ignore a config file and run a perfectly normal looking simulation
# using none of your settings. Stable, reproducible, and wrong.
#
# Which is why the last check here changes one parameter drastically and
# asserts the output actually moved. If halving the green doesn't change the
# queue then the settings aren't reaching the simulator and every other number
# on the page is decoration.

import subprocess
import sys

from . import config as C


class Failure(Exception):
    pass


class SelfTest:
    """Registry of checks plus the runner. Add one with @SelfTest.check(...)."""

    CHECKS = []

    @classmethod
    def check(cls, title):
        def wrap(fn):
            cls.CHECKS.append((title, fn))
            return fn
        return wrap

    @staticmethod
    def require(condition, message):
        if not condition:
            raise Failure(message)

    @classmethod
    def run(cls):
        print("\nSelf-test\n" + "-" * 66)
        failures = 0
        for title, fn in cls.CHECKS:
            try:
                detail = fn()
                print("  PASS  " + title)
                if detail:
                    print("        " + str(detail))
            except Failure as exc:
                failures += 1
                print("  FAIL  " + title)
                print("        " + str(exc))
            except Exception as exc:
                failures += 1
                print("  ERROR " + title)
                print("        " + type(exc).__name__ + ": " + str(exc))
        print("-" * 66)
        if failures:
            print(str(failures) + " of " + str(len(cls.CHECKS)) + " checks failed.")
        else:
            print("All " + str(len(cls.CHECKS)) + " checks passed.")
        return 1 if failures else 0


check = SelfTest.check
require = SelfTest.require


@check("SUMO binaries and python tools come from the same installation")
def _sumo_pinned():
    require(C.SUMO_BIN.exists(), "sumo binary not found at " + str(C.SUMO_BIN))
    version = subprocess.run([str(C.SUMO_BIN), "--version"], capture_output=True,
                             text=True).stdout.splitlines()[0]
    import sumolib
    require(str(C.SUMO_HOME) in sumolib.__file__,
            "sumolib comes from " + sumolib.__file__ + ", which isn't the "
            "installation the binaries come from (" + str(C.SUMO_HOME) + "). "
            "Mixed installs are how a simulation ends up quietly ignoring your "
            "configuration.")
    return version


@check("The junction is one node with four arms and a traffic light")
def _junction_shape():
    import sumolib
    net = sumolib.net.readNet(str(C.JUNCTION_NET))
    node = net.getNode(C.JUNCTION_ID)
    require(node.getType() == "traffic_light",
            "junction type is " + node.getType() + ", not traffic_light")
    require(len(node.getIncoming()) == 4,
            str(len(node.getIncoming())) + " incoming edges, expected 4")
    lights = [t.getID() for t in net.getTrafficLights()]
    require(lights == [C.JUNCTION_ID],
            "expected exactly one traffic light, found " + str(lights))
    return "one node, 4 arms, " + str(len(net.getEdges())) + " edges"


@check("The network is built for left-hand traffic")
def _lefthand():
    text = C.JUNCTION_NET.read_text(encoding="utf-8")[:2000]
    require('lefthand="true"' in text,
            "the network isn't marked lefthand. India drives on the left and "
            "netconvert reports nothing when it's missing - every turn would be "
            "mirrored and the model would silently be a different junction.")
    return "lefthand=true"


@check("The joined node has a real coordinate")
def _junction_coord():
    import sumolib
    net = sumolib.net.readNet(str(C.JUNCTION_NET))
    x, y = net.getNode(C.JUNCTION_ID).getCoord()
    require(abs(y) < 1e6 and abs(x) < 1e6,
            "junction sits at " + str((x, y)) + ", which is the invalid sentinel "
            "netconvert leaves on a joined node.")
    return "at " + format(x, ".1f") + ", " + format(y, ".1f")


@check("Traffic-light link indices are looked up, not hardcoded")
def _tls_links():
    from . import tls as T
    links = T.build_link_map()
    arms = {link.from_arm for link in links}
    require(arms >= set("NESW"),
            "link map covers arms " + str(sorted(arms)) + ", expected all four")
    phases = T.build_program(T.baseline_plan(), links)
    expected = 3 * len(C.phase_stages())      # green, yellow, all-red per stage
    require(len(phases) == expected,
            str(len(phases)) + " phases, expected " + str(expected) + " for the "
            + C.ACTIVE_PHASE_PLAN + " shape")
    for phase in phases:
        require(len(phase.state) == len(links),
                "phase state is " + str(len(phase.state)) + " characters for "
                + str(len(links)) + " links")
    return str(len(links)) + " links, " + str(len(phases)) + " phases"


@check("Every movement the archive measures can be routed in the model")
def _routes():
    from . import demand as D
    info = D.prepare(D.DemandSpec())
    require(not info["skipped"], "unroutable movements: " + str(info["skipped"]))
    require(info["routes"] == 12,
            str(info["routes"]) + " routes built, expected 12")
    return str(info["routes"]) + " routes, " + str(info["flows"]) + " flows"


@check("An access restriction withholds the demand it bans")
def _access_restriction():
    """A ban that quietly kept releasing the banned class would look exactly
    like a ban that worked, right up until someone read the numbers."""
    from . import demand as D
    spec = D.DemandSpec(access_restrictions={"E": ["twowheeler"]})
    info = D.prepare(spec)
    text = C.ROUTES_FILE.read_text(encoding="utf-8")

    require('type="twowheeler"' in text,
            "no two-wheeler flows at all - the ban can't be tested against a "
            "route file that never had any")
    banned = [line for line in text.splitlines()
              if 'route="r_E_' in line and 'type="twowheeler"' in line]
    require(not banned, str(len(banned)) + " two-wheeler flow(s) still enter on "
                        "the east approach after being banned from it")
    survivors = [line for line in text.splitlines()
                 if 'route="r_N_' in line and 'type="twowheeler"' in line]
    require(survivors, "banning two-wheelers on E also removed them from N, "
                       "which is a road closure and not an access restriction")
    withheld = info["turned_away_veh_per_hour"]
    require(withheld, "the ban withheld demand but reported none of it. Demand "
                      "that vanishes without being counted is the failure mode "
                      "this whole model is built to avoid.")

    # and it has to be reversible - a ban that leaks into the next spec would
    # quietly poison every run after it
    clean = D.prepare(D.DemandSpec())
    require(not clean["turned_away_veh_per_hour"],
            "a later unrestricted run still turned demand away")
    return ("east two-wheelers withheld ("
            + ", ".join(k + " " + str(v) for k, v in sorted(withheld.items()))
            + " veh/h), other arms untouched")


@check("The fleet levers reach the simulator")
def _fleet_levers():
    """Same guard as the signal check below: move a lever hard and assert the
    output actually moved. A dial that changes nothing is worse than no dial,
    because it hands you a confident number for a scenario that never ran."""
    from . import demand as D
    from . import sim

    plain = D.DemandSpec(veh_per_hour=1600, duration_s=600)
    base = sim.run_once(plain, seed=4)
    require(base.overall["person_throughput_per_hour"] > 0,
            "nobody was moved at all - person throughput is not being counted")

    # every heavy vehicle stops somewhere on its route
    stopping = plain.copy(hmv_stop_rate=1.0)
    stopped = sim.run_once(stopping, seed=4)
    require(stopped.overall["hmv_self_stops"] > 0,
            "at hmv_stop_rate=1.0 not one heavy vehicle stopped. The roll is "
            "happening but the setStop isn't landing.")
    require(base.overall["hmv_self_stops"] == 0,
            "heavy vehicles stopped unscheduled at rate 0, so the baseline is "
            "not the baseline")

    # a bus carries about thirty times what a car does, so injecting buses has
    # to move people much further than it moves vehicles
    injected = plain.copy(injected={"bus": 300.0})
    with_buses = sim.run_once(injected, seed=4)
    veh_gain = (with_buses.overall["throughput_veh_per_hour"]
                / max(base.overall["throughput_veh_per_hour"], 1.0))
    people_gain = (with_buses.overall["person_throughput_per_hour"]
                   / max(base.overall["person_throughput_per_hour"], 1.0))
    require(people_gain > veh_gain,
            "300 more buses an hour moved people by x" + format(people_gain, ".2f")
            + " and vehicles by x" + format(veh_gain, ".2f")
            + ". Occupancy isn't reaching the throughput figure, which is the "
              "one number the whole transit argument rests on.")
    return (str(stopped.overall["hmv_self_stops"]) + " self-triggered stops; "
            "injecting buses moved people x" + format(people_gain, ".2f")
            + " against vehicles x" + format(veh_gain, ".2f"))


@check("The archive loads and yields a target with a spread")
def _archive():
    from . import archive as A
    arc = A.load()
    coverage = arc.coverage(C.JUNCTION_KEY)
    require(coverage.get("observations", 0) > 0, "no observations loaded")
    slots = arc.hour_slots(C.JUNCTION_KEY, "NS")
    require(slots, "no hour slot has enough observations to quote a spread")
    target = arc.target(C.JUNCTION_KEY, "NS", hour_slot=slots[-1])
    require(target["n"] >= 3, "target has only " + str(target["n"]) + " observations")
    require(target["spread_p10_p90_s"][1] >= target["spread_p10_p90_s"][0],
            "target spread is inverted")
    return (str(coverage["observations"]) + " observations, " + str(len(slots))
            + " usable slots")


@check("The language layer refuses what it doesn't know")
def _nlu():
    from . import nlu
    good = nlu.parse("add 10 seconds to the northbound green")
    require(good.ok, "a valid instruction was rejected")
    require(good.action == "adjust_green", "wrong action: " + good.action)

    # which stage carries north depends on the plan shape, so ask the config
    # rather than naming a stage
    groups = C.phase_groups()
    group = good.params["phase_group"]
    require(group in groups, "resolved to an unknown stage: " + str(group))
    require("N" in groups[group]["arms"],
            "the northbound green resolved to " + group + ", which doesn't "
            "release the north approach")
    shared = len(groups[group]["arms"]) > 1
    require(bool(good.corrections) == shared,
            "the north approach " + ("shares" if shared else "does not share")
            + " its green under " + C.ACTIVE_PHASE_PLAN + ", and the parser "
            + ("said nothing" if shared else "said otherwise"))

    bad = nlu.parse("open the flyover and make it rain")
    require(not bad.ok, "an out-of-scope request was accepted")
    return ("accepts the known, rejects the rest, north green resolves to "
            + group + (" and says it's shared" if shared else ""))


@check("A dictated sentence survives the trip in one piece")
def _transcript_assembly():
    """Deepgram hands back a sentence in pieces and the pieces have to be glued.

    Reading one on its own gave the parser the tail of the sentence, and a
    stream closed mid-sentence gave it nothing at all, which is what "the mic
    does nothing when I stop talking" turned out to be.
    """
    from .voice import TranscriptAssembler

    def chunk(text, is_final=False, speech_final=False):
        return {"type": "Results",
                "channel": {"alternatives": [{"transcript": text}]},
                "is_final": is_final, "speech_final": speech_final}

    # vad_events=true puts these in the stream and their channel is a LIST,
    # not an object. Feeding one to .get() killed the whole pump task.
    speech_started = {"type": "SpeechStarted", "channel": [0, 1], "timestamp": 1.2}
    utterance_end = {"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": 3.4}

    def finals(events, flush=True):
        assembler = TranscriptAssembler()
        out = []
        for event in events:
            out.extend(assembler.feed(event))
        if flush:
            out.extend(assembler.flush())
        return [text for kind, text in out if kind == "final"]

    whole = finals([speech_started,
                    chunk("add ten"),
                    chunk("add ten seconds", is_final=True),
                    chunk("to the north", is_final=True),
                    chunk("approach green", is_final=True, speech_final=True)])
    require(whole == ["add ten seconds to the north approach green"],
            "a sentence finalised in pieces came back as " + str(whole))

    stopped = finals([chunk("put a cow", is_final=True),
                      chunk("on the east approach", is_final=True),
                      {"type": "Metadata"}], flush=False)
    require(stopped == ["put a cow on the east approach"],
            "stopping mid-sentence produced " + str(stopped)
            + " - speech_final is never raised on close, so Metadata has to flush")

    ended = finals([chunk("clear the road", is_final=True), utterance_end],
                   flush=False)
    require(ended == ["clear the road"],
            "UtteranceEnd should flush too, got " + str(ended))

    require(finals([chunk("")], flush=True) == [], "silence produced a proposal")

    # anything that isn't a transcript must not take the pump down with it
    for junk in ({"type": "Error"}, {"type": "Results", "channel": None},
                 {"type": "Results", "channel": {"alternatives": []}}, {}):
        TranscriptAssembler().feed(junk)

    return "glues pieces, flushes on stop, survives the vad frames"


@check("The same seed gives the same answer twice")
def _determinism():
    from . import demand as D
    from . import sim
    spec = D.DemandSpec(veh_per_hour=1600, duration_s=600)
    first = sim.run_once(spec, seed=3)
    second = sim.run_once(spec, seed=3)
    a = first.movements["NS"]["travel_time_s"]
    b = second.movements["NS"]["travel_time_s"]
    require(a == b, "seed 3 gave " + str(a) + " s then " + str(b) + " s; without "
                    "determinism paired-seed comparison means nothing")
    return "seed 3 reproduces exactly (" + str(a) + " s)"


@check("Changing a setting drastically actually changes the output")
def _settings_reach_the_simulator():
    from . import demand as D
    from . import sim
    from . import tls as T

    spec = D.DemandSpec(veh_per_hour=2000, duration_s=900)
    normal = sim.run_once(spec, plan=T.baseline_plan(), seed=5)

    # whichever stage releases the east approach, whatever this shape calls it
    stage = next(key for key, spec_ in C.phase_groups().items()
                 if "E" in spec_["arms"])
    starved = T.baseline_plan()
    was = starved[stage]["green"]
    starved[stage]["green"] = 8                # down to the floor
    changed = sim.run_once(spec, plan=starved, seed=5)

    before = normal.approaches["E"]["queue_mean_veh"]
    after = changed.approaches["E"]["queue_mean_veh"]
    require(after > before * 1.2 or after - before > 1.0,
            "cutting the " + stage + " green from " + str(was) + " s to 8 s "
            "moved the east queue from " + str(before) + " to " + str(after)
            + " vehicles. That's not a real change, which means the signal plan "
            "isn't reaching the simulator and every other number here is "
            "decoration.")
    return ("east queue " + str(before) + " -> " + str(after) + " vehicles when "
            "the " + stage + " green is cut to 8 s")


def run():
    return SelfTest.run()


if __name__ == "__main__":
    sys.exit(run())
