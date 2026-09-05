# Named versions of the control state, kept as a tree.
#
# A tree and not a list because branching off one starting point to try two
# what-ifs is the normal shape of this work. Not git though - no merge, no
# conflict resolution, and a checkout just overwrites the live state.

import json
import time
import uuid

from . import config as C
from . import controls as K

STORE = C.RESULTS / "versions.json"
MAX_VERSIONS = 200
PROJECT = "Indiranagar CMH"

# Named starting points, so a demo is one click and not six dials set by hand
# while everyone watches. A preset is a set of control edits and nothing more -
# it goes through K.lookup and the same clamp every dial does, and the caution
# rides along with it so the label can't travel without the caveat.
PRESETS = [
    {"id": "baseline", "label": "Calibrated baseline",
     "note": "Every scenario lever at its neutral value. The model the "
             "archive was validated against.",
     "exploratory": False, "edits": []},
    {"id": "ban_2w_east", "label": "No two-wheelers on CMH east",
     "note": "Access restriction on one arm. That demand is turned away and "
             "counted, because one junction has no other arm for it to "
             "arrive on.",
     "exploratory": True,
     "edits": [{"id": "access.twowheeler.E", "value": True}]},
    {"id": "ebus_240", "label": "240 more buses an hour",
     "note": "Added on top of the calibrated flow, the way buying vehicles "
             "works. Says what those buses do to THIS junction, not what a "
             "6,000-bus scheme does to the city. 'Electric' is not modelled.",
     "exploratory": True,
     "edits": [{"id": "fleet.injected.bus", "value": 240.0}]},
    {"id": "shift_20", "label": "20% shift to transit",
     "note": "A fifth of car and two-wheeler trips replaced by bus trips at "
             "declared occupancies. Read the people-moved line, not just the "
             "vehicle count.",
     "exploratory": True,
     "edits": [{"id": "fleet.mode_shift", "value": 0.20}]},
    {"id": "hmv_uncontrolled", "label": "Heavy vehicles, uncontrolled",
     "note": "Same demand, same signal, only the conduct changes - which is "
             "the whole argument. New behavioural ground, not a citation: run "
             "it against baseline and quote the pair.",
     "exploratory": True,
     "edits": [{"id": "fleet.hmv_discipline", "value": 1.0},
               {"id": "fleet.hmv_stop_rate", "value": 0.25}]},
]


def presets():
    """The library, with each preset's edits checked against the live control
    surface. A preset that names a control this build doesn't have is reported
    as broken rather than half-applied."""
    known = K.all_controls()
    out = []
    for preset in PRESETS:
        missing = [e["id"] for e in preset["edits"] if e["id"] not in known]
        entry = dict(preset)
        entry["missing_controls"] = missing
        entry["usable"] = not missing
        out.append(entry)
    return {"presets": out, "exploratory_note": K.EXPLORATORY_NOTE}


def preset(preset_id):
    for entry in presets()["presets"]:
        if entry["id"] == preset_id:
            if not entry["usable"]:
                raise KeyError("Preset '" + preset_id + "' names controls this "
                               "build doesn't have: "
                               + ", ".join(entry["missing_controls"]))
            return entry
    raise KeyError("No preset called '" + str(preset_id) + "'")


def preset_edits(preset_id):
    """A preset is a state, not a nudge.

    Every exploratory control goes back to its baseline value first, so
    clicking two presets in a row gives you the second one and not both at
    once. Signal and demand are left alone: a preset says what is on the road,
    it doesn't quietly retime the junction under you.
    """
    entry = preset(preset_id)
    baseline = K.baseline_state()
    edits = [{"id": cid, "value": baseline[cid]}
             for cid, control in sorted(K.all_controls().items())
             if control["group"] in K.EXPLORATORY_GROUPS and cid in baseline]
    wanted = {e["id"]: e["value"] for e in entry["edits"]}
    for edit in edits:
        if edit["id"] in wanted:
            edit["value"] = wanted.pop(edit["id"])
    edits.extend({"id": cid, "value": value} for cid, value in wanted.items())
    return {"preset": entry, "edits": edits}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class VersionStore:
    """The versions.json file, loaded and written back."""

    def __init__(self, path=None):
        self.path = path or STORE
        self.data = self._read()

    def _blank(self):
        return {"project": PROJECT, "created": now(), "versions": [], "head": None}

    def _read(self):
        if not self.path.exists():
            return self._blank()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # move the broken one aside rather than losing the ability to save
            broken = self.path.with_suffix(".broken.json")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            data = self._blank()
            data["warning"] = ("The version store couldn't be read and was moved "
                               "to " + broken.name + ". History restarts empty.")
        data.setdefault("versions", [])
        data.setdefault("head", None)
        data.setdefault("project", PROJECT)
        return data

    def save(self):
        self.data["versions"] = self.data["versions"][-MAX_VERSIONS:]
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    # -- reading -----------------------------------------------------------

    @property
    def versions(self):
        return self.data["versions"]

    @property
    def head(self):
        return self.data["head"]

    def find(self, version_id):
        for version in self.versions:
            if version["id"] == version_id:
                return version
        return None

    def need(self, version_id):
        version = self.find(version_id)
        if version is None:
            raise KeyError("No version " + str(version_id))
        return version

    def children_of(self, version_id):
        return [v for v in self.versions if v["parent"] == version_id]

    def depth_of(self, version):
        by_id = {v["id"]: v for v in self.versions}
        depth, seen, cursor = 0, set(), version["parent"]
        while cursor and cursor in by_id and cursor not in seen:
            seen.add(cursor)
            depth += 1
            cursor = by_id[cursor]["parent"]
        return depth

    # -- writing -----------------------------------------------------------

    def commit(self, state, message, metrics=None, source="manual"):
        """Record the state and move the head. Parent is the head, not the last
        entry, so two what-ifs off one point both point back at it."""
        parent = self.find(self.head) if self.head else None
        version = {
            "id": uuid.uuid4().hex[:12],
            "parent": self.head,
            "message": (message or "").strip() or "unnamed",
            "created": now(),
            "source": source,
            "state": dict(state),
            "metrics": metrics,
            "changes_from_parent": K.diff(parent["state"], state) if parent else [],
            "changes_from_baseline": K.diff(K.baseline_state(), state),
        }
        self.versions.append(version)
        self.data["head"] = version["id"]
        self.save()
        return version

    def checkout(self, version_id):
        version = self.need(version_id)
        self.data["head"] = version_id
        self.save()
        return version

    def rename(self, version_id, message):
        version = self.need(version_id)
        version["message"] = (message or "").strip() or version["message"]
        self.save()
        return version

    def drop(self, version_id):
        """Delete one node and re-parent its children onto the grandparent, so
        the branch below still has a truthful chain."""
        version = self.need(version_id)
        parent = self.find(version["parent"]) if version["parent"] else None
        for child in self.children_of(version_id):
            child["parent"] = version["parent"]
            child["changes_from_parent"] = (K.diff(parent["state"], child["state"])
                                            if parent else [])
        self.data["versions"] = [v for v in self.versions if v["id"] != version_id]
        if self.head == version_id:
            self.data["head"] = version["parent"]
        self.save()
        return self.data

    def compare(self, left_id, right_id):
        left, right = self.find(left_id), self.find(right_id)
        if left is None or right is None:
            raise KeyError("Both versions must exist to compare them.")
        changes = K.diff(left["state"], right["state"])
        return {"left": {"id": left["id"], "message": left["message"]},
                "right": {"id": right["id"], "message": right["message"]},
                "changes": changes, "summary": K.describe_changes(changes)}

    def tree(self):
        """The store plus depth and child counts, ready to draw."""
        by_id = {v["id"]: v for v in self.versions}
        listed = []
        for version in self.versions:
            entry = {k: v for k, v in version.items() if k != "state"}
            entry["depth"] = self.depth_of(version)
            entry["children"] = len(self.children_of(version["id"]))
            # a root has no parent to differ from so it describes itself against
            # the baseline. "nothing changed" on the first commit is just wrong.
            if version["parent"] and version["parent"] in by_id:
                entry["summary"] = K.describe_changes(version["changes_from_parent"])
            else:
                changes = version["changes_from_baseline"]
                entry["summary"] = ("Same as baseline." if not changes
                                    else "From baseline: " + K.describe_changes(changes))
            listed.append(entry)
        return {"project": self.data.get("project"), "head": self.head,
                "warning": self.data.get("warning"), "versions": listed}


# module level shorthands, the server calls these
def load():
    return VersionStore().data


def commit(state, message, metrics=None, source="manual"):
    return VersionStore().commit(state, message, metrics=metrics, source=source)


def checkout(version_id):
    return VersionStore().checkout(version_id)


def rename(version_id, message):
    return VersionStore().rename(version_id, message)


def drop(version_id):
    return VersionStore().drop(version_id)


def compare(left_id, right_id):
    return VersionStore().compare(left_id, right_id)


def tree():
    return VersionStore().tree()


if __name__ == "__main__":
    print(json.dumps(tree(), indent=2)[:1200])
