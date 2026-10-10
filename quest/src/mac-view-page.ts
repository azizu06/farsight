/**
 * Page entry for macview.html. Flat panel on the desktop, a single quad in
 * WebXR. Head and the right controller ray go back to the Mac. No /ws/control.
 */
import * as THREE from "three";
import { VRButton } from "three/addons/webxr/VRButton.js";
import { H264Viewer } from "./h264.js";
import { LABEL, MacView, rightInputIndex, socketUrl, MACVIEW_PATH } from "./mac-view.js";

const params = new URLSearchParams(location.search);
const origin = params.get("mac") || location.origin;

let renderer: THREE.WebGLRenderer | null = null;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.xr.enabled = true;
  renderer.setClearColor(0x080f16);
  document.body.prepend(renderer.domElement);
} catch (error) {
  // A desk browser with no WebGL can still play the flat stream and send pose.
  console.warn(`WebGL unavailable, flat video only: ${error}`);
}

const scene = renderer ? new THREE.Scene() : null;
const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.01, 100);
camera.position.set(0, 1.6, 0);
const view = new MacView(origin, document.body, new H264Viewer(
  () => {}, () => {}, socketUrl(origin, MACVIEW_PATH),
));
view.start();

if (renderer && scene) {
  const texture = new THREE.VideoTexture(view.viewer.video);
  const panel = new THREE.Mesh(
    new THREE.PlaneGeometry(1.6, 0.9),
    new THREE.MeshBasicMaterial({ map: texture }),
  );
  panel.position.set(0, 1.6, -1.5);
  scene.add(panel);
}

const label = document.createElement("div");
label.textContent = LABEL;
label.style.cssText = "position:fixed;left:12px;top:12px;color:#d7e2ea;font:13px monospace";
document.body.append(label);
if (renderer) document.body.append(VRButton.createButton(renderer));

const raycaster = new THREE.Raycaster();
const headPos = new THREE.Vector3();
const headQuat = new THREE.Quaternion();
const origin3 = new THREE.Vector3();
const direction = new THREE.Vector3();

function tick(_time: number, frame: XRFrame | null) {
  const now = { mono: performance.now(), wall: Date.now() };
  let ray = null;
  if (frame && renderer) {
    const ref = renderer.xr.getReferenceSpace();
    const pose = ref && frame.getViewerPose(ref);
    if (pose) {
      headPos.set(pose.transform.position.x, pose.transform.position.y, pose.transform.position.z);
      headQuat.set(
        pose.transform.orientation.x, pose.transform.orientation.y,
        pose.transform.orientation.z, pose.transform.orientation.w,
      );
    }
    const session = renderer.xr.getSession();
    const index = session ? rightInputIndex([...session.inputSources]) : -1;
    if (index >= 0) {
      const right = renderer.xr.getController(index);
      right.getWorldPosition(origin3);
      right.getWorldDirection(direction);
      ray = { origin: origin3, direction };
    }
  } else {
    headPos.copy(camera.position);
    headQuat.copy(camera.quaternion);
    raycaster.setFromCamera(new THREE.Vector2(), camera);
    ray = { origin: raycaster.ray.origin, direction: raycaster.ray.direction };
  }
  view.sendPose(now, { position: headPos, quaternion: headQuat }, ray);
  view.tick();
  if (renderer && scene) renderer.render(scene, camera);
}
if (renderer) renderer.setAnimationLoop(tick);
else setInterval(() => tick(0, null), 1000 / 72);
