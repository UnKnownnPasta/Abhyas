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
