# Deploying Abhyas to Render (Free Tier)

This guide explains how to host **Abhyas** on [Render](https://render.com) using **Render Blueprints**, on the **free plan** (no card on file, no paid worker or disk), while interacting with the project and its CLI via the **CLI & Workflows Dash**.

---

## Architecture Overview

Abhyas deploys as a **single free web service** on Render:

- **`abhyas-web` (Web Service, free plan)**:
   - Hosts the FastAPI backend, static web assets, and WebSocket streams.
   - Runs the live TraCI simulation worker (`SimWorker`) for the 3D Digital Twin.
   - Exposes the **Interactive CLI Dash** (`/ws/cli` and `/api/cli/...`) allowing users to trigger and monitor CLI operations (`1` to `9`) directly from their browser.
   - Also runs everything that used to be a separate `abhyas-workflows` worker service:
     - `run_fleet`: 7-agent validation fleet across 30 seeds with multi-process workers.
     - `run_agent`: Single agent runs (e.g. `calibration`, `asymmetry`, `sensitivity`).
     - `counterfactual`: Paired-seed what-if analysis with turning split sweeps.
     - `netbuild`: Compilation of OSM raw source into SUMO network with Indiranagar hand-repairs.
     - `selftest`: Pipeline verification suite (11 end-to-end checks).
   - With `RENDER_WORKFLOW_ENABLED=false` (and no `RENDER_API_KEY`), `abhyas.workflows` runs these in-process via local threads instead of dispatching to Render Cloud Workflows - Render's Workflows execution engine and Background Worker services both require a paid plan.

### Free tier tradeoffs

Render's free plan doesn't require a credit card, but comes with real limits:

- **No persistent disk.** The old two-service blueprint mounted `/app/interactive-ui/results` on a Render Persistent Disk (paid-plan only). On free, that directory lives on the container's ephemeral filesystem: version trees, validation cards, and run history are **wiped on every restart or redeploy**. If you need a result to survive that, download it or commit it to the repo.
- **Spins down when idle.** After ~15 minutes with no inbound requests, the service sleeps; the next request cold-starts it (can take 30-60s).
- **Shared CPU, 512 MB RAM, single instance.** Fine for the live 3D twin and light CLI use. A full 30-seed validation fleet run will be noticeably slower than on a paid plan and can hit the memory ceiling - if that happens, cut `DEFAULT_SEEDS`/`CALIBRATION_SEEDS` for a demo run, or run the heavy batch job elsewhere (locally, or a throwaway CI job) and commit the resulting `results/*.json` so the free service just serves precomputed data.

---

## 1-Click Deployment with Render Blueprints

### Step 1: Connect Your Repository to Render
1. Push this repository to your GitHub or GitLab account.
2. Log in to your [Render Dashboard](https://dashboard.render.com).
3. Click **New +** in the top right, then select **Blueprint**.
4. Connect your Abhyas repository.
5. Render will automatically parse `render.yaml` and discover one service:
   - `abhyas-web` (Web Service, Docker, free plan)

### Step 2: Configure Environment Variables
During the Blueprint creation (or under Service Settings -> Environment), configure:

| Variable | Required | Description | Default |
|---|---|---|---|
| `PORT` | Auto | Port exposed by the web service | `8000` |
| `ABHYAS_PHASE_PLAN` | No | Signal plan (`four_phase` or `two_phase`) | `four_phase` |
| `RENDER_WORKFLOW_ENABLED` | No | Enable Render Cloud Workflows dispatch (needs a paid worker service) | `false` |
| `ABHYAS_DEEPGRAM_API_KEY` | Optional | Deepgram key for browser speech recognition | Unset |
| `ABHYAS_LLM_API_KEY` | Optional | LLM API key for plain-English sentence parser | Unset |
| `ABHYAS_LLM_MODEL` | Optional | Model identifier | `openai/gpt-oss-120b` |

No `RENDER_API_KEY` is needed for the free-tier setup - it's only relevant if you later move to a paid plan and want `abhyas.workflows` to dispatch to Render's actual Workflows execution engine instead of running jobs in-process.

### Step 3: Apply Blueprint & Deploy
Click **Apply**. Render will:
1. Build the unified Docker image with Python 3.11, Eclipse SUMO 1.27.1, and project dependencies.
2. Run automated network building and selftest verification during build (`RUN python -m abhyas.selftest`).
3. Start `abhyas-web` on the free plan - no disk, no separate worker.

---

## Interacting with the CLI Dash

Once deployed, visit your Render URL (e.g. `https://abhyas-web.onrender.com`):

### Switching Views
Use the top navigation bar to toggle between:
- **🚦 Digital Twin (3D)**: Real-time 3D junction view with vehicles, signal countdowns, and knob controls.
- **💻 CLI & Workflows Dash**: The full command center for CLI operations and background workflows.

### Terminal Console
In the CLI Dash:
- **CLI Shortcuts Bar**: Click buttons `1) Data Summary`, `2) Slot`, `3) List Agents`, `4) Calibration`, `5) Fleet Run`, `6) What-If`, `7) NLU Ask`, `8) Netbuild`, `9) Self-Test`, or `help`.
- **Live Interactive Input**: Type commands directly into the prompt (e.g. `4 calibration` or `7 add 10 seconds to northbound green`) and press **Execute** or Enter.
- **Auto-scroll & History**: Navigate past commands with `Up` and `Down` arrow keys, clear the terminal, or copy output with one click.

### Workflows Hub
- Click **Validation Fleet** or **What-If Sweep** to run these jobs as local in-process threads on the same free web service (no separate worker on this plan).
- Watch live execution progress streamed in real time over WebSockets.
- Inspect the **Recent Workflow Runs** table to view run status, duration, parameters, and logs.
- Run single agents from the **Validation Agent Fleet** grid.
- Remember: results live on the container's ephemeral disk, so download anything you need before the service redeploys or spins down for good.

---

## Local Development & Testing

You can also run the exact same stack locally without any Render infrastructure:

```bash
cd interactive-ui
pip install -r requirements.txt

# Run the web server with CLI Dash & local workflows
python run.py --serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in your browser and click **CLI & Workflows Dash**.
