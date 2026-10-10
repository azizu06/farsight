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
import { DenseLayer, shadePass, splatMaterial } from "./dense-splats.mjs";

const params = new URLSearchParams(location.search);
const upstream = (params.get("upstream") || "").replace(/\/$/, "");
const width = Number(params.get("width") || 1280);
const height = Number(params.get("height") || 720);

const style = params.get("style") === "unlit" ? "unlit" : "lit";
// World splat diameter for dense 1 cm cells: large enough to close sampling gaps.
const splatSize = Number(params.get("splat") || 0.03);
// The widest vertical FOV drawn on request; beyond this the picture gets too coarse.
const MAX_FOV = 90;
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
const camera = new THREE.PerspectiveCamera(50, width / height, 0.05, 300);
camera.position.set(9, 6, 9);
camera.lookAt(0, 0, 0);

const root = new THREE.Group();
scene.add(root);
const grid = new THREE.GridHelper(40, 40, 0x3a424a, 0x2c343c);
grid.position.y = -0.002;
root.add(grid);

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

const pointsMaterial = new THREE.PointsMaterial({
  size: 0.025, vertexColors: true, toneMapped: style === "lit",
});
const cloud = new THREE.Points(new THREE.BufferGeometry(), pointsMaterial);
cloud.renderOrder = 0;
root.add(cloud);
const surfaceMaterial = style === "lit"
  ? new THREE.MeshStandardMaterial({
    color: 0xffffff, vertexColors: true, side: THREE.DoubleSide,
    roughness: 0.85, metalness: 0,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
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

// Dense measured map (relay /dense/delta.bin): RGB-D first, gray native ARKit only when RGB-D is empty.
const dense = new DenseLayer("rgbd");
const denseGray = new DenseLayer("geometry", 2_000_000, true);
const denseSplats = new THREE.Points(dense.geometry, splatMaterial(splatSize));
const graySplats = new THREE.Points(denseGray.geometry, splatMaterial(splatSize * 1.4));
denseSplats.frustumCulled = graySplats.frustumCulled = false;
root.add(denseSplats, graySplats);
const target = new THREE.WebGLRenderTarget(width, height, {
  depthTexture: new THREE.DepthTexture(width, height, THREE.FloatType),
});
// The shade pass writes sRGB directly, so hand it the background's sRGB components unconverted.
const shade = shadePass(target, camera, new THREE.Color().setHex(GRAPHITE, THREE.LinearSRGBColorSpace));
let denseAvailable = true;
function splatScale() {
  const scale = height / (2 * Math.tan((camera.fov * Math.PI) / 360));
  denseSplats.material.uniforms.scale.value = graySplats.material.uniforms.scale.value = scale;
}
splatScale();
async function fetchDense(query) {
  const response = await fetch(`${upstream}/dense/delta.bin?${query}`, { cache: "no-store" });
  if (response.status === 404 || response.status === 503) { const error = new Error("dense lane off"); error.off = true; throw error; }
  if (!response.ok) throw new Error(`dense ${response.status}`);
  return response.arrayBuffer();
}
async function pollDense() {
  if (params.get("dense") === "0") { denseAvailable = false; return; }
  try {
    await dense.poll(fetchDense);
    if (!dense.count) await denseGray.poll(fetchDense);
    denseAvailable = true;
  } catch (error) {
    denseAvailable = !error.off;
    if (!error.off) console.warn(String(error));
  }
  setTimeout(pollDense, denseAvailable ? 250 : 5000);
}
function denseShown() { return dense.count > 0 || denseGray.count > 0; }

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
let pose = null;
const tmp = new THREE.Quaternion();
const eye = new THREE.Vector3();
const aim = new THREE.Vector3();

function colorsAreValid(colors) {
  if (!colors || !colors.length) return false;
  for (let i = 0; i < colors.length; i += 1) {
    if (!Number.isFinite(colors[i])) return false;
  }
  return true;
}

function applyGeometry(object, data, indices) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(data.positions, 3));
  if (colorsAreValid(data.colors)) {
    geometry.setAttribute("color", new THREE.BufferAttribute(data.colors, 3));
  }
  if (indices) {
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    geometry.computeVertexNormals();
  }
  geometry.computeBoundingSphere();
  object.geometry.dispose();
  object.geometry = geometry;
}

/** One short brighten when a new map revision arrives, then back to rest. */
function reveal(object) {
  const material = object.material;
  if (!material || !material.emissive) return;
  material.emissive.setHex(ACCENT);
  material.emissiveIntensity = 0.35;
  const started = performance.now();
  const settle = () => {
    const t = Math.min(1, (performance.now() - started) / 700);
    material.emissiveIntensity = 0.35 * (1 - t);
    if (t < 1) requestAnimationFrame(settle);
    else material.emissive.setHex(0x000000);
  };
  requestAnimationFrame(settle);
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
      if (style === "lit") {
        const used = new Uint8Array(mesh.positions.length / 3);
        for (let i = 0; i < mesh.indices.length; i += 1) used[mesh.indices[i]] = 1;
        let loose = 0;
        for (let i = 0; i < used.length; i += 1) if (!used[i]) loose += 1;
        const positions = new Float32Array(loose * 3);
        const colors = new Float32Array(loose * 3);
        for (let i = 0, o = 0; i < used.length; i += 1) {
          if (used[i]) continue;
          positions.set(mesh.positions.subarray(i * 3, i * 3 + 3), o);
          colors.set(mesh.colors.subarray(i * 3, i * 3 + 3), o);
          o += 3;
        }
        applyGeometry(cloud, { positions, colors });
      } else applyGeometry(cloud, mesh);
      surfaces.renderOrder = 2;
      reveal(surfaces);
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
  window.__mac.style = style;
  window.__mac.points = cloud.geometry.getAttribute("position")?.count || 0;
  window.__mac.source = status?.source || "WAITING";
  const solid = dense.count ? `DENSE RGB-D ${dense.count.toLocaleString()} cells` : denseGray.count ? `NATIVE MESH (gray) ${denseGray.count.toLocaleString()} pts` : `${window.__mac.points} pts`;
  banner.textContent = `MAC RENDER · FLAT · ${window.__mac.source} · ${solid} · NOT STEREO`;
}

function applyPose() {
  const next = window.__macPose;
  if (!next || next === pose) return;
  // Stale is decided on the Mac clock, in Python, before the pose is handed
  // here. Comparing the browser clock with the Python clock is not a check.
  const heldMs = performance.now() - (next.pageMonoMs ?? performance.now());
  if (heldMs > 250) return;
  pose = next;
  const fov = Number.isFinite(next.fovY) ? Math.min(MAX_FOV, Math.max(20, next.fovY)) : camera.fov;
  if (fov !== camera.fov) { camera.fov = fov; camera.updateProjectionMatrix(); splatScale(); }
  camera.position.set(...next.position);
  tmp.set(...next.quaternion);
  camera.quaternion.copy(tmp);
  if (next.ray) {
    eye.set(...next.ray.origin);
    aim.set(...next.ray.direction).normalize().multiplyScalar(8).add(eye);
    rayLine.geometry.setFromPoints([eye, aim]);
    rayLine.visible = true;
  } else rayLine.visible = false;
}

window.__mac = { ready: true, stereo: false, renderMs: 0, draws: 0, points: 0, source: "WAITING", poseEcho: null };
renderer.setAnimationLoop(() => {
  const started = performance.now();
  applyPose();
  const solid = denseShown();
  denseSplats.visible = dense.count > 0;
  graySplats.visible = !dense.count && denseGray.count > 0;
  // Dense cells replace the coarse preview cloud/mesh; the grid stays as a floor cue.
  cloud.visible = surfaces.visible = grid.visible = !solid;
  if (solid) {
    renderer.setRenderTarget(target);
    renderer.render(scene, camera);
    shade.render(renderer);
  } else renderer.render(scene, camera);
  // Echo only the pose this draw used, so a sidecar cannot stamp a pose that
  // was received but not yet in a picture.
  if (pose) {
    window.__mac.poseEcho = {
      sent_mono_ms: pose.sentMonoMs,
      sent_wall_ms: pose.sentWallMs,
      received_mono_ms: pose.receivedMonoMs,
      pose_id: pose.poseId ?? null,
      session_id: pose.sessionId ?? null,
      // The frustum this picture was actually drawn with, not just the one requested.
      fov_y_deg: camera.fov,
      aspect: camera.aspect,
    };
  }
  window.__mac.renderMs = performance.now() - started;
  window.__mac.draws = (window.__mac.draws || 0) + 1;
  window.__mac.dense = { points: dense.count, gray: denseGray.count, shown: solid, available: denseAvailable };
});
pollDense();
refresh();
setInterval(refresh, 2000);
