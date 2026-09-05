'use strict';

const $ = (id) => document.getElementById(id);

function setChrome(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
  return el;
}

const state = {
  geometry: null,
  context: null,
  frame: null,
  surface: null,
  baseline: null,
  versions: null,
  proposal: null,
  scheme: 'dark',
  showIds: false,
  listening: false,
  micPermission: 'unknown',
  micLevel: 0,
  busy: false,
  compareFrom: null,
  dir: 'N',              // shared focus arm: map overlay + both Controls sections
  obstructionArm: {},    // per obstruction kind: which approach to place it on
};

/* ================================================================== boot */

async function boot() {
  try {
    state.geometry = await (await fetch('/api/geometry')).json();
    state.context = await (await fetch('/api/context')).json();
  } catch (err) {
    toast('Could not reach the local server: ' + err.message, 'bad');
    return;
  }

  if (!$('traffic-grid')) {
    let retried = false;
    try { retried = sessionStorage.getItem('abhyas-stale-reload') === '1'; } catch (e) {}
    if (retried) {
      toast('This page is out of date and a reload did not fix it. '
            + 'Clear the cache for this site (Ctrl+Shift+R).', 'bad');
      return;
    }
    try { sessionStorage.setItem('abhyas-stale-reload', '1'); } catch (e) {}
    toast('This page looks out of date -- reloading.', 'bad');
    location.reload();
    return;
  }
  try { sessionStorage.removeItem('abhyas-stale-reload'); } catch (e) {}
  setChrome('junction-name', state.context.junction.name);
  buildLegend();
  buildArmRows();
  buildTrafficGrid();
  setInterval(paintCompassHeadings, 150);
  renderArchive();
  renderPlan(state.context.baseline_plan);
  if (state.context.validation) renderValidation(state.context.validation);
  $('val-limitation').textContent =
    state.context.validation?.summary?.limitation ||
    'This model is validated on travel time only. Travel time says nothing ' +
    'about whether the turning proportions are right, and we cannot measure those.';

  await waitForScene3D();
  window.Scene3D.init(mapCanvas());
  window.Scene3D.setGeometry(state.geometry);

  fetch('/api/scenery')
    .then((r) => r.json())
    .then((data) => window.Scene3D.setScenery(data))
    .catch(() => {});
  window.Scene3D.setScheme(state.scheme);
  fitJunction();
  requestAnimationFrame(drawBlob);

  connect();
  wireStage();
  wireVoice();
  wireVersions();
  wireResults();
  wireViews();
  wireCliDash();
  wireControlModals();

  loadSurface();
  loadVersions();
  loadVoiceBackend();
}

/* ============================================================= websocket */

let socket = null;

function connect() {
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  socket = new WebSocket(url);
  socket.onopen = () => setConn('live', 'ok');
  socket.onclose = () => { setConn('reconnecting', 'dim'); setTimeout(connect, 1500); };
  socket.onerror = () => setConn('error', 'red');
  socket.onmessage = (event) => handleMessage(JSON.parse(event.data));
}

function send(command, params) {
  if (socket && socket.readyState === 1) {
    socket.send(JSON.stringify({ type: 'control', command, params: params || {} }));
  }
}

let connState = '';

function setConn(text, kind) {
  const dot = $('conn-dot');
  if (dot) dot.className = 'dot' + (kind === 'ok' ? '' : ' ' + kind);
  if (setChrome('conn-text', text)) return;
  if (kind === 'bad' && text !== connState) toast(text, 'bad');
  connState = text;
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'frame':
      state.frame = msg.data;
      renderFrame(msg.data);
      break;
    case 'controls':
      renderSurface(msg.surface);
      if (msg.changes && msg.changes.length) {
        flashChanged(msg.changes);
        if (msg.origin !== 'connect') toast(msg.summary, 'ok');
      }
      (msg.notes || []).forEach((note) => toast(note, 'warn'));
      break;
    case 'versions':
      renderVersions(msg.tree);
      break;
    case 'applied':
      if (msg.plan) renderPlan(msg.plan);
      break;
    case 'restarting':
      toast('Demand is baked into the route file, so the run restarts at ' +
            msg.veh_per_hour + ' veh/h.', 'warn');
      break;
    case 'progress': onProgress(msg); break;
    case 'job_started': startJob(msg.job); break;
    case 'job_finished': finishJob(msg); break;
    case 'job_failed':
      toast(msg.job + ' failed: ' + msg.message, 'bad');
      endJob();
      break;
    case 'sim_error':
    case 'command_error':
      toast(msg.message, 'bad');
      break;
  }
}

/* =================================================================== map */

const mapCanvas = () => $('map');

function waitForScene3D() {
  if (window.Scene3D) return Promise.resolve();
  return new Promise((resolve) => window.addEventListener('scene3d:ready', resolve, { once: true }));
}

function fitNetwork() { window.Scene3D?.fitNetwork(); }
function fitJunction(radius) { window.Scene3D?.fitJunction(radius); }

function updateStageReadout() {
  if (!window.Scene3D) return;
  const { label, slider, metresPerPx } = window.Scene3D.zoomLabel();
  $('zoom-label').textContent = label;
  $('zoom').value = String(slider);

  const targets = [5, 10, 20, 50, 100, 200, 500, 1000];
  const pxPerMetre = 1 / metresPerPx;
  const metres = targets.find((m) => m * pxPerMetre > 60) || 1000;
  $('scale-bar').style.width = Math.round(metres * pxPerMetre) + 'px';
  $('scale-label').textContent = metres + ' m';
}

/* =========================================================== stage chrome */

function buildLegend() {
  const el = $('legend');
  el.innerHTML = '';
  for (const spec of Object.values(state.geometry.vehicle_classes)) {
    const item = document.createElement('span');
    item.className = 'item';
    item.innerHTML = '<span class="swatch" style="background:rgb(' + spec.colour +
                     ')"></span>' + spec.label;
    el.appendChild(item);
  }
  const obstruction = document.createElement('span');
  obstruction.className = 'item';
  obstruction.innerHTML = '<span class="swatch" style="background:#ff8c42"></span>Obstruction';
  el.appendChild(obstruction);
}

const COMPASS_DIRS = ['N', 'E', 'S', 'W'];

// A real compass: the N/E/S/W buttons stay fixed (so "click N" is always in
// the same spot) and a needle rotates inside to show which way the 3d
// camera is actually looking - paintCompassHeadings() drives it from
// Scene3D's OrbitControls azimuth, polled on an interval since three.js has
// no change event for camera orbiting.
function compassHtml() {
  return '<div class="compass">' +
    '<div class="compass-dial"><div class="compass-needle"></div></div>' +
    COMPASS_DIRS.map((d) => '<button class="compass-btn dir-' + d +
      '" data-dir="' + d + '">' + d + '</button>').join('') +
    '</div>';
}

function wireCompass(root, selected, onSelect) {
  root.querySelectorAll('.compass-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.dir === selected);
    btn.onclick = () => onSelect(btn.dataset.dir);
  });
}

function paintCompassHeadings() {
  const heading = window.Scene3D?.getHeadingDeg?.();
  if (heading == null) return;
  document.querySelectorAll('.compass-needle').forEach((n) => {
    n.style.transform = 'rotate(' + heading + 'deg)';
  });
}

function setDir(dir) {
  state.dir = dir;
  buildArmRows();                        // rebuilds + rewires the canvas compass
  paintActiveTrafficCell();
  if (state.frame) renderFrame(state.frame);
  if (state.surface) renderSurface(state.surface); // rebuilds + rewires the Controls one
}

// The canvas-overlay compass: same widget as the Controls-pane one
// (renderDirPicker), just placed top-right on the 3d view instead of in the
// rail, next to the vehicle-model legend.
function buildArmRows() {
  const el = $('compass-overlay');
  if (!el) return;
  el.innerHTML = compassHtml();
  wireCompass(el, state.dir, setDir);
}

const DIRS = ['N', 'E', 'S', 'W'];

// Left panel: all four approaches at once, not just the focused one - a
// 2x2 read of the whole junction rather than the single-arm switcher.
function buildTrafficGrid() {
  const el = $('traffic-grid');
  if (!el) return;
  el.innerHTML = DIRS.map((d) =>
    '<div class="traffic-cell" data-dir="' + d + '">' +
      '<div class="traffic-cell-head"><span class="lamp" id="grid-lamp-' + d +
        '"></span><span class="traffic-cell-name">' + d + ' approach</span></div>' +
      '<div class="traffic-cell-q" id="grid-q-' + d + '">—</div>' +
    '</div>').join('');
  el.onclick = (event) => {
    const cell = event.target.closest('.traffic-cell');
    if (cell) setDir(cell.dataset.dir);
  };
  paintActiveTrafficCell();
}

function paintActiveTrafficCell() {
  document.querySelectorAll('.traffic-cell').forEach((cell) => {
    cell.classList.toggle('active', cell.dataset.dir === state.dir);
  });
}

function updateTrafficGrid(frame) {
  for (const d of DIRS) {
    const colour = frame.signal.arms?.[d];
    const lamp = $('grid-lamp-' + d);
    if (lamp) lamp.className = 'lamp' + (colour ? ' ' + colour : '');
    const q = $('grid-q-' + d);
    if (q) q.textContent = (frame.queues?.[d] ?? 0) + ' queued';
  }
}

function renderFrame(frame) {
  window.Scene3D?.updateFrame(frame);
  if ($('sim-clock')) $('sim-clock').textContent = formatClock(frame.time_s);
  $('phase-name').textContent = frame.signal.phase_name || '—';
  $('phase-countdown').textContent = Math.max(0, Math.round(frame.signal.time_to_switch));

  const name = state.dir;
  const colour = frame.signal.arms?.[name];
  const lamp = $('arm-active-lamp');
  if (lamp) lamp.className = 'lamp' + (colour ? ' ' + colour : '');
  const label = $('arm-active-name');
  if (label) label.textContent = name + ' approach';
  const q = $('arm-active-q');
  if (q) q.textContent = (frame.queues?.[name] ?? 0) + ' queued';

  updateTrafficGrid(frame);
  if (frame.plan) renderPlan(frame.plan);
}

function renderPlan(plan) {
  // The per-approach breakdown this used to print (plan-line) was dropped -
  // it just duplicated the cycle-tag and the master slider. cycle_seconds
  // comes from /api/controls via renderSurface instead.
}

function formatClock(seconds) {
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return m + ':' + String(s).padStart(2, '0');
}

function toast(text, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = text;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 6500);
}

function wireStage() {
  const canvas = mapCanvas();
  canvas.addEventListener('dblclick', () => fitJunction());
  setInterval(updateStageReadout, 200);

  // Fit-to-junction/network, the colour scheme switch and the id overlay
  // toggle were dropped from the redesigned bottom bar - guard rather than
  // wire, in case some future markup brings them back.
  if ($('fit-junction')) $('fit-junction').onclick = () => fitJunction();
  if ($('fit-network')) $('fit-network').onclick = () => fitNetwork();
  $('zoom').oninput = (event) => {
    window.Scene3D?.setZoomFraction(Number(event.target.value) / 100);
  };
  if ($('scheme')) {
    $('scheme').onchange = (event) => {
      state.scheme = event.target.value;
      window.Scene3D?.setScheme(state.scheme);
      document.querySelector('.canvas-wrap')
        .classList.toggle('light', state.scheme === 'real');
    };
  }
  if ($('show-ids')) {
    $('show-ids').onchange = (event) => {
      state.showIds = event.target.checked;
      window.Scene3D?.setShowIds(state.showIds);
    };
  }

  for (const button of document.querySelectorAll('[data-cmd]')) {
    button.onclick = () => send(button.dataset.cmd);
  }
  $('speed').oninput = (event) => {
    const value = Number(event.target.value);
    $('speed-label').textContent = value + '×';
    send('speed', { value });
  };

  // The Controls/Versions/Results tab bar became a dropdown in the rail.
  if ($('rail-mode-select')) {
    $('rail-mode-select').onchange = (event) => {
      const mode = event.target.value;
      for (const pane of document.querySelectorAll('.tabpane')) {
        pane.hidden = pane.dataset.pane !== mode;
      }
    };
  }
}

/* ============================================================== controls */

async function loadSurface() {
  try {
    renderSurface(await (await fetch('/api/controls')).json());
  } catch (err) {
    toast('Could not read the control surface: ' + err.message, 'bad');
  }
}

function renderDirPicker() {
  const el = $('dir-picker');
  if (!el) return;
  el.innerHTML = compassHtml();
  wireCompass(el, state.dir, setDir);
  paintCompassHeadings();
}

// One control id can serve N, S, E or W depending on the active plan shape:
// paired shapes ("north_south.green") answer for two arms at once, the
// four-way shape has one control per arm. Match on the label rather than a
// hardcoded id, since the id's own group key varies with the shape.
function greenControlForDir(signalControls, dir) {
  const word = { N: 'north', S: 'south', E: 'east', W: 'west' }[dir];
  const greens = signalControls.filter((c) => c.id.endsWith('.green'));
  return greens.find((c) => c.label.toLowerCase().includes(word)) || greens[0];
}

function renderSurface(surface) {
  state.surface = surface;
  state.baseline = surface.baseline;
  $('cycle-tag').textContent = 'cycle ' + surface.cycle_seconds + ' s';
  renderDirPicker();

  const signalControls = surface.controls.filter((c) => c.group === 'Signal');
  const trafficControls = surface.controls.filter((c) => c.group === 'Traffic');
  const obstructionControls = surface.controls.filter((c) => c.group === 'Obstructions');

  const master = $('master-slider');
  if (master) {
    master.innerHTML = '';
    const green = greenControlForDir(signalControls, state.dir);
    if (green) master.appendChild(buildControl(green));

    const advanced = $('advanced-signal-body');
    if (advanced) {
      advanced.innerHTML = '';
      for (const control of signalControls) {
        if (control === green) continue;
        advanced.appendChild(buildControl(control));
      }
    }
  }

  refreshTrafficModal(trafficControls);
  refreshObstructionModal(obstructionControls);

  renderLegacyControlDeck(surface);
}

function refreshTrafficModal(controls) {
  const body = $('traffic-modal-body');
  if (!body) return;
  body.innerHTML = '';
  buildTrafficSection(body, controls);
}

function refreshObstructionModal(controls) {
  const body = $('obstruction-modal-body');
  if (!body) return;
  body.innerHTML = '';
  buildObstructionPicker(body, controls);
}

function wireControlModals() {
  const openModal = (id) => { const el = $(id); if (el) el.hidden = false; };
  const closeModal = (id) => { const el = $(id); if (el) el.hidden = true; };

  if ($('open-traffic-modal')) {
    $('open-traffic-modal').onclick = () => openModal('traffic-modal');
  }
  if ($('btn-close-traffic-modal')) {
    $('btn-close-traffic-modal').onclick = () => closeModal('traffic-modal');
  }
  if ($('traffic-modal')) {
    $('traffic-modal').onclick = (e) => {
      if (e.target === $('traffic-modal')) closeModal('traffic-modal');
    };
  }

  if ($('open-obstruction-modal')) {
    $('open-obstruction-modal').onclick = () => openModal('obstruction-modal');
  }
  if ($('btn-close-obstruction-modal')) {
    $('btn-close-obstruction-modal').onclick = () => closeModal('obstruction-modal');
  }
  if ($('obstruction-modal')) {
    $('obstruction-modal').onclick = (e) => {
      if (e.target === $('obstruction-modal')) closeModal('obstruction-modal');
    };
  }
}

// The old per-group <details> deck: kept, but only rendered if the markup
// for it is present, so a page without #control-deck (the new Control pane)
// doesn't pay for it.
function renderLegacyControlDeck(surface) {
  const deck = $('control-deck');
  if (!deck) return;
  const open = new Set([...deck.querySelectorAll('details[open]')]
    .map((d) => d.dataset.group));
  deck.innerHTML = '';

  for (const group of surface.groups) {
    const controls = surface.controls.filter((c) => c.group === group);
    if (!controls.length) continue;
    const box = document.createElement('details');
    box.dataset.group = group;
    box.open = open.size ? open.has(group) : true;
    const badge = (group === 'Signal' && surface.shape_label)
      ? ' &middot; ' + escapeHtml(surface.shape_label) : '';
    box.innerHTML = '<summary><span>' + group + badge + '</span>' +
                    '<span class="count">' + controls.length +
                    '</span></summary>';
    const body = document.createElement('div');
    body.className = 'group-body';

    if (group === 'Obstructions') {
      buildObstructionPicker(body, controls);
    } else if (group === 'Traffic') {
      buildTrafficSection(body, controls);
    } else {
      for (const control of controls) body.appendChild(buildControl(control));
    }

    box.appendChild(body);
    deck.appendChild(box);
  }
}

function buildTrafficSection(body, controls) {
  // All four approach shares, not just the focused one - they renormalise
  // against each other, so seeing one in isolation is misleading.
  const shares = DIRS
    .map((d) => controls.find((c) => c.id === 'demand.arm_share.' + d))
    .filter(Boolean);
  const rest = controls.filter((c) => !c.id.startsWith('demand.arm_share.'));

  for (const control of rest) body.appendChild(buildControl(control));
  for (const control of shares) body.appendChild(buildControl(control));
}

function buildObstructionPicker(body, controls) {
  // One row per kind, each with its own arm picker: the kind dropdown that
  // used to swap a single stepper hid the fact that a cow on N and a cow on
  // E are separate placements.
  const kinds = [];
  const seen = new Set();
  for (const c of controls) {
    const kind = c.id.split('.')[1];
    if (seen.has(kind)) continue;
    seen.add(kind);
    kinds.push({ kind, label: c.label.replace(/ on [NSEW]$/, '') });
  }

  for (const { kind, label } of kinds) {
    const arm = state.obstructionArm[kind] || state.dir;
    const control = controls.find((c) => c.id === 'obstruction.' + kind + '.' + arm);
    if (!control) continue;

    const count = Math.round(control.value);
    const row = document.createElement('div');
    row.className = 'obstruction-row';
    row.innerHTML =
      '<span class="obstruction-name">' + escapeHtml(label) + '</span>' +
      '<select class="obstruction-arm">' +
        DIRS.map((d) => '<option value="' + d + '"' +
          (d === arm ? ' selected' : '') + '>' + d + '</option>').join('') +
      '</select>' +
      '<div class="stepper">' +
        '<button class="stepper-btn minus"' + (count <= control.min ? ' disabled' : '') + '>&minus;</button>' +
        '<span class="stepper-count">' + count + '</span>' +
        '<button class="stepper-btn plus"' + (count >= control.max ? ' disabled' : '') + '>+</button>' +
      '</div>';

    row.querySelector('.obstruction-arm').onchange = (event) => {
      state.obstructionArm[kind] = event.target.value;
      renderSurface(state.surface);
    };
    row.querySelector('.minus').onclick = () =>
      applyEdits([{ id: control.id, value: Math.max(control.min, count - 1) }]);
    row.querySelector('.plus').onclick = () =>
      applyEdits([{ id: control.id, value: Math.min(control.max, count + 1) }]);
    body.appendChild(row);
  }
}

function buildControl(control) {
  const el = document.createElement('div');
  el.className = 'control ' + control.kind;
  el.id = 'ctl-' + cssId(control.id);
  el.title = control.help;

  if (control.kind === 'toggle') {
    el.innerHTML = '<button class="toggle' + (control.value ? ' on' : '') +
      '"><span class="knob"></span></button><span class="label">' +
      escapeHtml(control.label) + '</span>';
    el.querySelector('button').onclick = () =>
      applyEdits([{ id: control.id, value: !control.value }]);
    return el;
  }

  if (control.kind === 'choice') {
    const chosen = (control.options || []).find((o) => o.value === control.value);
    el.innerHTML =
      '<div class="control-head"><span class="label">' +
        escapeHtml(control.label) + '</span></div>' +
      '<select>' + (control.options || []).map((o) =>
        '<option value="' + escapeHtml(o.value) + '"' +
        (o.value === control.value ? ' selected' : '') + '>' +
        escapeHtml(o.label) + '</option>').join('') + '</select>' +
      '<p class="fineprint">' + escapeHtml(chosen ? chosen.note : '') + '</p>';
    el.querySelector('select').onchange = (event) =>
      applyEdits([{ id: control.id, value: event.target.value }]);
    return el;
  }

  const changed = state.baseline && state.baseline[control.id] !== undefined &&
                  Math.abs(state.baseline[control.id] - control.value) > 1e-9;
  el.innerHTML =
    '<div class="control-head"><span class="label">' + escapeHtml(control.label) +
      '</span><span class="value' + (changed ? ' changed' : '') + '">' +
      fmtValue(control) + '</span></div>' +
    (control.kind === 'dial' ? dialSvg(control) : '') +
    '<input type="range" min="' + control.min + '" max="' + control.max +
      '" step="' + control.step + '" value="' + control.value + '"/>' +
    '<div class="control-foot"><span>' + fmtNumber(control.min) + '</span>' +
      (changed ? '<span class="base">baseline ' +
        fmtNumber(state.baseline[control.id]) + '</span>' : '<span></span>') +
      '<span>' + fmtNumber(control.max) + '</span></div>';

  const range = el.querySelector('input');
  const readout = el.querySelector('.value');
  range.oninput = () => {
    readout.textContent = fmtNumber(Number(range.value)) +
      (control.unit ? ' ' + control.unit : '');
    if (control.kind === 'dial') paintDial(el, control, Number(range.value));
  };
  range.onchange = () => applyEdits([{ id: control.id, value: Number(range.value) }]);
  if (control.kind === 'dial') paintDial(el, control, control.value);
  return el;
}

function dialSvg() {
  return '<svg class="dial-face" viewBox="0 0 100 62">' +
    '<path class="track" d="M8 56 A42 42 0 0 1 92 56"/>' +
    '<path class="fill" d="M8 56 A42 42 0 0 1 92 56"/>' +
    '<line class="needle" x1="50" y1="56" x2="50" y2="18"/>' +
    '<circle class="hub" cx="50" cy="56" r="4"/></svg>';
}

function paintDial(el, control, value) {
  const svg = el.querySelector('.dial-face');
  if (!svg) return;
  const t = (value - control.min) / Math.max(control.max - control.min, 1e-9);
  const arc = svg.querySelector('.fill');
  const length = arc.getTotalLength();
  arc.style.strokeDasharray = length;
  arc.style.strokeDashoffset = length * (1 - Math.max(0, Math.min(1, t)));
  const angle = -90 + t * 180;
  svg.querySelector('.needle').setAttribute(
    'transform', 'rotate(' + angle + ' 50 56)');
}

function fmtValue(control) {
  return fmtNumber(control.value) + (control.unit ? ' ' + control.unit : '');
}

function fmtEdit(value, control) {
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  if (value === undefined || value === null) return '—';
  return fmtNumber(value) + (control.unit ? ' ' + control.unit : '');
}

function fmtNumber(value) {
  if (typeof value !== 'number') return String(value);
  return Number.isInteger(value) ? String(value)
    : String(Math.round(value * 1000) / 1000);
}

const cssId = (id) => id.replace(/\./g, '_');

function flashChanged(changes) {
  for (const change of changes) {
    const el = $('ctl-' + cssId(change.id));
    if (!el) continue;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  }
}

async function applyEdits(edits, origin) {
  try {
    const response = await fetch('/api/controls', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits, origin: origin || 'dial' }),
    });
    const result = await response.json();
    if (!result.ok) toast(result.error || 'That edit was refused.', 'bad');
    return result.ok;
  } catch (err) {
    toast('Could not apply the edit: ' + err.message, 'bad');
    return false;
  }
}

/* ============================================================== versions */

async function loadVersions() {
  try {
    renderVersions(await (await fetch('/api/versions')).json());
  } catch (err) { /* the rail simply stays empty */ }
}

function renderVersions(tree) {
  state.versions = tree;
  $('version-count').textContent = tree.versions.length;
  if (tree.warning) toast(tree.warning, 'warn');

  const head = tree.versions.find((v) => v.id === tree.head);
  setChrome('version-head', head ? head.message : 'uncommitted');

  const el = $('version-tree');
  if (!tree.versions.length) {
    el.innerHTML = '<p class="fineprint">No versions yet. Commit one before ' +
      'you start moving dials and you can always get back to it.</p>';
    return;
  }
  el.innerHTML = tree.versions.slice().reverse().map((version) => {
    const isHead = version.id === tree.head;
    return '<div class="vnode' + (isHead ? ' head' : '') + '" data-id="' +
      version.id + '" style="margin-left:' + Math.min(version.depth, 6) * 12 + 'px">' +
      '<div class="vrow"><span class="vdot"></span>' +
        '<span class="vmsg">' + escapeHtml(version.message) + '</span>' +
        '<span class="vtime">' + version.created.slice(11, 16) + '</span></div>' +
      '<div class="vsum">' + escapeHtml(version.summary) + '</div>' +
      '<div class="vactions">' +
        '<button data-act="checkout">restore</button>' +
        '<button data-act="compare">' +
          (state.compareFrom === version.id ? 'comparing…' : 'compare') + '</button>' +
        '<button data-act="rename">rename</button>' +
        '<button data-act="delete">delete</button>' +
      '</div></div>';
  }).join('');
}

function wireVersions() {
  $('commit-form').onsubmit = async (event) => {
    event.preventDefault();
    await commitVersion($('commit-message').value);
    $('commit-message').value = '';
  };

  $('commit-controls').onclick = async () => {
    const message = prompt('Name this state:', 'what-if');
    if (message !== null) await commitVersion(message);
  };

  $('reset-controls').onclick = () => {
    if (!state.baseline) return;
    const edits = Object.entries(state.baseline)
      .map(([id, value]) => ({ id, value }));
    applyEdits(edits, 'baseline');
  };

  $('version-tree').onclick = async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const id = button.closest('.vnode').dataset.id;
    const act = button.dataset.act;

    if (act === 'checkout') {
      const result = await postJson('/api/versions/' + id + '/checkout', {});
      if (result.ok) toast('Restored “' + result.restored + '”.', 'ok');
    } else if (act === 'rename') {
      const message = prompt('Rename this version:');
      if (message) await postJson('/api/versions/' + id + '/rename', { message });
    } else if (act === 'delete') {
      await fetch('/api/versions/' + id, { method: 'DELETE' });
      loadVersions();
    } else if (act === 'compare') {
      if (!state.compareFrom || state.compareFrom === id) {
        state.compareFrom = id;
        renderVersions(state.versions);
        toast('Pick a second version to compare against.');
      } else {
        showDiff(state.compareFrom, id);
        state.compareFrom = null;
        renderVersions(state.versions);
      }
    }
  };
}

async function commitVersion(message) {
  const result = await postJson('/api/versions', { message });
  if (result.ok) toast('Committed “' + result.version.message + '”.', 'ok');
  else toast(result.error || 'Commit failed.', 'bad');
}

async function showDiff(left, right) {
  const response = await fetch('/api/versions/compare?left=' +
    encodeURIComponent(left) + '&right=' + encodeURIComponent(right));
  const data = await response.json();
  const el = $('version-diff');
  el.hidden = false;
  el.innerHTML = '<h3>' + escapeHtml(data.left.message) + ' → ' +
    escapeHtml(data.right.message) + '</h3><p class="headline">' +
    escapeHtml(data.summary) + '</p>' +
    (data.changes.length
      ? '<table><tbody>' + data.changes.map((c) =>
          '<tr><td>' + escapeHtml(c.label) + '</td><td class="num">' +
          fmtNumber(c.from) + '</td><td class="num">→ ' + fmtNumber(c.to) +
          ' ' + escapeHtml(c.unit || '') + '</td></tr>').join('') +
        '</tbody></table>'
      : '');
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return response.json();
}

/* ================================================================= blob */

const blobCtx = () => $('blob').getContext('2d');
let blobPhase = 0;

function drawBlob() {
  requestAnimationFrame(drawBlob);
  const ctx = blobCtx();
  const size = 162, mid = size / 2;
  ctx.clearRect(0, 0, size, size);
  blobPhase += state.listening ? 0.055 : (state.busy ? 0.04 : 0.014);

  const energy = state.listening ? 0.35 + state.micLevel * 0.9
                                 : (state.busy ? 0.3 : 0.12);
  const base = 52 + (state.listening ? 6 : 0);
  const rings = [
    { r: base + 11, alpha: 0.13, speed: 0.7, lobes: 3 },
    { r: base + 5, alpha: 0.22, speed: 1.15, lobes: 4 },
    { r: base, alpha: 1.0, speed: 1.0, lobes: 5 },
  ];

  for (const ring of rings) {
    ctx.beginPath();
    for (let a = 0; a <= Math.PI * 2 + 0.01; a += 0.06) {
      const wobble =
        Math.sin(a * ring.lobes + blobPhase * ring.speed) * 4.5 * energy +
        Math.sin(a * (ring.lobes + 2) - blobPhase * 1.6) * 2.5 * energy +
        Math.sin(a * 2 + blobPhase * 0.6) * 1.5;
      const r = ring.r + wobble;
      const x = mid + Math.cos(a) * r, y = mid + Math.sin(a) * r;
      a === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.closePath();
    const grad = ctx.createLinearGradient(mid - base, mid - base, mid + base, mid + base);
    if (state.micPermission === 'denied') {
      grad.addColorStop(0, 'rgba(255,107,107,' + ring.alpha + ')');
      grad.addColorStop(1, 'rgba(160,60,60,' + ring.alpha + ')');
    } else if (state.listening) {
      grad.addColorStop(0, 'rgba(89,194,255,' + ring.alpha + ')');
      grad.addColorStop(0.5, 'rgba(127,123,255,' + ring.alpha + ')');
      grad.addColorStop(1, 'rgba(61,220,151,' + ring.alpha + ')');
    } else if (state.busy) {
      grad.addColorStop(0, 'rgba(255,200,87,' + ring.alpha + ')');
      grad.addColorStop(1, 'rgba(127,123,255,' + ring.alpha + ')');
    } else {
      grad.addColorStop(0, 'rgba(255,107,107,' + ring.alpha * 0.9 + ')');
      grad.addColorStop(1, 'rgba(210,60,60,' + ring.alpha * 0.9 + ')');
    }
    ctx.fillStyle = grad;
    ctx.fill();
  }

  ctx.beginPath();
  ctx.arc(mid, mid, 2.5 + energy * 4, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(232,238,247,' + (0.35 + energy) + ')';
  ctx.fill();
}

/* ================================================================ voice */

let audioCtx = null, analyser = null, micStream = null;
let voiceSocket = null, mediaRecorder = null, flushTimer = null;
let sttAvailable = false;

async function loadVoiceBackend() {
  try {
    const backend = await (await fetch('/api/voice/status')).json();
    sttAvailable = !!(backend.stt && backend.stt.backend);
    $('mic').disabled = !sttAvailable;
  } catch (err) { /* leave the default state */ }
}

// The mic-state/voice-backend/voice-note strip was dropped from the pill
// bar - the blob's own colour (see drawBlob) is the only status readout now.
function setMicState() {}

async function readMicPermission() {
  if (!navigator.permissions || !navigator.permissions.query) return 'unknown';
  try {
    const status = await navigator.permissions.query({ name: 'microphone' });
    state.micPermission = status.state;
    status.onchange = () => { state.micPermission = status.state; reflectMic(); };
    return status.state;
  } catch (err) {
    return 'unknown';
  }
}

function reflectMic() {
  if (state.listening || !sttAvailable) return;
  if (state.micPermission === 'denied') {
    setMicState('Microphone blocked — allow it in the address bar, or type below', 'bad');
  } else if (state.micPermission === 'granted') {
    setMicState('Tap to speak · microphone allowed', 'ok');
  } else {
    setMicState('Tap to speak · the browser will ask for the microphone');
  }
}

function handleVoiceMessage(event) {
  let msg;
  try { msg = JSON.parse(event.data); } catch (err) { return; }

  if (msg.type === 'partial') {
    $('ask').value = msg.text;
    setMicState('“' + msg.text + '”', 'ok');
  } else if (msg.type === 'voice_result') {
    stopListening();
    closeVoiceSocket();
    renderProposal(msg, msg.utterance);
  } else if (msg.type === 'unavailable') {
    sttAvailable = false;
    $('mic').disabled = true;
    stopListening();
    toast(msg.reason || 'Voice input is off.', 'warn');
  } else if (msg.type === 'voice_error') {
    stopListening();
    toast('Voice input stopped: ' + (msg.reason || 'unknown error'), 'bad');
  }
}

async function startListening() {
  if (state.listening || !sttAvailable) return;
  state.listening = true;
  setMicState('Listening — tap to stop', 'ok');

  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.micPermission = 'granted';
  } catch (err) {
    state.micPermission = 'denied';
    state.listening = false;
    reflectMic();
    return;
  }

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioCtx.createMediaStreamSource(micStream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  pollLevel();

  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  voiceSocket = new WebSocket(protocol + '//' + location.host + '/ws/voice');
  voiceSocket.binaryType = 'arraybuffer';
  voiceSocket.onmessage = handleVoiceMessage;
  voiceSocket.onerror = () => {
    if (state.listening) toast('Voice socket error — check the connection.', 'bad');
  };
  voiceSocket.onclose = () => {
    clearTimeout(flushTimer);
    flushTimer = null;
    if (state.listening) stopListening();
  };
  voiceSocket.onopen = () => {
    mediaRecorder = new MediaRecorder(micStream, { mimeType: 'audio/webm;codecs=opus' });
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size && voiceSocket && voiceSocket.readyState === WebSocket.OPEN) {
        voiceSocket.send(e.data);
      }
    };
    mediaRecorder.start(250);
  };
}

function pollLevel() {
  if (!state.listening || !analyser) { state.micLevel = 0; return; }
  const data = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteTimeDomainData(data);
  let sum = 0;
  for (const value of data) sum += (value - 128) * (value - 128);
  state.micLevel = Math.min(1, Math.sqrt(sum / data.length) / 26);
  requestAnimationFrame(pollLevel);
}

function stopListening() {
  if (!state.listening) return;
  state.listening = false;
  state.micLevel = 0;

  const recorder = mediaRecorder;
  mediaRecorder = null;
  if (recorder && recorder.state !== 'inactive') {
    recorder.onstop = requestFlush;
    try { recorder.stop(); } catch (err) { requestFlush(); }
  } else {
    requestFlush();
  }

  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; analyser = null; }
  reflectMic();
}

function requestFlush() {
  if (!voiceSocket || voiceSocket.readyState !== WebSocket.OPEN) {
    closeVoiceSocket();
    return;
  }
  try {
    voiceSocket.send(JSON.stringify({ type: 'stop' }));
  } catch (err) {
    closeVoiceSocket();
    return;
  }
  setMicState('Transcribing what you said…');
  clearTimeout(flushTimer);
  flushTimer = setTimeout(() => {
    if (voiceSocket) {
      setMicState('Nothing came back from the transcriber — type it below instead', 'warn');
    }
    closeVoiceSocket();
  }, 6000);
}

function closeVoiceSocket() {
  clearTimeout(flushTimer);
  flushTimer = null;
  if (voiceSocket) {
    try { voiceSocket.close(); } catch (err) { /* ignore */ }
    voiceSocket = null;
  }
}

/* ---- sentence -> proposed dial moves ---- */

async function submitUtterance(text) {
  if (!text || !text.trim()) return;
  try {
    const result = await postJson('/api/voice', { text });
    renderProposal(result, text);
  } catch (err) {
    toast('Could not parse that: ' + err.message, 'bad');
  }
}

function renderProposal(result, text) {
  state.proposal = result;
  const card = $('proposal');
  card.hidden = false;
  $('heard-text').textContent = result.utterance || text;
  $('proposal-source').textContent =
    result.source === 'llm' ? (result.backend?.model || 'hosted model') : 'local rules';
  $('proposal-source').className = 'tag ' + (result.source === 'llm' ? 'warn' : '');

  const edits = result.edits || [];
  const known = new Map((state.surface?.controls || []).map((c) => [c.id, c]));

  if (result.ok) {
    $('proposal-summary').textContent = result.summary || '';
    $('proposal-edits').innerHTML = edits.length
      ? edits.map((edit) => {
          const control = known.get(edit.id) || { label: edit.id, unit: '' };
          return '<div class="edit"><span class="elabel">' +
            escapeHtml(control.label) + '</span><span class="efrom">' +
            fmtEdit(control.value, control) + '</span><span class="earrow">→</span>' +
            '<span class="eto">' + fmtEdit(edit.value, control) + '</span></div>';
        }).join('')
      : '<p class="fineprint">This one moves no dial — it is a command, and ' +
        'running it is the whole effect.</p>';
    $('apply-proposal').hidden = false;
    $('apply-proposal').textContent = edits.length
      ? 'Apply to the dials' : 'Run it';
  } else {
    $('proposal-summary').textContent = '';
    $('proposal-edits').innerHTML =
      '<div class="refusal"><b>Out of scope</b>' +
      escapeHtml(result.reason || 'That is not something this model can do.') +
      '</div>';
    $('apply-proposal').hidden = true;
  }

  renderNotes('proposal-notes',
    [...(result.corrections || []), ...(result.notes || [])]);
}

function renderNotes(targetId, notes) {
  const box = $(targetId);
  box.innerHTML = '';
  for (const note of notes || []) {
    const div = document.createElement('div');
    div.className = 'note';
    div.textContent = note;
    box.appendChild(div);
  }
}

async function applyProposal() {
  const proposal = state.proposal;
  if (!proposal || !proposal.ok) return;
  $('apply-proposal').disabled = true;
  try {
    if (proposal.edits && proposal.edits.length) {
      await applyEdits(proposal.edits, 'voice');
    } else if (proposal.action) {
      const result = await postJson('/api/execute', {
        action: proposal.action, params: proposal.params || {},
      });
      if (!result.ok) toast(result.error || 'The instruction was refused.', 'bad');
      else if (result.applied === 'status') showStatus(result);
    }
    $('proposal').hidden = true;
    state.proposal = null;
  } finally {
    $('apply-proposal').disabled = false;
  }
}

function showStatus(result) {
  const metrics = result.metrics || {};
  const spoken = Object.keys((state.context && state.context.movements) || {});
  const lines = Object.entries(metrics.movements || {})
    .filter(([id, m]) => m.travel_time_s && (!spoken.length || spoken.includes(id)))
    .map(([id, m]) => id + ' ' + m.travel_time_s + ' s over ' +
                      m.corridor_length_m + ' m (n=' + m.n + ')');
  toast(lines.length
    ? 'Live, this run only: ' + lines.join(' · ')
    : 'Not enough vehicles have completed a measured corridor yet.');
}

function wireVoice() {
  readMicPermission().then(reflectMic);

  $('mic').onclick = () => state.listening ? stopListening() : startListening();
  $('ask-form').onsubmit = (event) => {
    event.preventDefault();
    submitUtterance($('ask').value);
  };
  $('apply-proposal').onclick = applyProposal;
  $('discard-proposal').onclick = () => {
    $('proposal').hidden = true;
    state.proposal = null;
  };

  window.addEventListener('keydown', (event) => {
    if (event.code === 'Space' && event.target === document.body) {
      event.preventDefault();
      state.listening ? stopListening() : startListening();
    }
  });
}

/* ============================================================ long jobs */

function startJob(job) {
  state.busy = true;
  $('job-card').hidden = false;
  $('job-title').textContent = job === 'validation'
    ? 'Validation fleet running' : 'Counterfactual running';
  $('job-log').innerHTML = '';
  $('job-bar').style.width = '0%';
  $('job-line').textContent = 'Dispatching…';
  if (job === 'validation') $('run-fleet').disabled = true;
}

function endJob() {
  state.busy = false;
  $('run-fleet').disabled = false;
}

function onProgress(msg) {
  if (msg.kind === 'run') {
    $('job-bar').style.width = (msg.done / Math.max(msg.total, 1) * 100) + '%';
    $('job-line').textContent = msg.tag + ': ' + msg.done + ' of ' + msg.total + ' runs';
  } else if (msg.kind === 'agent') {
    if (msg.state === 'started') {
      logJob('▸ ' + msg.agent);
      markAgent(msg.agent, 'running', '');
    } else {
      logJob('  ' + msg.agent + ' — ' + (msg.headline || msg.status));
      markAgent(msg.agent, msg.status, msg.headline);
    }
  } else if (msg.kind === 'calibration') {
    logJob('  dial ' + msg.veh_per_hour + ' veh/h → ' + msg.model_s +
           ' s (target ' + msg.target_s + ' s)');
  } else if (msg.kind === 'counterfactual') {
    $('job-line').textContent = 'phase: ' + msg.state +
      (msg.splits ? ' (' + msg.splits + ')' : '');
  } else if (msg.kind === 'warning') {
    logJob('  ! ' + msg.message);
  }
}

function logJob(text) {
  const line = document.createElement('div');
  line.textContent = text;
  const log = $('job-log');
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function finishJob(msg) {
  endJob();
  $('job-line').textContent = 'Finished.';
  $('job-bar').style.width = '100%';
  if (msg.job === 'validation') {
    renderValidation(msg.result);
    toast('Validation fleet finished: ' + msg.summary.statement);
  } else {
    renderCounterfactual(msg.result);
    toast('Counterfactual finished.');
  }
}

/* =========================================================== validation */

const AGENT_ORDER = ['archive-audit', 'calibration', 'movement', 'asymmetry',
                     'seed-stability', 'sensitivity'];

function markAgent(name, status, headline) {
  const container = $('val-agents');
  let row = document.getElementById('agent-' + name);
  if (!row) {
    row = document.createElement('div');
    row.className = 'agent-row';
    row.id = 'agent-' + name;
    row.innerHTML = '<span class="state"></span><span><span class="name"></span>' +
                    '<span class="head"></span></span><span class="secs"></span>';
    container.appendChild(row);
  }
  row.querySelector('.state').className = 'state ' + (status || '');
  row.querySelector('.name').textContent = name;
  if (headline) row.querySelector('.head').textContent = headline;
}

function renderValidation(payload) {
  const summary = payload.summary || {};
  const tag = $('val-overall');
  tag.textContent = summary.overall || 'not run';
  tag.className = 'tag ' + ({ pass: 'pass', fail: 'fail', partial: 'warn' }[summary.overall] || '');
  $('val-statement').textContent = summary.statement || '';
  $('val-limitation').textContent = summary.limitation || '';

  const status = $('model-status');
  if (summary.overall === 'pass') {
    if (status) status.querySelector('.dot').className = 'dot';
    setChrome('model-status-text', 'Validated · ' + payload.slot);
  } else if (summary.overall) {
    if (status) status.querySelector('.dot').className = 'dot amber';
    setChrome('model-status-text', 'Model ' + summary.overall + ' · ' + payload.slot);
  }

  const movement = (payload.agents || []).find((a) => a.name === 'movement');
  const rows = movement?.data?.rows || [];
  $('val-table').innerHTML = rows.length ? validationTable(rows, payload) : '';

  $('val-agents').innerHTML = '';
  for (const name of AGENT_ORDER) {
    const agent = (payload.agents || []).find((a) => a.name === name);
    if (agent) markAgent(name, agent.status, agent.headline);
  }
}

function validationTable(rows, payload) {
  const head = '<table><thead><tr><th>Movement</th><th class="num">Real</th>' +
    '<th class="num">Model</th><th class="num">Error</th><th>Verdict</th></tr></thead><tbody>';
  const body = rows.map((row) => {
    const verdictClass = row.verdict === 'pass' ? 'verdict-pass'
      : row.verdict === 'fail' ? 'verdict-fail' : 'verdict-na';
    const real = row.measured_s == null ? '—'
      : row.measured_s + ' s<span class="range">n=' + row.measured_n +
        ', ' + (row.measured_spread_p10_p90_s || []).join('–') + ' s</span>';
    const model = row.model_median_s == null ? '—'
      : row.model_median_s + ' s<span class="range">' + row.model_runs +
        ' runs, CI ' + (row.model_ci95_s || []).join('–') + ' s</span>';
    return '<tr class="' + (row.comparable ? '' : 'excluded') + '">' +
      '<td>' + row.movement +
        (row.kind === 'turn' ? '<span class="kind">turn</span>' : '') +
        '<span class="range">' + (row.model_corridor_m || '?') + ' m model / ' +
        (row.archive_corridor_m || '?') + ' m measured</span></td>' +
      '<td class="num">' + real + '</td>' +
      '<td class="num">' + model + '</td>' +
      '<td class="num">' + (row.error_pct == null ? '—' :
        (row.error_pct > 0 ? '+' : '') + row.error_pct + '%') + '</td>' +
      '<td class="' + verdictClass + '">' + row.verdict +
        (row.inside_measurement_spread
          ? '<span class="range">inside the measurement spread</span>' : '') +
      '</td></tr>';
  }).join('');

  const excludedNotes = rows.filter((r) => !r.comparable).map(
    (r) => '<div class="note">' + escapeHtml(r.note) + '</div>').join('');

  return head + body + '</tbody></table>' +
    '<p class="fineprint">Median of ' + (payload.seeds || '?') +
    ' runs per movement, at ' +
    (payload.summary?.calibrated_veh_per_hour || '?') + ' veh/h.</p>' +
    (excludedNotes ? '<div class="notes">' + excludedNotes + '</div>' : '');
}

/* ======================================================= counterfactual */

function renderCounterfactual(card) {
  $('cf-card').hidden = false;
  const verdict = card.verdict || {};
  const tag = $('cf-verdict-tag');
  tag.textContent = verdict.junction_overall || '—';
  tag.className = 'tag ' + ({ improves: 'pass', worsens: 'fail' }[verdict.junction_overall] || 'warn');
  $('cf-headline').textContent = verdict.headline || '';

  const head = '<table><thead><tr><th>Approach</th><th class="num">Mean queue change</th>' +
    '<th>Verdict</th></tr></thead><tbody>';
  const body = ['N', 'E', 'S', 'W'].map((arm) => {
    const cmp = card.approaches?.[arm]?.queue_mean_veh?.comparison || {};
    const band = cmp.ci95_change_pct
      ? cmp.ci95_change_pct[0] + '% to ' + cmp.ci95_change_pct[1] + '%' : '—';
    const cls = cmp.verdict === 'improves' ? 'verdict-pass'
      : cmp.verdict === 'worsens' ? 'verdict-fail' : 'verdict-na';
    return '<tr><td>' + arm + '</td><td class="num">' + band +
      '<span class="range">' + (cmp.n_pairs || 0) + ' paired seeds</span></td>' +
      '<td class="' + cls + '">' + (cmp.verdict || '—') + '</td></tr>';
  }).join('');
  $('cf-table').innerHTML = head + body + '</tbody></table>';

  renderNotes('cf-notes', card.notes);
}

/* ============================================================== archive */

function renderArchive() {
  const archive = state.context.archive;
  const coverage = archive.coverage || {};
  const load = archive.load || {};
  const dropped = (load.dropped_error || 0) + (load.dropped_no_time || 0) +
                  (load.dropped_bad_status || 0);
  $('archive-summary').innerHTML = [
    ['Observations', coverage.observations ?? 0],
    ['Days covered', coverage.days ?? 0],
    ['First', (coverage.first || '—').slice(0, 16)],
    ['Last', (coverage.last || '—').slice(0, 16)],
    ['Junk rows dropped', dropped],
    ['Sheets read', (load.sheets_used || []).length],
  ].map(([k, v]) => '<dt>' + k + '</dt><dd>' + v + '</dd>').join('');

  $('archive-note').textContent =
    'Read from ' + (load.sheets_used || []).join(', ') + '. Skipped ' +
    ((load.sheets_skipped || []).join(', ') || 'nothing') +
    ' — collected before the corridor endpoints were fixed.';

  drawProfile(archive.daily_profile || {});
}

function drawProfile(profiles) {
  const canvas = $('profile');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 340;
  canvas.width = width * ratio;
  canvas.height = 120 * ratio;
  const ctx = canvas.getContext('2d');
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, 120);

  const all = Object.values(profiles).flat();
  if (!all.length) return;
  const maxTime = Math.max(...all.map((p) => p.p90_s)) * 1.05;
  const hours = [...new Set(all.map((p) => p.hour))].sort((a, b) => a - b);
  const x = (h) => 26 + (hours.indexOf(h) / Math.max(hours.length - 1, 1)) * (width - 40);
  const y = (v) => 104 - (v / maxTime) * 88;

  ctx.strokeStyle = 'rgba(34,48,68,.9)';
  ctx.beginPath(); ctx.moveTo(24, 104); ctx.lineTo(width - 10, 104); ctx.stroke();

  const colours = { NS: '#59c2ff', SN: '#7f7bff', EW: '#3ddc97', WE: '#ffc857' };
  for (const [movement, points] of Object.entries(profiles)) {
    if (!points.length) continue;
    ctx.strokeStyle = colours[movement] || '#93a4bb';
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    points.forEach((p, i) => {
      const px = x(p.hour), py = y(p.median_s);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    ctx.stroke();
  }

  ctx.fillStyle = 'rgba(99,117,141,.9)';
  ctx.font = '9px ui-monospace, monospace';
  ctx.fillText(Math.round(maxTime) + 's', 2, 18);
  ctx.fillText('0', 12, 106);
  hours.forEach((h, i) => {
    if (i % Math.ceil(hours.length / 6) === 0) {
      ctx.fillText(String(h).padStart(2, '0'), x(h) - 6, 117);
    }
  });
}

function wireResults() {
  $('run-fleet').onclick = async () => {
    const seeds = Number($('fleet-seeds').value);
    await postJson('/api/execute', { action: 'run_validation', params: { seeds } });
  };
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ======================================================== view switching */

function wireViews() {
  const tabSim = $('tab-sim-view');
  const tabDash = $('tab-dash-view');
  const viewSim = $('view-sim');
  const viewDash = $('view-dash');

  if (!tabSim || !tabDash) return;

  function setView(viewName) {
    if (viewName === 'sim') {
      tabSim.classList.add('active');
      tabDash.classList.remove('active');
      viewSim.hidden = false;
      viewDash.hidden = true;
      requestAnimationFrame(() => {
        if (window.Scene3D && window.Scene3D.resize) {
          window.Scene3D.resize();
        }
      });
    } else {
      tabDash.classList.add('active');
      tabSim.classList.remove('active');
      viewDash.hidden = false;
      viewSim.hidden = true;
      refreshWorkflowRuns();
      updateDashMetrics();
    }
  }

  tabSim.addEventListener('click', () => setView('sim'));
  tabDash.addEventListener('click', () => setView('dash'));
}

/* ===================================================== cli & workflows dash */

let cliSocket = null;
let cmdHistory = [];
let cmdHistoryIndex = -1;

function wireCliDash() {
  connectCliSocket();
  setupTerminal();
  setupWorkflowTriggers();
  setupAgentsDeck();
  setupCounterfactualStudio();
  updateDashMetrics();
  refreshWorkflowRuns();
}

function updateDashMetrics() {
  if (!state.context) return;
  const ctx = state.context;
  const archive = ctx.archive || {};
  const slots = archive.hour_slots || [];
  const slot = slots.length ? slots[slots.length - 1] : 'weekday 09:00-10:00';

  if ($('dash-active-slot')) $('dash-active-slot').textContent = slot;
  if ($('dash-demand-vph')) {
    const vph = state.surface?.state?.['demand.veh_per_hour'] || 2400;
    $('dash-demand-vph').innerHTML = Math.round(vph) + ' <span class="unit">veh/h</span>';
  }
  if ($('dash-signal-plan')) {
    const planName = ctx.active_phase_plan || 'four_phase';
    $('dash-signal-plan').textContent = ctx.phase_plans?.[planName]?.label || planName;
  }
}

function connectCliSocket() {
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/cli';
  cliSocket = new WebSocket(url);

  cliSocket.onopen = () => {
    appendTerminalLine('System', 'Connected to Abhyas CLI & Render Workflows Engine', 'term-ok');
  };

  cliSocket.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (!msg) return;

      if (msg.type === 'cli_init') {
        const lines = msg.history || [];
        const term = $('terminal-lines');
        if (term) term.innerHTML = '';
        lines.forEach((l) => appendTerminalLineRaw(l));
        if (msg.workflows) updateWorkflowStatus(msg.workflows);
        if (msg.runs) renderWorkflowRuns(msg.runs);
      } else if (msg.type === 'cli_output') {
        appendTerminalLineRaw(msg.line, msg.is_progress);
      } else if (msg.type === 'cli_status') {
        const busy = Boolean(msg.busy);
        if ($('terminal-busy-badge')) {
          $('terminal-busy-badge').textContent = busy ? 'Executing...' : 'Ready';
          $('terminal-busy-badge').className = busy ? 'badge busy' : 'badge';
        }
        if ($('dash-busy-indicator')) $('dash-busy-indicator').hidden = !busy;
      } else if (msg.type === 'workflow_started') {
        toast('Workflow started: ' + (msg.record?.task_title || 'Task'), 'info');
        refreshWorkflowRuns();
      } else if (msg.type === 'workflow_progress') {
        if (msg.headline) {
          appendTerminalLine('Workflow', msg.headline, 'term-info');
        }
      } else if (msg.type === 'workflow_finished') {
        toast('Workflow completed: ' + (msg.record?.task_title || 'Task'), 'good');
        refreshWorkflowRuns();
        if (msg.record?.task === 'counterfactual' && msg.record?.result?.verdict) {
          showDashCounterfactualVerdict(msg.record.result.verdict, msg.record.result.notes);
        }
      } else if (msg.type === 'workflow_failed') {
        toast('Workflow failed: ' + (msg.record?.error || 'Unknown error'), 'bad');
        refreshWorkflowRuns();
      }
    } catch (err) {
      console.error('cliSocket parse error:', err);
    }
  };

  cliSocket.onclose = () => {
    setTimeout(connectCliSocket, 2000);
  };
}

function appendTerminalLineRaw(rawText, isProgress = false) {
  const container = $('terminal-lines');
  if (!container) return;

  let cls = 'term-line';
  let cleanText = String(rawText ?? '');

  if (cleanText.includes('[ok]')) cls += ' term-ok';
  else if (cleanText.includes('[warn]')) cls += ' term-warn';
  else if (cleanText.includes('[FAIL]')) cls += ' term-fail';
  else if (cleanText.startsWith('>>')) cls += ' term-step';
  else if (cleanText.startsWith('you>')) cls += ' term-prompt-line';
  else if (cleanText.startsWith('===') || cleanText.startsWith('---')) cls += ' term-rule';
  else if (cleanText.startsWith('  Abhyas -') || cleanText.startsWith('Verdict') || cleanText.startsWith('What do you')) cls += ' term-headline';

  if (isProgress && container.lastElementChild && container.lastElementChild.classList.contains('term-prog')) {
    container.lastElementChild.textContent = cleanText;
  } else {
    const div = document.createElement('div');
    div.className = cls + (isProgress ? ' term-prog' : '');
    div.textContent = cleanText;
    container.appendChild(div);
  }

  while (container.children.length > 1000) {
    container.removeChild(container.firstChild);
  }

  if ($('term-autoscroll')?.checked) {
    const win = $('terminal-window');
    if (win) win.scrollTop = win.scrollHeight;
  }
}

function appendTerminalLine(tag, text, customCls = '') {
  const container = $('terminal-lines');
  if (!container) return;
  const div = document.createElement('div');
  div.className = 'term-line ' + customCls;
  div.textContent = (tag ? '[' + tag + '] ' : '') + text;
  container.appendChild(div);
  if ($('term-autoscroll')?.checked) {
    const win = $('terminal-window');
    if (win) win.scrollTop = win.scrollHeight;
  }
}

function setupTerminal() {
  const form = $('terminal-form');
  const input = $('terminal-input');
  if (!form || !input) return;

  form.onsubmit = (e) => {
    e.preventDefault();
    const cmd = input.value.trim();
    if (!cmd) return;
    cmdHistory.push(cmd);
    cmdHistoryIndex = cmdHistory.length;
    input.value = '';

    if (cliSocket && cliSocket.readyState === WebSocket.OPEN) {
      cliSocket.send(JSON.stringify({ type: 'command', command: cmd }));
    } else {
      postJson('/api/cli/execute', { command: cmd });
    }
  };

  input.onkeydown = (e) => {
    if (e.key === 'ArrowUp') {
      if (cmdHistoryIndex > 0) {
        cmdHistoryIndex--;
        input.value = cmdHistory[cmdHistoryIndex] || '';
      }
      e.preventDefault();
    } else if (e.key === 'ArrowDown') {
      if (cmdHistoryIndex < cmdHistory.length - 1) {
        cmdHistoryIndex++;
        input.value = cmdHistory[cmdHistoryIndex] || '';
      } else {
        cmdHistoryIndex = cmdHistory.length;
        input.value = '';
      }
      e.preventDefault();
    }
  };

  if ($('btn-term-clear')) {
    $('btn-term-clear').onclick = () => {
      $('terminal-lines').innerHTML = '';
    };
  }

  if ($('btn-term-copy')) {
    $('btn-term-copy').onclick = () => {
      const text = $('terminal-lines').innerText;
      navigator.clipboard.writeText(text).then(() => toast('Copied terminal output', 'good'));
    };
  }

  document.querySelectorAll('.cli-pill').forEach((btn) => {
    btn.onclick = () => {
      const cmd = btn.getAttribute('data-cmd');
      if (!cmd) return;
      if (cliSocket && cliSocket.readyState === WebSocket.OPEN) {
        cliSocket.send(JSON.stringify({ type: 'command', command: cmd }));
      } else {
        postJson('/api/cli/execute', { command: cmd });
      }
    };
  });
}

function updateWorkflowStatus(wf) {
  if (!wf) return;
  const isCloud = wf.mode === 'cloud';
  if ($('wf-engine-text')) $('wf-engine-text').textContent = isCloud ? 'Render Workflows: Cloud' : 'Render Workflows: Local';
  if ($('dash-wf-mode')) $('dash-wf-mode').textContent = isCloud ? 'Render Cloud' : 'Local Worker';
  if ($('dash-wf-tasks-count')) $('dash-wf-tasks-count').textContent = (wf.registered_tasks?.length || 5) + ' tasks registered';
}

function setupWorkflowTriggers() {
  if ($('btn-wf-fleet')) {
    $('btn-wf-fleet').onclick = async () => {
      await postJson('/api/workflows/trigger', { task: 'run_fleet', params: { seeds: 30 } });
      toast('Triggered Fleet Validation Workflow', 'good');
    };
  }

  if ($('btn-wf-calibration')) {
    $('btn-wf-calibration').onclick = async () => {
      await postJson('/api/workflows/trigger', { task: 'run_agent', params: { agent_name: 'calibration', seeds: 12 } });
      toast('Triggered Calibration Agent Workflow', 'good');
    };
  }

  if ($('btn-wf-counterfactual')) {
    $('btn-wf-counterfactual').onclick = async () => {
      const group = $('cf-group-select')?.value || 'north_south';
      const delta = Number($('cf-delta-slider')?.value || 10);
      const seeds = Number($('cf-seeds-select')?.value || 30);
      const splits = Boolean($('cf-splits-sweep')?.checked);
      await postJson('/api/workflows/trigger', {
        task: 'counterfactual',
        params: { delta_seconds: delta, phase_group: group, seeds, splits_sweep: splits }
      });
      toast('Triggered Counterfactual Workflow', 'good');
    };
  }

  if ($('btn-wf-selftest')) {
    $('btn-wf-selftest').onclick = async () => {
      await postJson('/api/workflows/trigger', { task: 'selftest', params: {} });
      toast('Triggered Pipeline Selftest Workflow', 'good');
    };
  }

  if ($('btn-wf-netbuild')) {
    $('btn-wf-netbuild').onclick = async () => {
      await postJson('/api/workflows/trigger', { task: 'netbuild', params: { force: true } });
      toast('Triggered OSM Network Rebuild Workflow', 'good');
    };
  }

  if ($('btn-refresh-runs')) $('btn-refresh-runs').onclick = () => refreshWorkflowRuns();

  if ($('btn-close-modal')) $('btn-close-modal').onclick = () => { $('run-modal').hidden = true; };
  if ($('run-modal')) {
    $('run-modal').onclick = (e) => {
      if (e.target === $('run-modal')) $('run-modal').hidden = true;
    };
  }
}

async function refreshWorkflowRuns() {
  try {
    const runs = await (await fetch('/api/workflows/runs')).json();
    renderWorkflowRuns(runs);
    const status = await (await fetch('/api/workflows/status')).json();
    updateWorkflowStatus(status);
  } catch (err) {
    console.error('Failed to refresh workflow runs:', err);
  }
}

function renderWorkflowRuns(runs) {
  const tbody = $('wf-runs-tbody');
  if (!tbody) return;
  if (!runs || !runs.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No workflow runs logged yet.</td></tr>';
    if ($('wf-runs-summary')) $('wf-runs-summary').textContent = '0 runs';
    return;
  }

  if ($('wf-runs-summary')) $('wf-runs-summary').textContent = runs.length + ' run' + (runs.length === 1 ? '' : 's');

  tbody.innerHTML = runs.slice(0, 15).map((r) => {
    const dur = r.duration_s ? r.duration_s + 's' : (r.status === 'running' ? 'running...' : '—');
    const badgeCls = r.status === 'completed' ? 'badge-good'
      : r.status === 'running' ? 'badge-info'
      : r.status === 'failed' ? 'badge-bad' : 'badge-queued';
    return `
      <tr>
        <td><strong>${escapeHtml(r.task_title || r.task)}</strong></td>
        <td><span class="status-pill-small ${badgeCls}">${escapeHtml(r.status)}</span></td>
        <td class="num">${dur}</td>
        <td><code>${escapeHtml(r.id)}</code></td>
        <td><button class="btn-micro" onclick="showRunDetails('${escapeHtml(r.id)}')">View</button></td>
      </tr>
    `;
  }).join('');
}

window.showRunDetails = async function(runId) {
  try {
    const run = await (await fetch('/api/workflows/runs/' + encodeURIComponent(runId))).json();
    if (!run) return;
    $('modal-run-title').textContent = (run.task_title || run.task) + ' — ' + run.id;
    $('modal-meta-grid').innerHTML = `
      <div class="meta-item"><label>Status</label><span>${escapeHtml(run.status)}</span></div>
      <div class="meta-item"><label>Duration</label><span>${run.duration_s ? run.duration_s + 's' : '—'}</span></div>
      <div class="meta-item"><label>Started</label><span>${escapeHtml(run.started_at || '—')}</span></div>
      <div class="meta-item"><label>Finished</label><span>${escapeHtml(run.finished_at || '—')}</span></div>
      <div class="meta-item full"><label>Parameters</label><pre>${escapeHtml(JSON.stringify(run.params, null, 2))}</pre></div>
    `;

    const logs = run.logs || [];
    if (!logs.length) {
      $('modal-logs').innerHTML = '<div class="empty-logs">No progressive log events captured for this run.</div>';
    } else {
      $('modal-logs').innerHTML = logs.map((l) => {
        const ev = l.event || {};
        const head = ev.headline || ev.message || JSON.stringify(ev);
        return `<div class="log-entry"><span class="log-time">${new Date((l.t || 0) * 1000).toLocaleTimeString()}</span> ${escapeHtml(head)}</div>`;
      }).join('');
    }

    $('run-modal').hidden = false;
  } catch (err) {
    toast('Could not fetch run details: ' + err.message, 'bad');
  }
};

async function setupAgentsDeck() {
  const container = $('dash-agents-deck');
  if (!container) return;
  try {
    const agents = await (await fetch('/api/agents/list')).json();
    container.innerHTML = agents.map((a, i) => `
      <div class="agent-card">
        <div class="agent-card-head">
          <span class="agent-idx">${i + 1}</span>
          <span class="agent-name">${escapeHtml(a.name)}</span>
        </div>
        <h4>${escapeHtml(a.title)}</h4>
        <p class="agent-blurb">${escapeHtml(a.blurb)}</p>
        <div class="agent-footer">
          <span class="agent-mode">${a.needs_simulation ? 'SUMO runs' : 'Sheet audit'}</span>
          <button class="btn-subtle" onclick="runSingleAgentAction('${escapeHtml(a.name)}')">Run Agent</button>
        </div>
      </div>
    `).join('');
  } catch (err) {
    console.error('Failed to load agents deck:', err);
  }
}

window.runSingleAgentAction = async function(agentName) {
  try {
    await postJson('/api/workflows/trigger', {
      task: 'run_agent',
      params: { agent_name: agentName, seeds: 16 }
    });
    toast('Dispatched agent workflow: ' + agentName, 'good');
  } catch (err) {
    toast('Error running agent: ' + err.message, 'bad');
  }
};

function setupCounterfactualStudio() {
  const slider = $('cf-delta-slider');
  const out = $('cf-delta-val');
  if (slider && out) {
    slider.oninput = () => {
      const v = Number(slider.value);
      out.textContent = (v > 0 ? '+' : '') + v + 's';
    };
  }

  const btn = $('btn-trigger-cf');
  if (btn) {
    btn.onclick = async () => {
      const group = $('cf-group-select')?.value || 'north_south';
      const delta = Number($('cf-delta-slider')?.value || 10);
      const seeds = Number($('cf-seeds-select')?.value || 30);
      const splits = Boolean($('cf-splits-sweep')?.checked);

      btn.disabled = true;
      btn.textContent = 'Executing paired seeds...';
      try {
        await postJson('/api/workflows/trigger', {
          task: 'counterfactual',
          params: { delta_seconds: delta, phase_group: group, seeds, splits_sweep: splits }
        });
        toast('Counterfactual sweep triggered in Render Workflows', 'good');
      } catch (err) {
        toast('Failed to trigger counterfactual: ' + err.message, 'bad');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Dispatch Counterfactual Analysis';
      }
    };
  }
}

function showDashCounterfactualVerdict(verdict, notes) {
  const box = $('dash-cf-result');
  if (!box) return;
  box.hidden = false;
  $('dash-cf-verdict-banner').textContent = verdict?.headline || 'Analysis Completed';
  $('dash-cf-notes').innerHTML = (notes || []).map((n) => '<div class="note-bullet">• ' + escapeHtml(n) + '</div>').join('');
}

boot();
