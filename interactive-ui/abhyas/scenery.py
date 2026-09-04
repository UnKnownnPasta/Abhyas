# Everything around the junction that we draw but never simulate.
#
# netbuild deliberately crops the model down to one junction, which is right
# for the simulation and makes the stage look like a diagram floating in the
# dark. The neighbourhood it threw away is still on disk - pass 2 leaves the
# whole wide network behind, and the OSM download has ~900 building footprints
# in it - so we can put the junction back in its street without simulating a
# single extra vehicle.
#
# None of this is stepped, routed or measured. It is scenery.

import json
import xml.etree.ElementTree as ET

import sumolib

from . import config as C

CACHE = C.BUILD / "scenery.json"

# a floor for buildings with nothing useful tagged, and what one storey is
DEFAULT_HEIGHT_M = 9.0
STOREY_M = 3.2

# how far out from the junction to keep scenery, metres. The download reaches
# about 700 m; past that it is off screen at any sane zoom and just weight.
RADIUS_M = 700.0


class Scenery:
    """Context geometry in the same XY frame the simulation uses.

    Both networks come out of netconvert with the same netOffset and
    projection, so wide-net coordinates drop straight onto junction-net
    coordinates with no transform. Worth stating because if that ever stops
    being true the scenery slides off the junction and it will look like a
    rendering bug rather than a projection one.
    """

    def __init__(self, net=None):
        self.net = net or sumolib.net.readNet(str(C.JUNCTION_NET))
        self.jx, self.jy = self.net.getNode(C.JUNCTION_ID).getCoord()

    # -- roads we are not simulating ---------------------------------------

    def roads(self):
        """Lane shapes from the wide network, before the crop threw them away."""
        if not C.WIDE_NET.exists():
            return []
        wide = sumolib.net.readNet(str(C.WIDE_NET))
        out = []
        for edge in wide.getEdges():
            if edge.getFunction() == "internal":
                continue
            for lane in edge.getLanes():
                shape = [(round(x, 2), round(y, 2)) for x, y in lane.getShape()]
                if len(shape) < 2 or not self._near(shape):
                    continue
                out.append({"shape": shape, "width": round(lane.getWidth(), 2)})
        return out

    def junction_shapes(self):
        """The other junctions' polygons, so intersections read as intersections."""
        if not C.WIDE_NET.exists():
            return []
        wide = sumolib.net.readNet(str(C.WIDE_NET))
        out = []
        for node in wide.getNodes():
            if node.getID() == C.JUNCTION_ID:
                continue                      # ours is drawn properly elsewhere
            shape = [(round(x, 2), round(y, 2)) for x, y in node.getShape()]
            if len(shape) < 3 or not self._near(shape):
                continue
            out.append({"shape": shape})
        return out

    # -- buildings ---------------------------------------------------------

    def buildings(self):
        """OSM footprints, projected into network XY and given a height."""
        if not C.OSM_FILE.exists():
            return []
        root = ET.parse(str(C.OSM_FILE)).getroot()

        nodes = {}
        for node in root.findall("node"):
            lat, lon = node.get("lat"), node.get("lon")
            if lat and lon:
                nodes[node.get("id")] = (float(lat), float(lon))

        out = []
        for way in root.findall("way"):
            tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
            if "building" not in tags and "building:part" not in tags:
                continue
            shape = []
            for ref in way.findall("nd"):
                pos = nodes.get(ref.get("ref"))
                if pos is None:
                    continue
                x, y = self.net.convertLonLat2XY(pos[1], pos[0])
                shape.append((round(x, 2), round(y, 2)))
            # a closed way repeats its first node; the renderer closes it itself
            if len(shape) > 2 and shape[0] == shape[-1]:
                shape = shape[:-1]
            if len(shape) < 3 or not self._near(shape):
                continue
            out.append({"shape": shape, "height": self._height(tags)})
        return out

    @staticmethod
    def _height(tags):
        for key, scale in (("height", 1.0), ("building:levels", STOREY_M)):
            raw = tags.get(key)
            if not raw:
                continue
            try:
                # tags arrive as "12", "12 m", "3.5" and occasionally worse
                return round(float(str(raw).split()[0].replace("m", "")) * scale, 1)
            except (ValueError, IndexError):
                continue
        return DEFAULT_HEIGHT_M

    def _near(self, shape):
        """Keep anything with a point inside the radius. Cheap and good enough -
        a building straddling the edge is drawn whole rather than clipped."""
        for x, y in shape:
            if abs(x - self.jx) <= RADIUS_M and abs(y - self.jy) <= RADIUS_M:
                return True
        return False

    # -- the payload -------------------------------------------------------

    def to_dict(self):
        roads = self.roads()
        buildings = self.buildings()
        return {
            "roads": roads,
            "junctions": self.junction_shapes(),
            "buildings": buildings,
            "junction_xy": [round(self.jx, 2), round(self.jy, 2)],
            "radius_m": RADIUS_M,
            "counts": {"roads": len(roads), "buildings": len(buildings)},
        }

    @classmethod
    def load(cls, force=False):
        """Cached - it is static for a given build and takes a second to make."""
        if CACHE.exists() and not force:
            try:
                return json.loads(CACHE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass                          # rebuild it, no drama
        data = cls().to_dict()
        CACHE.write_text(json.dumps(data), encoding="utf-8")
        return data


if __name__ == "__main__":
    data = Scenery.load(force=True)
    print(json.dumps(data["counts"], indent=2))
    print("cached to", CACHE, round(CACHE.stat().st_size / 1e6, 2), "MB")
