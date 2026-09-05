# Paths, junction geometry, and the words the rest of the code is allowed to use.
# Nothing here reads the .net.xml at import time - SUMO renumbers its internal
# traffic light indices on every rebuild, so we look those up when we need them.

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # interactive-ui/
PARENT = ROOT.parent                               # Abhyas/
NET_SRC = PARENT / "net"
DATA = ROOT / "data"
BUILD = ROOT / "build"
RESULTS = ROOT / "results"
WEB = ROOT / "web"
DOCS = ROOT / "docs"
RUNS = BUILD / "runs"

for d in (BUILD, RESULTS, DOCS, RUNS, DATA):
    d.mkdir(exist_ok=True)


# .env keys live under their vendor names, we read them under ours
ENV_ALIASES = {
    "ABHYAS_LLM_API_KEY": "GROQ_API_KEY",
    "ABHYAS_DEEPGRAM_API_KEY": "DEEPGRAM_API",
}


def load_env_file():
    """Pull the two optional API keys out of a .env if there is one."""
    filled = []
    for candidate in (ROOT / ".env", PARENT / ".env"):
        if not candidate.exists():
            continue
        found = {}
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip().strip('"').strip("'")
        for target, source in ENV_ALIASES.items():
            if not os.environ.get(target) and found.get(source):
                os.environ[target] = found[source]
                filled.append(target)
        break
    return filled


load_env_file()

OSM_FILE = NET_SRC / "indiranagar_bbox.osm.xml"
WIDE_NET = BUILD / "indiranagar_wide.net.xml"
JUNCTION_NET = BUILD / "junction.net.xml"
JOIN_NODES = BUILD / "join.nod.xml"

# Every process gets its own route/config files. The validation fleet runs
# several SUMO processes at once and they were happily reading each other's
# half written files. The network itself is read only so it can be shared.
# Also bin anything older than a day on the way in, this folder used to grow
# forever.
_now = time.time()
for _stale in RUNS.iterdir():
    if _now - _stale.stat().st_mtime > 86400:
        _stale.unlink(missing_ok=True)

_TAG = str(os.getpid())
VTYPES_FILE = RUNS / ("vtypes-" + _TAG + ".add.xml")
ROUTES_FILE = RUNS / ("junction-" + _TAG + ".rou.xml")
SUMO_CFG = RUNS / ("junction-" + _TAG + ".sumocfg")


def _newest(folder, pattern, fallback):
    matches = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else folder / fallback


# The travel time archive and the incident archive. Found by pattern because
# these are manual downloads of a Google Sheet and the browser names them
# after whatever the sheet is called today.
ARCHIVE_XLSX = _newest(DATA, "TomTom*.xlsx", "TomTom Traffic Data Sheet.xlsx")
INCIDENTS_XLSX = _newest(DATA, "*Traffic Incidents*.xlsx",
                         "Abhyas - Traffic Incidents.xlsx")


def use_files(travel=None, incidents=None):
    """Point the archive at a specific pair of spreadsheets (the CLI does this)."""
    global ARCHIVE_XLSX, INCIDENTS_XLSX
    if travel:
        ARCHIVE_XLSX = Path(travel).expanduser().resolve()
    if incidents:
        INCIDENTS_XLSX = Path(incidents).expanduser().resolve()
    return {"travel": ARCHIVE_XLSX, "incidents": INCIDENTS_XLSX}


# Sheet name prefixes. "sep " is the first collection run, "final " is
# everything collected after the corridor endpoints got fixed. If any final
# sheet exists it wins outright, the older ones measure SN over 892 m because
# the route went round the median.
# (Google Sheet writes them as "[final] ", Excel strips the brackets on export,
# so we compare against the stripped version.)
ARCHIVE_SEPARATE_PREFIX = "sep "
ARCHIVE_FINAL_PREFIX = "final "


# ---- SUMO ----------------------------------------------------------------
# There are usually two SUMO installs on a dev box, the system one and the one
# pip put in the venv. Mixing binaries and python bindings from different
# versions gives you a simulation that ignores your config and looks fine.
# So pin everything to the venv copy.

def _venv_sumo_home():
    """Find wherever pip put the eclipse-sumo data package.

    Guessing site-packages off sys.prefix breaks the moment pip installs to
    the user site instead (no active venv, no write access to the system
    prefix - exactly what a shared container gives you). find_spec walks the
    real sys.path, so it finds the package wherever it actually landed.
    """
    import importlib.util
    spec = importlib.util.find_spec("sumo")
    if spec and spec.submodule_search_locations:
        candidate = Path(list(spec.submodule_search_locations)[0])
        if candidate.exists():
            return candidate
    return None


SUMO_HOME = _venv_sumo_home()
if SUMO_HOME is None and os.environ.get("SUMO_HOME"):
    SUMO_HOME = Path(os.environ["SUMO_HOME"])
if SUMO_HOME is None:
    raise RuntimeError("No SUMO found. pip install eclipse-sumo, or set SUMO_HOME.")

os.environ["SUMO_HOME"] = str(SUMO_HOME)
_TOOLS = SUMO_HOME / "tools"
if _TOOLS.exists() and str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

_EXE = ".exe" if os.name == "nt" else ""
SUMO_BIN = SUMO_HOME / "bin" / ("sumo" + _EXE)
SUMO_GUI_BIN = SUMO_HOME / "bin" / ("sumo-gui" + _EXE)
NETCONVERT_BIN = SUMO_HOME / "bin" / ("netconvert" + _EXE)
TYPEMAP = SUMO_HOME / "data" / "typemap" / "osmNetconvert.typ.xml"


# ---- the junction --------------------------------------------------------

JUNCTION_ID = "CMH_100FT"
JUNCTION_NAME = "CMH Road x 100 Feet Road, Indiranagar"
JUNCTION_KEY = "indiranagar_cmh"        # what the `junction` column says

# OSM splits this one crossroads into four nodes and netconvert won't join
# them by itself (a bus stop sits on a connecting edge), so we name them.
CLUSTER_NODES = ["11187974841", "11494414328", "11494414329", "11494414330"]

JUNCTION_LAT = 12.978381
JUNCTION_LON = 77.640866

# how far out each arm goes from the stop line. archive corridors are ~200 m
# either side so 320 covers them
ARM_RADIUS_M = 320.0

# India drives on the left. Miss this and every turn is mirrored, silently.
LEFT_HAND_TRAFFIC = True


# ---- movements -----------------------------------------------------------
# The four through movements. This is all the interface will talk about.

MOVEMENTS = {
    "NS": {"label": "Northbound-to-south (N->S)", "approach": "N", "heading": "south"},
    "SN": {"label": "Southbound-to-north (S->N)", "approach": "S", "heading": "north"},
    "EW": {"label": "Eastbound-to-west (E->W)", "approach": "E", "heading": "west"},
    "WE": {"label": "Westbound-to-east (W->E)", "approach": "W", "heading": "east"},
}

# The eight turns. Measured, because the archive measures all twelve corridors,
# but not offered as controls - there's no dial that moves a turn on its own.
_ARM_WORD = {"N": "North", "S": "South", "E": "East", "W": "West"}
_ARM_DIRECTION = {"N": "north", "S": "south", "E": "east", "W": "west"}

TURN_MOVEMENTS = {}
for _a in "NSEW":
    for _b in "NSEW":
        if _a == _b or (_a + _b) in MOVEMENTS:
            continue
        TURN_MOVEMENTS[_a + _b] = {
            "label": (_ARM_WORD[_a] + "bound-to-" + _ARM_DIRECTION[_b]
                      + " (" + _a + "->" + _b + ")"),
            "approach": _a,
            "heading": _ARM_DIRECTION[_b],
        }

ALL_MOVEMENTS = dict(MOVEMENTS)
ALL_MOVEMENTS.update(TURN_MOVEMENTS)

# Where TomTom actually measures. `in` sits ~200 m before the junction on the
# approach, `out` ~200 m after it on the far side.
# N and W `out` are deliberately off the centreline - on the centreline they
# snapped to the opposite carriageway and TomTom drove past, u-turned at the
# next median gap and came back. These ones are on the right carriageway.
ARCHIVE_ARM_POINTS = {
    "N": {"in": (12.9802403, 77.6410403), "out": (12.98024, 77.64070)},
    "S": {"in": (12.9766431, 77.6408557), "out": (12.9766431, 77.6410403)},
    "E": {"in": (12.9783518, 77.6427938), "out": (12.9785316, 77.6427938)},
    "W": {"in": (12.9785316, 77.6391022), "out": (12.97824, 77.63910)},
}

ARCHIVE_ENDPOINTS = {}
for _m in ALL_MOVEMENTS:
    ARCHIVE_ENDPOINTS[_m] = [ARCHIVE_ARM_POINTS[_m[0]]["in"],
                             ARCHIVE_ARM_POINTS[_m[1]]["out"]]


# Two shapes of signal plan. Which one this junction really runs is a question
# about Bengaluru, so both are declared and the phase-plan agent asks the data.
#
# two_phase: opposing arms green together, right turns have to find a gap.
# four_phase: one arm at a time, every turn off it is protected.
PHASE_PLANS = {
    "two_phase": {
        "label": "Two-phase (opposing pairs)",
        "note": "North and south share a green; east and west share the other. "
                "Right turns are permissive and must yield to the opposing "
                "through movement.",
        "stages": [
            {"key": "north_south", "label": "North-south green",
             "arms": ["N", "S"], "movements": ["NS", "SN"],
             "timing": {"green": 42, "yellow": 4, "allred": 2}},
            {"key": "east_west", "label": "East-west green",
             "arms": ["E", "W"], "movements": ["EW", "WE"],
             "timing": {"green": 34, "yellow": 4, "allred": 2}},
        ],
    },
    "four_phase": {
        "label": "Four-phase (one approach at a time)",
        "note": "Each approach gets its own green, so its right turn is "
                "protected rather than filtering through gaps.",
        "stages": [
            {"key": "north", "label": "North approach green", "arms": ["N"],
             "timing": {"green": 22, "yellow": 4, "allred": 2}},
            {"key": "east", "label": "East approach green", "arms": ["E"],
             "timing": {"green": 16, "yellow": 4, "allred": 2}},
            {"key": "south", "label": "South approach green", "arms": ["S"],
             "timing": {"green": 22, "yellow": 4, "allred": 2}},
            {"key": "west", "label": "West approach green", "arms": ["W"],
             "timing": {"green": 16, "yellow": 4, "allred": 2}},
        ],
    },
}

# four_phase by default. Under two_phase a right turner sits in lane 1 waiting
# for a gap and stops the through traffic behind it - over 12 seeds that
# gridlocked 2 runs outright and left 2 more with no completed through
# movement. four_phase runs at a 12% coefficient of variation instead of 51%.
DEFAULT_PHASE_PLAN = "four_phase"
ACTIVE_PHASE_PLAN = os.environ.get("ABHYAS_PHASE_PLAN", DEFAULT_PHASE_PLAN)
if ACTIVE_PHASE_PLAN not in PHASE_PLANS:
    ACTIVE_PHASE_PLAN = DEFAULT_PHASE_PLAN


def phase_stages(shape=None):
    return PHASE_PLANS[shape or ACTIVE_PHASE_PLAN]["stages"]


def phase_groups(shape=None):
    """stage key -> label, arms, and the movements it lets go."""
    groups = {}
    for stage in phase_stages(shape):
        movements = stage.get("movements")
        if movements is None:
            # derive them, writing the turn geometry out by hand twice is asking
            # for one of the two copies to be wrong
            movements = [m for m in ALL_MOVEMENTS if m[0] in stage["arms"]]
        groups[stage["key"]] = {"label": stage["label"], "arms": stage["arms"],
                                "movements": movements}
    return groups


def arm_to_phase(shape=None):
    out = {}
    for stage in phase_stages(shape):
        for arm in stage["arms"]:
            out[arm] = stage["key"]
    return out


def baseline_timings(shape=None):
    return {stage["key"]: dict(stage["timing"]) for stage in phase_stages(shape)}


PHASE_GROUPS = phase_groups()
ARM_TO_PHASE = arm_to_phase()
BASELINE_PLAN = baseline_timings()
