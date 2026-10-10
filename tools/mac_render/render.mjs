/**
 * Headless (or desktop) Three.js map. Same wire as the Quest viewer:
 * GET /map/points.bin, /map/mesh.bin and map status from ?upstream=.
 * Camera follows the newest pose from the parent server. Visual only.
 */
window.__mac = { ready: false, error: null };
window.addEventListener("error", (event) => {
  window.__mac.error = String(event.message || event.error);
});
import * as THREE from "/quest/node_modules/three/build/three.module.js";
import { decodeMesh, decodePoints } from "/quest/src/protocol.js";
import { mapResponseRevision } from "/quest/src/map-response.js";

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
const cloud = new THREE.Points(new THREE.BufferGeometry(), pointsMaterial);
root.add(cloud);
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
const surfaces = new THREE.Mesh(new THREE.BufferGeometry(), surfaceMaterial);
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

const rayLine = new THREE.Line(
  new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
  new THREE.LineBasicMaterial({ color: ACCENT }),
);
rayLine.visible = false;
scene.add(rayLine);

const banner = document.getElementById("banner");
let status = null;
let appliedPoints = -1;
let appliedMesh = -1;
let lastSnapshotKind = null;
let pose = null;
const tmp = new THREE.Quaternion();
const eye = new THREE.Vector3();
const aim = new THREE.Vector3();

function applyGeometry(object, data, indices) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(data.positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(data.colors, 3));
  if (indices) {
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    geometry.computeVertexNormals();
  }
  geometry.computeBoundingSphere();
  object.geometry.dispose();
  object.geometry = geometry;
}

let refreshBusy = false;
let mapIdentity = null;
let mapGeneration = -1;
let mapError = null;
let statusReceivedAt = -Infinity;
let incomingPose = null;

function clearGeometry() {
  cloud.geometry.dispose(); cloud.geometry = new THREE.BufferGeometry();
  surfaces.geometry.dispose(); surfaces.geometry = new THREE.BufferGeometry();
  appliedPoints = appliedMesh = -1;
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
function exposeMap() {
  const points = cloud.geometry.getAttribute("position")?.count || 0;
  const triangles = (surfaces.geometry.index?.count || 0) / 3;
  const statusAge = status && Number.isFinite(status.age_ms) ? status.age_ms + Math.max(0, performance.now() - statusReceivedAt) : Infinity;
  const fresh = status?.fresh === true && statusAge < 2000;
  const retained = points > 0 && (mapError !== null || !fresh || status?.geometry?.retained === true);
  window.__mac.points = points;
  window.__mac.triangles = triangles;
  window.__mac.source = status?.geometry_source || status?.source || "WAITING";
  window.__mac.retained = retained;
  window.__mac.mapError = mapError;
  window.__mac.mapStatus = status ? {
    source: status.source, geometry_source: status.geometry_source ?? null,
    points: status.points, triangles: status.triangles,
    generation: status.generation, revision: status.revision, mesh_revision: status.mesh_revision,
    applied_points_revision: appliedPoints, applied_mesh_revision: appliedMesh,
    fresh, age_ms: Number.isFinite(statusAge) ? statusAge : null, connected: status.connected === true,
    retained, partial: status.geometry?.partial === true,
  } : null;
  banner.textContent = `MAC RENDER · FLAT · ${window.__mac.source} · ${points} pts · ${triangles} tris${retained ? " · RETAINED" : ""}${mapError ? " · map unavailable" : ""} · NOT STEREO · GRID: DISPLAY GUIDE`;
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
  return validStatus(JSON.parse(new TextDecoder().decode(await boundedBody(response, 65536, signal))));
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
      applyGeometry(surfaces, mesh, mesh.indices);
      if (!next.capped || next.geometry_source) { applyGeometry(cloud, mesh); appliedPoints = next.revision; }
      appliedMesh = result.revision;
      lastSnapshotKind = "mesh";
    } else if (next.points && next.revision > appliedPoints) {
      const result = await pull("/map/points.bin", next, abort.signal);
      if (!await snapshotStillCurrent(nextIdentity, next.generation, abort.signal)) return false;
      applyGeometry(cloud, decodePoints(result.body)); appliedPoints = result.revision;
      lastSnapshotKind = "points";
      if (!next.triangles) { surfaces.geometry.dispose(); surfaces.geometry = new THREE.BufferGeometry(); appliedMesh = next.mesh_revision; }
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
  const quaternionLength = finiteVector(next?.quaternion, 4) ? Math.hypot(...next.quaternion) : 0;
  if (!next || next.tracked !== true || !Number.isFinite(next.receiptAgeMs) || next.receiptAgeMs < 0 ||
      !Number.isFinite(age) || age < 0 || age >= 250 || !finiteVector(next.position, 3) || quaternionLength < 1e-6 ||
      !Number.isFinite(quaternionLength) || ![next.sentMonoMs, next.sentWallMs, next.receivedMonoMs].every(value =>
        typeof value === "number" && Number.isFinite(value)) || (next.ray && (!finiteVector(next.ray.origin, 3) ||
        !finiteVector(next.ray.direction, 3) || Math.hypot(...next.ray.direction) < 1e-6))) {
    clearPose(); return;
  }
  window.__mac.poseValid = true;
  if (next !== pose) {
    pose = next;
    camera.position.set(...next.position);
    tmp.set(...next.quaternion).normalize(); camera.quaternion.copy(tmp);
    if (Number.isFinite(next.fov_y_deg) && next.fov_y_deg >= 1 && next.fov_y_deg <= 170) camera.fov = next.fov_y_deg;
    if (Number.isFinite(next.aspect) && next.aspect >= .1 && next.aspect <= 5) camera.aspect = next.aspect;
    camera.updateProjectionMatrix();
    if (next.ray) {
      eye.set(...next.ray.origin);
      aim.set(...next.ray.direction).normalize().multiplyScalar(8).add(eye);
      rayLine.geometry.setFromPoints([eye, aim]); rayLine.visible = true;
    } else rayLine.visible = false;
  }
  window.__mac.poseEcho = { sent_mono_ms: next.sentMonoMs, sent_wall_ms: next.sentWallMs,
    received_mono_ms: next.receivedMonoMs, session_id: next.session_id,
    frame_id: next.frame_id, pose_id: next.pose_id, client_nonce: next.client_nonce,
    space: next.space, map_revision: next.map_revision, eyes: next.eyes };
  window.__mac.fovY = camera.fov; window.__mac.aspect = camera.aspect;
}

window.__mac = { ready: true, stereo: false, renderMs: 0, draws: 0, points: 0, source: "WAITING", poseEcho: null };
function drawScene() {
  const started = performance.now();
  applyPose();
  renderer.render(scene, camera);
  window.__mac.renderMs = performance.now() - started;
  window.__mac.drawnPoints = renderer.info.render.points;
  window.__mac.drawnTriangles = renderer.info.render.triangles;
  window.__mac.rayVisible = rayLine.visible;
  window.__mac.viewPosition = camera.position.toArray();
  window.__mac.draws = (window.__mac.draws || 0) + 1;
  exposeMap();
}
// Pixels and provenance are read in one JavaScript turn, without an intervening
// screenshot/RAF. Geometry callbacks cannot replace buffers during this call.
window.__macCapture = () => {
  drawScene();
  const data = renderer.domElement.toDataURL("image/jpeg", 0.8).split(",")[1];
  return { data, meta: { ...window.__mac,
    mapStatus: window.__mac.mapStatus ? { ...window.__mac.mapStatus } : null,
    poseEcho: window.__mac.poseEcho ? { ...window.__mac.poseEcho } : null } };
};
renderer.setAnimationLoop(() => { if (!window.__macCaptureDriven) drawScene(); });
window.__macRefreshMap = refresh;
refresh();
const refreshTimer = window.setInterval(refresh, 2000);
window.addEventListener("pagehide", () => { window.clearInterval(refreshTimer); incomingPose = null; clearPose(); });
