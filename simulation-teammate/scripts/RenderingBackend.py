"""3D SUMO network viewer (pyray / raylib).

Fly around in first person. Road geometry is streamed in 256 m chunks.
"""

from __future__ import annotations

import gzip
import math
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

import pyray as rl
from raylib import ffi

# ---------------------------------------------------------------------------
# Put the path of any SUMO .net.xml file here (absolute or relative).
# ---------------------------------------------------------------------------
NET_XML_PATH = r"osm.net.xml"

CHUNK_SIZE = 256.0
LOAD_RADIUS = 2
UNLOAD_RADIUS = 3
DEFAULT_LANE_WIDTH = 3.2
GROUND_Y = 0.0
JUNCTION_Y = 0.06
LANE_Y = 0.10
NEAR_PLANE = 0.2
FAR_PLANE = 5000.0
WINDOW_W = 1280
WINDOW_H = 720
MOVE_SPEED = 28.0
SPRINT_MULT = 2.6
MOUSE_SENS = 0.0024
PITCH_LIMIT = math.radians(89.0)

TYPE_COLORS = {
    "highway.primary": (92, 92, 98),
    "highway.secondary": (88, 88, 90),
    "highway.tertiary": (96, 96, 96),
    "highway.residential": (72, 72, 74),
    "highway.service": (64, 64, 60),
}
JUNCTION_COLOR = (78, 78, 80)
PEDESTRIAN_COLOR = (92, 86, 70)
DEFAULT_ROAD_COLOR = (82, 82, 84)
GROUND_COLOR = (42, 52, 42, 255)


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _project_root() -> str:
    return os.path.dirname(_script_dir())


def resolve_net_path(path: str) -> str:
    candidates_to_try = [path, path + ".gz"] if not path.endswith(".gz") else [path]
    for p in candidates_to_try:
        if os.path.isabs(p) and os.path.isfile(p):
            return os.path.abspath(p)
        for base in (os.getcwd(), _project_root(), _script_dir()):
            candidate = os.path.join(base, p)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return os.path.abspath(os.path.join(_project_root(), path))


def _parse_shape(shape: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not shape or not shape.strip():
        return points
    parts = shape.strip().split()
    for part in parts:
        if "," not in part:
            continue
        xs, ys = part.split(",", 1)
        points.append((float(xs), float(ys)))
    return points


def _parse_boundary(text: str) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = (float(v) for v in text.split(","))
    return xmin, ymin, xmax, ymax


def parse_net(path: str) -> dict:
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            tree = ET.parse(f)
    else:
        tree = ET.parse(path)
    root = tree.getroot()

    location = root.find("location")
    if location is None or not location.get("convBoundary"):
        raise ValueError(f"No <location convBoundary> in {path}")
    xmin, ymin, xmax, ymax = _parse_boundary(location.get("convBoundary"))
    cx = (xmin + xmax) * 0.5
    cy = (ymin + ymax) * 0.5

    lanes = []
    for edge in root.findall("edge"):
        if edge.get("function") == "internal":
            continue
        edge_type = edge.get("type") or ""
        for lane in edge.findall("lane"):
            pts = _parse_shape(lane.get("shape") or "")
            if len(pts) < 2:
                continue
            width = float(lane.get("width", DEFAULT_LANE_WIDTH))
            allow = lane.get("allow") or ""
            lanes.append(
                {
                    "points": pts,
                    "width": width,
                    "type": edge_type,
                    "allow": allow,
                }
            )

    junctions = []
    for junction in root.findall("junction"):
        pts = _parse_shape(junction.get("shape") or "")
        unique = []
        for p in pts:
            if not unique or (abs(unique[-1][0] - p[0]) > 1e-4 or abs(unique[-1][1] - p[1]) > 1e-4):
                unique.append(p)
        if len(unique) >= 3 and unique[0] == unique[-1]:
            unique = unique[:-1]
        if len(unique) < 3:
            continue
        junctions.append({"points": unique})

    return {
        "cx": cx,
        "cy": cy,
        "bounds": (xmin, ymin, xmax, ymax),
        "lanes": lanes,
        "junctions": junctions,
    }


def sumo_to_world(x: float, y: float, cx: float, cy: float) -> tuple[float, float]:
    return x - cx, cy - y


def _chunk_key(wx: float, wz: float) -> tuple[int, int]:
    return int(math.floor(wx / CHUNK_SIZE)), int(math.floor(wz / CHUNK_SIZE))


def _lane_color(lane: dict) -> tuple[int, int, int]:
    allow = lane["allow"]
    if allow and "passenger" not in allow and "delivery" not in allow:
        if "pedestrian" in allow or "bicycle" in allow:
            return PEDESTRIAN_COLOR
    return TYPE_COLORS.get(lane["type"], DEFAULT_ROAD_COLOR)


def _perp_xz(dx: float, dz: float) -> tuple[float, float]:
    length = math.hypot(dx, dz)
    if length < 1e-9:
        return 0.0, 0.0
    return -dz / length, dx / length


def _dedupe_polyline(xz: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in xz:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-4:
            out.append(p)
    return out


def _miter_offsets(
    xz: list[tuple[float, float]], half_w: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Shared left/right vertices so consecutive segments do not overlap."""
    pts = _dedupe_polyline(xz)
    n = len(pts)
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    if n < 2:
        return left, right

    perps: list[tuple[float, float]] = []
    for i in range(n - 1):
        perps.append(_perp_xz(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))

    for i in range(n):
        if i == 0:
            nx, nz = perps[0]
            scale = 1.0
        elif i == n - 1:
            nx, nz = perps[-1]
            scale = 1.0
        else:
            n1x, n1z = perps[i - 1]
            n2x, n2z = perps[i]
            mx, mz = n1x + n2x, n1z + n2z
            length = math.hypot(mx, mz)
            if length < 1e-8:
                nx, nz = n2x, n2z
                scale = 1.0
            else:
                nx, nz = mx / length, mz / length
                denom = nx * n2x + nz * n2z
                scale = 1.0 / max(0.45, abs(denom))
        ox, oz = nx * half_w * scale, nz * half_w * scale
        x, z = pts[i]
        left.append((x + ox, z + oz))
        right.append((x - ox, z - oz))
    return left, right


def _ribbon_tris(
    xz: list[tuple[float, float]],
    half_w: float,
    color: tuple[int, int, int],
    y: float = LANE_Y,
) -> list[tuple]:
    left, right = _miter_offsets(xz, half_w)
    tris = []
    for i in range(len(left) - 1):
        l1 = (left[i][0], y, left[i][1])
        r1 = (right[i][0], y, right[i][1])
        l2 = (left[i + 1][0], y, left[i + 1][1])
        r2 = (right[i + 1][0], y, right[i + 1][1])
        tris.append((l1, l2, r1, color))
        tris.append((r1, l2, r2, color))
    return tris


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _normal(p1, p2, p3):
    n = _cross(_sub(p2, p1), _sub(p3, p1))
    length = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
    if length < 1e-12:
        return (0.0, 1.0, 0.0)
    return (n[0] / length, n[1] / length, n[2] / length)


def _signed_area_xz(pts: list[tuple[float, float]]) -> float:
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, z1 = pts[i]
        x2, z2 = pts[(i + 1) % n]
        area += x1 * z2 - x2 * z1
    return 0.5 * area


def _point_in_tri_xz(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1 = sign(p, a, b)
    d2 = sign(p, b, c)
    d3 = sign(p, c, a)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _earclip_xz(pts: list[tuple[float, float]]) -> list[tuple]:
    poly = _dedupe_polyline(pts)
    if len(poly) >= 3 and poly[0] == poly[-1]:
        poly = poly[:-1]
    if len(poly) < 3:
        return []
    if _signed_area_xz(poly) < 0:
        poly = list(reversed(poly))

    ears: list[tuple] = []
    remaining = list(poly)
    guard = 0
    while len(remaining) > 3 and guard < 4096:
        guard += 1
        n = len(remaining)
        found = False
        for i in range(n):
            prev = remaining[(i - 1) % n]
            curr = remaining[i]
            nxt = remaining[(i + 1) % n]
            cross = (curr[0] - prev[0]) * (nxt[1] - curr[1]) - (curr[1] - prev[1]) * (
                nxt[0] - curr[0]
            )
            if cross <= 1e-10:
                continue
            occupied = False
            for j, q in enumerate(remaining):
                if j in ((i - 1) % n, i, (i + 1) % n):
                    continue
                if _point_in_tri_xz(q, prev, curr, nxt):
                    occupied = True
                    break
            if occupied:
                continue
            ears.append((prev, curr, nxt))
            del remaining[i]
            found = True
            break
        if not found:
            break
    if len(remaining) >= 3:
        origin = remaining[0]
        for i in range(1, len(remaining) - 1):
            ears.append((origin, remaining[i], remaining[i + 1]))
    return ears


def _poly_tris(
    xz: list[tuple[float, float]], color: tuple[int, int, int], y: float
) -> list[tuple]:
    tris = []
    for a, b, c in _earclip_xz(xz):
        p1 = (a[0], y, a[1])
        p2 = (b[0], y, b[1])
        p3 = (c[0], y, c[1])
        if _normal(p1, p2, p3)[1] < 0:
            p2, p3 = p3, p2
        tris.append((p1, p2, p3, color))
    return tris


def bin_into_chunks(net: dict) -> dict[tuple[int, int], list[tuple]]:
    cx, cy = net["cx"], net["cy"]
    chunks: dict[tuple[int, int], list[tuple]] = defaultdict(list)

    def _bin_tri(tri: tuple) -> None:
        p1, p2, p3, _color = tri
        mx = (p1[0] + p2[0] + p3[0]) / 3.0
        mz = (p1[2] + p2[2] + p3[2]) / 3.0
        chunks[_chunk_key(mx, mz)].append(tri)

    for lane in net["lanes"]:
        world = [sumo_to_world(x, y, cx, cy) for x, y in lane["points"]]
        for tri in _ribbon_tris(world, lane["width"] * 0.5, _lane_color(lane), LANE_Y):
            _bin_tri(tri)

    for junction in net["junctions"]:
        world = [sumo_to_world(x, y, cx, cy) for x, y in junction["points"]]
        for tri in _poly_tris(world, JUNCTION_COLOR, JUNCTION_Y):
            _bin_tri(tri)

    return dict(chunks)


def _alloc_floats(values: list[float]):
    n = len(values)
    ptr = rl.mem_alloc(n * 4)
    src = ffi.new("float[]", values)
    ffi.memmove(ptr, src, n * 4)
    return ffi.cast("float *", ptr)


def _alloc_ubytes(values: list[int]):
    n = len(values)
    ptr = rl.mem_alloc(n)
    src = ffi.new("unsigned char[]", values)
    ffi.memmove(ptr, src, n)
    return ffi.cast("unsigned char *", ptr)


def mesh_from_tris(tris: list[tuple]) -> rl.Mesh | None:
    if not tris:
        return None
    vertices: list[float] = []
    normals: list[float] = []
    colors: list[int] = []
    for p1, p2, p3, color in tris:
        n = _normal(p1, p2, p3)
        for p in (p1, p2, p3):
            vertices.extend((p[0], p[1], p[2]))
            normals.extend(n)
            colors.extend((color[0], color[1], color[2], 255))

    mesh = rl.Mesh()
    mesh.vertexCount = len(vertices) // 3
    mesh.triangleCount = mesh.vertexCount // 3
    mesh.vertices = _alloc_floats(vertices)
    mesh.normals = _alloc_floats(normals)
    mesh.colors = _alloc_ubytes(colors)
    mesh.texcoords = ffi.NULL
    mesh.indices = ffi.NULL
    rl.upload_mesh(mesh, False)
    return mesh


class ChunkStreamer:
    def __init__(self, chunk_tris: dict[tuple[int, int], list[tuple]]):
        self.chunk_tris = chunk_tris
        self.loaded: dict[tuple[int, int], rl.Model] = {}

    def wanted(self, cam_x: float, cam_z: float, radius: int) -> set[tuple[int, int]]:
        cx, cz = _chunk_key(cam_x, cam_z)
        keys = set()
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                key = (cx + dx, cz + dz)
                if key in self.chunk_tris:
                    keys.add(key)
        return keys

    def sync(self, cam_x: float, cam_z: float) -> None:
        keep = self.wanted(cam_x, cam_z, LOAD_RADIUS)
        unload_ok = self.wanted(cam_x, cam_z, UNLOAD_RADIUS)
        for key in list(self.loaded):
            if key not in unload_ok:
                rl.unload_model(self.loaded.pop(key))
        for key in keep:
            if key in self.loaded:
                continue
            mesh = mesh_from_tris(self.chunk_tris[key])
            if mesh is None:
                continue
            model = rl.load_model_from_mesh(mesh)
            self.loaded[key] = model

    def draw(self) -> None:
        tint = rl.Color(255, 255, 255, 255)
        origin = rl.Vector3(0, 0, 0)
        for model in self.loaded.values():
            rl.draw_model(model, origin, 1.0, tint)

    def unload_all(self) -> None:
        for model in self.loaded.values():
            rl.unload_model(model)
        self.loaded.clear()


class FlyCamera:
    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z
        self.yaw = 0.0
        self.pitch = -0.18
        self.mouse_look = True

    def look_dir(self) -> tuple[float, float, float]:
        cp = math.cos(self.pitch)
        return (
            math.sin(self.yaw) * cp,
            math.sin(self.pitch),
            math.cos(self.yaw) * cp,
        )

    def to_camera3d(self) -> rl.Camera3D:
        lx, ly, lz = self.look_dir()
        cam = rl.Camera3D()
        cam.position = rl.Vector3(self.x, self.y, self.z)
        cam.target = rl.Vector3(self.x + lx, self.y + ly, self.z + lz)
        cam.up = rl.Vector3(0, 1, 0)
        cam.fovy = 70.0
        cam.projection = rl.CAMERA_PERSPECTIVE
        return cam

    def update(self, dt: float) -> None:
        if rl.is_key_pressed(rl.KEY_ESCAPE):
            if self.mouse_look:
                rl.enable_cursor()
                self.mouse_look = False
            else:
                rl.disable_cursor()
                self.mouse_look = True
        elif not self.mouse_look and rl.is_mouse_button_pressed(rl.MOUSE_BUTTON_LEFT):
            rl.disable_cursor()
            self.mouse_look = True

        if self.mouse_look:
            delta = rl.get_mouse_delta()
            self.yaw -= delta.x * MOUSE_SENS
            self.pitch -= delta.y * MOUSE_SENS
            self.pitch = max(-PITCH_LIMIT, min(PITCH_LIMIT, self.pitch))

        speed = MOVE_SPEED * (SPRINT_MULT if rl.is_key_down(rl.KEY_LEFT_SHIFT) else 1.0)
        lx, ly, lz = self.look_dir()
        rx, rz = math.cos(self.yaw), -math.sin(self.yaw)

        move_x = move_y = move_z = 0.0
        if rl.is_key_down(rl.KEY_W):
            move_x += lx
            move_y += ly
            move_z += lz
        if rl.is_key_down(rl.KEY_S):
            move_x -= lx
            move_y -= ly
            move_z -= lz
        if rl.is_key_down(rl.KEY_A):
            move_x += rx
            move_z += rz
        if rl.is_key_down(rl.KEY_D):
            move_x -= rx
            move_z -= rz
        if rl.is_key_down(rl.KEY_SPACE):
            move_y += 1.0
        if rl.is_key_down(rl.KEY_LEFT_CONTROL):
            move_y -= 1.0

        mag = math.sqrt(move_x * move_x + move_y * move_y + move_z * move_z)
        if mag > 1e-8:
            inv = speed * dt / mag
            self.x += move_x * inv
            self.y += move_y * inv
            self.z += move_z * inv
            self.y = max(0.6, self.y)


def _sumo_heading_yaw(angle_deg: float) -> float:
    """SUMO 0=north, 90=east → yaw degrees around world +Y (0 looks +Z)."""
    rad = math.radians(angle_deg)
    fx = math.sin(rad)
    fz = -math.cos(rad)
    return math.degrees(math.atan2(fx, fz))


def _signal_rgb(state: str) -> tuple[int, int, int]:
    ch = (state or "o")[0].lower()
    if ch == "r":
        return (220, 40, 40)
    if ch in "yu":
        return (230, 190, 40)
    if ch == "g":
        return (40, 200, 70)
    return (50, 50, 50)


class RenderingBackend:
    """Reusable 3D view of a SUMO network, plus live vehicles and signals."""

    def __init__(
        self,
        net_path: str | None = None,
        width: int = WINDOW_W,
        height: int = WINDOW_H,
        title: str = "SUMO Network",
    ):
        self.net_path = resolve_net_path(net_path or NET_XML_PATH)
        self.width = width
        self.height = height
        self.title = title
        self.cx = 0.0
        self.cy = 0.0
        self.net: dict | None = None
        self.streamer: ChunkStreamer | None = None
        self.fly: FlyCamera | None = None
        self._open = False
        self._in_3d = False
        self.ground = rl.Color(*GROUND_COLOR)
        self.sky = rl.Color(135, 170, 200, 255)
        self.hud_extra = ""
        self.car_model: rl.Model | None = None
        self.car_model_size: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def to_world(self, sumo_x: float, sumo_y: float) -> tuple[float, float]:
        return sumo_to_world(sumo_x, sumo_y, self.cx, self.cy)

    def load_car_model(self) -> None:
        model_path = os.path.join(_project_root(), "assets", "low_poly_car", "scene.gltf")
        if not os.path.isfile(model_path):
            for base in (os.getcwd(), _project_root(), _script_dir()):
                candidate = os.path.join(base, "assets", "low_poly_car", "scene.gltf")
                if os.path.isfile(candidate):
                    model_path = candidate
                    break
        if os.path.isfile(model_path):
            try:
                self.car_model = rl.load_model(model_path)
                bbox = rl.get_model_bounding_box(self.car_model)
                dx = max(0.001, bbox.max.x - bbox.min.x)
                dy = max(0.001, bbox.max.y - bbox.min.y)
                dz = max(0.001, bbox.max.z - bbox.min.z)
                self.car_model_size = (dx, dy, dz)
            except Exception as e:
                print(f"Warning: Could not load 3D car model from {model_path}: {e}")
                self.car_model = None

    def load_network(self, net_path: str | None = None) -> None:
        path = resolve_net_path(net_path or self.net_path)
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Network file not found: {path}\nSet NET_XML_PATH at the top of RenderingBackend.py"
            )
        self.net_path = path
        self.net = parse_net(path)
        self.cx = self.net["cx"]
        self.cy = self.net["cy"]
        self.streamer = ChunkStreamer(bin_into_chunks(self.net))

    def open(self) -> None:
        if self._open:
            return
        if self.streamer is None:
            self.load_network()
        rl.set_config_flags(rl.FLAG_MSAA_4X_HINT | rl.FLAG_WINDOW_RESIZABLE)
        rl.init_window(self.width, self.height, self.title)
        rl.set_exit_key(rl.KEY_NULL)
        rl.rl_set_clip_planes(NEAR_PLANE, FAR_PLANE)
        rl.set_target_fps(60)
        rl.disable_cursor()
        self.load_car_model()
        self.fly = FlyCamera(0.0, 14.0, 28.0)
        self.streamer.sync(self.fly.x, self.fly.z)
        self._open = True

    def should_close(self) -> bool:
        if not self._open:
            return True
        if rl.window_should_close():
            return True
        if self.fly and rl.is_key_pressed(rl.KEY_Q) and not self.fly.mouse_look:
            return True
        return False

    def begin_frame(self) -> bool:
        """Camera + map. Call draw_vehicles / draw_signals, then end_frame."""
        if self.should_close():
            return False
        dt = rl.get_frame_time()
        self.fly.update(dt)
        self.streamer.sync(self.fly.x, self.fly.z)

        cam = self.fly.to_camera3d()
        rl.begin_drawing()
        rl.clear_background(self.sky)
        rl.begin_mode_3d(cam)
        self._in_3d = True
        rl.draw_plane(
            rl.Vector3(0.0, GROUND_Y, 0.0),
            rl.Vector2(CHUNK_SIZE * 12, CHUNK_SIZE * 12),
            self.ground,
        )
        self.streamer.draw()
        return True

    def draw_vehicles(self, vehicles: list[dict]) -> None:
        """Each vehicle: x, y (SUMO), angle (deg), length, width, height, optional color (r,g,b)."""
        for veh in vehicles:
            wx, wz = self.to_world(float(veh["x"]), float(veh["y"]))
            length = float(veh.get("length", 4.5))
            width = float(veh.get("width", 1.8))
            height = float(veh.get("height", 1.5))
            yaw = _sumo_heading_yaw(float(veh.get("angle", 0.0)))
            rgb = veh.get("color") or (40, 90, 200)
            color = rl.Color(int(rgb[0]), int(rgb[1]), int(rgb[2]), 255)

            if self.car_model is not None:
                mdx, mdy, mdz = self.car_model_size
                scale_x = width / mdx
                scale_y = height / mdy
                scale_z = length / mdz
                wy = LANE_Y + 0.02

                rl.rl_push_matrix()
                rl.rl_translatef(wx, wy, wz)
                rl.rl_rotatef(yaw, 0.0, 1.0, 0.0)
                rl.rl_scalef(scale_x, scale_y, scale_z)
                rl.draw_model(self.car_model, rl.Vector3(0.0, 0.0, 0.0), 1.0, rl.WHITE)
                rl.rl_pop_matrix()
            else:
                wy = LANE_Y + height * 0.5 + 0.02
                rl.rl_push_matrix()
                rl.rl_translatef(wx, wy, wz)
                rl.rl_rotatef(yaw, 0.0, 1.0, 0.0)
                rl.draw_cube(rl.Vector3(0.0, 0.0, 0.0), width, height, length, color)
                rl.draw_cube_wires(rl.Vector3(0.0, 0.0, 0.0), width, height, length, rl.Color(20, 20, 20, 255))
                rl.draw_cube(
                    rl.Vector3(0.0, height * 0.18, length * 0.12),
                    width * 0.92,
                    height * 0.35,
                    length * 0.45,
                    rl.Color(30, 40, 55, 255),
                )
                rl.rl_pop_matrix()

    def draw_signals(self, signals: list[dict]) -> None:
        """Each signal: x, y (SUMO stop-line), state ('r'/'y'/'g'), optional heading_deg."""
        pole = rl.Color(45, 45, 48, 255)
        for sig in signals:
            wx, wz = self.to_world(float(sig["x"]), float(sig["y"]))
            heading = float(sig.get("heading_deg", 0.0))
            rad = math.radians(heading)
            # Offset to the left of approach (lefthand network: near the curb).
            lx = -math.cos(rad) * 1.6
            lz = -math.sin(rad) * 1.6
            px, pz = wx + lx, wz + lz
            lamp_rgb = _signal_rgb(str(sig.get("state", "o")))
            lamp = rl.Color(lamp_rgb[0], lamp_rgb[1], lamp_rgb[2], 255)

            rl.draw_cube(rl.Vector3(px, 1.7, pz), 0.12, 3.4, 0.12, pole)
            rl.draw_cube(rl.Vector3(px, 3.45, pz), 0.32, 0.32, 0.32, lamp)
            rl.draw_cube(rl.Vector3(px, 3.45, pz), 0.38, 0.38, 0.38, rl.Color(lamp_rgb[0], lamp_rgb[1], lamp_rgb[2], 90))

    def end_frame(self) -> None:
        if self._in_3d:
            rl.end_mode_3d()
            self._in_3d = False
        if self.fly:
            cx, cz = _chunk_key(self.fly.x, self.fly.z)
            loaded = len(self.streamer.loaded) if self.streamer else 0
            rl.draw_fps(12, 12)
            rl.draw_text(
                f"chunk {cx},{cz}  loaded {loaded}  {self.hud_extra}  WASD fly  ESC mouse  Q quit",
                12,
                36,
                16,
                rl.Color(20, 20, 20, 255),
            )
        rl.end_drawing()

    def close(self) -> None:
        if not self._open:
            return
        if self.streamer:
            self.streamer.unload_all()
        if self.car_model is not None:
            rl.unload_model(self.car_model)
            self.car_model = None
        rl.enable_cursor()
        rl.close_window()
        self._open = False

    def run(self, on_frame=None) -> None:
        """Standalone loop. on_frame(self) is called each frame while 3D is active."""
        self.open()
        try:
            while self.begin_frame():
                if on_frame is not None:
                    on_frame(self)
                self.end_frame()
        finally:
            self.close()


def run_viewer(net_path: str | None = None) -> None:
    RenderingBackend(net_path).run()


if __name__ == "__main__":
    xml_path = sys.argv[1] if len(sys.argv) > 1 else NET_XML_PATH
    run_viewer(xml_path)

