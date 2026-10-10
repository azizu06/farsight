/** Standalone flat Mac picture. Current tracking only; no /ws/control. */
import * as THREE from "three";
import { VRButton } from "three/addons/webxr/VRButton.js";
import { H264Viewer } from "./h264.js";
import { MacView, MACVIEW_PATH, streamOrigin, streamURL, trackedXR } from "./mac-view.js";

const origin = streamOrigin(new URLSearchParams(location.search).get("mac") || location.origin);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); renderer.setSize(innerWidth, innerHeight);
renderer.xr.enabled = true; renderer.xr.setReferenceSpaceType("local-floor"); renderer.setClearColor(0x080f16);
document.body.prepend(renderer.domElement);
const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(50, innerWidth / innerHeight, .01, 100);
camera.position.set(0, 1.6, 0);
let view: MacView;
const decoder = new H264Viewer(() => {}, () => view?.streamReset(), streamURL(origin, MACVIEW_PATH),
  { onMetadata: message => view.noteSidecar(message) });
view = new MacView(origin, document.body, decoder); view.start();
const texture = new THREE.VideoTexture(decoder.video); texture.colorSpace = THREE.SRGBColorSpace;
const geometry = new THREE.PlaneGeometry(1.6, .9), material = new THREE.MeshBasicMaterial({ map: texture, toneMapped: false });
const panel = new THREE.Mesh(geometry, material); panel.position.set(0, 1.6, -1.5); scene.add(panel);
// Status is also in the XR scene: DOM overlays alone disappear in immersive mode.
const labelCanvas = document.createElement("canvas"); labelCanvas.width = 1024; labelCanvas.height = 160;
const context = labelCanvas.getContext("2d")!, labelTexture = new THREE.CanvasTexture(labelCanvas);
labelTexture.colorSpace = THREE.SRGBColorSpace;
const labelGeometry = new THREE.PlaneGeometry(1.6, .25), labelMaterial = new THREE.MeshBasicMaterial({ map: labelTexture, transparent: true, depthTest: false, toneMapped: false });
const labelPanel = new THREE.Mesh(labelGeometry, labelMaterial); labelPanel.position.set(0, 1.0, -1.49); labelPanel.renderOrder = 2; scene.add(labelPanel);
const button = VRButton.createButton(renderer); document.body.append(button);
const raycaster = new THREE.Raycaster(); let disposed = false, labelAt = -Infinity;
let reference: XRReferenceSpace | null = null;
const resetReference = () => { view.untracked(); view.resetPoseSession(); };
renderer.xr.addEventListener("sessionstart", () => {
  resetReference(); reference = renderer.xr.getReferenceSpace(); reference?.addEventListener("reset", resetReference);
});
renderer.xr.addEventListener("sessionend", () => {
  reference?.removeEventListener("reset", resetReference); reference = null; resetReference();
});
const visibility = () => { if (document.hidden) view.untracked(); };
document.addEventListener("visibilitychange", visibility);
const resize = () => { renderer.setSize(innerWidth, innerHeight); camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); };
window.addEventListener("resize", resize);
renderer.setAnimationLoop((_time, frame) => {
  if (disposed) return;
  const now = { mono: performance.now(), wall: Date.now() };
  if (renderer.xr.isPresenting) {
    const tracked = trackedXR(frame, renderer.xr.getReferenceSpace(), renderer.xr.getSession());
    if (tracked) view.sendPose(now, tracked.head, tracked.ray); else view.untracked();
  } else if (!document.hidden) {
    raycaster.setFromCamera(new THREE.Vector2(), camera);
    view.sendPose(now, { position: camera.position, quaternion: camera.quaternion }, { origin: raycaster.ray.origin, direction: raycaster.ray.direction });
  } else view.untracked();
  view.update();
  if (now.mono - labelAt >= 250) {
    labelAt = now.mono; context.fillStyle = "#08121fee"; context.fillRect(0, 0, 1024, 160);
    context.fillStyle = view.frameAgeMs === null || view.frameAgeMs >= 500 ? "#ffe08a" : "#d7e2ea";
    context.font = "24px system-ui";
    const text = view.age.textContent ?? "", words = text.split(" · ");
    let line = "", y = 32;
    for (const word of words) {
      const next = line ? line + " · " + word : word;
      if (context.measureText(next).width > 984 && line) { context.fillText(line, 20, y); y += 32; line = word; } else line = next;
    }
    context.fillText(line, 20, y); labelTexture.needsUpdate = true;
  }
  renderer.render(scene, camera);
});
const dispose = () => {
  if (disposed) return; disposed = true; renderer.setAnimationLoop(null); view.dispose();
  reference?.removeEventListener("reset", resetReference);
  document.removeEventListener("visibilitychange", visibility); window.removeEventListener("resize", resize);
  texture.dispose(); material.dispose(); geometry.dispose(); labelTexture.dispose(); labelMaterial.dispose(); labelGeometry.dispose();
  void renderer.xr.getSession()?.end().catch(() => {}); renderer.dispose(); renderer.domElement.remove(); button.remove();
};
window.addEventListener("pagehide", dispose, { once: true }); window.addEventListener("beforeunload", dispose, { once: true });
