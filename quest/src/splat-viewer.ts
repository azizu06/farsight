import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { VRButton } from "three/addons/webxr/VRButton.js";
import { SparkRenderer, SplatMesh, type SplatFileType } from "@sparkjsdev/spark";
import { splatParams, sceneFileType, sceneStart } from "./splat-nav.js";
import { SplatNavigation } from "./splat-navigation";

/**
 * Post-run Gaussian splat walkthrough (splat.html). Separate from the driving page:
 * no rover sockets, no control. Spark renders the splats inside three.js, so the
 * stock WebXR path (VRButton, local-floor) drives the stereo views.
 */
const params = splatParams(location.search);
const status = document.getElementById("splat-status")!;
const shParam = Number(new URLSearchParams(location.search).get("sh"));

const renderer = new THREE.WebGLRenderer({ antialias: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.xr.enabled = true;
renderer.xr.setReferenceSpaceType("local-floor");
// Fixed foveation trims fragment cost at the lens edges, where splat overdraw is worst.
renderer.xr.setFoveation(1);
document.body.appendChild(renderer.domElement);
document.body.appendChild(VRButton.createButton(renderer));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x080f16);
const spark = new SparkRenderer({ renderer });
scene.add(spark);

// The rig is the walkthrough: XR head pose is applied inside it, sticks move and turn it.
const rig = new THREE.Group();
const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.05, 500);
rig.add(camera);
scene.add(rig);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
const navigation = new SplatNavigation(renderer, rig, camera, controls);

let mesh: SplatMesh | null = null;
let flipped = params.flip;
const UPRIGHT = new THREE.Quaternion(), FLIPPED = new THREE.Quaternion(1, 0, 0, 0);
let transforms: unknown = null;

function setStart() {
  const box = mesh?.isInitialized ? mesh.getBoundingBox(true).applyMatrix4(mesh.matrixWorld) : null;
  const start = sceneStart(transforms, box, flipped);
  const query = new URLSearchParams(location.search);
  if (query.has("pos")) start.eye = [params.start[0], params.start[1] + 1.6, params.start[2]];
  if (query.has("yaw")) start.yaw = THREE.MathUtils.degToRad(params.yawDeg);
  navigation.setStart(start);
  stats.start = start;
}

function resetView() {
  navigation.reset();
}

/** Explicit transforms= wins, then scene-specific or adjacent dataset sidecars. */
async function loadTransforms(url?: string) {
  if (!url) return null;
  const sceneUrl = new URL(url, location.href);
  const sidecar = new URL(sceneUrl.href);
  sidecar.pathname = sidecar.pathname.replace(/\.[^/.]+$/, "") + ".transforms.json";
  const explicit = new URLSearchParams(location.search).get("transforms");
  const candidates = explicit ? [new URL(explicit, location.href)] : [sidecar, new URL("transforms.json", sceneUrl)];
  for (const candidate of candidates) {
    try {
      const response = await fetch(candidate);
      if (response.ok) return await response.json();
    } catch { /* A bare scene file still has a bounds-based start. */ }
  }
  return null;
}

async function load(options: { url?: string; fileBytes?: ArrayBuffer; fileName?: string; fileType?: string }) {
  if (mesh) { scene.remove(mesh); mesh.dispose(); mesh = null; }
  status.textContent = `Loading ${options.fileName ?? options.url}…`;
  const started = performance.now();
  const pose = loadTransforms(options.url);
  try {
    const next = new SplatMesh({ ...options, fileType: options.fileType as SplatFileType | undefined });
    next.quaternion.copy(flipped ? FLIPPED : UPRIGHT);
    mesh = next;
    scene.add(next);
    const [, capture] = await Promise.all([next.initialized, pose]);
    if (mesh !== next) return;
    transforms = capture;
    if (Number.isInteger(shParam) && shParam >= 0 && shParam <= 3) next.maxSh = shParam;
    next.updateMatrixWorld(true);
    stats.splats = next.packedSplats?.numSplats ?? null;
    stats.loadMs = Math.round(performance.now() - started);
    status.textContent = `${options.fileName ?? options.url} · ${stats.splats?.toLocaleString() ?? "?"} splats · ${stats.loadMs} ms`;
    setStart(); resetView();
  } catch (error) {
    status.textContent = `Could not load scene: ${error instanceof Error ? error.message : String(error)}`;
    stats.error = status.textContent;
  }
}

document.getElementById("splat-reset")!.onclick = resetView;
document.getElementById("splat-flip")!.onclick = () => {
  flipped = !flipped;
  mesh?.quaternion.copy(flipped ? FLIPPED : UPRIGHT);
  mesh?.updateMatrixWorld(true);
  setStart(); resetView();
};
document.getElementById("splat-file")!.addEventListener("change", async event => {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (file) await load({ fileBytes: await file.arrayBuffer(), fileName: file.name, fileType: sceneFileType(file.name) });
});

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
renderer.xr.addEventListener("sessionstart", () => { controls.enabled = false; resetView(); });
renderer.xr.addEventListener("sessionend", () => { controls.enabled = true; resetView(); });

/** Read by tests and over CDP on the headset: frame rate, splat count, load time. */
const stats = { fps: 0, splats: null as number | null, loadMs: null as number | null, xr: false, error: null as string | null,
  start: null as ReturnType<typeof sceneStart> | null, rig: [0, 0, 0], yaw: 0, inputs: navigation.inputs,
  eye: [0, 1.6, 0], flatGamepads: 0 };
(globalThis as unknown as { __splatStats: typeof stats }).__splatStats = stats;

let last = performance.now(), frames = 0, windowStart = last;
const eye = new THREE.Vector3(), orientation = new THREE.Quaternion(), heading = new THREE.Euler(0, 0, 0, "YXZ");
renderer.setAnimationLoop((_time, frame) => {
  const now = performance.now(), dt = (now - last) / 1000;
  last = now;
  stats.xr = renderer.xr.isPresenting;
  const pads = stats.xr ? [] : [...navigator.getGamepads()];
  navigation.update(dt, frame, pads);
  if (!stats.xr) controls.update();
  stats.rig = rig.position.toArray();
  stats.inputs = navigation.inputs; stats.flatGamepads = pads.filter(Boolean).length;
  renderer.render(scene, camera);
  camera.getWorldPosition(eye); camera.getWorldQuaternion(orientation);
  stats.eye = eye.toArray(); stats.yaw = heading.setFromQuaternion(orientation, "YXZ").y;
  frames++;
  if (now - windowStart >= 1000) { stats.fps = Math.round(frames * 1000 / (now - windowStart)); frames = 0; windowStart = now; }
});

setStart(); resetView();
if (params.scene) void load({ url: params.scene, fileType: params.type });
