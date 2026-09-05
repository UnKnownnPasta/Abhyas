# The thing you actually run.
#
#     from abhyas.cli import Abhyas
#     Abhyas("TomTom Traffic Data Sheet.xlsx", "Abhyas - Traffic Incidents.xlsx").run()
#
# Hand it the two spreadsheets, get a menu, pick things off it. Every agent
# prints what it's doing while it's doing it instead of going quiet for four
# minutes and then dumping json at you.

import json
import os
import sys
import time

from . import config as C


class Console:
    """Printing. Kept in one place so the whole thing looks the same."""

    WIDTH = 74

    def rule(self, char="-"):
        print(char * self.WIDTH)

    def title(self, text):
        print()
        self.rule("=")
        print("  " + text)
        self.rule("=")

    def head(self, text):
        print()
        print(text)
        self.rule()

    def say(self, text, indent=2):
        print(" " * indent + str(text))

    def wrap(self, text, indent=4):
        """Print a long sentence without it running off the terminal."""
        words, line = str(text).split(), ""
        for word in words:
            if len(line) + len(word) + 1 > self.WIDTH - indent:
                print(" " * indent + line)
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            print(" " * indent + line)

    def kv(self, key, value, width=22):
        print("  " + format(str(key), str(width)) + str(value))

    def bar(self, done, total, tag=""):
        """One line progress bar that overwrites itself."""
        width = 28
        filled = int(width * done / max(total, 1))
        sys.stdout.write("\r    [" + "#" * filled + "." * (width - filled) + "] "
                         + str(done) + "/" + str(total) + "  " + tag[:24]
                         + "        ")
        sys.stdout.flush()
        if done >= total:      # wipe the bar so the next print isn't ragged
            sys.stdout.write("\r" + " " * (width + 44) + "\r")
            sys.stdout.flush()

    def ask(self, prompt, default=""):
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        return answer or default


class ProgressPrinter:
    """The progress callback the agents and the counterfactual call into.

    They emit dicts, this turns them into something readable in real time.
    """

    def __init__(self, console):
        self.out = console
        self.started_at = {}

    def __call__(self, **event):
        kind = event.get("kind")
        if kind == "agent":
            self._agent(event)
        elif kind == "run":
            self.out.bar(event.get("done", 0), event.get("total", 1),
                         event.get("tag", ""))
        elif kind == "calibration":
            self.out.say("dial " + str(event.get("veh_per_hour")) + " veh/h -> "
                         + str(event.get("model_s")) + " s  (target "
                         + str(event.get("target_s")) + " s)", indent=6)
        elif kind == "counterfactual":
            label = event.get("state", "")
            extra = event.get("splits") or ""
            self.out.say("running " + label + (" " + extra if extra else ""), indent=4)
        elif kind == "warning":
            self.out.say("! " + str(event.get("message", "")), indent=4)

    def _agent(self, event):
        name = event.get("agent", "")
        if event.get("state") == "started":
            self.started_at[name] = time.time()
            print()
            self.out.say(">> " + name + " ...")
        else:
            took = time.time() - self.started_at.get(name, time.time())
            mark = {"ok": "[ok]  ", "warning": "[warn]", "failed": "[FAIL]"}
            self.out.say(mark.get(event.get("status"), "[??]  ") + " " + name
                         + "  (" + format(took, ".0f") + "s)")
            if event.get("headline"):
                self.out.wrap(event["headline"], indent=8)


class Abhyas:
    """Entry point. Give it the spreadsheets, call run(), pick from the menu."""

    def __init__(self, travel_file=None, incidents_file=None, workers=4,
                 seeds=30, duration_s=1800.0):
        self.files = C.use_files(travel=travel_file, incidents=incidents_file)
        self.workers = workers
        self.seeds = seeds
        self.duration_s = duration_s
        self.slot = None
        self.out = Console()
        self.progress = ProgressPrinter(self.out)
        self._archive = None
        self.last_validation = None

    # -- data ---------------------------------------------------------------

    @property
    def archive(self):
        """Loaded lazily and cached - reading the workbook takes a second."""
        if self._archive is None:
            from . import archive as A
            self._archive = A.load()
        return self._archive

    def reload(self):
        self._archive = None
        return self.archive

    def default_slot(self):
        if self.slot:
            return self.slot
        slots = self.archive.hour_slots(C.JUNCTION_KEY, "NS")
        self.slot = slots[-1] if slots else "weekday 09:00-10:00"
        return self.slot

    # -- the menu -----------------------------------------------------------

    MENU = [
        ("1", "Data summary", "show_data"),
        ("2", "Pick the time slot to validate against", "choose_slot"),
        ("3", "List the agents", "show_agents"),
        ("4", "Run one agent", "run_one_agent"),
        ("5", "Run the whole validation fleet", "run_fleet"),
        ("6", "What-if on the signal (counterfactual)", "run_counterfactual"),
        ("7", "Ask in plain english", "ask_question"),
        ("8", "Build / rebuild the junction network", "build_network"),
        ("9", "Self test the pipeline", "self_test"),
        ("s", "Start the web interface", "serve"),
        ("q", "Quit", None),
    ]

    def banner(self):
        from . import voice
        status = voice.backend_status()
        self.out.title("Abhyas - " + C.JUNCTION_NAME)
        self.out.kv("Travel times", self.files["travel"].name)
        self.out.kv("Incidents", self.files["incidents"].name)
        self.out.kv("SUMO", str(C.SUMO_HOME))
        self.out.kv("Signal plan", C.PHASE_PLANS[C.ACTIVE_PHASE_PLAN]["label"])
        self.out.kv("Speech", status["stt"]["model"] or "off (no key)")
        self.out.kv("Text fallback", status["model"] or "off, local rules only")

    def menu(self):
        self.out.head("What do you want to do?")
        for key, label, _ in self.MENU:
            self.out.say("  " + key + ")  " + label)
        if self.slot:
            self.out.say("")
            self.out.say("slot: " + self.slot + "   seeds: " + str(self.seeds)
                         + "   workers: " + str(self.workers))

    def run(self):
        self.banner()
        while True:
            self.menu()
            choice = self.out.ask("\n> ").lower()
            if choice in ("q", "quit", "exit", ""):
                print("bye")
                return 0
            handler = dict((k, h) for k, _l, h in self.MENU).get(choice)
            if handler is None:
                self.out.say("no such option, try again")
                continue
            try:
                getattr(self, handler)()
            except KeyboardInterrupt:
                print()
                self.out.say("stopped.")
            except Exception as exc:
                self.out.say("that blew up: " + type(exc).__name__ + ": " + str(exc))

    # -- options ------------------------------------------------------------

    def show_data(self):
        archive = self.archive
        load = archive.report.to_dict()
        coverage = archive.coverage(C.JUNCTION_KEY)

        self.out.head("What's in the sheets")
        self.out.kv("Sheets read", ", ".join(load["sheets_used"]) or "none")
        if load["sheets_skipped"]:
            self.out.kv("Skipped", ", ".join(load["sheets_skipped"]))
        self.out.kv("Rows read", load["rows_read"])
        self.out.kv("Rows kept", load["rows_kept"])
        self.out.kv("Dropped", str(load["dropped_total"]) + " ("
                    + str(load["dropped_error"]) + " error, "
                    + str(load["dropped_no_time"]) + " no time, "
                    + str(load["dropped_bad_status"]) + " bad status)")
        if not coverage.get("observations"):
            self.out.say("nothing for " + C.JUNCTION_KEY + " in there.")
            return
        self.out.kv("Observations", coverage["observations"])
        self.out.kv("Span", coverage["first"][:16] + "  ->  " + coverage["last"][:16])
        self.out.kv("Days", coverage["days"])

        self.out.head("Per movement")
        for movement in sorted(C.ALL_MOVEMENTS):
            count = coverage["per_movement"].get(movement, 0)
            slots = archive.hour_slots(C.JUNCTION_KEY, movement)
            self.out.say(format(movement, "4") + format(str(count) + " obs", "10")
                         + str(len(slots)) + " usable hour slot(s)")

        free_flow = archive.free_flow_for()
        if free_flow:
            self.out.head("Observed free flow speed (km/h)")
            for movement, speed in sorted(free_flow.items()):
                self.out.say(format(movement, "4") + str(speed))

        self.out.head("Target for the current slot (" + self.default_slot() + ")")
        for movement in sorted(C.MOVEMENTS):
            target = archive.target(C.JUNCTION_KEY, movement,
                                    hour_slot=self.default_slot())
            if not target.get("n"):
                self.out.say(format(movement, "4") + "no observations")
                continue
            self.out.say(format(movement, "4")
                         + format(str(target["travel_time_s"]) + " s", "10")
                         + "p10-p90 " + str(target["spread_p10_p90_s"])
                         + "   n=" + str(target["n"]))

    def choose_slot(self):
        slots = self.archive.hour_slots(C.JUNCTION_KEY, "NS")
        if not slots:
            self.out.say("no slot has enough observations to quote a spread.")
            return
        self.out.head("Slots with at least three observations")
        for index, slot in enumerate(slots, 1):
            self.out.say(format(str(index) + ")", "5") + slot)
        answer = self.out.ask("\npick one (enter for the last) > ")
        if not answer:
            self.slot = slots[-1]
        elif answer.isdigit() and 1 <= int(answer) <= len(slots):
            self.slot = slots[int(answer) - 1]
        else:
            self.out.say("didn't get that, leaving it alone")
            return
        self.out.say("slot is now " + self.slot)

    def show_agents(self):
        from . import agents as G
        self.out.head("The fleet, in the order they run")
        for index, agent in enumerate(G.FLEET, 1):
            self.out.say(str(index) + ")  " + format(agent.name, "16") + agent.title)
            self.out.wrap(agent.blurb, indent=8)
            if not agent.needs_simulation:
                self.out.say("(reads the spreadsheet only, no SUMO runs)", indent=8)

    def run_one_agent(self):
        from . import agents as G
        self.show_agents()
        answer = self.out.ask("\nwhich one? (number or name) > ")
        if not answer:
            return
        if answer.isdigit() and 1 <= int(answer) <= len(G.FLEET):
            chosen = G.FLEET[int(answer) - 1]
        elif answer in G.AGENTS_BY_NAME:
            chosen = G.AGENTS_BY_NAME[answer]
        else:
            self.out.say("no agent called that")
            return

        # the later agents need the calibrated dial, so drag the ones before it
        # along unless it's the audit
        order = [a.name for a in G.FLEET]
        needed = order[:order.index(chosen.name) + 1]
        if chosen.name not in ("archive-audit", "calibration"):
            needed = [n for n in needed
                      if n in ("archive-audit", "calibration", chosen.name)]
            self.out.say("(running calibration first, " + chosen.name
                         + " needs the dial)")

        result = self._fleet(only=needed)
        self._print_agent(result, chosen.name)

    def run_fleet(self):
        self.out.head("Validation fleet - slot " + self.default_slot())
        self.out.say("seeds " + str(self.seeds) + ", " + str(self.workers)
                     + " worker processes. This takes a while.")
        result = self._fleet()
        self.out.head("Summary")
        summary = result["summary"]
        self.out.kv("Overall", summary["overall"].upper())
        self.out.wrap(summary["statement"])
        self.out.kv("Calibrated", str(summary["calibrated_veh_per_hour"]) + " veh/h")
        self.out.kv("Total runs", result["total_runs"])
        self.out.kv("Took", str(result["seconds"]) + " s")
        self.out.kv("Saved to", result["saved_to"])
        if self.out.ask("\nprint the full agent reports? [y/N] > ").lower() == "y":
            for agent in result["agents"]:
                self._print_report(agent)

    def _fleet(self, only=None):
        from . import agents as G
        result = G.run_fleet(slot=self.default_slot(), seeds=self.seeds,
                             workers=self.workers, duration_s=self.duration_s,
                             only=only, progress=self.progress)
        self.last_validation = result
        return result

    def _print_agent(self, result, name):
        for agent in result["agents"]:
            if agent["name"] == name:
                self._print_report(agent)

    def _print_report(self, agent):
        self.out.head(agent["title"] + "  [" + agent["status"] + "]")
        self.out.wrap(agent["headline"], indent=2)
        for finding in agent["findings"]:
            print()
            self.out.wrap("- " + finding, indent=4)
        if agent["runs"]:
            self.out.say("")
            self.out.kv("Runs", agent["runs"])
        self._print_rows(agent)

    def _print_rows(self, agent):
        """The two tables worth showing on a terminal."""
        rows = agent["data"].get("rows")
        if not rows or agent["name"] != "movement":
            return
        self.out.head("Movement table")
        self.out.say(format("move", "6") + format("model", "12")
                     + format("measured", "12") + format("err%", "8") + "verdict")
        for row in rows:
            if row.get("verdict") == "no data":
                self.out.say(format(row["movement"], "6") + "no data")
                continue
            self.out.say(format(row["movement"], "6")
                         + format(str(row["model_median_s"]) + " s", "12")
                         + format(str(row["measured_s"]) + " s", "12")
                         + format(str(row["error_pct"]), "8")
                         + row["verdict"])

    def run_counterfactual(self):
        from . import counterfactual as CF
        from . import demand as D

        groups = list(C.PHASE_GROUPS)
        self.out.head("Which green do you want to change?")
        for index, group in enumerate(groups, 1):
            self.out.say(format(str(index) + ")", "5")
                         + C.PHASE_GROUPS[group]["label"])
        answer = self.out.ask("\n> ", "1")
        if not (answer.isdigit() and 1 <= int(answer) <= len(groups)):
            self.out.say("didn't get that")
            return
        group = groups[int(answer) - 1]

        delta = self.out.ask("seconds to add (negative to cut) [10] > ", "10")
        seeds = self.out.ask("paired seeds [" + str(self.seeds) + "] > ",
                             str(self.seeds))
        try:
            delta, seeds = float(delta), int(seeds)
        except ValueError:
            self.out.say("those weren't numbers")
            return

        veh_per_hour = 2400.0
        if self.last_validation:
            veh_per_hour = (self.last_validation["summary"]
                            .get("calibrated_veh_per_hour") or veh_per_hour)

        self.out.head("Baseline vs scenario on " + str(seeds) + " paired seeds")
        card = CF.run(delta_seconds=delta, phase_group=group, seeds=seeds,
                      workers=self.workers,
                      spec=D.DemandSpec(veh_per_hour=float(veh_per_hour),
                                        duration_s=self.duration_s),
                      progress=self.progress)

        self.out.head("Verdict")
        self.out.wrap(card["verdict"]["headline"])
        for note in card["notes"]:
            print()
            self.out.wrap("- " + note, indent=4)
        self.out.say("")
        self.out.kv("Saved to", card["saved_to"])

    def ask_question(self):
        from . import nlu
        self.out.head("Say what you want. 'back' to leave.")
        self.out.say("try: add 10 seconds to the north approach green")
        self.out.say("     put a cow on the east approach")
        self.out.say("     validate the model at 9am with 30 runs")
        while True:
            text = self.out.ask("\nyou> ")
            if not text or text.lower() in ("back", "q", "exit"):
                return
            instruction = nlu.parse(text)
            if instruction.ok:
                self.out.say("-> " + instruction.summary, indent=4)
                self.out.say("   action " + instruction.action + "  "
                             + json.dumps(instruction.params), indent=4)
            else:
                self.out.wrap("-> rejected: " + instruction.reason, indent=4)
            for correction in instruction.corrections:
                print()
                self.out.wrap("! " + correction, indent=6)
            for note in instruction.notes:
                self.out.wrap("  " + note, indent=6)

    def build_network(self):
        from . import netbuild
        force = self.out.ask("force a full rebuild? [y/N] > ").lower() == "y"
        self.out.head("Building")
        arms = netbuild.build(force=force)
        self.out.say("")
        for name in sorted(arms):
            arm = arms[name]
            self.out.say("arm " + name + ": " + format(arm.length_m, ".0f")
                         + " m, enters on " + arm.entry_edge)
        self.out.say("changelog: " + str(netbuild.CHANGELOG))

    def self_test(self):
        from . import selftest
        return selftest.run()

    def serve(self, host="127.0.0.1", port=8000):
        from . import netbuild
        import uvicorn
        netbuild.build()
        self.out.head("Serving on http://" + host + ":" + str(port))
        uvicorn.run("abhyas.server:app", host=host, port=port, log_level="warning")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Abhyas junction console.")
    parser.add_argument("--travel", default=None,
                        help="TomTom travel time .xlsx")
    parser.add_argument("--incidents", default=None,
                        help="incidents .xlsx")
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--duration", type=float, default=1800.0)
    parser.add_argument("--serve", action="store_true",
                        help="skip the menu and start the web interface")
    parser.add_argument("--check", action="store_true",
                        help="build, self-test, exit")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args(argv)

    app = Abhyas(travel_file=args.travel, incidents_file=args.incidents,
                 workers=args.workers, seeds=args.seeds,
                 duration_s=args.duration)
    if args.check:
        app.banner()
        from . import netbuild
        netbuild.build()
        return app.self_test() or 0
    if args.serve:
        app.banner()
        app.serve(host=args.host, port=args.port)
        return 0
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
