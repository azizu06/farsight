/** The clean browser canvas presents actual Mac encoder frames, without /ws/control. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { H264Viewer } from "./h264";
import { streamOrigin } from "./mac-view";
import { socketUrl } from "./mac-view-urls.js";
import { macFormat, macRectangle } from "./desktop-mac-format.js";
import "./style.css";

const query = new URLSearchParams(location.search);
const app = document.querySelector<HTMLElement>("#app")!;
app.innerHTML = `<main class="viewport" aria-label="Mac renderer preview">
  <canvas id="mac-render-canvas" tabindex="0" aria-label="Actual Mac-rendered 3D scene"></canvas>
</main><dialog id="mac-preview-settings" class="settings">
  <header><h1>Mac render preview</h1><button id="close-mac-settings" aria-label="Close settings">×</button></header>
  <p id="mac-preview-status" role="status">Connecting to Mac renderer…</p>
  <form id="mac-origin-form"><label>Renderer origin<input id="mac-origin" type="url" required /></label>
    <button type="submit">Connect</button></form>
  <label class="check"><input id="mac-inspect" type="checkbox"/> Inspect with mouse</label>
  <p>Right-drag to orbit, left-drag to pan, scroll to zoom. Home resets the view.
  Turn off mouse inspection to mirror the headset. An active headset keeps ownership of its camera.</p>
  <p id="mac-preview-source">Waiting for rendered geometry</p>
  <p id="mac-preview-format">Waiting for encoder format</p>
  <p>This canvas shows the encoded Mac output. It sends no rover commands.</p>
  <a href="?view=map&local=1">Open local geometry view</a>
</dialog>`;
const canvas = document.querySelector<HTMLCanvasElement>("#mac-render-canvas")!;
const context = canvas.getContext("2d")!;
const settings = document.querySelector<HTMLDialogElement>("#mac-preview-settings")!;
const input = document.querySelector<HTMLInputElement>("#mac-origin")!;
const inspectInput = document.querySelector<HTMLInputElement>("#mac-inspect")!;
// A desk preview must work without a worn headset. The server's exclusive
// visual pose socket keeps a currently connected headset authoritative.
inspectInput.checked = query.get("inspect") !== "0";
const status = document.querySelector<HTMLElement>("#mac-preview-status")!;
const source = document.querySelector<HTMLElement>("#mac-preview-source")!;
const formatText = document.querySelector<HTMLElement>("#mac-preview-format")!;
const camera = new THREE.PerspectiveCamera(50, 16 / 9, .01, 2000);
camera.position.set(9, 6, 9); camera.lookAt(0, 0, 0);
const orbit = new OrbitControls(camera, canvas);
orbit.enableDamping = true; orbit.dampingFactor = .12; orbit.minDistance = .1; orbit.maxDistance = 200;
orbit.mouseButtons = { LEFT: THREE.MOUSE.PAN, RIGHT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN };
orbit.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
orbit.saveState();
let origin = streamOrigin(query.get("mac") || location.origin);
input.value = origin;
let viewer: H264Viewer;
let format: ReturnType<typeof macFormat> = null;
let stereo = query.get("stereo") === "1", explicitLayout = query.has("stereo");
let pose: WebSocket | null = null, poseRetry = 0, frameLoop = 0, disposed = false;
let session = crypto.randomUUID(), sequence = 0, lastPoseAt = -Infinity;
let lastFrame = "", lastPaintAt = -Infinity, startedAt = 0, cameraState = "Connecting visual camera";
let lastStatusAt = -Infinity, dirty = true;
if (!inspectInput.checked) cameraState = "Receive-only mirror · headset camera";
const eyeOffset = new THREE.Vector3(), eyePosition = new THREE.Vector3();

function connectPose() {
  if (disposed || !inspectInput.checked || pose) return;
  const socket = new WebSocket(socketUrl(origin, "/ws/macview-pose"));
  pose = socket;
  socket.onopen = () => { cameraState = "Mouse inspection · visual camera only"; };
  socket.onmessage = event => {
    try { const message = JSON.parse(String(event.data));
      if (message.type === "pose_rejected") cameraState = `Visual camera rejected: ${message.reason}`;
    } catch { /* A diagnostic never changes the stream. */ }
  };
  socket.onerror = () => { cameraState = "Camera unavailable or owned by headset · mirroring output"; socket.close(); };
  socket.onclose = () => {
    if (pose !== socket || disposed) return;
    pose = null;
    if (inspectInput.checked) poseRetry = window.setTimeout(connectPose, 3000);
  };
}
function closePose() {
  window.clearTimeout(poseRetry);
  const socket = pose; pose = null;
  if (socket) { socket.onclose = null; socket.close(); }
}
function startVideo() {
  viewer?.stop(); format = null; lastFrame = ""; startedAt = performance.now();
  viewer = new H264Viewer(() => { dirty = true; }, () => { format = null; },
    socketUrl(origin, `/ws/macview${stereo ? "?layout=sbs-left-right" : ""}`), {
      onMetadata(message) {
        if (message.type !== "video_init" && message.type !== "video_segment") return;
        const next = macFormat(message);
        if (!next || next.stereo !== stereo) { format = null; dirty = true; return false; }
        format = next;
        return true;
      },
    });
  viewer.start();
}
function drawGrid() {
  context.fillStyle = "#202226"; context.fillRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = "rgba(120,130,145,.055)"; context.lineWidth = 1;
  const horizon = canvas.height * .55, center = canvas.width / 2;
  context.beginPath();
  for (let i = -12; i <= 12; i++) {
    context.moveTo(center + i * 8, horizon); context.lineTo(center + i * canvas.width / 6, canvas.height);
  }
  for (let i = 1; i <= 10; i++) {
    const y = horizon + (canvas.height - horizon) * (i / 10) ** 2;
    context.moveTo(0, y); context.lineTo(canvas.width, y);
  }
  context.stroke();
}
function sendPose(now: number) {
  if (!inspectInput.checked || document.hidden || settings.open || pose?.readyState !== WebSocket.OPEN ||
      pose.bufferedAmount || now - lastPoseAt < 1000 / 30) return;
  lastPoseAt = now; camera.updateMatrixWorld();
  const eyes = stereo ? ["left", "right"].map((eye, i) => {
    eyeOffset.set(i ? .032 : -.032, 0, 0).applyQuaternion(camera.quaternion);
    eyePosition.copy(camera.position).add(eyeOffset);
    return { eye, position: eyePosition.toArray(), quaternion: camera.quaternion.toArray(), projection: camera.projectionMatrix.toArray() };
  }) : undefined;
  pose.send(JSON.stringify({ type: "pose", version: 1, tracked: true, space: "ar_map",
    session_id: session, frame_id: ++sequence, client_nonce: crypto.randomUUID(),
    sent_mono_ms: now, sent_wall_ms: Date.now(),
    head: { position: camera.position.toArray(), quaternion: camera.quaternion.toArray() }, ray: null,
    fov_y_deg: camera.fov, aspect: camera.aspect, stereo, ...(eyes ? { eyes } : {}) }));
}
function tick(now: number) {
  if (disposed) return;
  orbit.enabled = inspectInput.checked && !settings.open; orbit.update(); sendPose(now);
  viewer.update();
  if (!format && !explicitLayout && now - startedAt > 4000) { stereo = !stereo; startVideo(); }
  const frame = viewer.displayedFrame;
  const drawnFormat = frame ? macFormat(frame.metadata) : null;
  if (frame && format && drawnFormat && frame.id !== lastFrame && viewer.video.videoWidth && viewer.video.videoHeight) {
    const rectangle = macRectangle(viewer.video.videoWidth, viewer.video.videoHeight, canvas.width, canvas.height, drawnFormat.stereo);
    const aspect = rectangle.sourceWidth / viewer.video.videoHeight;
    if (camera.aspect !== aspect) { camera.aspect = aspect; camera.updateProjectionMatrix(); }
    context.fillStyle = "#202226"; context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(viewer.video, 0, 0, rectangle.sourceWidth, viewer.video.videoHeight,
      rectangle.x, rectangle.y, rectangle.width, rectangle.height);
    lastFrame = frame.id; lastPaintAt = now; dirty = false;
    canvas.dataset.frameId = frame.id; canvas.dataset.layout = format.stereo ? "sbs-left-eye" : "flat";
    canvas.dataset.poseVerified = String(frame.pose?.session_id === session);
    canvas.dataset.poseFrame = String(frame.pose?.frame_id ?? "");
    const map = (frame.metadata as unknown as { map?: { source?: string; points?: number; triangles?: number; retained?: boolean } }).map;
    source.textContent = map ? `${map.source ?? "Source unreported"} · ${map.points ?? 0} points · ${map.triangles ?? 0} triangles${map.retained ? " · retained" : ""}` : "Mac render · geometry source unreported";
    canvas.dataset.source = map?.source ?? "unreported";
  } else if (dirty && lastPaintAt === -Infinity) { drawGrid(); dirty = false; }
  if (now - lastStatusAt > 250) {
    lastStatusAt = now;
    const fresh = frame && format && drawnFormat && frame.age < 1000;
    canvas.dataset.state = fresh ? "rendering" : lastPaintAt > -Infinity ? "retained" : "waiting";
    status.textContent = `${fresh ? `Mac rendering · ${Math.ceil(frame.age)} ms picture age` : lastPaintAt > -Infinity ? "Render paused · last decoded picture retained" : viewer.state} · ${cameraState}`;
    formatText.textContent = format ? `${format.stereo ? "Stereo stream · left-eye preview" : "Flat stream"} · ${viewer.video.videoWidth} × ${viewer.video.videoHeight}` : "Waiting for matching flat/stereo stream";
  }
  frameLoop = requestAnimationFrame(tick);
}
const resize = new ResizeObserver(() => {
  const ratio = Math.min(devicePixelRatio, 2);
  canvas.width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  canvas.height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  lastFrame = ""; lastPaintAt = -Infinity; dirty = true;
});
resize.observe(canvas);
canvas.addEventListener("contextmenu", event => event.preventDefault());
canvas.addEventListener("pointerdown", () => canvas.focus({ preventScroll: true }));
document.querySelector("#close-mac-settings")!.addEventListener("click", () => settings.close());
window.addEventListener("keydown", event => {
  if (event.key === "Escape" && !settings.open) { event.preventDefault(); settings.showModal(); }
  if (event.key === "Home" && !settings.open) { orbit.reset(); }
});
inspectInput.onchange = () => {
  if (inspectInput.checked) connectPose();
  else { closePose(); cameraState = "Receive-only mirror · headset camera"; }
};
document.querySelector("#mac-origin-form")!.addEventListener("submit", event => {
  event.preventDefault();
  try {
    const selected = streamOrigin(input.value); closePose(); origin = selected;
    session = crypto.randomUUID(); sequence = 0; lastPoseAt = -Infinity;
    lastPaintAt = -Infinity; dirty = true; startVideo(); connectPose(); settings.close();
  } catch (error) { status.textContent = String(error); }
});
function dispose() {
  if (disposed) return; disposed = true;
  cancelAnimationFrame(frameLoop); closePose(); viewer.stop(); orbit.dispose(); resize.disconnect();
}
window.addEventListener("pagehide", dispose, { once: true });
window.addEventListener("beforeunload", dispose, { once: true });
startVideo(); connectPose(); frameLoop = requestAnimationFrame(tick);
