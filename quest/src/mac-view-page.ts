/**
 * Page entry for macview.html. Flat panel on the desktop, a single quad in
 * WebXR. Head and the right controller ray go back to the Mac. No /ws/control.
 */
import * as THREE from "three";
import { VRButton } from "three/addons/webxr/VRButton.js";
import { H264Viewer } from "./h264.js";
import { LABEL, MacView, MACVIEW_PATH } from "./mac-view.js";

const params = new URLSearchParams(location.search);
const origin = params.get("mac") || location.origin;

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.xr.enabled = true;
renderer.setClearColor(0x080f16);
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.01, 100);
camera.position.set(0, 1.6, 0);
const view = new MacView(origin, document.body, new H264Viewer(() => {}, () => {}, MACVIEW_PATH));
view.start();

const texture = new THREE.VideoTexture(view.viewer.video);
const panel = new THREE.Mesh(
  new THREE.PlaneGeometry(1.6, 0.9),
  new THREE.MeshBasicMaterial({ map: texture }),
);
panel.position.set(0, 1.6, -1.5);
scene.add(panel);

const label = document.createElement("div");
label.textContent = LABEL;
label.style.cssText = "position:fixed;left:12px;top:12px;color:#d7e2ea;font:13px monospace";
document.body.append(label, VRButton.createButton(renderer));

const raycaster = new THREE.Raycaster();
const headPos = new THREE.Vector3();
const headQuat = new THREE.Quaternion();
const origin3 = new THREE.Vector3();
const direction = new THREE.Vector3();

renderer.setAnimationLoop((_time, frame) => {
  const now = { mono: performance.now(), wall: Date.now() };
  let ray = null;
  if (frame) {
    const ref = renderer.xr.getReferenceSpace();
    const pose = ref && frame.getViewerPose(ref);
    if (pose) {
      headPos.set(pose.transform.position.x, pose.transform.position.y, pose.transform.position.z);
      headQuat.set(
        pose.transform.orientation.x, pose.transform.orientation.y,
        pose.transform.orientation.z, pose.transform.orientation.w,
      );
    }
    const right = renderer.xr.getController(0);
    right.getWorldPosition(origin3);
    right.getWorldDirection(direction);
    ray = { origin: origin3, direction };
  } else {
    headPos.copy(camera.position);
    headQuat.copy(camera.quaternion);
    raycaster.setFromCamera(new THREE.Vector2(), camera);
    ray = { origin: raycaster.ray.origin, direction: raycaster.ray.direction };
  }
  view.sendPose(now, { position: headPos, quaternion: headQuat }, ray);
  renderer.render(scene, camera);
});
