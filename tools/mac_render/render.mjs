/**
 * Headless (or desktop) Three.js map. Same wire as the Quest viewer:
 * GET /map/points.bin, /map/mesh.bin and map status from ?upstream=.
 * Camera follows the newest pose from the parent server. Visual only.
 *
 * Perf structure:
 * - Geometry lives in pooled buffers that grow; snapshots write into them
 *   once (partial GPU re-upload via updateRanges), never new objects.
 * - Upload order is nested LOD prefixes: first quarter is every 4th item,
 *   first half is every 2nd, so setDrawRange alone changes fidelity with
 *   zero re-upload. The LOD ladder trades pixel ratio and draw fraction
 *   against a measured frame-cost EMA, and climbs back up (to a
 *   supersampled top rung) whenever the budget allows.
 * - The render loop only rasterizes when something changed; per-frame work
 *   allocates nothing.
 * - Plug-in layers (e.g. tools/mac_render/dense-splats.mjs) register via
 *   window.__macRegisterLayer({ init, poll, pre, post, quality, dispose })
 *   without touching this loop: poll runs every tick, pre/post wrap the
 *   main render, quality fires on every LOD change.
 */
window.__mac = { ready: false, error: null };
window.addEventListener("error", (event) => {
  window.__mac.error = String(event.message || event.error);
});
import * as THREE from "/quest/node_modules/three/build/three.module.js";
import { decodeMesh, decodePoints } from "/quest/src/protocol.js";
import { mapResponseRevision } from "/quest/src/map-response.js";
import { DenseCloudState, DenseCloudReceiver, DenseGpuChunks } from "./dense-cloud.mjs";

const params = new URLSearchParams(location.search);
const upstream = (params.get("upstream") || "").replace(/\/$/, "");
const width = Number(params.get("width") || 1280);
const height = Number(params.get("height") || 720);

const style = params.get("style") === "unlit" ? "unlit" : "lit";
const GRAPHITE = 0x1c2228;
const FOG = 0x242b31;
const ACCENT = 0xc4a574;

const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false });
renderer.setSize(width, height, false);
renderer.setPixelRatio(1);
renderer.setClearColor(GRAPHITE);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.shadowMap.enabled = style === "lit";
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(GRAPHITE);
scene.fog = new THREE.Fog(FOG, 12, 42);
const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 2000);
camera.position.set(9, 6, 9);
camera.lookAt(0, 0, 0);

const root = new THREE.Group();
scene.add(root);
const grid = new THREE.GridHelper(40, 40, 0x3a424a, 0x2c343c);
grid.position.y = -0.002;
root.add(grid);
// Lights and grid are display aids, never sensor geometry or counted data.
const hemi = new THREE.HemisphereLight(0xcfd6dc, 0x2a3036, style === "lit" ? 0.55 : 0);
const key = new THREE.DirectionalLight(0xfff4e6, style === "lit" ? 0.85 : 0);
key.position.set(8, 14, 6);
key.castShadow = style === "lit";
key.shadow.mapSize.set(1024, 1024);
key.shadow.camera.near = 0.5;
key.shadow.camera.far = 40;
key.shadow.camera.left = key.shadow.camera.bottom = -12;
key.shadow.camera.right = key.shadow.camera.top = 12;
key.shadow.bias = -0.0004;
scene.add(hemi, key);
if (style !== "lit") scene.add(new THREE.AmbientLight(0xffffff, 1));

const pointsMaterial = new THREE.PointsMaterial({ size: style === "lit" ? 0.014 : 0.025, vertexColors: true, toneMapped: style === "lit" });
const cloudGeometry = new THREE.BufferGeometry();
const cloud = new THREE.Points(cloudGeometry, pointsMaterial);
root.add(cloud);
// Full measured RGB-D detail stays on the Mac. Older relays retain the bounded
// snapshot fallback when they do not provide the incremental dense endpoint.
const denseState = new DenseCloudState({ capacity: 5_000_000 });
const denseReceiver = new DenseCloudReceiver({ state: denseState, endpoint: `${upstream}/dense/delta.bin` });
const denseGpu = new DenseGpuChunks({ state: denseState, THREE, material: pointsMaterial, parent: root });
const surfaceMaterial = style === "lit"
  ? new THREE.MeshStandardMaterial({
    color: 0xffffff, vertexColors: true, side: THREE.DoubleSide,
    roughness: 0.85, metalness: 0,
    // Retain all measured points in their buffers/counts; surface depth bias
    // keeps coincident mesh vertices from stippling the shaded faces.
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  })
  : new THREE.MeshBasicMaterial({
    vertexColors: true, side: THREE.DoubleSide, toneMapped: false,
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  });
const surfaceGeometry = new THREE.BufferGeometry();
const surfaces = new THREE.Mesh(surfaceGeometry, surfaceMaterial);
surfaces.castShadow = style === "lit";
surfaces.receiveShadow = style === "lit";
if (style === "lit") {
  surfaceMaterial.onBeforeCompile = (shader) => {
    shader.fragmentShader = shader.fragmentShader.replace(
      "#include <emissivemap_fragment>",
      "#include <emissivemap_fragment>\ntotalEmissiveRadiance += vColor * 0.45;",
    );
  };
}
root.add(surfaces);

// Frustum culling would need an up-to-date boundingSphere per snapshot (an
// O(n) CPU pass); the map fills the view anyway, so pin a huge sphere and
// skip per-object culling. Raycast is unused on this page.
const PINNED_BOUNDS = new THREE.Sphere(new THREE.Vector3(), 1e9);
cloudGeometry.boundingSphere = PINNED_BOUNDS;
surfaceGeometry.boundingSphere = PINNED_BOUNDS;
cloud.frustumCulled = false;
surfaces.frustumCulled = false;

// Pooled attributes: capacity grows 1.5x, snapshots write into the existing
// typed arrays and mark the used range for a partial upload. No per-snapshot
// BufferGeometry/attribute churn.
function pooledAttribute(geometry, name, itemSize, Type) {
  let capacity = 0;
  let attribute = null;
  return {
    ensure(elements) {
      if (capacity >= elements) return attribute;
      capacity = Math.max(elements, capacity + (capacity >> 1), 1024);
      attribute = new THREE.BufferAttribute(new Type(capacity), itemSize);
      attribute.setUsage(THREE.DynamicDrawUsage);
      geometry.setAttribute(name, attribute);
      return attribute;
    },
    get array() { return attribute.array; },
    flush(elements) {
      attribute.clearUpdateRanges();
      attribute.addUpdateRange(0, elements);
      attribute.needsUpdate = true;
    },
  };
}
const cloudPosition = pooledAttribute(cloudGeometry, "position", 3, Float32Array);
const cloudColor = pooledAttribute(cloudGeometry, "color", 3, Float32Array);
const meshPosition = pooledAttribute(surfaceGeometry, "position", 3, Float32Array);
const meshColor = pooledAttribute(surfaceGeometry, "color", 3, Float32Array);
const meshNormal = style === "lit" ? pooledAttribute(surfaceGeometry, "normal", 3, Float32Array) : null;
let indexAttribute = null;
let indexCapacity = 0;
let indexUsed = 0;
function ensureIndex(count) {
  if (indexCapacity < count) {
    indexCapacity = Math.max(count, indexCapacity + (indexCapacity >> 1), 3072);
    indexAttribute = new THREE.BufferAttribute(new Uint32Array(indexCapacity), 1);
    indexAttribute.setUsage(THREE.DynamicDrawUsage);
    surfaceGeometry.setIndex(indexAttribute);
    indexUsed = 0;
  }
  return indexAttribute;
}

// Nested LOD prefixes: [i%4==0] ++ [i%4==2] ++ [odd] makes the first quarter
// a uniform every-4th sample and the first half every-2nd, so LOD switches
// are pure setDrawRange with no rewrite.
function scatterOrder(count, i, n0, n2) {
  const quad = i & 3;
  if (quad === 0) return i >> 2;
  if (quad === 2) return n0 + ((i - 2) >> 2);
  return n0 + n2 + ((i - 1) >> 1);
}
let cloudCount = 0;
let cloudHalf = 0;
let cloudQuarter = 0;
let meshTris = 0;
let meshHalfTris = 0;
let meshQuarterTris = 0;

function uploadCloud(data) {
  const count = data.count;
  const position = cloudPosition.ensure(count * 3);
  const color = cloudColor.ensure(count * 3);
  const positions = cloudPosition.array;
  const colors = cloudColor.array;
  const srcP = data.positions;
  const srcC = data.colors;
  const n0 = (count + 3) >> 2;
  const n2 = (count + 1) >> 2;
  for (let i = 0; i < count; i++) {
    const dst = scatterOrder(count, i, n0, n2) * 3;
    const src = i * 3;
    positions[dst] = srcP[src];
    positions[dst + 1] = srcP[src + 1];
    positions[dst + 2] = srcP[src + 2];
    colors[dst] = srcC[src];
    colors[dst + 1] = srcC[src + 1];
    colors[dst + 2] = srcC[src + 2];
  }
  cloudPosition.flush(count * 3);
  cloudColor.flush(count * 3);
  cloudCount = count;
  cloudQuarter = n0;
  cloudHalf = n0 + n2;
  cloudGeometry.setDrawRange(0, cloudDrawCount());
  frameDirty = true;
}

function uploadMesh(data) {
  const vertices = data.count;
  meshPosition.ensure(vertices * 3);
  meshColor.ensure(vertices * 3);
  meshPosition.array.set(data.positions);
  meshColor.array.set(data.colors);
  meshPosition.flush(vertices * 3);
  meshColor.flush(vertices * 3);
  const triangles = data.indices.length / 3;
  const index = ensureIndex(data.indices.length);
  const dstIndex = index.array;
  const srcIndex = data.indices;
  const n0 = (triangles + 3) >> 2;
  const n2 = (triangles + 1) >> 2;
  for (let t = 0; t < triangles; t++) {
    const dst = scatterOrder(triangles, t, n0, n2) * 3;
    const src = t * 3;
    dstIndex[dst] = srcIndex[src];
    dstIndex[dst + 1] = srcIndex[src + 1];
    dstIndex[dst + 2] = srcIndex[src + 2];
  }
  // Stale indices beyond the new range would feed computeVertexNormals;
  // zero them so they fold into a degenerate (0,0,0) triangle.
  if (indexUsed > data.indices.length) dstIndex.fill(0, data.indices.length, indexUsed);
  indexUsed = data.indices.length;
  index.clearUpdateRanges();
  index.addUpdateRange(0, data.indices.length);
  index.needsUpdate = true;
  if (meshNormal) {
    meshNormal.ensure(vertices * 3);
    surfaceGeometry.computeVertexNormals();
    meshNormal.flush(vertices * 3);
  }
  meshTris = triangles;
  meshQuarterTris = n0;
  meshHalfTris = n0 + n2;
  surfaceGeometry.setDrawRange(0, meshDrawCount());
  frameDirty = true;
}

function clearGeometry() {
  cloudCount = cloudHalf = cloudQuarter = 0;
  meshTris = meshHalfTris = meshQuarterTris = 0;
  indexUsed = 0;
  cloudGeometry.setDrawRange(0, 0);
  surfaceGeometry.setDrawRange(0, 0);
  appliedPoints = appliedMesh = -1;
  frameDirty = true;
}

// Adaptive quality ladder: spend measured headroom on fidelity, surrender it
// under load. Top rung supersamples (a 1.5x backing store downsampled into
// the same canvas = free anti-aliasing for the stream) at full geometry.
const LADDER = [
  { pixelRatio: 1.5, fraction: 1 },
  { pixelRatio: 1.0, fraction: 1 },
  { pixelRatio: 1.0, fraction: 0.5 },
  { pixelRatio: 0.75, fraction: 0.5 },
  { pixelRatio: 0.75, fraction: 0.25 },
];
const LOD_DROP_MS = 14;      // sustained cost above this -> next rung down
const LOD_CLIMB_MS = 6;      // sustained cost below this -> next rung up
const LOD_DROP_DWELL = 12;   // frames between drops
const LOD_CLIMB_DWELL = 150; // frames of headroom before climbing
const lodParam = params.get("lod");
const lodPinned = lodParam !== null && lodParam !== "" && lodParam !== "adaptive";
let lodIndex = lodPinned
  ? Math.min(LADDER.length - 1, Math.max(0, Number(lodParam) || 0))
  : 1;

function cloudDrawCount() {
  const fraction = LADDER[lodIndex].fraction;
  return fraction === 1 ? cloudCount : fraction === 0.5 ? cloudHalf : cloudQuarter;
}
function meshDrawCount() {
  const fraction = LADDER[lodIndex].fraction;
  return (fraction === 1 ? meshTris : fraction === 0.5 ? meshHalfTris : meshQuarterTris) * 3;
}

let frameDirty = true;
const lodView = { index: lodIndex, pixelRatio: 1, fraction: 1, adaptive: !lodPinned };
function applyLod() {
  const level = LADDER[lodIndex];
  if (renderer.getPixelRatio() !== level.pixelRatio) {
    renderer.setPixelRatio(level.pixelRatio);
    renderer.setSize(width, height, false);
  }
  cloudGeometry.setDrawRange(0, cloudDrawCount());
  surfaceGeometry.setDrawRange(0, meshDrawCount());
  lodView.index = lodIndex;
  lodView.pixelRatio = level.pixelRatio;
  lodView.fraction = level.fraction;
  for (const layer of layers) layer.quality?.(layerContext, lodView);
  frameDirty = true;
}

let costEma = 0;
let stableFrames = 0;
let framesSinceChange = 1e9;
function observeFrame(cost) {
  costEma = costEma === 0 ? cost : costEma * 0.85 + cost * 0.15;
  framesSinceChange++;
  if (lodPinned) return;
  if (costEma > LOD_DROP_MS && framesSinceChange >= LOD_DROP_DWELL && lodIndex < LADDER.length - 1) {
    lodIndex++;
    applyLod();
    framesSinceChange = 0;
    stableFrames = 0;
  } else if (costEma < LOD_CLIMB_MS) {
    if (++stableFrames >= LOD_CLIMB_DWELL && framesSinceChange >= LOD_CLIMB_DWELL && lodIndex > 0) {
      lodIndex--;
      applyLod();
      framesSinceChange = 0;
      stableFrames = 0;
    }
  } else stableFrames = 0;
}

const rayPositions = new Float32Array(6);
const rayLine = new THREE.Line(
  new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(rayPositions, 3)),
  new THREE.LineBasicMaterial({ color: ACCENT }),
);
rayPositions[4] = -1; // direction (0,0,-1) end
rayLine.geometry.attributes.position.needsUpdate = true;
rayLine.visible = false;
scene.add(rayLine);

const banner = document.getElementById("banner");
let status = null;
let appliedPoints = -1;
let appliedMesh = -1;
let lastSnapshotKind = null;
let pose = null;
const tmp = new THREE.Quaternion();
const viewPosition = [0, 0, 0];

// Plug-in layer seam. A layer registers once and hooks every tick without
// editing this loop: init(ctx) once; poll(ctx, now) every tick — return
// truthy or call ctx.requestFrame() to mark the frame dirty; pre(ctx) before
// the main render (e.g. refresh an offscreen target); post(ctx) after it
// (shade/composite pass); quality(ctx, lod) on every LOD change; dispose()
// on unregister.
const layers = [];
const layerContext = {
  THREE, renderer, scene, camera, root, params, style, width, height,
  requestFrame: () => { frameDirty = true; },
  quality: () => lodView,
  registerLayer: (layer) => registerLayer(layer),
};
function registerLayer(layer) {
  layers.push(layer);
  layer.init?.(layerContext);
  frameDirty = true;
  return () => {
    const i = layers.indexOf(layer);
    if (i >= 0) layers.splice(i, 1);
    layer.dispose?.();
    frameDirty = true;
  };
}
window.__macRegisterLayer = registerLayer;

const mapStatusView = {};
let bannerText = "";
let cloudShown = -1;
let trisShown = -1;
let sourceShown = "";
let retainedShown = null;
let errorShown = null;
function exposeMap() {
  const mac = window.__mac;
  const statusAge = status && Number.isFinite(status.age_ms) ? status.age_ms + Math.max(0, performance.now() - statusReceivedAt) : Infinity;
  const fresh = status?.fresh === true && statusAge < 2000;
  const points = denseState.size || cloudCount || 0;
  const triangles = surfaces.visible ? meshTris : 0;
  const sourceLabel = status?.geometry_source || status?.source || "WAITING";
  const source = denseState.size ? `${sourceLabel} · ${denseReceiver.sourceLabel}` : sourceLabel;
  const retained = points > 0 && (mapError !== null || !fresh || status?.geometry?.retained === true);
  mac.points = points;
  mac.triangles = triangles;
  mac.source = source;
  mac.retained = retained;
  mac.mapError = mapError;
  mac.lod = lodView;
  if (status) {
    mapStatusView.source = status.source;
    mapStatusView.geometry_source = status.geometry_source ?? null;
    mapStatusView.points = status.points;
    mapStatusView.triangles = status.triangles;
    mapStatusView.generation = status.generation;
    mapStatusView.revision = status.revision;
    mapStatusView.mesh_revision = status.mesh_revision;
    mapStatusView.applied_points_revision = appliedPoints;
    mapStatusView.applied_mesh_revision = appliedMesh;
    mapStatusView.fresh = fresh;
    mapStatusView.age_ms = Number.isFinite(statusAge) ? statusAge : null;
    mapStatusView.connected = status.connected === true;
    mapStatusView.retained = retained;
    mapStatusView.partial = status.geometry?.partial === true;
    mac.mapStatus = mapStatusView;
  } else mac.mapStatus = null;
  // DOM writes only when the banner actually changes — never per frame.
  if (points !== cloudShown || triangles !== trisShown || source !== sourceShown ||
      retained !== retainedShown || mapError !== errorShown) {
    cloudShown = points; trisShown = triangles; sourceShown = source;
    retainedShown = retained; errorShown = mapError;
    bannerText = `MAC RENDER · FLAT · ${source} · ${points} pts · ${triangles} tris${retained ? " · RETAINED" : ""}${mapError ? " · map unavailable" : ""} · NOT STEREO · GRID: DISPLAY GUIDE`;
    banner.textContent = bannerText;
  }
}
async function boundedBody(response, limit, signal) {
  if (!response.ok) throw new Error(`Map HTTP ${response.status}`);
  const reader = response.body.getReader();
  const chunks = []; let length = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (signal.aborted) throw new Error("Map refresh cancelled");
      if (done) break;
      length += value.byteLength;
      if (length > limit) throw new Error("Map response exceeds limit");
      chunks.push(value);
    }
    const body = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    return body.buffer;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
async function pull(path, expected, signal) {
  const response = await fetch(`${upstream}${path}`, { cache: "no-store", signal });
  const revision = mapResponseRevision(response.headers, expected);
  if (revision === null) {
    await response.body?.cancel().catch(() => {});
    throw new Error("Map snapshot identity/revision mismatch");
  }
  return { revision, body: await boundedBody(response, 12 * 1024 * 1024, signal) };
}
function validStatus(next) {
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  if (!next || typeof next !== "object" || typeof next.source !== "string" ||
      ![next.generation, next.revision, next.mesh_revision, next.points, next.triangles].every(integer)) {
    throw new Error("Invalid map status");
  }
  return next;
}
function sourceIdentity(next) {
  return JSON.stringify([next.source, next.session_id?.toLowerCase() ?? null, next.map_epoch ?? null,
    next.geometry_source ?? null]);
}
function acceptMapStatus(next) {
  const nextIdentity = sourceIdentity(next);
  if (mapIdentity !== nextIdentity || mapGeneration !== next.generation) {
    if (mapIdentity === nextIdentity && next.generation < mapGeneration) return false;
    clearGeometry(); mapIdentity = nextIdentity; mapGeneration = next.generation;
  } else if (status && (next.revision < status.revision || next.mesh_revision < status.mesh_revision)) return false;
  status = next; statusReceivedAt = performance.now();
  return true;
}
async function readStatus(signal) {
  const response = await fetch(`${upstream}/map/status`, { cache: "no-store", signal });
  return validStatus(JSON.parse(textDecoder.decode(await boundedBody(response, 65536, signal))));
}
async function snapshotStillCurrent(expectedIdentity, expectedGeneration, signal) {
  const latest = await readStatus(signal);
  return acceptMapStatus(latest) && !signal.aborted && mapIdentity === expectedIdentity && mapGeneration === expectedGeneration;
}
async function refresh() {
  if (refreshBusy) return false;
  refreshBusy = true;
  window.__mac.refreshBusy = true;
  const abort = new AbortController();
  const timer = window.setTimeout(() => abort.abort(), 60000);
  try {
    const next = await readStatus(abort.signal);
    const nextIdentity = sourceIdentity(next);
    if (!acceptMapStatus(next)) return false;
    if (!next.points && !next.triangles) {
      clearGeometry(); appliedPoints = next.revision; appliedMesh = next.mesh_revision;
    } else if (next.triangles && next.mesh_revision > appliedMesh &&
        !(next.capped && lastSnapshotKind === "mesh" && next.points && next.revision > appliedPoints)) {
      const result = await pull("/map/mesh.bin", { ...next, revision: next.mesh_revision }, abort.signal);
      if (!await snapshotStillCurrent(nextIdentity, next.generation, abort.signal)) return false;
      const mesh = decodeMesh(result.body);
      uploadMesh(mesh);
      if (!next.capped || next.geometry_source) { uploadCloud(mesh); appliedPoints = next.revision; }
      appliedMesh = result.revision;
      lastSnapshotKind = "mesh";
    } else if (next.points && next.revision > appliedPoints) {
      const result = await pull("/map/points.bin", next, abort.signal);
      if (!await snapshotStillCurrent(nextIdentity, next.generation, abort.signal)) return false;
      uploadCloud(decodePoints(result.body)); appliedPoints = result.revision;
      lastSnapshotKind = "points";
      if (!next.triangles) { meshTris = meshHalfTris = meshQuarterTris = 0; indexUsed = 0; surfaceGeometry.setDrawRange(0, 0); appliedMesh = next.mesh_revision; frameDirty = true; }
    }
    mapError = null;
    return true;
  } catch (error) {
    mapError = String(error).slice(0, 160);
    return false;
  } finally {
    window.clearTimeout(timer); refreshBusy = false; window.__mac.refreshBusy = false; exposeMap();
  }
}

let refreshBusy = false;
let mapIdentity = null;
let mapGeneration = -1;
let mapError = null;
let statusReceivedAt = -Infinity;
let incomingPose = null;
const textDecoder = new TextDecoder();

function clearPose() { rayLine.visible = false; window.__mac.poseEcho = null; window.__mac.poseValid = false; }
function finiteVector(value, count) {
  return Array.isArray(value) && value.length === count && value.every(item => typeof item === "number" && Number.isFinite(item));
}
Object.defineProperty(window, "__macPose", {
  configurable: true,
  get: () => incomingPose,
  set: next => {
    // This setter runs inside page.evaluate, so this receipt and RAF use one
    // browser monotonic clock. Python's monotonic epoch is never compared here.
    incomingPose = next ? { ...next, browserReceiptMs: performance.now() } : null;
    clearPose();
  },
});
function applyPose() {
  const next = incomingPose;
  const age = next ? next.receiptAgeMs + performance.now() - next.browserReceiptMs : Infinity;
  if (!next) { clearPose(); return; }
  // Per-object validation cache: the pose setter replaces the object on each
  // update, so a repeated object needs only the age check re-evaluated.
  let valid = next === pose ? next.__valid !== false : undefined;
  if (valid === undefined) {
    const q = next.quaternion;
    const quaternionLength = finiteVector(q, 4) ? Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]) : 0;
    valid = next.tracked === true && Number.isFinite(next.receiptAgeMs) && next.receiptAgeMs >= 0 &&
        finiteVector(next.position, 3) && quaternionLength >= 1e-6 &&
        [next.sentMonoMs, next.sentWallMs, next.receivedMonoMs].every(value =>
          typeof value === "number" && Number.isFinite(value)) &&
        (!next.ray || (finiteVector(next.ray.origin, 3) && finiteVector(next.ray.direction, 3) &&
          Math.hypot(next.ray.direction[0], next.ray.direction[1], next.ray.direction[2]) >= 1e-6));
    next.__valid = valid;
  }
  if (!valid || !Number.isFinite(age) || age < 0 || age >= 250) { clearPose(); return; }
  window.__mac.poseValid = true;
  if (next !== pose) {
    pose = next;
    const p = next.position;
    camera.position.set(p[0], p[1], p[2]);
    const q = next.quaternion;
    tmp.set(q[0], q[1], q[2], q[3]).normalize();
    camera.quaternion.copy(tmp);
    if (Number.isFinite(next.fov_y_deg) && next.fov_y_deg >= 1 && next.fov_y_deg <= 170) camera.fov = next.fov_y_deg;
    if (Number.isFinite(next.aspect) && next.aspect >= .1 && next.aspect <= 5) camera.aspect = next.aspect;
    camera.updateProjectionMatrix();
    if (next.ray) {
      const o = next.ray.origin, d = next.ray.direction;
      const scale = 8 / Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
      rayPositions[0] = o[0]; rayPositions[1] = o[1]; rayPositions[2] = o[2];
      rayPositions[3] = o[0] + d[0] * scale; rayPositions[4] = o[1] + d[1] * scale; rayPositions[5] = o[2] + d[2] * scale;
      rayLine.geometry.attributes.position.needsUpdate = true;
      rayLine.visible = true;
    } else rayLine.visible = false;
    next.__echo = { sent_mono_ms: next.sentMonoMs, sent_wall_ms: next.sentWallMs,
      received_mono_ms: next.receivedMonoMs, session_id: next.session_id,
      frame_id: next.frame_id, pose_id: next.pose_id, client_nonce: next.client_nonce,
      space: next.space, map_revision: next.map_revision, eyes: next.eyes };
    window.__mac.fovY = camera.fov;
    window.__mac.aspect = camera.aspect;
    frameDirty = true;
  }
  window.__mac.poseEcho = next.__echo;
}

window.__mac = { ready: true, stereo: false, renderMs: 0, captureMs: 0, draws: 0, points: 0, source: "WAITING", poseEcho: null };
let lastTickAt = 0;
let densePagesSeen = 0;
let denseResetSeen = -1;
let cloudVisibleSeen = true;
let surfacesVisibleSeen = true;
const denseMetricsView = {};
function drawScene(measure) {
  const started = performance.now();
  denseReceiver.update();
  // Dense deltas, resets and TTL expiries are the dense lane's "changed"
  // signal; without one a clean frame may skip rasterizing entirely.
  if (denseState.dirty.size > 0 || denseReceiver.pages !== densePagesSeen ||
      denseState.resetVersion !== denseResetSeen) frameDirty = true;
  densePagesSeen = denseReceiver.pages;
  denseResetSeen = denseState.resetVersion;
  denseGpu.flush();
  cloud.visible = denseState.size === 0;
  const denseMatchesMap = denseReceiver.identity === `rgbd:${status?.session_id?.toLowerCase()}:${status?.map_epoch}`;
  surfaces.visible = denseState.size === 0 || denseMatchesMap;
  if (cloud.visible !== cloudVisibleSeen || surfaces.visible !== surfacesVisibleSeen) {
    cloudVisibleSeen = cloud.visible;
    surfacesVisibleSeen = surfaces.visible;
    frameDirty = true;
  }
  applyPose();
  const now = performance.now();
  for (const layer of layers) {
    if (layer.poll?.(layerContext, now)) frameDirty = true;
  }
  if (frameDirty) {
    denseGpu.beginFrame();
    for (const layer of layers) layer.pre?.(layerContext);
    renderer.render(scene, camera);
    for (const layer of layers) layer.post?.(layerContext);
    denseGpu.endFrame();
    frameDirty = false;
    window.__mac.drawnPoints = renderer.info.render.points;
    window.__mac.drawnTriangles = renderer.info.render.triangles;
  }
  Object.assign(denseMetricsView, denseReceiver.metrics(), denseGpu.metrics());
  window.__mac.dense = denseMetricsView;
  window.__mac.renderMs = performance.now() - started;
  window.__mac.rayVisible = rayLine.visible;
  viewPosition[0] = camera.position.x;
  viewPosition[1] = camera.position.y;
  viewPosition[2] = camera.position.z;
  window.__mac.viewPosition = viewPosition;
  window.__mac.draws = (window.__mac.draws || 0) + 1;
  exposeMap();
  if (measure === "raf") {
    // Vsync overshoot is the only GPU-cost signal available without a
    // readback; idle frames stay near it, saturated ones stretch past it.
    if (lastTickAt > 0) {
      const gap = started - lastTickAt;
      observeFrame(window.__mac.renderMs + Math.max(0, gap - 17));
    }
    lastTickAt = started;
  }
}
// Pixels and provenance are read in one JavaScript turn, without an intervening
// screenshot/RAF. Geometry callbacks cannot replace buffers during this call.
window.__macCapture = () => {
  drawScene("capture");
  const captureStarted = performance.now();
  const data = renderer.domElement.toDataURL("image/jpeg", 0.8).split(",")[1];
  window.__mac.captureMs = performance.now() - captureStarted;
  // The readback is where rasterization actually bills: render+capture is the
  // real per-frame cost in the server-driven pipeline.
  observeFrame(window.__mac.renderMs + window.__mac.captureMs);
  return { data, meta: { ...window.__mac,
    mapStatus: window.__mac.mapStatus ? { ...window.__mac.mapStatus } : null,
    poseEcho: window.__mac.poseEcho ? { ...window.__mac.poseEcho } : null } };
};
renderer.setAnimationLoop(() => { if (!window.__macCaptureDriven) drawScene("raf"); });
window.__macRefreshMap = refresh;
applyLod();
refresh();
const refreshTimer = window.setInterval(refresh, 2000);
window.addEventListener("pagehide", () => {
  window.clearInterval(refreshTimer); incomingPose = null; clearPose();
  denseReceiver.dispose(); denseGpu.dispose();
});
