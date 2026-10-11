import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { MapWalk, mapWalk } from "./map-walk.js";
import { ExploreNavigation } from "./explore-navigation.js";
import { ExploreStream } from "./explore-stream";
import { streamOrigin } from "./mac-view";
import "./explore.css";

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `<main class="explore" aria-label="Explore your reconstructed scene">
  <div id="scene"></div>
  <header class="masthead"><a class="wordmark" href="/" aria-label="Farsight operator">FARSIGHT<span>EXPLORE</span></a>
    <div class="connection" role="status"><i></i><span id="connection">Connecting to Mac</span></div></header>
  <div class="waiting" id="waiting"><span class="eyebrow">YOUR SPACE, RECONSTRUCTED</span>
    <h1>Step inside.</h1><p id="waiting-message">Waiting for your scene on the Mac.</p></div>
  <footer class="toolbar"><div class="scene-caption"><span id="source">3D exploration</span>
    <span class="desktop-hint">Drag to look · Scroll to zoom</span></div>
    <nav aria-label="Exploration actions"><button id="home" class="quiet" title="Return to the starting view (Home)">Home</button>
      <button id="help" class="help" aria-label="Navigation help">?</button>
      <button id="enter-vr" class="enter-vr" disabled>Checking VR…<span aria-hidden="true">↗</span></button></nav></footer>
  <dialog id="help-dialog"><form method="dialog"><button class="close" aria-label="Close help">×</button></form>
    <span class="eyebrow">MAKE YOURSELF AT HOME</span><h2>Move through the scene.</h2>
    <div class="help-columns"><section><h3>In your headset</h3><dl>
      <div><dt>Left stick</dt><dd>Walk & strafe</dd></div><div><dt>Right stick ← →</dt><dd>Turn 30°</dd></div>
      <div><dt>Right stick ↑ ↓</dt><dd>Rise & descend</dd></div><div><dt>A / B</dt><dd>Help / return home</dd></div>
    </dl></section><section><h3>On your Mac</h3><dl><div><dt>Drag</dt><dd>Orbit the scene</dd></div>
      <div><dt>Right drag</dt><dd>Pan</dd></div><div><dt>Scroll</dt><dd>Zoom</dd></div>
      <div><dt>W A S D · Q E</dt><dd>Move · rise & descend</dd></div><div><dt>Home</dt><dd>Return to start</dd></div>
    </dl></section></div><p class="help-note">Your Mac renders the scene. Your headset streams the view.</p>
  </dialog>
</main>`;
const element = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const query = new URLSearchParams(location.search);
const origin = streamOrigin(query.get("mac") || location.origin);
const renderer = new THREE.WebGLRenderer({ antialias: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setClearColor(0x1c2228); renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.xr.enabled = true; renderer.xr.setReferenceSpaceType("local-floor");
element("scene").append(renderer.domElement);
renderer.domElement.tabIndex = 0;
renderer.domElement.setAttribute("aria-label", "Mac-rendered 3D scene");
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(60, 1, .01, 2000);
camera.position.set(4, 3, 6);
const orbit = new OrbitControls(camera, renderer.domElement);
orbit.target.set(0, .5, 0); orbit.enableDamping = true; orbit.dampingFactor = .09;
orbit.rotateSpeed = .65; orbit.panSpeed = .8;
orbit.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, RIGHT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY };
orbit.update();
// Orientation aid while connecting. It is never passed to the map or counted as geometry.
const grid = new THREE.GridHelper(80, 80, 0x3a424a, 0x293037);
grid.material.transparent = true; grid.material.opacity = .35;
scene.add(grid);
const stream = new ExploreStream(renderer, scene, camera, origin, query.get("stereo") !== "0");
const navigation = new ExploreNavigation(mapWalk), deskWalk = new MapWalk(), deskNavigation = new ExploreNavigation(deskWalk);
const keys = new Set<string>(), heading = new THREE.Vector3(), head = new THREE.Vector3(), rotation = new THREE.Quaternion();
const help = element<HTMLDialogElement>("help-dialog"), vrButton = element<HTMLButtonElement>("enter-vr");
let lastAt = -1, labelAt = -Infinity, framed = false, identity = "", xrHelp = false, disposed = false;
let reference: XRReferenceSpace | null = null;
let anchor: { floor_y: number; center: number[]; span: number } | null = null;
const savedPosition = new THREE.Vector3(), savedTarget = new THREE.Vector3();
const helpCanvas = document.createElement("canvas"); helpCanvas.width = 1024; helpCanvas.height = 448;
const context = helpCanvas.getContext("2d")!;
let helpText = "";
const helpTexture = new THREE.CanvasTexture(helpCanvas); helpTexture.colorSpace = THREE.SRGBColorSpace;
const helpPanel = new THREE.Mesh(new THREE.PlaneGeometry(.84, .368), new THREE.MeshBasicMaterial({
  map: helpTexture, transparent: true, depthTest: false, toneMapped: false }));
helpPanel.renderOrder = 20; helpPanel.visible = false; scene.add(helpPanel);
function paintHelp(message?: string) {
  const text = message || "Move through the scene.";
  if (text === helpText) return;
  helpText = text;
  context.clearRect(0, 0, 1024, 448); context.fillStyle = "rgba(20,25,30,.94)";
  context.beginPath(); context.roundRect(0, 0, 1024, 448, 28); context.fill();
  context.fillStyle = "#c4a574"; context.font = "500 24px system-ui"; context.fillText("FARSIGHT / EXPLORE", 52, 64);
  context.fillStyle = "#eeeae2"; context.font = "400 38px system-ui";
  context.fillText(text, 52, 125);
  context.font = "400 28px system-ui";
  ["Left stick   Walk & strafe", "Right stick   Turn / rise & descend", "A   Help               B   Return home"].forEach((line, i) => context.fillText(line, 52, 207 + i * 62));
  helpTexture.needsUpdate = true;
}
paintHelp();
function home() {
  navigation.reset(); deskNavigation.reset(); keys.clear();
  if (renderer.xr.isPresenting) { mapWalk.reset(); mapWalk.x = -head.x; mapWalk.z = -head.z; }
  else {
    const center = anchor?.center ?? [0, .5, 0], span = anchor?.span ?? 5;
    orbit.target.fromArray(center); camera.position.fromArray(center).add(new THREE.Vector3(span * .7, span * .5, span * .9));
    orbit.update();
  }
}
element("home").onclick = home;
element("help").onclick = () => help.showModal();
const resetReference = () => { stream.resetSession(true); navigation.reset(); };
renderer.xr.addEventListener("sessionstart", () => {
  savedPosition.copy(camera.position); savedTarget.copy(orbit.target);
  document.body.classList.add("immersive"); help.close(); keys.clear(); orbit.enabled = false;
  camera.position.set(0, 0, 0); camera.quaternion.identity();
  mapWalk.setMode("walk"); mapWalk.reset(); navigation.reset(); stream.resetSession(true);
  reference = renderer.xr.getReferenceSpace(); reference?.addEventListener("reset", resetReference);
});
renderer.xr.addEventListener("sessionend", () => {
  reference?.removeEventListener("reset", resetReference); reference = null;
  document.body.classList.remove("immersive"); camera.position.copy(savedPosition); orbit.target.copy(savedTarget);
  orbit.enabled = true; orbit.update(); mapWalk.setMode("overview"); navigation.reset(); xrHelp = false;
  stream.resetSession(false); vrButton.disabled = false; vrButton.innerHTML = 'Enter VR<span aria-hidden="true">↗</span>';
});
vrButton.onclick = async () => {
  vrButton.disabled = true;
  try {
    const session = await navigator.xr!.requestSession("immersive-vr", { optionalFeatures: ["local-floor", "bounded-floor"] });
    await renderer.xr.setSession(session);
  } catch (error) {
    element("connection").textContent = error instanceof Error ? error.message : String(error);
    vrButton.disabled = false;
  }
};
void (async () => {
  const supported = await navigator.xr?.isSessionSupported("immersive-vr").catch(() => false);
  vrButton.disabled = !supported;
  vrButton.innerHTML = supported ? 'Enter VR<span aria-hidden="true">↗</span>' : 'Open in headset<span aria-hidden="true">↗</span>';
  if (!supported) vrButton.title = "Open this page in the Quest browser to enter VR";
})();
const resize = new ResizeObserver(() => {
  const { width, height } = element("scene").getBoundingClientRect();
  renderer.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix();
});
resize.observe(element("scene"));
const keydown = (event: KeyboardEvent) => {
  if (help.open) return;
  if (["KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE"].includes(event.code)) { keys.add(event.code); event.preventDefault(); }
  if (event.code === "Home") home();
  if (event.key === "?" || event.key === "Escape") help.showModal();
};
const keyup = (event: KeyboardEvent) => { keys.delete(event.code); };
const blur = () => { keys.clear(); deskNavigation.reset(); };
window.addEventListener("keydown", keydown); window.addEventListener("keyup", keyup); window.addEventListener("blur", blur);
renderer.domElement.addEventListener("contextmenu", event => event.preventDefault());
renderer.domElement.addEventListener("pointerdown", () => renderer.domElement.focus({ preventScroll: true }));
renderer.setAnimationLoop((now, frame) => {
  if (disposed) return;
  const dt = lastAt < 0 ? 0 : Math.min((now - lastAt) / 1000, .05); lastAt = now;
  const immersive = renderer.xr.isPresenting;
  const viewerPose = immersive && frame && reference ? frame.getViewerPose(reference) : null;
  if (viewerPose) {
    const { position: p, orientation: q } = viewerPose.transform;
    head.set(p.x, p.y, p.z); rotation.set(q.x, q.y, q.z, q.w);
    heading.set(0, 0, -1).applyQuaternion(rotation);
    mapWalk.observeHead(p.x, p.z, Math.atan2(-heading.x, -heading.z));
    const actions = navigation.update(dt, Array.from(renderer.xr.getSession()?.inputSources ?? []));
    if (actions.home) home(); if (actions.help) { xrHelp = !xrHelp; paintHelp(); }
  } else if (!immersive) {
    orbit.enabled = !help.open; orbit.update();
    camera.getWorldDirection(heading); deskWalk.observeHead(0, 0, Math.atan2(-heading.x, -heading.z));
    const before = [deskWalk.x, deskWalk.y, deskWalk.z];
    deskNavigation.update(dt, [], help.open ? new Set() : keys);
    const delta = new THREE.Vector3(deskWalk.x - before[0], deskWalk.y - before[1], deskWalk.z - before[2]);
    camera.position.add(delta); orbit.target.add(delta);
  }
  const shown = stream.update(now, viewerPose); grid.visible = !shown;
  const map = stream.map;
  if (map?.scene) {
    anchor = map.scene;
    const nextIdentity = JSON.stringify([map.source, map.session_id, map.map_epoch, map.generation]);
    if (identity !== nextIdentity) {
      identity = nextIdentity; mapWalk.setFloor(query.has("floor") ? Number(query.get("floor")) : anchor.floor_y);
      if (!framed && !immersive) { framed = true; home(); }
    }
  }
  const showMessage = immersive && !shown;
  helpPanel.visible = immersive && (xrHelp || showMessage);
  if (helpPanel.visible) {
    helpPanel.position.set(0, -.38, -1.4).applyQuaternion(rotation).add(head); helpPanel.quaternion.copy(rotation);
  }
  if (now - labelAt > 250) {
    labelAt = now;
    element("waiting").hidden = shown;
    document.querySelector(".connection")!.classList.toggle("connected", shown);
    element("connection").textContent = shown ? "Mac connected" : stream.cameraState === "unavailable" ? "Mac unavailable" : "Connecting to Mac";
    const source = map?.geometry_source || map?.source || "";
    element("source").textContent = /simulat|demo|replay/i.test(source) ? source : map?.retained ? "Retained scan" : shown ? "Live reconstruction" : "3D exploration";
    if (showMessage) paintHelp("Waiting for the Mac view…"); else if (xrHelp) paintHelp();
    renderer.domElement.dataset.state = shown ? "rendering" : "waiting";
    renderer.domElement.dataset.source = source;
  }
  renderer.render(scene, camera);
});
function dispose() {
  if (disposed) return; disposed = true; renderer.setAnimationLoop(null); stream.dispose(); orbit.dispose(); resize.disconnect();
  reference?.removeEventListener("reset", resetReference);
  window.removeEventListener("keydown", keydown); window.removeEventListener("keyup", keyup); window.removeEventListener("blur", blur);
  grid.geometry.dispose(); grid.material.dispose(); helpPanel.geometry.dispose(); helpPanel.material.dispose(); helpTexture.dispose(); renderer.dispose();
}
window.addEventListener("pagehide", dispose, { once: true }); stream.start();
