# The interface's back end.
#
# The simulation, the archive and the fleet are all local and the page loads
# no font, script or tile off the network. The one exception is the spoken
# language fallback in voice.py, which is off unless a key is set and labelled
# on screen when it's on.
#
# The live simulation lives on one dedicated thread because TraCI is a single
# connection that has to be driven from one place. Commands reach it through a
# queue and get applied between steps. Long jobs (the fleet, a counterfactual)
# run in worker processes and stream progress back over the same websocket the
# frames use.

import asyncio
import json
import os
import queue
import threading
import time
import traceback

import websockets
# Explicitly: websockets lazy-loads its submodules, so websockets.exceptions
# is not reachable by attribute access and blows up at the moment you
# actually need to catch something.
from websockets.exceptions import ConnectionClosed
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents as G
from . import archive as A
from . import config as C
from . import controls as K
from . import counterfactual as CF
from . import demand as D
from . import netbuild
from . import nlu
from . import sim
from . import tls as T
from . import versions as V
from . import voice
from .clirunner import CLIRunner
from .workflows import WorkflowManager

LIVE_DURATION_S = 100000.0        # the live run never ends on its own


class Broadcaster:
    """One place everything publishes to, websockets subscribe."""

    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()
        self._loop = None

    def bind(self, loop):
        self._loop = loop

    def add(self, websocket):
        with self._lock:
            self._clients.add(websocket)

    def discard(self, websocket):
        with self._lock:
            self._clients.discard(websocket)

    def publish(self, message):
        """Safe from any thread, including the sim thread."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(message), self._loop)
        except RuntimeError:
            pass

    async def _send(self, message):
        with self._lock:
            clients = list(self._clients)
        payload = json.dumps(message)
        for client in clients:
            try:
                await client.send_text(payload)
            except Exception:
                self.discard(client)


bus = Broadcaster()
workflows = WorkflowManager(bus)
cli_runner = CLIRunner(bus)


class SimWorker(threading.Thread):
    """Owns the TraCI connection. Nothing else is allowed near it."""

    daemon = True


    def __init__(self, broadcaster):
        super().__init__(name="abhyas-sim")
        self.bus = broadcaster
        self.commands = queue.Queue()
        self.session = None
        self.paused = False
        self.speed = 4.0                  # simulated seconds per wall second
        self.stop_flag = threading.Event()
        self.latest = {}
        self.error = None
        self.ready = threading.Event()

    # -- thread safe, call from anywhere -----------------------------------

    def send(self, command, payload=None):
        self.commands.put((command, payload or {}))

    def snapshot(self):
        return self.latest

    def controls(self):
        if self.session is None:
            return K.surface(T.baseline_plan(), D.DemandSpec(), [])
        return K.surface(self.session.plan, self.session.spec,
                         self.session.obstructions)

    def state(self):
        if self.session is None:
            return K.state_of(T.baseline_plan(), D.DemandSpec(), [])
        return K.state_of(self.session.plan, self.session.spec,
                          self.session.obstructions)

    # -- the thread body ---------------------------------------------------

    def run(self):
        try:
            self._start_session()
        except Exception as exc:
            self.error = str(exc)
            self.bus.publish({"type": "sim_error", "message": str(exc),
                              "detail": traceback.format_exc()})
            self.ready.set()
            return

        self.ready.set()
        next_frame = time.time()
        while not self.stop_flag.is_set():
            self._drain_commands()
            if self.paused:
                time.sleep(0.05)
                next_frame = time.time()
                continue

            delta_t = max(self.session.conn.simulation.getDeltaT(), 0.01)
            steps = max(1, int(self.speed / delta_t / 10))
            try:
                for _ in range(steps):
                    self.session.step()
            except Exception as exc:
                self.error = str(exc)
                self.bus.publish({"type": "sim_error", "message": str(exc)})
                break

            self.latest = self.session.snapshot()
            self.bus.publish({"type": "frame", "data": self.latest})

            next_frame += 0.1      # ~10 fps. more than that and the socket
            # backs up faster than the browser drains it
            delay = next_frame - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.time()

        if self.session is not None:
            self.session.stop()

    def _start_session(self):
        spec = D.DemandSpec(veh_per_hour=calibrated_veh_per_hour(),
                            duration_s=LIVE_DURATION_S)
        self.session = sim.LiveSession(spec=spec, plan=T.baseline_plan(), seed=1)
        self.session.start()
        self.latest = self.session.snapshot()

    def _drain_commands(self):
        while True:
            try:
                command, payload = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply(command, payload)
            except Exception as exc:
                self.bus.publish({"type": "command_error", "command": command,
                                  "message": str(exc)})

    def _apply(self, command, payload):
        session = self.session
        if command == "pause":
            self.paused = True
        elif command == "resume":
            self.paused = False
        elif command == "speed":
            self.speed = max(0.5, min(30.0, float(payload.get("value", 4.0))))
        elif command == "set_plan":
            session.set_plan(payload["plan"])
            self.bus.publish({"type": "applied", "what": "signal plan",
                              "plan": T.describe(payload["plan"])})
        elif command == "add_obstruction":
            placed = session.add_obstruction(payload)
            self.bus.publish({"type": "applied", "what": "obstruction",
                              "obstruction": placed})
        elif command == "set_arm_signal":
            applied = session.set_arm_signal(payload["arm"], payload["colour"])
            self.bus.publish({"type": "applied", "what": "signal",
                              "signal": applied})
        elif command == "clear_obstructions":
            removed = session.clear_obstructions()
            self.bus.publish({"type": "applied", "what": "obstructions cleared",
                              "removed": removed})
        elif command == "set_demand":
            self._restart(payload["veh_per_hour"], session.plan)
        elif command == "reset":
            self._restart(calibrated_veh_per_hour(), T.baseline_plan())
        elif command == "apply_controls":
            self._apply_controls(payload)

    def _apply_controls(self, payload):
        """A batch of edits at once, because a demand change restarts the run
        and two separate commands would restart it twice."""
        session = self.session
        before = self.state()
        plan = {g: dict(spec) for g, spec in session.plan.items()}
        spec = session.spec
        notes, toggles = [], []
        plan_changed = demand_changed = False
        edits = list(payload.get("edits") or [])

        # a plan shape change replaces the whole plan, so it goes first and the
        # stage edits after it get read against the new shape
        for edit in [e for e in edits if e.get("id") == "signal.plan"]:
            wanted = str(edit.get("value"))
            if wanted not in C.PHASE_PLANS:
                raise K.Rejected("No signal plan called '" + wanted + "'.")
            if wanted != T.shape_of(plan):
                plan = T.baseline_plan(wanted)
                plan_changed = True

        for edit in [e for e in edits if e.get("id") != "signal.plan"]:
            control = K.lookup(edit.get("id"))
            value = edit.get("value")
            note = ""
            if control["group"] == "Signal":
                if control["id"].split(".")[0] not in plan:
                    notes.append(control["label"] + " isn't a stage of the "
                                 + C.PHASE_PLANS[T.shape_of(plan)]["label"]
                                 + " plan, so that edit was dropped.")
                    continue
                plan, note = K.plan_edit(plan, control["id"], value)
                plan_changed = True
            elif control["group"] == "Traffic":
                spec, note = K.demand_edit(spec, control["id"], value)
                demand_changed = True
            elif control["group"] == "Access":
                # both of these are baked into the route and vType files, same
                # as demand is, so they restart the run rather than being
                # nudged into a running one
                spec, note = K.access_edit(spec, control["id"], value)
                demand_changed = True
            elif control["group"] == "Fleet":
                spec, note = K.fleet_edit(spec, control["id"], value)
                demand_changed = True
            else:
                toggles.append((control["id"], int(value)))
            if note:
                notes.append(note)

        # a restart clears the road, so obstructions go back afterwards - a
        # demand change is not a request to clear the road
        if demand_changed:
            self._restart(spec.veh_per_hour, plan, spec=spec, keep_obstructions=True)
        elif plan_changed:
            self.session.set_plan(plan)

        for control_id, wanted in toggles:
            self._set_obstruction_count(control_id, wanted)

        if self.session.spec.is_exploratory:
            notes.append(K.EXPLORATORY_NOTE)
        for note in (getattr(self.session, "access", None) or {}).get("notes", []):
            notes.append(note)

        changes = K.diff(before, self.state())
        self.bus.publish({"type": "controls", "surface": self.controls(),
                          "changes": changes,
                          "summary": K.describe_changes(changes),
                          "notes": notes, "restarted": demand_changed,
                          "exploratory": self.session.spec.is_exploratory,
                          "origin": payload.get("origin", "dial")})

    def _set_obstruction_count(self, control_id, wanted):
        """Reconcile how many of one kind sit on one arm with the dial's
        count - each add/remove gets its own randomised spot and lane."""
        _, kind, arm = control_id.split(".")
        session = self.session
        standing = [o for o in session.obstructions
                    if o.get("kind") == kind and o.get("arm") == arm]
        wanted = max(0, min(int(wanted), sim.OBSTRUCTION_MAX_PER_ARM))
        if wanted > len(standing):
            for _ in range(wanted - len(standing)):
                session.add_obstruction({"kind": kind, "arm": arm})
        elif wanted < len(standing):
            for obstruction in standing[:len(standing) - wanted]:
                session.remove_obstruction(obstruction)

    def _restart(self, veh_per_hour, plan, spec=None, keep_obstructions=False):
        """Demand is baked into the route file, so changing it means a new run."""
        self.bus.publish({"type": "restarting", "veh_per_hour": round(veh_per_hour)})
        standing = [dict(o) for o in self.session.obstructions] if keep_obstructions else []
        self.session.stop()

        if spec is None:
            spec = D.DemandSpec(veh_per_hour=veh_per_hour, duration_s=LIVE_DURATION_S)
        else:
            spec = spec.copy(veh_per_hour=veh_per_hour, duration_s=LIVE_DURATION_S)

        self.session = sim.LiveSession(spec=spec, plan=plan, seed=1)
        self.session.start()
        for obstruction in standing:
            try:
                self.session.add_obstruction({"kind": obstruction["kind"],
                                              "arm": obstruction["arm"]})
            except Exception:
                pass
        self.latest = self.session.snapshot()
        self.bus.publish({"type": "applied", "what": "demand",
                          "veh_per_hour": round(veh_per_hour)})


def calibrated_veh_per_hour():
    """Start from the calibrated dial if the fleet has produced one."""
    cached = C.RESULTS / "validation.json"
    if cached.exists():
        try:
            payload = json.loads(cached.read_text(encoding="utf-8"))
            value = payload.get("summary", {}).get("calibrated_veh_per_hour")
            if value:
                return float(value)
        except Exception:
            pass
    return D.DemandSpec().veh_per_hour


worker = None
jobs_lock = threading.Lock()
running_jobs = set()

app = FastAPI(title="Abhyas junction interface")


@app.on_event("startup")
async def startup():
    global worker
    bus.bind(asyncio.get_running_loop())
    netbuild.build()                       # no-op when the network's already there
    worker = SimWorker(bus)
    worker.start()


@app.on_event("shutdown")
async def shutdown():
    if worker is not None:
        worker.stop_flag.set()
        worker.join(timeout=5)


@app.get("/")
async def index():
    """Stamp the asset links with each file's mtime.

    Without it the browser keeps serving the previous app.js and style.css out
    of memory cache and an edit looks like it did nothing. The stamping only
    works if this document is itself fetched fresh, hence no-store - otherwise
    the tab can be running yesterday's HTML with today's cache-busted app.js,
    which is exactly the failure that looks styled and dead at the same time.
    """
    html = (C.WEB / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css", "scene3d.js"):
        stamp = str(int((C.WEB / asset).stat().st_mtime))
        html = html.replace("/web/" + asset, "/web/" + asset + "?v=" + stamp)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ---- read only state -----------------------------------------------------

@app.get("/api/geometry")
async def geometry():
    return JSONResponse(sim.network_geometry())


@app.get("/api/scenery")
async def scenery():
    """The neighbourhood around the junction. Drawn, never simulated."""
    from . import scenery as S
    return JSONResponse(S.Scenery.load())


@app.get("/api/context")
async def context():
    """Everything the page needs to describe the model it's showing."""
    archive = A.load()
    cached = C.RESULTS / "validation.json"
    validation = None
    if cached.exists():
        try:
            validation = json.loads(cached.read_text(encoding="utf-8"))
        except Exception:
            validation = None
    return JSONResponse({
        "junction": {"id": C.JUNCTION_ID, "name": C.JUNCTION_NAME,
                     "key": C.JUNCTION_KEY},
        "movements": C.MOVEMENTS,
        "phase_groups": C.PHASE_GROUPS,
        "phase_plans": {name: {"label": spec["label"], "note": spec["note"],
                               "stages": [st["key"] for st in spec["stages"]]}
                        for name, spec in C.PHASE_PLANS.items()},
        "active_phase_plan": C.ACTIVE_PHASE_PLAN,
        "baseline_plan": T.describe(T.baseline_plan()),
        "vocabulary": nlu.ACTIONS,
        "archive": {
            "coverage": archive.coverage(C.JUNCTION_KEY),
            "load": archive.report.to_dict(),
            "hour_slots": archive.hour_slots(C.JUNCTION_KEY, "NS"),
            "daily_profile": {m: archive.daily_profile(C.JUNCTION_KEY, m)
                              for m in C.MOVEMENTS},
        },
        "validation": validation,
        "vehicle_classes": {name: {"label": spec["label"], "colour": spec["colour"],
                                   "share": spec["share"], "source": spec["source"]}
                            for name, spec in D.VEHICLE_CLASSES.items()},
        "offline": True,
    })


@app.get("/api/archive/target")
async def archive_target(movement="NS", slot=None):
    archive = A.load()
    if slot is None:
        candidates = archive.hour_slots(C.JUNCTION_KEY, movement)
        slot = candidates[-1] if candidates else None
    return JSONResponse(archive.target(C.JUNCTION_KEY, movement, hour_slot=slot))


@app.get("/api/results/{name}")
async def results(name: str):
    path = C.RESULTS / (name + ".json")
    if not path.exists():
        return JSONResponse({"error": "no cached result named " + name},
                            status_code=404)
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


def _no_worker():
    """The 503 every endpoint that steers the running sim owes. Returns a
    response when there's nothing to steer and None when there is."""
    if worker is None or worker.error:
        return JSONResponse({"ok": False,
                             "error": worker.error if worker else "not started"},
                            status_code=503)
    return None


class Utterance(BaseModel):
    text: str


@app.post("/api/parse")
async def parse(body: Utterance):
    """Parse only. This endpoint never touches the simulation."""
    return JSONResponse(nlu.parse(body.text).to_dict())


class Execution(BaseModel):
    action: str
    params: dict = {}


@app.post("/api/execute")
async def execute(body: Execution):
    """Run an instruction that's already been parsed and shown on screen."""
    if body.action not in nlu.ACTIONS:
        return JSONResponse({"ok": False, "error": "Action '" + body.action
                             + "' is outside the schema."}, status_code=400)
    stopped = _no_worker()
    if stopped:
        return stopped

    action, params = body.action, body.params
    plan = worker.session.plan if worker.session else T.baseline_plan()

    if action in ("adjust_green", "set_green"):
        group = params.get("phase_group")
        if group not in C.PHASE_GROUPS:
            return JSONResponse({"ok": False, "error": "unknown phase group"},
                                status_code=400)
        if action == "adjust_green":
            delta = float(params.get("delta_seconds", 0))
        else:
            delta = float(params.get("seconds", 0)) - plan[group]["green"]
        new_plan = T.apply_delta(plan, group, delta)
        worker.send("set_plan", {"plan": new_plan})
        return JSONResponse({"ok": True, "applied": "signal plan",
                             "plan": T.describe(new_plan)})

    if action == "add_obstruction":
        worker.send("add_obstruction", params)
        return JSONResponse({"ok": True, "applied": "obstruction queued"})

    if action == "clear_obstructions":
        worker.send("clear_obstructions", {})
        return JSONResponse({"ok": True, "applied": "obstructions cleared"})

    if action == "set_demand":
        current = worker.session.spec.veh_per_hour if worker.session else 2400.0
        if params.get("veh_per_hour") is not None:
            target = float(params["veh_per_hour"])
        else:
            target = current * float(params.get("multiplier", 1.0))
        worker.send("set_demand", {"veh_per_hour": max(100.0, min(6000.0, target))})
        return JSONResponse({"ok": True, "applied": "demand",
                             "veh_per_hour": round(target)})

    if action in ("pause", "resume", "reset"):
        worker.send(action, {})
        return JSONResponse({"ok": True, "applied": action})

    if action == "status":
        return JSONResponse({"ok": True, "applied": "status",
                             "snapshot": worker.snapshot(),
                             "metrics": worker.session.live_metrics()
                             if worker.session else {}})

    if action == "run_validation":
        return _dispatch("validation", _validation_job, params)

    if action == "run_counterfactual":
        return _dispatch("counterfactual", _counterfactual_job, params)

    return JSONResponse({"ok": False, "error": "unhandled action"}, status_code=400)


# ---- the control surface -------------------------------------------------

class ControlEdits(BaseModel):
    edits: list = []
    origin: str = "dial"
    commit: bool = False
    message: str = ""


@app.get("/api/controls")
async def get_controls():
    if worker is None:
        return JSONResponse(K.surface(T.baseline_plan(), D.DemandSpec(), []))
    return JSONResponse(worker.controls())


@app.post("/api/controls")
async def post_controls(body: ControlEdits):
    """Validated here, applied on the sim thread."""
    stopped = _no_worker()
    if stopped:
        return stopped
    try:
        for edit in body.edits:
            K.lookup(edit.get("id"))
    except K.Rejected as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    worker.send("apply_controls", {"edits": body.edits, "origin": body.origin})
    response = {"ok": True, "queued": len(body.edits)}
    if body.commit:
        response["commit_requested"] = body.message or "voice edit"
    return JSONResponse(response)


# ---- the language layer, aimed at the dials ------------------------------

class Spoken(BaseModel):
    text: str
    allow_llm: bool = True


@app.get("/api/voice/status")
async def voice_status():
    return JSONResponse(voice.backend_status())


@app.post("/api/voice")
async def interpret(body: Spoken):
    """Sentence -> proposed control edits. Nothing is applied here."""
    result = voice.interpret(body.text, current_control_state(),
                             allow_llm=body.allow_llm)
    result["backend"] = voice.backend_status()
    return JSONResponse(result)


# ---- scenario presets ----------------------------------------------------

@app.get("/api/presets")
async def list_presets():
    return JSONResponse(V.presets())


@app.post("/api/presets/{preset_id}")
async def apply_preset(preset_id: str):
    """Load a named scenario. Same path a dial takes: every edit still goes
    through the control surface, so a preset can't set something by hand that
    the interface would refuse."""
    stopped = _no_worker()
    if stopped:
        return stopped
    try:
        resolved = V.preset_edits(preset_id)
    except KeyError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    worker.send("apply_controls", {"edits": resolved["edits"],
                                   "origin": "preset"})
    return JSONResponse({"ok": True, "preset": resolved["preset"],
                         "edits": len(resolved["edits"]),
                         "note": resolved["preset"]["note"],
                         "exploratory": resolved["preset"]["exploratory"]})


@app.get("/api/scope")
async def scope():
    """What the vocabulary now covers and what it still refuses. Shrinking the
    out-of-scope list deliberately means saying so, not going quiet."""
    return JSONResponse({"actions": nlu.ACTIONS,
                         "out_of_scope": nlu.OUT_OF_SCOPE,
                         "changes": nlu.SCOPE_CHANGES,
                         "exploratory_groups": sorted(K.EXPLORATORY_GROUPS),
                         "exploratory_note": K.EXPLORATORY_NOTE})


# ---- versions ------------------------------------------------------------

class Commit(BaseModel):
    message: str = ""
    source: str = "manual"


@app.get("/api/versions")
async def list_versions():
    return JSONResponse(V.tree())


@app.post("/api/versions")
async def commit_version(body: Commit):
    if worker is None or worker.session is None:
        return JSONResponse({"ok": False, "error": "not started"}, status_code=503)
    session = worker.session
    try:
        live = session.live_metrics()
        metrics = {"time_s": round(session.time, 1),
                   "movements": live.get("movements", {}),
                   "queues": worker.snapshot().get("queues", {})}
    except Exception:
        metrics = None
    version = V.commit(worker.state(), body.message, metrics=metrics,
                       source=body.source)
    bus.publish({"type": "versions", "tree": V.tree()})
    return JSONResponse({"ok": True,
                         "version": {k: v for k, v in version.items() if k != "state"},
                         "tree": V.tree()})


@app.post("/api/versions/{version_id}/checkout")
async def checkout_version(version_id: str):
    stopped = _no_worker()
    if stopped:
        return stopped
    try:
        version = V.checkout(version_id)
    except KeyError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    known = set(K.all_controls())
    edits = [{"id": cid, "value": value}
             for cid, value in version["state"].items() if cid in known]
    worker.send("apply_controls", {"edits": edits, "origin": "checkout"})
    bus.publish({"type": "versions", "tree": V.tree()})
    return JSONResponse({"ok": True, "restored": version["message"],
                         "edits": len(edits)})


@app.post("/api/versions/{version_id}/rename")
async def rename_version(version_id: str, body: Commit):
    try:
        V.rename(version_id, body.message)
    except KeyError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    bus.publish({"type": "versions", "tree": V.tree()})
    return JSONResponse({"ok": True, "tree": V.tree()})


@app.delete("/api/versions/{version_id}")
async def delete_version(version_id: str):
    try:
        V.drop(version_id)
    except KeyError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    bus.publish({"type": "versions", "tree": V.tree()})
    return JSONResponse({"ok": True, "tree": V.tree()})


@app.get("/api/versions/compare")
async def compare_versions(left: str, right: str):
    try:
        return JSONResponse(V.compare(left, right))
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


# ---- long jobs -----------------------------------------------------------

def _dispatch(name, target, params):
    with jobs_lock:
        if name in running_jobs:
            return JSONResponse({"ok": False, "error": "A " + name + " run is "
                                 "already in progress."}, status_code=409)
        running_jobs.add(name)

    def body():
        try:
            target(params)
        except Exception as exc:
            bus.publish({"type": "job_failed", "job": name, "message": str(exc),
                         "detail": traceback.format_exc()})
        finally:
            with jobs_lock:
                running_jobs.discard(name)

    threading.Thread(target=body, name="abhyas-" + name, daemon=True).start()
    bus.publish({"type": "job_started", "job": name, "params": params})
    return JSONResponse({"ok": True, "applied": name + " dispatched",
                         "streaming": True})


def _progress(job):
    def emit(**kw):
        bus.publish({"type": "progress", "job": job, **kw})
    return emit


def _validation_job(params):
    result = G.run_fleet(slot=params.get("slot"),
                         seeds=int(params.get("seeds") or G.DEFAULT_SEEDS),
                         workers=int(params.get("workers") or 4),
                         include_separate=bool(params.get("include_separate")),
                         duration_s=float(params.get("duration_s") or 1800.0),
                         progress=_progress("validation"))
    bus.publish({"type": "job_finished", "job": "validation",
                 "summary": result["summary"], "result": result})


def _counterfactual_job(params):
    veh_per_hour = params.get("veh_per_hour") or calibrated_veh_per_hour()
    spec = D.DemandSpec(veh_per_hour=float(veh_per_hour),
                        duration_s=float(params.get("duration_s") or 1800.0))

    # A fleet or access scenario is compared against the same baseline the same
    # way a signal change is: the levers named here move on the scenario side
    # only, both sides run the same seeds, and the verdict is still allowed to
    # come back "cannot resolve".
    levers = {k: params[k] for k in D.DemandSpec.LEVERS if params.get(k)}
    scenario_spec = spec.copy(**levers) if levers else None

    # a signal counterfactual with no size named still means the old default;
    # a fleet one means "change the traffic, leave the signal alone"
    default_delta = 0.0 if levers else 10.0
    result = CF.run(delta_seconds=float(params.get("delta_seconds",
                                                   default_delta)),
                    phase_group=params.get("phase_group", "north_south"),
                    seeds=int(params.get("seeds") or 30),
                    workers=int(params.get("workers") or 4),
                    spec=spec, scenario_spec=scenario_spec,
                    splits_sweep=bool(params.get("splits_sweep", True)),
                    progress=_progress("counterfactual"))
    bus.publish({"type": "job_finished", "job": "counterfactual",
                 "verdict": result["verdict"], "result": result})


def current_control_state():
    return worker.state() if worker is not None else K.state_of(
        T.baseline_plan(), D.DemandSpec(), [])


# ---- websockets ----------------------------------------------------------

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    bus.add(ws)
    try:
        if worker is not None and worker.snapshot():
            await ws.send_text(json.dumps({"type": "frame",
                                           "data": worker.snapshot()}))
            await ws.send_text(json.dumps({"type": "controls",
                                           "surface": worker.controls(),
                                           "changes": [], "notes": [],
                                           "origin": "connect"}))
        await ws.send_text(json.dumps({"type": "versions", "tree": V.tree()}))
        while True:
            message = await ws.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "control" and worker is not None:
                worker.send(payload.get("command", ""), payload.get("params") or {})
    except WebSocketDisconnect:
        pass
    finally:
        bus.discard(ws)


# ---- CLI & Render Workflows Dash Endpoints -------------------------------

class CLICommand(BaseModel):
    command: str


class WorkflowTrigger(BaseModel):
    task: str
    params: dict = {}


@app.get("/api/cli/menu")
async def cli_menu():
    return JSONResponse(cli_runner.get_menu_info())


@app.post("/api/cli/execute")
async def cli_execute(body: CLICommand):
    res = cli_runner.execute_command_async(body.command)
    return JSONResponse(res)


@app.get("/api/cli/history")
async def cli_history():
    return JSONResponse({"lines": cli_runner.get_history(), "busy": cli_runner.busy})


@app.get("/api/workflows/status")
async def workflow_status():
    return JSONResponse(workflows.status())


@app.get("/api/workflows/runs")
async def workflow_runs():
    return JSONResponse(workflows.list_runs())


@app.get("/api/workflows/runs/{run_id}")
async def workflow_run_detail(run_id: str):
    record = workflows.get_run(run_id)
    if not record:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(record)


@app.post("/api/workflows/trigger")
async def workflow_trigger(body: WorkflowTrigger):
    try:
        record = workflows.trigger(body.task, body.params)
        return JSONResponse({"ok": True, "record": record})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/agents/list")
async def agents_list():
    items = []
    for a in G.FLEET:
        items.append({
            "name": a.name,
            "title": a.title,
            "blurb": a.blurb,
            "needs_simulation": a.needs_simulation,
        })
    return JSONResponse(items)


@app.post("/api/agents/run/{agent_name}")
async def run_single_agent(agent_name: str, body: dict = None):
    params = body or {}
    params["agent_name"] = agent_name
    try:
        record = workflows.trigger("run_agent", params)
        return JSONResponse({"ok": True, "record": record})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/netbuild/status")
async def netbuild_status():
    changelog_text = ""
    if netbuild.CHANGELOG.exists():
        changelog_text = netbuild.CHANGELOG.read_text(encoding="utf-8")
    net_xml = C.BUILD / "junction.net.xml"
    return JSONResponse({
        "built": net_xml.exists(),
        "mtime": net_xml.stat().st_mtime if net_xml.exists() else None,
        "changelog": changelog_text,
    })


@app.post("/api/netbuild/run")
async def netbuild_run(force: bool = False):
    record = workflows.trigger("netbuild", {"force": force})
    return JSONResponse({"ok": True, "record": record})


@app.post("/api/selftest/run")
async def selftest_run():
    record = workflows.trigger("selftest", {})
    return JSONResponse({"ok": True, "record": record})


@app.websocket("/ws/cli")
async def websocket_cli(ws: WebSocket):
    await ws.accept()
    bus.add(ws)
    try:
        # Send initial state
        await ws.send_text(json.dumps({
            "type": "cli_init",
            "history": cli_runner.get_history()[-150:],
            "menu": cli_runner.get_menu_info(),
            "workflows": workflows.status(),
            "runs": workflows.list_runs()[:10],
        }))
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "command":
                cmd = msg.get("command", "").strip()
                if cmd:
                    cli_runner.execute_command_async(cmd)
    except WebSocketDisconnect:
        pass
    finally:
        bus.discard(ws)


def _deepgram_header_kwarg():
    """What this websockets version calls the extra-headers argument.

    Renamed extra_headers -> additional_headers when the asyncio client landed
    in 14.0. We pin no exact version because google-genai and the realtime
    packages cap websockets well below the newest, and one hard pin here made
    the whole environment unresolvable. So ask the installed copy instead.
    """
    try:
        major = int(websockets.__version__.split(".")[0])
    except (AttributeError, ValueError):
        major = 14                      # unknown build, assume the modern name
    return "additional_headers" if major >= 14 else "extra_headers"


DEEPGRAM_HEADER_KWARG = _deepgram_header_kwarg()

# How often to tell Deepgram we are still here while nobody is talking.
# Its own patience is about ten seconds.
DEEPGRAM_KEEPALIVE_S = 5.0


@app.websocket("/ws/voice")
async def websocket_voice(ws: WebSocket):
    """Browser mic audio in, live captions and a final proposal out.

    A private pipe, not the shared /ws broadcast - this carries one listener's
    raw audio to Deepgram and nothing else.
    """
    await ws.accept()
    if not voice.deepgram_available():
        await ws.send_text(json.dumps({
            "type": "unavailable",
            "reason": "Set ABHYAS_DEEPGRAM_API_KEY to enable voice."}))
        await ws.close()
        return

    params = ("model=" + voice.DEEPGRAM_MODEL
              + "&smart_format=true&interim_results=true"
              + "&endpointing=300&vad_events=true")
    headers = {"Authorization": "Token " + os.environ[voice.DEEPGRAM_KEY_ENV]}

    async def pump_audio_in(upstream):
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                audio = message.get("bytes")
                if audio:
                    await upstream.send(audio)
                    continue
                text = message.get("text")
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "stop":
                    await upstream.send(json.dumps({"type": "CloseStream"}))
        except ConnectionClosed:
            pass                      # upstream went first, nothing to send to


    async def keep_upstream_alive(upstream):
        """Deepgram hangs up if nothing reaches it for about ten seconds.

        Opening the mic and pausing to think is exactly that, and so is a
        browser that takes a moment to hand over its first MediaRecorder chunk.
        The socket died with 1011 'did not receive audio data ... within the
        timeout window' and took the transcript pump down with it.
        """
        try:
            while True:
                await asyncio.sleep(DEEPGRAM_KEEPALIVE_S)
                await upstream.send(json.dumps({"type": "KeepAlive"}))
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    assembler = voice.TranscriptAssembler()

    async def send_transcript(kind, text):
        if kind == "partial":
            await ws.send_text(json.dumps({"type": "partial", "text": text}))
            return
        result = voice.interpret(text, current_control_state(), allow_llm=True)
        result["backend"] = voice.backend_status()
        result["utterance"] = text
        await ws.send_text(json.dumps({"type": "voice_result", **result}))

    async def pump_transcripts_out(upstream):
        dropped = None
        try:
            async for raw in upstream:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for kind, text in assembler.feed(event):
                    await send_transcript(kind, text)
        except ConnectionClosed as exc:
            # Expected after CloseStream, and also what the silence timeout
            # looks like. Neither is a reason to let the task die unhandled.
            dropped = exc
        finally:
            # whatever was said before the socket went still belongs to the user
            try:
                for kind, text in assembler.flush():
                    await send_transcript(kind, text)
            except Exception:
                pass

        if dropped is not None and "did not receive audio" in str(dropped):
            try:
                await ws.send_text(json.dumps({
                    "type": "voice_error",
                    "reason": "the transcriber heard no audio and closed the "
                              "connection. Check the microphone is actually "
                              "sending, then try again."}))
            except Exception:
                pass

    try:
        connect_kwargs = {DEEPGRAM_HEADER_KWARG: headers}
        async with websockets.connect(voice.DEEPGRAM_URL + "?" + params,
                                      **connect_kwargs) as upstream:
            in_task = asyncio.create_task(pump_audio_in(upstream))
            out_task = asyncio.create_task(pump_transcripts_out(upstream))
            alive_task = asyncio.create_task(keep_upstream_alive(upstream))
            done, pending = await asyncio.wait({in_task, out_task},
                                               return_when=asyncio.FIRST_COMPLETED)
            alive_task.cancel()
            for task in pending:
                task.cancel()
            # collect them so a failure is reported here rather than surfacing
            # later as "Task exception was never retrieved"
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_text(json.dumps({
                "type": "voice_error",
                "reason": type(exc).__name__ + ": " + str(exc)}))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


app.mount("/web", StaticFiles(directory=str(C.WEB)), name="web")
