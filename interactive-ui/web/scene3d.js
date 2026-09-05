import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import * as SkeletonUtils from 'three/addons/utils/SkeletonUtils.js';

const LANE_Y = 0.08, CASING_Y = 0.05, JUNCTION_Y = 0.03, STOPBAR_Y = 0.10, MARK_Y = 0.11;
const MIN_DIST = 18, MAX_DIST = 900;

const SCHEMES = {
  dark: {
    bg: 0x080b10, fog: 0x0d1219, ground: 0x0f1420,
    asphalt: 0x28313f, casing: 0x0d1219, junction: 0x39465a,
    marking: 0x9fb4cc, ambient: 0x9fb4cc, sun: 0xdfe8f5,
    farRoad: 0x161c26, farJunction: 0x1b2230, building: 0x141a24,
  },
  real: {
    bg: 0xeceae4, fog: 0xcac5b6, ground: 0x3a4030,
    asphalt: 0x2f3136, casing: 0x15171b, junction: 0x101216,
    marking: 0xffffff, ambient: 0xffffff, sun: 0xfff4e0,
    farRoad: 0x4a4c52, farJunction: 0x434549, building: 0xb9b2a4,
  },
};

const FAR_ROAD_Y = 0.02, FAR_JUNCTION_Y = 0.015;
const SIGNAL_COLOUR = { green: 0x2fd07f, yellow: 0xffc233, red: 0xff5c5c, off: 0x5a6577 };

const VEHICLE_MODELS = {
  twowheeler: { dir: 'two_wheeler', yaw: -90 },
  auto:       { dir: 'auto_rickshaw', yaw: -90 },
  car:        { dir: 'low_poly_car', fit: 'stretch' },
  bus:        { dir: 'bus' },
  hcv:        { dir: 'truck' },

  obstruction_cow:             { dir: 'cow', size: [2.2, 1.0, 1.4] },
  obstruction_stalled_vehicle: { dir: 'stalled_vehicle', size: [4.2, 1.8, 1.5] },
  obstruction_roadworks:       { dir: 'roadworks', size: [8.0, 2.2, 1.2] },
};

const MODEL_DEFAULTS = { files: ['scene.gltf'], yaw: 0, fit: 'uniform', lift: 0 };

let renderer, scene, camera, controls, canvasEl;
let groundMesh, laneMesh, casingMesh, junctionMesh, markingLines, armLabelGroup;
let vehicleGroup, stopBarGroup, labelGroup;
let carTemplate = null, carSize = null, modelsReady = false;
const modelTemplates = new Map();
let geometry = null;
let scenery = null;
let sceneryGroup = null;
let schemeName = 'dark';
let showIds = false;
const vehicleMeshes = new Map();   // id -> Object3D
const stopBarMeshes = new Map();   // "arm|laneId" -> Mesh
const labelSprites = new Map();    // id -> Sprite

function sumoToWorld(x, y) { return [x, -y]; }

function sumoYawRad(angleDeg) {
  const rad = angleDeg * Math.PI / 180;
  return Math.atan2(Math.sin(rad), -Math.cos(rad));
}

/* ---- geometry construction ---- */

function dedupe(points) {
  const out = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) > 1e-4) out.push(p);
  }
  return out;
}

function miterOffsets(points, halfWidth) {
  const pts = dedupe(points);
  const n = pts.length;
  const left = [], right = [];
  if (n < 2) return { left, right };
  const perps = [];
  for (let i = 0; i < n - 1; i++) {
    const dx = pts[i + 1][0] - pts[i][0], dy = pts[i + 1][1] - pts[i][1];
    const len = Math.hypot(dx, dy) || 1e-9;
    perps.push([-dy / len, dx / len]);
  }
  for (let i = 0; i < n; i++) {
    let nx, ny, scale = 1.0;
    if (i === 0) { [nx, ny] = perps[0]; }
    else if (i === n - 1) { [nx, ny] = perps[perps.length - 1]; }
    else {
      const [n1x, n1y] = perps[i - 1], [n2x, n2y] = perps[i];
      const mx = n1x + n2x, my = n1y + n2y;
      const len = Math.hypot(mx, my);
      if (len < 1e-8) { nx = n2x; ny = n2y; }
      else {
        nx = mx / len; ny = my / len;
        const denom = nx * n2x + ny * n2y;
        scale = 1.0 / Math.max(0.45, Math.abs(denom));
      }
    }
    const ox = nx * halfWidth * scale, oy = ny * halfWidth * scale;
    left.push([pts[i][0] + ox, pts[i][1] + oy]);
    right.push([pts[i][0] - ox, pts[i][1] - oy]);
  }
  return { left, right };
}

function ribbonPositions(points, halfWidth, y) {
  const { left, right } = miterOffsets(points, halfWidth);
  const pos = [];
  for (let i = 0; i < left.length - 1; i++) {
    const l1 = sumoToWorld(...left[i]), r1 = sumoToWorld(...right[i]);
    const l2 = sumoToWorld(...left[i + 1]), r2 = sumoToWorld(...right[i + 1]);
    pos.push(l1[0], y, l1[1], l2[0], y, l2[1], r1[0], y, r1[1]);
    pos.push(r1[0], y, r1[1], l2[0], y, l2[1], r2[0], y, r2[1]);
  }
  return pos;
}

function buildRibbonMesh(lanes, widthFn, y, color) {
  const positions = [];
  for (const lane of lanes) positions.push(...ribbonPositions(lane.shape, widthFn(lane), y));
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geo.computeVertexNormals();
  const mat = new THREE.MeshStandardMaterial({ color, flatShading: true, side: THREE.DoubleSide });
  return new THREE.Mesh(geo, mat);
}

function buildJunctionMesh(junctions, color, y = JUNCTION_Y) {
  const group = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color, flatShading: true, side: THREE.DoubleSide });
  for (const junction of junctions) {
    const pts = dedupe(junction.shape);
    if (pts.length < 3) continue;
    const shape = new THREE.Shape(pts.map(([x, y]) => new THREE.Vector2(x, y)));
    const geo = new THREE.ShapeGeometry(shape);
    geo.rotateX(-Math.PI / 2);
    group.add(new THREE.Mesh(geo, mat));
  }
  group.position.y = y;
  return group;
}

function buildBuildings(buildings, color) {
  const positions = [];
  for (const b of buildings) {
    const pts = dedupe(b.shape);
    if (pts.length < 3) continue;
    const shape = new THREE.Shape(pts.map(([x, y]) => new THREE.Vector2(x, y)));
    let geo;
    try {
      geo = new THREE.ExtrudeGeometry(shape, {
        depth: b.height, bevelEnabled: false, curveSegments: 1 });
    } catch (err) {
      continue;                      // self-intersecting footprint, skip it
    }
    // extrusion runs along +z; this stands it up and puts the footprint in
    // the same x/-y frame sumoToWorld uses
    geo.rotateX(-Math.PI / 2);
    const arr = geo.toNonIndexed().getAttribute('position').array;
    for (let i = 0; i < arr.length; i++) positions.push(arr[i]);
    geo.dispose();
  }
  if (!positions.length) return null;
  const merged = new THREE.BufferGeometry();
  merged.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  merged.computeVertexNormals();
  return new THREE.Mesh(merged,
    new THREE.MeshStandardMaterial({ color, flatShading: true }));
}


function buildScenery(colours) {
  const group = new THREE.Group();
  if (!scenery) return group;
  if (scenery.roads && scenery.roads.length) {
    group.add(buildRibbonMesh(scenery.roads, (l) => l.width / 2,
                              FAR_ROAD_Y, colours.farRoad));
  }
  if (scenery.junctions && scenery.junctions.length) {
    group.add(buildJunctionMesh(scenery.junctions, colours.farJunction,
                                FAR_JUNCTION_Y));
  }
  if (scenery.buildings && scenery.buildings.length) {
    const mesh = buildBuildings(scenery.buildings, colours.building);
    if (mesh) group.add(mesh);
  }
  return group;
}

function buildMarkingLines(lanes, color) {
  const positions = [];
  for (const lane of lanes) {
    const pts = dedupe(lane.shape);
    for (let i = 0; i < pts.length - 1; i++) {
      const [ax, ay] = sumoToWorld(...pts[i]), [bx, by] = sumoToWorld(...pts[i + 1]);
      positions.push(ax, MARK_Y, ay, bx, MARK_Y, by);
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  const mat = new THREE.LineDashedMaterial({ color, dashSize: 2.2, gapSize: 3.2, transparent: true, opacity: 0.55 });
  const lines = new THREE.LineSegments(geo, mat);
  lines.computeLineDistances();
  return lines;
}

function disposeMesh(obj) {
  if (!obj) return;
  obj.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    if (node.material) (Array.isArray(node.material) ? node.material : [node.material]).forEach((m) => m.dispose());
  });
}

function rebuildStaticScene() {
  for (const mesh of [laneMesh, casingMesh, junctionMesh, markingLines,
                      armLabelGroup, sceneryGroup]) {
    if (mesh) { scene.remove(mesh); disposeMesh(mesh); }
  }
  const colours = SCHEMES[schemeName];
  casingMesh = buildRibbonMesh(geometry.lanes, (l) => l.width / 2 + 1.25, CASING_Y, colours.casing);
  laneMesh = buildRibbonMesh(geometry.lanes, (l) => l.width / 2, LANE_Y, colours.asphalt);
  junctionMesh = buildJunctionMesh(geometry.junctions, colours.junction);
  markingLines = buildMarkingLines(geometry.lanes, colours.marking);
  armLabelGroup = buildArmLabels();
  sceneryGroup = buildScenery(colours);
  scene.add(sceneryGroup, casingMesh, laneMesh, junctionMesh, markingLines,
            armLabelGroup);
}

function buildArmLabels() {
  const group = new THREE.Group();
  const [jx, jy] = geometry.junction_xy;
  for (const [name, arm] of Object.entries(geometry.arms)) {
    const rad = arm.bearing * Math.PI / 180;
    const r = 130;
    const [x, z] = sumoToWorld(jx + Math.cos(rad) * r, jy + Math.sin(rad) * r);
    const label = makeLabel(name);
    label.scale.set(7, 3.5, 1);
    label.position.set(x, 7, z);
    group.add(label);
  }
  return group;
}

/* ---- vehicles ---- */

function rgbCsvToHex(csv) {
  const [r, g, b] = csv.split(',').map(Number);
  return (r << 16) | (g << 8) | b;
}

function makeFallbackVehicle(color) {
  const group = new THREE.Group();
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshStandardMaterial({ color, flatShading: true }));
  body.name = 'body';
  group.add(body);
  return group;
}

function loadVehicleModels() {
  const loader = new GLTFLoader();

  function tryFiles(vtype, entry, names) {
    if (!names.length) return Promise.resolve();
    const url = '/web/assets/' + entry.dir + '/' + names[0];
    return new Promise((resolve) => {
      loader.load(url, (gltf) => {
        const holder = new THREE.Group();
        gltf.scene.rotation.y = entry.yaw * Math.PI / 180;
        holder.add(gltf.scene);
        holder.updateMatrixWorld(true);

        const size = new THREE.Vector3();
        new THREE.Box3().setFromObject(holder, true).getSize(size);
        // A degenerate axis (a rigged model's bind pose measured before its
        // skeleton is fully attached, or any other bad gltf) turns into a
        // divide-by-near-zero in the uniform scale factor below and the
        // model fills the screen - the cow did exactly this. A floor of a
        // few cm is generous for any real vehicle/obstruction dimension.
        const MIN_DIM = 0.05;
        const dims = [Math.max(size.x, MIN_DIM), Math.max(size.y, MIN_DIM), Math.max(size.z, MIN_DIM)];
        modelTemplates.set(vtype, { scene: holder, size: dims, entry });
        if (vtype === 'car') { carTemplate = holder; carSize = dims; }
        resolve();
      }, undefined, () => resolve(tryFiles(vtype, entry, names.slice(1))));
    });
  }

  const jobs = Object.entries(VEHICLE_MODELS).map(([vtype, raw]) => {
    const entry = Object.assign({}, MODEL_DEFAULTS, raw);
    return tryFiles(vtype, entry, entry.files);
  });
  Promise.all(jobs).then(() => { modelsReady = true; });
}

function vehicleDims(spec, entry) {
  if (spec) return [spec.length, spec.width, spec.height || 1.5];
  if (entry && entry.size) return entry.size;
  return [4.2, 1.8, 1.5];
}

function makeVehicleMesh(vtype, spec, isObstruction) {
  const color = isObstruction ? 0xff8c42 : (spec ? rgbCsvToHex(spec.colour) : 0x9fb4cc);
  const model = modelTemplates.get(vtype);

  const group = new THREE.Group();

  if (model || carTemplate) {
    const source = model || modelTemplates.get('car');
    const entry = model ? model.entry : Object.assign({}, MODEL_DEFAULTS, VEHICLE_MODELS.car);
    const [length, width, height] = vehicleDims(spec, entry);
    // A rigged model's SkinnedMesh binds to specific bone objects. A plain
    // Object3D.clone(true) copies the nodes but leaves the skin's bone
    // references pointing at the ORIGINAL skeleton, not the cloned one - the
    // cow (the first rigged model here) just vanished with a plain clone.
    // SkeletonUtils.clone remaps the skin onto the cloned bones and is a
    // drop-in replacement for static (non-rigged) models too.
    const clone = SkeletonUtils.clone(source.scene);
    clone.traverse((node) => {
      if (node.isMesh) node.material = node.material.clone();
    });

    const [sx, sy, sz] = source.size;
    // Clamped against a bad measurement (a rigged model's collapsed bind-pose
    // axis, say) turning into a factor that fills the screen - no real
    // vehicle/obstruction model here needs more than a 10x correction either
    // way between its raw export size and its real-world dimensions.
    const clampFactor = (f) => Math.min(10, Math.max(0.1, f));
    if (entry.fit === 'uniform') {
      const factor = clampFactor(Math.min(length / sz, width / sx, height / sy));
      clone.scale.set(factor, factor, factor);
    } else {
      clone.scale.set(clampFactor(width / sx), clampFactor(height / sy), clampFactor(length / sz));
    }
    clone.position.y = entry.lift;   // yaw is already baked into the template

    if (isObstruction) {
      clone.traverse((node) => {
        if (node.isMesh) { node.material.emissive = new THREE.Color(0xff8c42); node.material.emissiveIntensity = 0.6; }
      });
    }
    group.add(clone);
    return group;
  }

  const [length, width] = vehicleDims(spec, VEHICLE_MODELS[vtype]);
  const box = makeFallbackVehicle(color);
  box.scale.set(width, 1.5, length);
  box.position.y = 0.75;
  group.add(box);
  return group;
}

function makeLabel(text) {
  const canvas = document.createElement('canvas');
  canvas.width = 128; canvas.height = 32;
  const ctx = canvas.getContext('2d');
  ctx.font = '20px ui-monospace, monospace';
  ctx.fillStyle = 'rgba(232,238,247,0.9)';
  ctx.fillText(text, 2, 22);
  const tex = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
  sprite.scale.set(4, 1, 1);
  return sprite;
}

function updateVehicles(vehicles) {
  if (!modelsReady) return;
  const seen = new Set();
  for (const v of vehicles) {
    seen.add(v.id);
    const spec = geometry.vehicle_classes[v.t];
    const isObstruction = (v.t || '').startsWith('obstruction');
    let mesh = vehicleMeshes.get(v.id);
    if (!mesh) {
      mesh = makeVehicleMesh(v.t, spec, isObstruction);
      vehicleGroup.add(mesh);
      vehicleMeshes.set(v.id, mesh);
    }
    const [wx, wz] = sumoToWorld(v.x, v.y);
    mesh.position.x = wx; mesh.position.z = wz;
    mesh.rotation.y = sumoYawRad(v.a);

    if (showIds) {
      let label = labelSprites.get(v.id);
      if (!label) { label = makeLabel(v.id); labelGroup.add(label); labelSprites.set(v.id, label); }
      label.position.set(wx, 2.4, wz);
      label.visible = true;
    } else {
      const label = labelSprites.get(v.id);
      if (label) label.visible = false;
    }
  }
  for (const [id, mesh] of vehicleMeshes) {
    if (!seen.has(id)) {
      vehicleGroup.remove(mesh);
      disposeMesh(mesh);
      vehicleMeshes.delete(id);
      const label = labelSprites.get(id);
      if (label) { labelGroup.remove(label); labelSprites.delete(id); }
    }
  }
}

/* ---- signals / stop bars ---- */

function updateStopBars(arms) {
  const incoming = {};
  for (const [name, arm] of Object.entries(geometry.arms)) incoming[arm.incoming_edge] = name;

  const seen = new Set();
  for (const lane of geometry.lanes) {
    const arm = incoming[lane.edge];
    if (!arm) continue;
    const key = lane.id;
    seen.add(key);
    const shape = lane.shape;
    const [ax, ay] = shape[shape.length - 2] || shape[0];
    const [bx, by] = shape[shape.length - 1];
    const len = Math.hypot(bx - ax, by - ay) || 1;
    const nx = -(by - ay) / len, ny = (bx - ax) / len;
    const half = lane.width / 2;
    const [p1x, p1y] = sumoToWorld(bx + nx * half, by + ny * half);
    const [p2x, p2y] = sumoToWorld(bx - nx * half, by - ny * half);
    const midX = (p1x + p2x) / 2, midZ = (p1y + p2y) / 2;
    const barLen = Math.hypot(p2x - p1x, p2y - p1y) || 1;
    const angle = Math.atan2(p2y - p1y, p2x - p1x);

    let bar = stopBarMeshes.get(key);
    if (!bar) {
      const geo = new THREE.BoxGeometry(1, 0.28, 0.35);
      const mat = new THREE.MeshStandardMaterial({ flatShading: true });
      bar = new THREE.Mesh(geo, mat);
      stopBarGroup.add(bar);
      stopBarMeshes.set(key, bar);
    }
    bar.position.set(midX, STOPBAR_Y, midZ);
    bar.rotation.y = -angle;
    bar.scale.x = barLen;
    const colourHex = SIGNAL_COLOUR[arms[arm]] ?? SIGNAL_COLOUR.off;
    bar.material.color.setHex(colourHex);
    bar.material.emissive.setHex(colourHex);
    bar.material.emissiveIntensity = arms[arm] ? 0.5 : 0.05;
  }
  for (const [key, bar] of stopBarMeshes) {
    if (!seen.has(key)) { stopBarGroup.remove(bar); bar.geometry.dispose(); bar.material.dispose(); stopBarMeshes.delete(key); }
  }
}

/* ---- camera framing ---- */

function frameBox(x0, y0, x1, y1, pad) {
  const [wx0, wz0] = sumoToWorld(x0, y1), [wx1, wz1] = sumoToWorld(x1, y0);
  const cx = (wx0 + wx1) / 2, cz = (wz0 + wz1) / 2;
  const span = Math.max(wx1 - wx0, wz1 - wz0) + pad * 2;
  const dist = Math.min(MAX_DIST, Math.max(MIN_DIST, span * 0.72));
  controls.target.set(cx, 0, cz);
  camera.position.set(cx, dist * 0.62, cz + dist * 0.78);
  controls.update();
}

function fitJunction(radius) {
  if (!geometry) return;
  const [jx, jy] = geometry.junction_xy;
  const r = radius || 165;
  frameBox(jx - r, jy - r, jx + r, jy + r, 24);
}

function fitNetwork() {
  if (!geometry) return;
  const [x0, y0, x1, y1] = geometry.bounds;
  frameBox(x0, y0, x1, y1, 40);
}

/* ---- public surface ---- */

function resize() {
  const rect = canvasEl.parentElement.getBoundingClientRect();
  renderer.setSize(rect.width, rect.height, false);
  camera.aspect = rect.width / Math.max(1, rect.height);
  camera.updateProjectionMatrix();
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

function applyScheme() {
  const colours = SCHEMES[schemeName];
  scene.background = new THREE.Color(colours.bg);
  scene.fog = new THREE.Fog(colours.fog, MIN_DIST * 3, MAX_DIST * 1.1);
  if (groundMesh) groundMesh.material.color.setHex(colours.ground);
  if (geometry) rebuildStaticScene();
}

function init(canvas) {
  canvasEl = canvas;
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(55, 1, 0.5, 4000);

  const ambient = new THREE.HemisphereLight(0xdfe8f5, 0x30281c, 0.9);
  const sun = new THREE.DirectionalLight(0xffffff, 1.1);
  sun.position.set(120, 220, 90);
  scene.add(ambient, sun);

  groundMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(6000, 6000),
    new THREE.MeshStandardMaterial({ flatShading: true }));
  groundMesh.rotation.x = -Math.PI / 2;
  scene.add(groundMesh);

  vehicleGroup = new THREE.Group();
  stopBarGroup = new THREE.Group();
  labelGroup = new THREE.Group();
  scene.add(vehicleGroup, stopBarGroup, labelGroup);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = MIN_DIST;
  controls.maxDistance = MAX_DIST;
  controls.minPolarAngle = 0.15;
  controls.maxPolarAngle = 0.92;
  controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE };
  controls.screenSpacePanning = false;

  /* we do the wheel ourselves. see wheelZoom. pinch on a touchscreen still
   * goes through OrbitControls, that path is fine. */
  canvasEl.parentElement.addEventListener('wheel', wheelZoom, { passive: false, capture: true });

  applyScheme();
  resize();
  window.addEventListener('resize', resize);
  new ResizeObserver(resize).observe(canvasEl.parentElement);

  loadVehicleModels();

  animate();
}

function setGeometry(geo) {
  geometry = geo;
  rebuildStaticScene();
}

function setScenery(data) {
  scenery = data;
  if (geometry) rebuildStaticScene();   // nothing to hang it on yet otherwise
}

function setScheme(name) {
  schemeName = SCHEMES[name] ? name : 'dark';
  applyScheme();
}

function setShowIds(value) {
  showIds = value;
  if (!value) for (const label of labelSprites.values()) label.visible = false;
}

function updateFrame(frame) {
  if (!geometry) return;
  updateVehicles(frame.vehicles || []);
  updateStopBars(frame.signal?.arms || {});
}

const WHEEL_SPAN = 2200;   // total deltaY to walk the full zoom range

function wheelZoom(event) {
  event.preventDefault();
  event.stopPropagation();          // keep OrbitControls' own handler out of it
  let delta = event.deltaY;
  if (event.deltaMode === 1) delta *= 16;         // lines
  else if (event.deltaMode === 2) delta *= 400;   // pages
  const here = fractionForDistance(camera.position.distanceTo(controls.target));
  setZoomFraction(here - delta / WHEEL_SPAN);     // wheel up is negative, zooms in
}

function zoomBy(factor) {
  const dir = new THREE.Vector3().subVectors(camera.position, controls.target);
  const dist = Math.min(MAX_DIST, Math.max(MIN_DIST, dir.length() / factor));
  dir.setLength(dist);
  camera.position.copy(controls.target).add(dir);
  controls.update();
}

function distanceForFraction(t) {
  return Math.exp(Math.log(MAX_DIST) - t * (Math.log(MAX_DIST) - Math.log(MIN_DIST)));
}
function fractionForDistance(dist) {
  const clamped = Math.min(MAX_DIST, Math.max(MIN_DIST, dist));
  return (Math.log(MAX_DIST) - Math.log(clamped)) / (Math.log(MAX_DIST) - Math.log(MIN_DIST));
}

function setZoomFraction(t) {
  const dir = new THREE.Vector3().subVectors(camera.position, controls.target);
  dir.setLength(distanceForFraction(Math.max(0, Math.min(1, t))));
  camera.position.copy(controls.target).add(dir);
  controls.update();
}

function zoomLabel() {
  const dist = camera.position.distanceTo(controls.target);
  const visibleHeight = 2 * dist * Math.tan((camera.fov * Math.PI / 180) / 2);
  const metresPerPx = visibleHeight / (canvasEl.clientHeight || 600);
  const label = metresPerPx < 1 ? (1 / metresPerPx).toFixed(1) + ' px/m' : metresPerPx.toFixed(1) + ' m/px';
  return { label, metresPerPx, slider: Math.round(fractionForDistance(dist) * 100) };
}

// OrbitControls.getAzimuthalAngle(): 0 when the camera sits on world +Z
// relative to its target. sumoToWorld maps SUMO's +y (which this junction's
// arms treat as north) to world -z, so a camera parked on +Z is looking
// toward -Z, i.e. looking north - hence the +180. Approximate (this junction
// isn't perfectly grid-aligned to true compass directions either), good
// enough for "which way am I facing" at a glance.
function getHeadingDeg() {
  if (!controls) return null;
  const deg = controls.getAzimuthalAngle() * 180 / Math.PI;
  return (deg + 180 + 360) % 360;
}

window.Scene3D = {
  init, resize, setGeometry, setScenery, setScheme, setShowIds, updateFrame,
  fitJunction, fitNetwork, zoomBy, setZoomFraction, zoomLabel, getHeadingDeg,
};
window.dispatchEvent(new Event('scene3d:ready'));
