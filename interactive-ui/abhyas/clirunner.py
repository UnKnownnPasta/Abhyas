# Programmatic & Interactive CLI Runner for Abhyas.
#
# Enables full interaction with the Abhyas console CLI over REST and WebSockets,
# capturing terminal output with ANSI styling, handling progress bars,
# and dispatching commands asynchronously without blocking the server.

import collections
import contextlib
import io
import json
import re
import sys
import threading
import time
import traceback

from . import config as C
from .cli import Abhyas, Console


class BufferCapture(io.StringIO):
    """Captures stream writes, preserves them in history, and broadcasts lines."""
    def __init__(self, on_line_callback=None, max_lines=1000):
        super().__init__()
        self.on_line = on_line_callback
        self.max_lines = max_lines
        self.lines = collections.deque(maxlen=max_lines)
        self._current = ""
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._current += s
            while "\n" in self._current or "\r" in self._current:
                # Handle carriage returns from progress bars
                split_idx = -1
                delim = "\n"
                idx_n = self._current.find("\n")
                idx_r = self._current.find("\r")
                if idx_n != -1 and idx_r != -1:
                    if idx_n < idx_r:
                        split_idx = idx_n
                        delim = "\n"
                    else:
                        split_idx = idx_r
                        delim = "\r"
                elif idx_n != -1:
                    split_idx = idx_n
                    delim = "\n"
                elif idx_r != -1:
                    split_idx = idx_r
                    delim = "\r"

                line = self._current[:split_idx]
                self._current = self._current[split_idx + 1:]
                if line or delim == "\n":
                    self.lines.append(line)
                    if self.on_line:
                        try:
                            self.on_line(line, is_progress=(delim == "\r"))
                        except Exception:
                            pass
        return len(s)

    def flush(self):
        with self._lock:
            if self._current:
                line = self._current
                self._current = ""
                self.lines.append(line)
                if self.on_line:
                    try:
                        self.on_line(line, is_progress=False)
                    except Exception:
                        pass

    def get_lines(self):
        with self._lock:
            return list(self.lines)


class HeadlessConsole(Console):
    """Console subclass redirecting output through a custom capture stream."""
    def __init__(self, stream):
        self.stream = stream

    def rule(self, char="-"):
        self.stream.write(char * self.WIDTH + "\n")

    def title(self, text):
        self.stream.write("\n")
        self.rule("=")
        self.stream.write("  " + text + "\n")
        self.rule("=")

    def head(self, text):
        self.stream.write("\n" + text + "\n")
        self.rule()

    def say(self, text, indent=2):
        self.stream.write(" " * indent + str(text) + "\n")

    def wrap(self, text, indent=4):
        words, line = str(text).split(), ""
        for word in words:
            if len(line) + len(word) + 1 > self.WIDTH - indent:
                self.stream.write(" " * indent + line + "\n")
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            self.stream.write(" " * indent + line + "\n")

    def kv(self, key, value, width=22):
        self.stream.write("  " + format(str(key), str(width)) + str(value) + "\n")

    def bar(self, done, total, tag=""):
        width = 28
        filled = int(width * done / max(total, 1))
        msg = ("\r    [" + "#" * filled + "." * (width - filled) + "] "
               + str(done) + "/" + str(total) + "  " + tag[:24] + "        ")
        self.stream.write(msg)
        if done >= total:
            self.stream.write("\r" + " " * (width + 44) + "\r\n")
        self.stream.flush()

    def ask(self, prompt, default=""):
        self.stream.write(prompt + (default or "") + "\n")
        return default


class CLIRunner:
    """Headless executor and command bridge for the Abhyas CLI."""

    def __init__(self, broadcaster=None):
        self.bus = broadcaster
        self.capture = BufferCapture(on_line_callback=self._on_captured_line)
        self.console = HeadlessConsole(self.capture)
        self.cli = Abhyas()
        self.cli.out = self.console
        from .cli import ProgressPrinter
        self.cli.progress = ProgressPrinter(self.console)
        self.busy = False
        self._lock = threading.Lock()

        # Emit initial banner
        self.cli.banner()
        self.capture.flush()

    def _on_captured_line(self, line, is_progress=False):
        if self.bus:
            self.bus.publish({
                "type": "cli_output",
                "line": line,
                "is_progress": is_progress,
                "timestamp": time.time(),
            })

    def get_history(self):
        return self.capture.get_lines()

    def get_menu_info(self):
        from . import agents as G
        return {
            "menu": [
                {"key": "1", "label": "Data summary", "action": "show_data"},
                {"key": "2", "label": "Pick time slot", "action": "choose_slot"},
                {"key": "3", "label": "List agents", "action": "show_agents"},
                {"key": "4", "label": "Run one agent", "action": "run_one_agent"},
                {"key": "5", "label": "Run validation fleet", "action": "run_fleet"},
                {"key": "6", "label": "What-if counterfactual", "action": "run_counterfactual"},
                {"key": "7", "label": "Ask in plain english", "action": "ask_question"},
                {"key": "8", "label": "Build/rebuild network", "action": "build_network"},
                {"key": "9", "label": "Self test pipeline", "action": "self_test"},
            ],
            "active_slot": self.cli.slot or self.cli.default_slot(),
            "seeds": self.cli.seeds,
            "workers": self.cli.workers,
            "agents": [{"name": a.name, "title": a.title, "blurb": a.blurb,
                        "needs_simulation": a.needs_simulation} for a in G.FLEET],
            "busy": self.busy,
        }

    def execute_command_async(self, cmd_text: str):
        """Dispatches CLI command in a separate worker thread."""
        with self._lock:
            if self.busy:
                return {"ok": False, "error": "CLI runner is currently busy with another operation."}
            self.busy = True

        def run_target():
            try:
                self.execute_command(cmd_text)
            finally:
                with self._lock:
                    self.busy = False
                if self.bus:
                    self.bus.publish({"type": "cli_status", "busy": False})

        threading.Thread(target=run_target, daemon=True, name="abhyas-cli-cmd").start()
        if self.bus:
            self.bus.publish({"type": "cli_status", "busy": True, "command": cmd_text})
        return {"ok": True, "queued": cmd_text}

    def execute_command(self, cmd_text: str):
        cmd = cmd_text.strip()
        self.console.say(f"\nyou> {cmd}")

        if not cmd:
            self.cli.menu()
            self.capture.flush()
            return

        parts = cmd.split(None, 1)
        verb = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        try:
            if verb in ("1", "data", "summary"):
                self.cli.show_data()
            elif verb in ("2", "slot"):
                if arg:
                    self.cli.slot = arg
                    self.console.say(f"slot is now {self.cli.slot}")
                else:
                    self.cli.choose_slot()
            elif verb in ("3", "agents", "list"):
                self.cli.show_agents()
            elif verb in ("4", "agent"):
                if arg:
                    from . import agents as G
                    if arg in G.AGENTS_BY_NAME:
                        chosen = G.AGENTS_BY_NAME[arg]
                    elif arg.isdigit() and 1 <= int(arg) <= len(G.FLEET):
                        chosen = G.FLEET[int(arg) - 1]
                    else:
                        self.console.say(f"no agent called {arg}")
                        return
                    self.console.head(f"Running {chosen.name} on {self.cli.default_slot()}")
                    # Run target agent
                    fleet = G.Fleet(slot=self.cli.default_slot(), seeds=self.cli.seeds,
                                    workers=self.cli.workers, only=[chosen.name],
                                    duration_s=self.cli.duration_s,
                                    progress=self.cli.progress)
                    result = fleet.run()
                    self.console.say(f"Agent finished. Status: {result.get('summary', {}).get('overall')}")
                else:
                    self.cli.show_agents()
                    self.console.say("Specify agent: e.g. '4 calibration' or '4 2'")
            elif verb in ("5", "fleet", "validate"):
                self.console.head(f"Fleet run: slot {self.cli.default_slot()}, {self.cli.seeds} seeds")
                from . import agents as G
                res = G.run_fleet(slot=self.cli.default_slot(), seeds=self.cli.seeds,
                                  workers=self.cli.workers, duration_s=self.cli.duration_s,
                                  progress=self.cli.progress)
                self.console.head("Validation Result")
                self.console.wrap(res["summary"]["statement"])
            elif verb in ("6", "whatif", "counterfactual"):
                delta = 10.0
                group = "north_south"
                if arg:
                    subparts = arg.split()
                    if len(subparts) >= 1:
                        try:
                            delta = float(subparts[0])
                        except ValueError:
                            pass
                    if len(subparts) >= 2:
                        group = subparts[1]
                from . import counterfactual as CF
                from . import demand as D
                self.console.head(f"Counterfactual: delta {delta}s on {group}")
                res = CF.run(delta_seconds=delta, phase_group=group, seeds=self.cli.seeds,
                             workers=self.cli.workers,
                             spec=D.DemandSpec(duration_s=self.cli.duration_s),
                             progress=self.cli.progress)
                self.console.head("Verdict")
                self.console.wrap(res["verdict"]["headline"])
            elif verb in ("7", "ask", "nlu"):
                if not arg:
                    self.console.say("Say what you want: e.g. '7 add 10 seconds to north approach green'")
                else:
                    from . import nlu
                    instruction = nlu.parse(arg)
                    if instruction.ok:
                        self.console.say("-> " + instruction.summary, indent=4)
                        self.console.say("   action " + instruction.action + "  "
                                         + json.dumps(instruction.params), indent=4)
                    else:
                        self.console.wrap("-> rejected: " + instruction.reason, indent=4)
                    for correction in instruction.corrections:
                        self.console.wrap("! " + correction, indent=6)
            elif verb in ("8", "build", "netbuild"):
                force = (arg.lower() in ("force", "yes", "-f", "true"))
                self.console.head(f"Building Network (force={force})")
                from . import netbuild
                arms = netbuild.build(force=force)
                for name in sorted(arms):
                    arm = arms[name]
                    self.console.say(f"arm {name}: {format(arm.length_m, '.0f')} m, enters on {arm.entry_edge}")
                self.console.say(f"changelog: {netbuild.CHANGELOG}")
            elif verb in ("9", "check", "selftest"):
                self.console.head("Running Pipeline Selftest (11 checks)...")
                from . import selftest
                code = selftest.run()
                if code == 0 or code is None:
                    self.console.say("[PASS] All selftests passed successfully!")
                else:
                    self.console.say(f"[FAIL] Selftest failed with exit code {code}")
            elif verb in ("help", "?"):
                self.cli.menu()
            elif verb in ("banner", "status"):
                self.cli.banner()
            else:
                self.console.say(f"Unknown command '{verb}'. Type 'help' or 1-9.")
        except Exception as exc:
            self.console.say(f"Error executing '{cmd}': {type(exc).__name__}: {exc}")
            traceback.print_exc(file=self.capture)
        finally:
            self.capture.flush()
