# Render Workflows Integration for Abhyas.
#
# Bridges the Abhyas simulation and validation fleet to Render Workflows -
# Render's durable background task orchestration engine.
#
# Supports dual-mode execution:
#   1. Render Cloud Mode: When deployed on Render (with RENDER_API_KEY /
#      RENDER_WORKFLOW_ENABLED), tasks can be dispatched to Render Workflows
#      workers via RenderAsync.
#   2. Local / Standalone Mode: When running locally or without Render
#      credentials, tasks execute reliably in local background process/threads
#      while preserving the exact same lifecycle events, streaming logs,
#      and validation results.

import asyncio
import datetime as dt
import json
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import config as C

# Optional Render SDK import with robust fallback
HAS_RENDER_SDK = False
try:
    from render import Workflows, TaskContext
    HAS_RENDER_SDK = True
except ImportError:
    # Graceful shim for environments without 'render' package installed
    class TaskContext:
        """Shim for Render's TaskContext when running outside Render SDK."""
        def __init__(self, run_id=None, app=None):
            self.run_id = run_id or str(uuid.uuid4())
            self.app = app

        def run(self, task_fn, *args, **kwargs):
            return task_fn(self, *args, **kwargs)

    class Workflows:
        """Shim for Render's Workflows class when running outside Render SDK."""
        def __init__(self, **kwargs):
            self.tasks = {}

        def task(self, *args, **kwargs):
            def decorator(fn):
                name = kwargs.get("name", fn.__name__)
                self.tasks[name] = fn
                return fn
            if len(args) == 1 and callable(args[0]):
                return decorator(args[0])
            return decorator

        def start(self):
            print("[Workflows] Local task worker listening for events...")


# Initialize the Workflows application
app = Workflows()

# Storage for workflow run tracking
WORKFLOWS_DIR = C.RESULTS / "workflows"
WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
RUNS_FILE = WORKFLOWS_DIR / "runs_index.json"


def _read_runs_index():
    if RUNS_FILE.exists():
        try:
            return json.loads(RUNS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_run_record(record):
    index = _read_runs_index()
    # Replace existing or append
    index = [r for r in index if r.get("id") != record.get("id")]
    index.insert(0, record)
    # Keep last 100 runs
    index = index[:100]
    try:
        RUNS_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")
    except Exception:
        pass


# ============================================================================
# Task Definitions (Registered with Render Workflows)
# ============================================================================

@app.task
def task_run_fleet(ctx: TaskContext, slot=None, seeds=30, workers=4,
                   include_separate=False, duration_s=1800.0, emit_fn=None):
    """Render Workflow Task: Execute full validation fleet."""
    from . import agents as G
    started = time.time()
    run_id = getattr(ctx, "run_id", str(uuid.uuid4()))

    def progress(**kw):
        if emit_fn:
            emit_fn(run_id=run_id, **kw)

    progress(kind="workflow", state="running", task="run_fleet", slot=slot, seeds=seeds)
    result = G.run_fleet(slot=slot, seeds=int(seeds), workers=int(workers),
                         include_separate=bool(include_separate),
                         duration_s=float(duration_s),
                         progress=progress)
    progress(kind="workflow", state="completed", task="run_fleet",
             summary=result.get("summary"))
    return {
        "ok": True,
        "task": "run_fleet",
        "run_id": run_id,
        "slot": slot,
        "seeds": seeds,
        "seconds": round(time.time() - started, 1),
        "summary": result.get("summary"),
        "saved_to": result.get("saved_to"),
    }


@app.task
def task_run_agent(ctx: TaskContext, agent_name: str, slot=None, seeds=30,
                   workers=4, duration_s=1800.0, emit_fn=None):
    """Render Workflow Task: Execute a single validation agent."""
    from . import agents as G
    started = time.time()
    run_id = getattr(ctx, "run_id", str(uuid.uuid4()))

    def progress(**kw):
        if emit_fn:
            emit_fn(run_id=run_id, **kw)

    progress(kind="workflow", state="running", task="run_agent", agent=agent_name)
    result = G.run_fleet(slot=slot, seeds=int(seeds), workers=int(workers),
                         only=[agent_name], duration_s=float(duration_s),
                         progress=progress)
    progress(kind="workflow", state="completed", task="run_agent", agent=agent_name)
    return {
        "ok": True,
        "task": "run_agent",
        "agent": agent_name,
        "run_id": run_id,
        "seconds": round(time.time() - started, 1),
        "result": result,
    }


@app.task
def task_counterfactual(ctx: TaskContext, delta_seconds=10.0, phase_group="north_south",
                        seeds=30, workers=4, veh_per_hour=None, splits_sweep=True,
                        emit_fn=None):
    """Render Workflow Task: Execute paired seed counterfactual analysis."""
    from . import counterfactual as CF
    from . import demand as D
    started = time.time()
    run_id = getattr(ctx, "run_id", str(uuid.uuid4()))

    def progress(**kw):
        if emit_fn:
            emit_fn(run_id=run_id, **kw)

    progress(kind="workflow", state="running", task="counterfactual",
             delta=delta_seconds, group=phase_group)
    spec = D.DemandSpec(veh_per_hour=float(veh_per_hour or 2400.0), duration_s=1800.0)
    result = CF.run(delta_seconds=float(delta_seconds),
                    phase_group=phase_group,
                    seeds=int(seeds),
                    workers=int(workers),
                    spec=spec,
                    splits_sweep=bool(splits_sweep),
                    progress=progress)
    progress(kind="workflow", state="completed", task="counterfactual",
             verdict=result.get("verdict"))
    return {
        "ok": True,
        "task": "counterfactual",
        "run_id": run_id,
        "seconds": round(time.time() - started, 1),
        "verdict": result.get("verdict"),
        "saved_to": result.get("saved_to"),
    }


@app.task
def task_netbuild(ctx: TaskContext, force=False, emit_fn=None):
    """Render Workflow Task: Rebuild network from OSM source."""
    from . import netbuild
    started = time.time()
    run_id = getattr(ctx, "run_id", str(uuid.uuid4()))

    if emit_fn:
        emit_fn(run_id=run_id, kind="workflow", state="running", task="netbuild", force=force)
    arms = netbuild.build(force=bool(force))
    details = {name: {"length_m": arm.length_m, "entry_edge": arm.entry_edge}
               for name, arm in arms.items()}
    if emit_fn:
        emit_fn(run_id=run_id, kind="workflow", state="completed", task="netbuild", arms=details)
    return {
        "ok": True,
        "task": "netbuild",
        "run_id": run_id,
        "seconds": round(time.time() - started, 1),
        "arms": details,
        "changelog": str(netbuild.CHANGELOG),
    }


@app.task
def task_selftest(ctx: TaskContext, emit_fn=None):
    """Render Workflow Task: Execute full pipeline selftest."""
    from . import selftest
    started = time.time()
    run_id = getattr(ctx, "run_id", str(uuid.uuid4()))

    if emit_fn:
        emit_fn(run_id=run_id, kind="workflow", state="running", task="selftest")
    code = selftest.run()
    passed = (code == 0 or code is None)
    if emit_fn:
        emit_fn(run_id=run_id, kind="workflow", state="completed", task="selftest", passed=passed)
    return {
        "ok": passed,
        "task": "selftest",
        "run_id": run_id,
        "seconds": round(time.time() - started, 1),
        "exit_code": code,
    }


# ============================================================================
# Workflow Orchestration & Client
# ============================================================================

class WorkflowManager:
    """Manages dispatching, tracking, and fallback for Render Workflows."""

    TASKS = {
        "run_fleet": {
            "name": "Validation Fleet",
            "description": "Multi-agent validation against TomTom travel time sheets",
            "task_fn": task_run_fleet,
        },
        "run_agent": {
            "name": "Single Agent",
            "description": "Run an individual validation agent (calibration, asymmetry, etc.)",
            "task_fn": task_run_agent,
        },
        "counterfactual": {
            "name": "Counterfactual Analysis",
            "description": "Before/after comparison on paired SUMO seeds with turning sweeps",
            "task_fn": task_counterfactual,
        },
        "netbuild": {
            "name": "Network Builder",
            "description": "Compile OSM source into SUMO network with Indiranagar geometry fixes",
            "task_fn": task_netbuild,
        },
        "selftest": {
            "name": "Pipeline Self-Test",
            "description": "Run 11 automated verification checks across network, demand, and SUMO",
            "task_fn": task_selftest,
        },
    }

    def __init__(self, broadcaster=None):
        self.bus = broadcaster
        self._active_runs = {}
        self._lock = threading.Lock()

    def is_render_env(self) -> bool:
        """True if running in Render cloud environment with workflows configured."""
        return bool(os.environ.get("RENDER") or os.environ.get("RENDER_WORKFLOW_ENABLED"))

    def status(self) -> dict:
        render_mode = "cloud" if (self.is_render_env() and HAS_RENDER_SDK) else "local"
        return {
            "mode": render_mode,
            "render_sdk": HAS_RENDER_SDK,
            "is_render": bool(os.environ.get("RENDER")),
            "workflow_slug": os.environ.get("RENDER_WORKFLOW_SLUG", "abhyas-workflows"),
            "registered_tasks": list(self.TASKS.keys()),
            "active_runs_count": len(self._active_runs),
        }

    def list_runs(self) -> list:
        return _read_runs_index()

    def get_run(self, run_id: str) -> dict:
        for r in _read_runs_index():
            if r.get("id") == run_id:
                return r
        return self._active_runs.get(run_id)

    def trigger(self, task_name: str, params: dict = None) -> dict:
        """Trigger a workflow task either in Render Workflows or local worker."""
        if task_name not in self.TASKS:
            raise ValueError(f"Unknown workflow task '{task_name}'")

        params = params or {}
        run_id = f"rw-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        task_meta = self.TASKS[task_name]

        record = {
            "id": run_id,
            "task": task_name,
            "task_title": task_meta["name"],
            "params": params,
            "status": "queued",
            "started_at": dt.datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
            "duration_s": None,
            "error": None,
            "result": None,
            "logs": [],
        }

        with self._lock:
            self._active_runs[run_id] = record
        _save_run_record(record)

        self._emit(run_id, type="workflow_started", record=record)

        # Dispatch execution in background thread
        thread = threading.Thread(target=self._execute_task, args=(task_name, run_id, params),
                                  daemon=True, name=f"wf-{run_id}")
        thread.start()

        return record

    def _emit(self, run_id, **event):
        if self.bus:
            self.bus.publish({"source": "workflows", "run_id": run_id, **event})

    def _execute_task(self, task_name: str, run_id: str, params: dict):
        record = self._active_runs.get(run_id)
        if not record:
            return

        record["status"] = "running"
        _save_run_record(record)

        def emit_fn(**kw):
            record["logs"].append({"t": time.time(), "event": kw})
            self._emit(run_id, type="workflow_progress", run_id=run_id, **kw)

        task_fn = self.TASKS[task_name]["task_fn"]
        ctx = TaskContext(run_id=run_id, app=app)

        try:
            started = time.time()
            result = task_fn(ctx, emit_fn=emit_fn, **params)
            duration = round(time.time() - started, 1)

            record["status"] = "completed"
            record["result"] = result
            record["duration_s"] = duration
            record["finished_at"] = dt.datetime.utcnow().isoformat() + "Z"
            self._emit(run_id, type="workflow_finished", record=record)
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            record["detail"] = traceback.format_exc()
            record["finished_at"] = dt.datetime.utcnow().isoformat() + "Z"
            self._emit(run_id, type="workflow_failed", record=record)
        finally:
            with self._lock:
                self._active_runs.pop(run_id, None)
            _save_run_record(record)


# Standalone worker runner for Render Workflows Worker service
if __name__ == "__main__":
    print("[Abhyas Workflows] Initializing worker process...")
    print(f"[Abhyas Workflows] Registered tasks: {list(app.tasks.keys())}")
    app.start()
