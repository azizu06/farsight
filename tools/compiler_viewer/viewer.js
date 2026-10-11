// farsight-compile environment viewer.
// PBR three.js viewer for compiler outputs: scene.glb | mesh.ply | splat.ply |
// points.ply | report.json, served by `farsight-compile view`.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { GTAOPass } from 'three/addons/postprocessing/GTAOPass.js';
import { FXAAPass } from 'three/addons/postprocessing/FXAAPass.js';
import { parsePLY, vertexArrays } from '/viewer/ply.js';
import { SplatCloud, splatFieldsFromPLY } from '/viewer/splats.js';

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(location.search);
const errors = [];
window.addEventListener('error', (e) => errors.push(String(e.message || e)));

const state = {
  mode: params.get('mode') || null,
  quality: params.get('q') || 'high',
  camMode: 'orbit',
  assets: { files: [], report: null },
  mesh: null,      // THREE.Object3D used by mesh modes
  meshGlb: null,   // original GLB scene for textured mode
  points: null,
  splat: null,
  supportMax: 0.05,
  floorY: 0,
  diag: 10,
};

// ---------- renderer / scene ----------
const canvas = $('#view');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, powerPreference: 'high-performance' });
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0e12);

const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 500);
camera.position.set(4, 3, 5);

const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
scene.environmentIntensity = 0.55;

const key = new THREE.DirectionalLight(0xfff4e0, 2.2);
key.position.set(3, 6, 2);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.bias = -0.0002;
key.shadow.normalBias = 0.02;
scene.add(key, key.target);
const fill = new THREE.HemisphereLight(0x9db4d4, 0x2a2620, 0.5);
scene.add(fill);

// ---------- helpers ----------
function geometryFromArrays(a) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(a.position, 3));
  if (a.color) g.setAttribute('color', new THREE.BufferAttribute(a.color, 3));
  if (a.normal) g.setAttribute('normal', new THREE.BufferAttribute(a.normal, 3));
  if (a.uv) g.setAttribute('uv', new THREE.BufferAttribute(a.uv, 2));
  if (a.support) g.setAttribute('support', new THREE.BufferAttribute(a.support, 1));
  if (a.flags) g.setAttribute('flags', new THREE.BufferAttribute(a.flags, 1));
  if (a.index) g.setIndex(new THREE.BufferAttribute(a.index, 1));
  if (!a.normal) g.computeVertexNormals();
  return g;
}

// measured->interpolated heatmap material (support metres -> ramp)
function heatMaterial() {
  return new THREE.ShaderMaterial({
    uniforms: { uMax: { value: state.supportMax } },
    vertexShader: /* glsl */`
      attribute float support;
      varying float vS;
      void main() { vS = support; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: /* glsl */`
      uniform float uMax; varying float vS;
      vec3 ramp(float t) {
        vec3 a = vec3(0.10, 0.75, 0.65); // teal: measured
        vec3 b = vec3(1.00, 0.75, 0.20); // amber
        vec3 c = vec3(1.00, 0.15, 0.30); // red: hallucinated
        return t < 0.5 ? mix(a, b, t * 2.0) : mix(b, c, (t - 0.5) * 2.0);
      }
      void main() { gl_FragColor = vec4(ramp(clamp(vS / uMax, 0.0, 1.0)), 1.0); }`,
  });
}

function flagPointsMaterial(size) {
  return new THREE.ShaderMaterial({
    vertexColors: true,
    uniforms: { uSize: { value: size } },
    vertexShader: /* glsl */`
      attribute float flags;
      varying vec3 vC;
      uniform float uSize;
      void main() {
        vC = (flags > 0.5) ? vec3(1.0, 0.35, 0.15) : color;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = uSize * (200.0 / max(0.5, -mv.z));
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */`
      varying vec3 vC;
      void main() {
        vec2 d = gl_PointCoord - 0.5;
        if (dot(d, d) > 0.25) discard;
        gl_FragColor = vec4(vC, 1.0);
      }`,
  });
}

// ---------- load artefacts ----------
async function fetchBuf(name) {
  const r = await fetch(`/out/${name}`);
  if (!r.ok) throw new Error(`${name}: ${r.status}`);
  return r.arrayBuffer();
}
async function fetchJSON(url) { const r = await fetch(url); return r.ok ? r.json() : null; }

async function load() {
  const manifest = await fetchJSON('/api/manifest');
  if (!manifest) { showMsg('viewer API unreachable'); return; }
  state.assets.files = manifest.files;
  const have = (n) => manifest.files.some((f) => f.name === n);

  const jobs = [];
  jobs.push(fetchJSON('/out/report.json').then((r) => { state.assets.report = r; }).catch(() => {}));

  if (have('scene.glb')) {
    jobs.push(new Promise((res) => {
      new GLTFLoader().load('/out/scene.glb', (g) => { state.meshGlb = g.scene; res(); }, undefined,
        (e) => { errors.push('glb: ' + e); res(); });
    }));
  }
  if (have('mesh.ply')) {
    jobs.push(fetchBuf('mesh.ply').then((b) => { state._meshPly = parsePLY(b); }).catch((e) => errors.push(String(e))));
  }
  if (have('points.ply')) {
    jobs.push(fetchBuf('points.ply').then((b) => { state._pointsPly = parsePLY(b); }).catch((e) => errors.push(String(e))));
  }
  if (have('splat.ply')) {
    jobs.push(fetchBuf('splat.ply').then((b) => {
      const fields = splatFieldsFromPLY(parsePLY(b));
      const cap = +params.get('splatcap') || 0;
      if (cap > 0 && cap < fields.n) subsampleFields(fields, cap);
      state.splat = new SplatCloud(fields);
    }).catch((e) => errors.push('splat: ' + e)));
  }
  if (!have('points.ply') && have('cloud.ply')) jobs.push(fetchBuf('cloud.ply').then((b) => { state._pointsPly = parsePLY(b); }).catch(() => {}));

  await Promise.all(jobs);
  buildScene();
  buildHUD();
  $('#msg').classList.add('hidden');
}

// Keep every ceil(n/cap)-th splat for slow renderers (headless SwiftShader checks).
function subsampleFields(f, cap) {
  const n = f.n, stride = n / cap;
  const pick = (src, w) => { const d = new Float32Array(cap * w); for (let i = 0; i < cap; i++) { const s = Math.floor(i * stride); for (let k = 0; k < w; k++) d[i * w + k] = src[s * w + k]; } return d; };
  f.positions = pick(f.positions, 3); f.colors = pick(f.colors, 3);
  f.opacities = pick(f.opacities, 1); f.scales = pick(f.scales, 3); f.rotations = pick(f.rotations, 4);
  f.n = cap;
}

function meshGeometry() {
  // Prefer GLB geometry (may carry _SUPPORT/uv/texture) else mesh.ply.
  if (state.meshGlb) {
    let geo = null;
    state.meshGlb.traverse((o) => { if (!geo && o.isMesh) geo = o.geometry; });
    if (geo) return geo;
  }
  if (state._meshPly) return geometryFromArrays(vertexArrays(state._meshPly));
  return null;
}

function supportAttr(geo) {
  return geo.getAttribute('support') || geo.getAttribute('_SUPPORT') || null;
}

function buildScene() {
  const geo = meshGeometry();
  const group = new THREE.Group();
  group.name = 'artefacts';
  scene.add(group);
  state.group = group;

  if (state.meshGlb) {
    state.meshGlb.traverse((o) => {
      if (o.isMesh) {
        o.castShadow = true; o.receiveShadow = true;
        const a = supportAttr(o.geometry);
        if (a) {
          if (!o.geometry.getAttribute('support')) o.geometry.setAttribute('support', a);
          for (const v of a.array) state.supportMax = Math.max(state.supportMax, v);
        }
      }
    });
  }
  if (state._meshPly) {
    const a = vertexArrays(state._meshPly);
    if (a.support) for (const v of a.support) state.supportMax = Math.max(state.supportMax, v);
  }

  // mesh.ply points -> Points object (points mode)
  if (state._pointsPly) {
    const a = vertexArrays(state._pointsPly);
    const pg = new THREE.BufferGeometry();
    pg.setAttribute('position', new THREE.BufferAttribute(a.position, 3));
    if (a.color) pg.setAttribute('color', new THREE.BufferAttribute(a.color, 3));
    else { const c = new Float32Array(a.n * 3).fill(0.75); pg.setAttribute('color', new THREE.BufferAttribute(c, 3)); }
    pg.setAttribute('flags', new THREE.BufferAttribute(a.flags || new Float32Array(a.n), 1));
    state.points = new THREE.Points(pg, flagPointsMaterial(2.0));
    state.points.frustumCulled = false;
    state._pointsA = a;
  }

  if (state.splat) group.add(state.splat);

  // compute scene bounds
  const box = new THREE.Box3();
  let found = false;
  const grow = (g) => { if (g) { g.computeBoundingBox(); box.union(g.boundingBox); found = true; } };
  if (state.meshGlb) { const b = new THREE.Box3().setFromObject(state.meshGlb); box.union(b); found = true; }
  if (geo && !state.meshGlb) grow(geo);
  if (state._pointsA && !found) {
    const p = state._pointsA.position;
    for (let i = 0; i < state._pointsA.n; i++) box.expandByPoint(new THREE.Vector3(p[3 * i], p[3 * i + 1], p[3 * i + 2]));
    found = true;
  }
  if (state.splat && !found) {
    const p = state.splat._positions;
    for (let i = 0; i < state.splat.count; i++) box.expandByPoint(new THREE.Vector3(p[3 * i], p[3 * i + 1], p[3 * i + 2]));
    found = true;
  }
  if (found) {
    state.floorY = box.min.y;
    state.diag = box.getSize(new THREE.Vector3()).length() || 10;
    state.bounds = { min: box.min.toArray(), max: box.max.toArray(), center: box.getCenter(new THREE.Vector3()).toArray() };
    const c = box.getCenter(new THREE.Vector3());
    camera.position.set(c.x + state.diag * 0.45, c.y + state.diag * 0.35, c.z + state.diag * 0.45);
    controls?.target.copy(c);
    camera.far = Math.max(500, state.diag * 10);
    camera.updateProjectionMatrix();
    const s = key.shadow.camera;
    s.left = s.bottom = -state.diag * 0.7; s.right = s.top = state.diag * 0.7;
    s.near = 0.1; s.far = state.diag * 4;
    key.shadow.camera.updateProjectionMatrix();
  }

  // mesh objects per mode (rebuilt on mode change is fine, geo is shared)
  state.meshGeo = geo;
  state.modeObjects = {};
  setMode(state.mode || (state.splat ? 'blend' : state.meshGlb || geo ? 'textured' : 'points'));
}

function meshWith(mat) {
  if (!state.meshGeo) return null;
  const m = new THREE.Mesh(state.meshGeo, mat);
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

function texturedObject() {
  if (state.meshGlb) return state.meshGlb;
  return meshWith(new THREE.MeshStandardMaterial({ vertexColors: !!state.meshGeo?.getAttribute('color'), roughness: 0.9, metalness: 0.02 }));
}

// ---------- modes ----------
const MODES = [
  { key: 'textured', label: 'textured', need: () => state.meshGlb || state.meshGeo,
    make: () => texturedObject() },
  { key: 'vcolor', label: 'vertex color', need: () => state.meshGeo,
    make: () => meshWith(new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.92, metalness: 0.0 })) },
  { key: 'heatmap', label: 'support heat', need: () => state.meshGeo,
    make: () => meshWith(heatMaterial()) },
  { key: 'normals', label: 'normals', need: () => state.meshGeo,
    make: () => meshWith(new THREE.MeshNormalMaterial({ flatShading: false })) },
  { key: 'wire', label: 'wire', need: () => state.meshGeo,
    make: () => {
      const g = new THREE.Group();
      g.add(meshWith(new THREE.MeshStandardMaterial({ color: 0x101418, roughness: 1, metalness: 0 })));
      const wf = new THREE.LineSegments(new THREE.WireframeGeometry(state.meshGeo),
        new THREE.LineBasicMaterial({ color: 0x59d8ff, transparent: true, opacity: 0.5 }));
      g.add(wf);
      return g;
    } },
  { key: 'points', label: 'points', need: () => state.points,
    make: () => state.points },
  { key: 'splat', label: 'splat', need: () => state.splat,
    make: () => null }, // splat object toggled separately
  { key: 'blend', label: 'mesh+splat', need: () => state.splat && (state.meshGlb || state.meshGeo),
    make: () => texturedObject() },
];

let activeObj = null;
function setMode(mode) {
  const m = MODES.find((x) => x.key === mode) || MODES[0];
  state.mode = m.key;
  if (activeObj) { state.group.remove(activeObj); activeObj = null; }
  if (state.splat) state.splat.visible = (m.key === 'splat' || m.key === 'blend');
  const obj = (m.key === 'splat') ? null : m.make();
  if (obj) { state.group.add(obj); activeObj = obj; }
  for (const b of document.querySelectorAll('#modes button')) b.classList.toggle('on', b.dataset.m === m.key);
  syncStat();
}

// ---------- controls ----------
let controls = null;
const walk = { yaw: 0, pitch: 0, keys: new Set(), eyeHeight: 1.6 };

function initControls() {
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.target.copy(new THREE.Vector3());
  controls.enabled = true;
}

function setCamMode(cm) {
  state.camMode = cm;
  const walkMode = cm !== 'orbit';
  controls.enabled = !walkMode;
  canvas.style.cursor = walkMode ? 'crosshair' : 'grab';
  if (walkMode) {
    walk.eyeHeight = cm === 'rover' ? 0.3 : 1.6;
    const c = controls.target;
    camera.position.set(c.x, state.floorY + walk.eyeHeight, c.z + state.diag * 0.15);
    const e = new THREE.Euler().setFromQuaternion(camera.quaternion, 'YXZ');
    walk.yaw = e.y; walk.pitch = e.x;
  }
  for (const b of document.querySelectorAll('#cams button')) b.classList.toggle('on', b.dataset.c === cm);
}

const keyMap = { w: 'f', s: 'b', a: 'l', d: 'r', arrowup: 'f', arrowdown: 'b', arrowleft: 'yawL', arrowright: 'yawR', ' ': 'u', control: 'dn', shift: 'boost', q: 'q' };
window.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if (keyMap[k]) { walk.keys.add(keyMap[k]); if (k === ' ') e.preventDefault(); }
  const mi = '12345678'.indexOf(k);
  if (mi >= 0) { const avail = MODES.filter((m) => m.need()); if (avail[mi]) setMode(avail[mi].key); }
  if (k === 'c') setCamMode(state.camMode === 'orbit' ? 'human' : state.camMode === 'human' ? 'rover' : 'orbit');
  if (k === 'q') { const qs = ['high', 'medium', 'low']; setQuality(qs[(qs.indexOf(state.quality) + 1) % 3]); }
  if (k === 'r') setCamMode(state.camMode); // re-place camera
  if (k === 'f') frameScene();
});
window.addEventListener('keyup', (e) => { const k = keyMap[e.key.toLowerCase()]; if (k) walk.keys.delete(k); });

let dragging = false;
canvas.addEventListener('mousedown', () => { if (state.camMode !== 'orbit') dragging = true; });
window.addEventListener('mouseup', () => { dragging = false; });
window.addEventListener('mousemove', (e) => {
  if (state.camMode === 'orbit' || !dragging) return;
  walk.yaw -= e.movementX * 0.0032;
  walk.pitch = Math.max(-1.45, Math.min(1.45, walk.pitch - e.movementY * 0.0032));
});

function frameScene() {
  controls.target.copy(new THREE.Vector3());
  const c = new THREE.Vector3();
  // recompute center from bounds
  const box = new THREE.Box3();
  if (state.meshGeo) { state.meshGeo.computeBoundingBox(); box.union(state.meshGeo.boundingBox); }
  if (state._pointsA) {
    const p = state._pointsA.position;
    for (let i = 0; i < state._pointsA.n; i += 16) box.expandByPoint(new THREE.Vector3(p[3 * i], p[3 * i + 1], p[3 * i + 2]));
  }
  box.getCenter(c);
  if (state.meshGlb) box.setFromObject(state.meshGlb).getCenter(c);
  if (isFinite(c.x)) {
    controls.target.copy(c);
    camera.position.set(c.x + state.diag * 0.45, c.y + state.diag * 0.35, c.z + state.diag * 0.45);
  }
}

function updateWalk(dt) {
  if (state.camMode === 'orbit') return;
  const speed = (walk.keys.has('boost') ? 3.2 : 1.1) * (state.camMode === 'rover' ? 0.4 : 1);
  const yawRate = 1.8;
  if (walk.keys.has('yawL')) walk.yaw += yawRate * dt;
  if (walk.keys.has('yawR')) walk.yaw -= yawRate * dt;
  camera.quaternion.setFromEuler(new THREE.Euler(walk.pitch, walk.yaw, 0, 'YXZ'));
  const f = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion);
  const r = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
  const mv = new THREE.Vector3();
  if (walk.keys.has('f')) mv.add(f);
  if (walk.keys.has('b')) mv.sub(f);
  if (walk.keys.has('l')) mv.sub(r);
  if (walk.keys.has('r')) mv.add(r);
  if (mv.lengthSq() > 0) mv.normalize().multiplyScalar(speed * dt);
  camera.position.addScaledVector(mv, 1);
  if (walk.keys.has('u')) camera.position.y += speed * dt;
  if (walk.keys.has('dn')) camera.position.y -= speed * dt;
}

// ---------- post fx ----------
let composer = null, passes = {};
function buildComposer() {
  const size = renderer.getSize(new THREE.Vector2());
  composer?.dispose?.();
  composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  passes = {};
  if (state.quality === 'high') {
    const g = new GTAOPass(scene, camera, size.x, size.y);
    g.output = GTAOPass.OUTPUT.Default;
    composer.addPass(g); passes.gtao = g;
    const b = new UnrealBloomPass(new THREE.Vector2(size.x, size.y), 0.22, 0.55, 0.82);
    composer.addPass(b); passes.bloom = b;
  } else if (state.quality === 'medium') {
    const b = new UnrealBloomPass(new THREE.Vector2(size.x, size.y), 0.15, 0.5, 0.85);
    composer.addPass(b); passes.bloom = b;
  }
  composer.addPass(new OutputPass());
  composer.addPass(new FXAAPass());
  key.castShadow = state.quality !== 'low';
  for (const b of document.querySelectorAll('#quality button')) b.classList.toggle('on', b.dataset.q === state.quality);
}
function setQuality(q) { state.quality = q; buildComposer(); }

// ---------- HUD ----------
function buildHUD() {
  const modesEl = $('#modes');
  for (const b of [...modesEl.querySelectorAll('button')]) b.remove();
  let i = 1;
  for (const m of MODES) {
    if (!m.need()) continue;
    const b = document.createElement('button');
    b.textContent = `${i++} ${m.label}`;
    b.dataset.m = m.key;
    b.onclick = () => setMode(m.key);
    modesEl.appendChild(b);
  }
  for (const b of document.querySelectorAll('#quality button')) b.onclick = () => setQuality(b.dataset.q);
  for (const b of document.querySelectorAll('#cams button')) b.onclick = () => setCamMode(b.dataset.c);
  for (const b of document.querySelectorAll('#modes button')) b.classList.toggle('on', b.dataset.m === state.mode);
  for (const b of document.querySelectorAll('#cams button')) b.classList.toggle('on', b.dataset.c === 'orbit');

  const rep = $('#report');
  const r = state.assets.report;
  if (r) {
    let html = '<h2>report.json</h2>';
    const kv = [];
    if (r.points_out != null) kv.push(['points', fmt(r.points_out)]);
    if (r.vertices != null) kv.push(['verts', fmt(r.vertices)]);
    if (r.triangles != null) kv.push(['tris', fmt(r.triangles)]);
    if (r.interpolated_points != null && r.points_out) {
      const f = r.interpolated_points / Math.max(1, r.points_out);
      kv.push(['interpolated', `${fmt(r.interpolated_points)} (${(f * 100).toFixed(1)}%)`]);
    }
    if (r.textured != null) kv.push(['textured', String(r.textured)]);
    if (r.extra) {
      const e = r.extra;
      if (e.mesh?.hole_recall != null) kv.push(['hole recall', (e.mesh.hole_recall * 100).toFixed(1) + '%']);
      else if (e.densified?.hole_recall != null) kv.push(['hole recall', (e.densified.hole_recall * 100).toFixed(1) + '%']);
      if (e.mesh?.f_score != null) kv.push(['mesh F@2cm', e.mesh.f_score.toFixed(3)]);
      if (e.densified?.f_score != null) kv.push(['cloud F@2cm', e.densified.f_score.toFixed(3)]);
      if (e.input?.f_score != null) kv.push(['input F@2cm', e.input.f_score.toFixed(3)]);
    }
    html += '<table class="kv">' + kv.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('') + '</table>';
    if (r.stages?.length) {
      const tot = r.total_ms || r.stages.reduce((s, x) => s + x.ms, 0);
      html += '<h2 style="margin-top:8px">stages</h2>';
      for (const s of r.stages) {
        const pct = Math.min(100, (s.ms / tot) * 100);
        html += `<table><tr><td>${s.stage || s.name}</td><td>${s.ms.toFixed(1)} ms</td></tr></table><div class="bar"><i style="width:${pct}%"></i></div>`;
      }
      html += `<table class="kv"><tr><td>total</td><td>${tot.toFixed(0)} ms</td></tr></table>`;
    }
    html += '<h2 style="margin-top:8px">artefacts</h2><div id="files">' +
      state.assets.files.map((f) => `<div class="f"><span>${f.name}</span><span>${fmtBytes(f.bytes)}</span></div>`).join('') + '</div>';
    rep.innerHTML = html;
    rep.classList.remove('hidden');
  } else if (state.assets.files.length) {
    rep.innerHTML = '<h2>artefacts</h2><div id="files">' +
      state.assets.files.map((f) => `<div class="f"><span>${f.name}</span><span>${fmtBytes(f.bytes)}</span></div>`).join('') + '</div>';
    rep.classList.remove('hidden');
  }
}

function fmt(n) { return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n); }
function fmtBytes(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n >= 1e3 ? (n / 1e3).toFixed(1) + ' kB' : n + ' B'; }

function showMsg(t) { $('#msg').innerHTML = `<div class="box"><div class="err">${t}</div></div>`; }

let fpsE = 60, msE = 16;
function syncStat() {
  const info = renderer.info;
  $('#stat').innerHTML =
    `<b>${fpsE.toFixed(0)}</b> fps  <b>${msE.toFixed(1)}</b> ms  ·  ${state.mode}` +
    `  ·  ${info.render.calls} calls  ${fmt(info.render.triangles)} tris`;
}

// ---------- adaptive pixel ratio ----------
const prMax = Math.min(window.devicePixelRatio || 1, 2);
let pr = prMax;
function adaptPR() {
  const target = msE > 20 ? Math.max(0.6, pr - 0.25) : msE < 11 ? Math.min(prMax, pr + 0.1) : pr;
  if (target !== pr) { pr = target; applySize(); }
}
function applySize() {
  const w = window.innerWidth, h = window.innerHeight;
  renderer.setPixelRatio(pr);
  renderer.setSize(w, h, false);
  if (composer) { composer.setPixelRatio(pr); composer.setSize(w, h); }
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', applySize);

// ---------- main loop ----------
renderer.info.autoReset = false;
let last = performance.now(), frames = 0, tAccum = 0;
function animate(now) {
  requestAnimationFrame(animate);
  renderer.info.reset();
  const dt = Math.min(0.1, (now - last) / 1000); last = now;
  updateWalk(dt);
  controls?.update();
  camera.updateMatrixWorld();
  if (state.splat?.visible) { state.splat.update(camera, renderer); state.splat.requestSort(camera, state.diag * 2); }
  composer.render();
  if (!window.__ready) window.__ready = true;
  msE = msE * 0.94 + (now - (animate._t || now)) * 0.06; animate._t = now;
  frames++; tAccum += dt;
  if (tAccum > 0.5) { fpsE = fpsE * 0.5 + (frames / tAccum) * 0.5; frames = 0; tAccum = 0; syncStat(); adaptPR(); }
}

// playwright / debug hooks
window.__viz = {
  ready: false,
  errors,
  get mode() { return state.mode; },
  get fps() { return fpsE; },
  setMode: (m) => setMode(m),
  setQuality: (q) => setQuality(q),
  setCamMode: (c) => setCamMode(c),
  setCamera: (px, py, pz, tx, ty, tz) => {
    camera.position.set(px, py, pz);
    controls?.target.set(tx, ty, tz);
    camera.lookAt(tx, ty, tz);
  },
  files: () => state.assets.files.map((f) => f.name),
  bounds: () => state.bounds ? { ...state.bounds, diag: state.diag, floorY: state.floorY } : null,
  shot: () => { composer.render(); return renderer.domElement.toDataURL('image/png'); },
  info: () => ({ calls: renderer.info.render.calls, triangles: renderer.info.render.triangles,
    mode: state.mode, quality: state.quality, ms: msE, errors: [...errors] }),
  _exportTarget: () => {
    const geo = meshGeometry();
    if (!geo) return state.meshGlb || state.points || null;
    const src = state.meshGlb || new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true }));
    // GLTFExporter only round-trips underscore custom attributes; the real
    // export writes support as _SUPPORT, so mirror that in the fixture.
    const clone = src.clone(true);
    clone.traverse((o) => {
      if (o.isMesh && o.geometry.getAttribute('support') && !o.geometry.getAttribute('_SUPPORT')) {
        o.geometry.setAttribute('_SUPPORT', o.geometry.getAttribute('support'));
      }
    });
    return clone;
  },
};

// ---------- boot ----------
(async function main() {
  try {
    initControls();
    applySize();
    buildComposer();
    await load();
    setCamMode('orbit');
    requestAnimationFrame(animate);
    window.__viz.ready = true;
  } catch (e) {
    errors.push(String(e?.stack || e));
    showMsg(String(e?.stack || e));
    window.__viz.ready = true;
  }
})();
