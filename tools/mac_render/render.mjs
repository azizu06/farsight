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

const params = new URLSearchParams(location.search);
const upstream = (params.get("upstream") || "").replace(/\/$/, "");
const width = Number(params.get("width") || 1280);
const height = Number(params.get("height") || 720);

const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false });
renderer.setSize(width, height, false);
renderer.setPixelRatio(1);
renderer.setClearColor(0x080f16);
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 2000);
camera.position.set(9, 6, 9);
camera.lookAt(0, 0, 0);

const root = new THREE.Group();
scene.add(root);
const grid = new THREE.GridHelper(40, 40, 0x616161, 0x4d4d4d);
grid.position.y = -0.002;
root.add(grid);
scene.add(new THREE.AmbientLight(0xffffff, 1));

const pointsMaterial = new THREE.PointsMaterial({ size: 0.025, vertexColors: true, toneMapped: false });
const cloud = new THREE.Points(new THREE.BufferGeometry(), pointsMaterial);
root.add(cloud);
const surfaces = new THREE.Mesh(
  new THREE.BufferGeometry(),
  new THREE.MeshBasicMaterial({
    vertexColors: true, side: THREE.DoubleSide, toneMapped: false,
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  }),
);
root.add(surfaces);

const rayLine = new THREE.Line(
  new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
  new THREE.LineBasicMaterial({ color: 0xacbdda }),
);
rayLine.visible = false;
scene.add(rayLine);

const banner = document.getElementById("banner");
let status = null;
let appliedPoints = -1;
let appliedMesh = -1;
let pose = null;
const tmp = new THREE.Quaternion();
const eye = new THREE.Vector3();
const aim = new THREE.Vector3();

function applyGeometry(object, data, indices) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(data.positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(data.colors, 3));
  if (indices) geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.computeBoundingSphere();
  object.geometry.dispose();
  object.geometry = geometry;
}

async function pull(path) {
  const response = await fetch(`${upstream}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.arrayBuffer();
}

async function refresh() {
  const started = performance.now();
  try {
    const response = await fetch(`${upstream}/health/map`, { cache: "no-store" });
    if (response.ok) status = await response.json();
  } catch {
    /* next tick retries; a missing map stays an empty scene */
  }
  try {
    if (status && status.mesh_revision !== appliedMesh && status.triangles) {
      const mesh = decodeMesh(await pull("/map/mesh.bin"));
      applyGeometry(surfaces, mesh, mesh.indices);
      applyGeometry(cloud, mesh);
      appliedMesh = status.mesh_revision;
      appliedPoints = status.revision;
    } else if (status && status.revision !== appliedPoints && status.points) {
      applyGeometry(cloud, decodePoints(await pull("/map/points.bin")));
      appliedPoints = status.revision;
    }
  } catch (error) {
    banner.textContent = `MAC RENDER · ${error}`;
  }
  window.__mac = window.__mac || {};
  window.__mac.points = cloud.geometry.getAttribute("position")?.count || 0;
  window.__mac.source = status?.source || "WAITING";
  banner.textContent = `MAC RENDER · FLAT · ${window.__mac.source} · ${window.__mac.points} pts · NOT STEREO`;
}

function applyPose() {
  const next = window.__macPose;
  if (!next || next === pose) return;
  pose = next;
  const age = performance.now() - (next.receivedMonoMs ?? performance.now());
  if (age > 250) return;
  camera.position.set(...next.position);
  tmp.set(...next.quaternion);
  camera.quaternion.copy(tmp);
  if (next.ray) {
    eye.set(...next.ray.origin);
    aim.set(...next.ray.direction).normalize().multiplyScalar(8).add(eye);
    rayLine.geometry.setFromPoints([eye, aim]);
    rayLine.visible = true;
  } else rayLine.visible = false;
  window.__mac.poseEcho = {
    sent_mono_ms: next.sentMonoMs,
    sent_wall_ms: next.sentWallMs,
    received_mono_ms: next.receivedMonoMs,
  };
}

window.__mac = { ready: true, stereo: false, renderMs: 0, draws: 0, points: 0, source: "WAITING", poseEcho: null };
renderer.setAnimationLoop(() => {
  const started = performance.now();
  applyPose();
  renderer.render(scene, camera);
  window.__mac.renderMs = performance.now() - started;
  window.__mac.draws = (window.__mac.draws || 0) + 1;
});
refresh();
setInterval(refresh, 2000);
