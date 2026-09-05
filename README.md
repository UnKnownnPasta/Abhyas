# Abhyas

One junction in Bengaluru - CMH Road x 100 Feet Road, Indiranagar - simulated
in SUMO, driven from a console or a browser, and checked against real TomTom
travel time data by a handful of agents that are each allowed to say "no".

## Run it

```bash
cd interactive-ui
pip install -r requirements.txt

python run.py                          # menu, using the sheets in data/
python run.py --travel a.xlsx --incidents b.xlsx
python run.py --serve                  # web UI & interactive CLI Dash on :8000
python run.py --check                  # build + self test, then quit
```

## Deploy on Render with Workflows

Deploy the complete stack (3D Digital Twin, Interactive CLI Dash, and Render Workflows background task engine) on Render via Blueprints:

- **Blueprint**: [`render.yaml`](render.yaml) automatically sets up `abhyas-web` and `abhyas-workflows`.
- **Interactive CLI Dash**: Access all 9 CLI actions, live streaming terminal, and workflow task triggers in the browser.
- **Detailed Guide**: See [`RENDER.md`](RENDER.md) for deployment steps, environment variables, and persistent storage setup.

Or from python:

```python
from abhyas.cli import Abhyas

Abhyas("data/TomTom Traffic Data Sheet.xlsx",
       "data/Abhyas - Traffic Incidents.xlsx").run()
```

The menu:

```
1) Data summary                     6) What-if on the signal
2) Pick the time slot               7) Ask in plain english
3) List the agents                  8) Build / rebuild the network
4) Run one agent                    9) Self test the pipeline
5) Run the whole fleet              s) Start the web interface
```

Agents print what they're doing while they do it - a progress bar per batch of
seeds, the calibration dial walking up and down, and a one line headline from
each agent as it finishes.

## The agents

| name | what it does |
|---|---|
| archive-audit | reads the sheets and says whether they're fit to validate against |
| calibration | sweeps vehicles per hour until the model matches the measured times |
| movement | model vs measured on all twelve corridors, median of many runs |
| asymmetry | does the model get the fast/slow direction right on each road |
| seed-stability | how wrong a single run would have been |
| sensitivity | sweeps the turning splits, says which verdicts survive them |
| phase-plan | runs both signal shapes and asks the data which one it matches |

## Layout

```
net/                       the original OSM download
interactive-ui/
  run.py                   entry point
  data/                    the two spreadsheets
  abhyas/
    cli.py                 the console (class Abhyas)
    config.py              paths, junction geometry, the closed vocabulary
    netbuild.py            OSM -> one junction network (NetBuilder)
    archive.py             the TomTom sheets (Archive)
    demand.py              vehicle types + route files (Demand, DemandSpec)
    tls.py                 signal plans, link map
    sim.py                 run_once() and LiveSession
    agents.py              the fleet (Agent, Batch, Fleet)
    counterfactual.py      before/after on paired seeds
    stats.py               Stats - medians, bootstraps, verdicts
    controls.py            every knob, declared once
    versions.py            named states as a tree (VersionStore)
    nlu.py / voice.py      text -> instruction, optional hosted fallback
    selftest.py            eleven checks on the whole pipeline
    server.py              FastAPI + websocket back end
  web/                     the browser front end
```

## Vehicle models

The stage picks one 3d model per vehicle class. Commit a model here and it gets
used on the next reload, no code change:

```
interactive-ui/web/assets/
  two_wheeler/scene.gltf      twowheeler    committed
  auto_rickshaw/scene.gltf    auto          committed
  low_poly_car/scene.gltf     car           committed
  truck/scene.gltf            hcv           committed
  bus/scene.gltf              bus           committed
  cow/scene.gltf              obstruction   still falls back to the car
  stalled_vehicle/scene.gltf  obstruction   still falls back to the car
  roadworks/scene.gltf        obstruction   still falls back to the car
```

`truck/` used to be `bus/`. The model is a semi truck and always was, and with
an `hcv` class in the fleet it now drives the class it actually looks like. A
real bus - a Volvo 8700 LE - took over the `bus/` slot, so the two heavy
classes are no longer the same silhouette on screen. That matters more than it
sounds: the whole point of the HMV work is watching how a bus and a truck
behave differently, and two identical boxes hide it.

Licences of what is committed, all from Sketchfab, all requiring credit:

| folder | model | licence |
|---|---|---|
| low_poly_car | BMW M3 Low-Poly Stylized, by kulonee | CC-BY-**SA**-4.0 |
| two_wheeler | Stylized Muscle Bike, by Poras_Gamer | CC-BY-4.0 |
| auto_rickshaw | Autorickshaw 3d model free, by iGauravRajput | CC-BY-4.0 |
| bus | Volvo 8700 LE, by zairiq-zairiq-123-pixar-cars-bfdi | CC-BY-4.0 |
| truck | White Low Poly Semi Truck, by Polyjo | CC-BY-**NC**-4.0 |

Two of those carry more than plain attribution. The car is share-alike, so
anything derived from it inherits that licence. The truck is
**non-commercial** - as long as it is in the tree, the bundle cannot be used
commercially. Swap it for a CC-BY or CC0 model if that matters.

`scene.gltf` is what a Sketchfab zip unpacks to - keep the `.bin` and textures
beside it. For a single `.glb`, set `files` on that entry in `VEHICLE_MODELS`
at the top of `web/scene3d.js`. That table also holds the per model
calibration: `yaw` if it comes in facing sideways, `lift` if its origin isn't
at the wheels, and `fit` (`uniform` keeps a real model's proportions and is
what you want; `stretch` squashes it to the class box).

A class with no file falls back to the car scaled to that class's dimensions,
which is why an empty `bus/` currently gives you a 10 m long sports car. You'll
see one 404 per missing class in the console - that's the loader telling you
where it looked.

Every model needs a `license.txt` next to it, the way `low_poly_car/` has one.
Note the committed car is CC-BY-**SA**, so anything derived from it inherits
that license.

## Acknowledgements

`web/scene3d.js` (the three.js stage: SUMO-to-scene coordinate mapping, a
per-frame vehicle/signal update loop driven by a polled REST endpoint, and
orbit camera controls) follows the pattern set by
[sidewalklabs/sumo-web3d](https://github.com/sidewalklabs/sumo-web3d), an
open-source three.js visualizer for SUMO traffic simulations (archived,
EPL-2.0). Our implementation differs in specifics - loaded GLTF vehicle
models with per-class calibration instead of procedural meshes, a day/night
colour scheme, and `OrbitControls` from three.js's own addons instead of a
custom camera rig - but the overall shape (coordinate transform, static
network geometry built once, vehicles updated per frame) is drawn from that
project.

## Things worth knowing

- Everything runs locally. Two optional backends go out: Deepgram for speech,
  and a hosted model for sentences the local rules can't place. Both are off
  unless their key is in the environment, and the banner says which are live.
- One dial is fitted to data: vehicles per hour. Turning splits are a declared
  prior and get swept, never fitted.
- The network build makes five hand repairs that OSM/netconvert got wrong
  *silently* - the joined node's y coordinate, the four node join, missing lane
  tags, the lane assignment at the stop line, and a 100 km/h speed limit on an
  Indiranagar arterial. They're all written to `build/network-changelog.txt`.
- `run.py --check` runs eleven checks. The last one cuts a green in half and
  asserts the queue actually moved, because a misconfigured SUMO will happily
  run a normal looking simulation using none of your settings.
