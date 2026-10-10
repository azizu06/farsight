import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { VRButton } from "three/addons/webxr/VRButton.js";
import { decodeMesh, decodePoints } from "./protocol.js";
import "./style.css";
import { RoverExperience } from "./rover";

type Status = {
  type: string;
  source: string;
  device: string;
  connected: boolean;
  fresh: boolean;
  session_id: string | null;
  map_epoch: number | null;
  generation: number;
  revision: number;
  mesh_revision: number;
  tracking: string;
  paused: boolean;
  points: number;
  triangles: number;
  frames: number;
  rejected: number;
  dropped: number;
  processing_ms: number;
  age_ms: number | null;
  position: number[];
  transform: number[];
  reason: string | null;
  depth_range: number[] | null;
};
const $ = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <main class="viewport" aria-label="Farsight 3D viewport">
    <div id="scene"></div>
    <div id="full-camera" hidden><img alt="Latest rover camera frame" /></div>
    <div class="view-label" id="view-label">User Perspective</div>
    <div class="cloud-status" id="cloud-status" role="status" aria-live="off">Connecting to mapper…</div>
    <button class="settings-toggle" id="settings-toggle" title="Settings (Escape)" aria-label="Open viewer settings">Settings</button>
    <p class="navigation-hint" id="navigation-help">
      <span><b>Right drag</b> Orbit</span><span><b>Left drag</b> Pan</span><span><b>Scroll</b> Zoom</span>
      <span><b>F</b> Frame scan</span><span><b>P</b> Points / hybrid</span><span><b>Home</b> Reset view</span><span><b>Esc</b> Settings</span>
    </p>
    <p class="sr-only" id="keyboard-help">Arrow keys pan. Shift and arrow keys rotate. Plus and minus zoom.
      On a touchscreen use one finger to orbit, or two fingers to pan and pinch to zoom.</p>
  </main>
  <dialog id="settings" class="settings">
    <header><h1>Farsight</h1><button id="close-settings" aria-label="Close viewer settings">×</button></header>
    <p id="connection">Waiting for sensor</p>
    <div class="view-options" role="tablist" aria-label="View mode">
      <button data-view="3d" class="selected" role="tab" aria-selected="true">3D</button>
      <button data-view="map" role="tab" aria-selected="false">2D</button>
      <button data-view="camera" role="tab" aria-selected="false">Camera</button>
    </div>
    <div class="button-row"><button id="frame">Frame scan</button><button id="home">Reset view</button></div>
    <label class="check"><input id="points-only" type="checkbox"/> Points only</label>
    <label class="check"><input id="follow" type="checkbox"/> Follow camera position</label>
    <div class="button-row"><button id="pause">Pause capture</button><button id="export">Export PLY</button></div>
    <div class="sensor-head"><h2>RGB + depth</h2><span id="sensor-age">No frames</span></div>
    <div class="sensor-images"><img id="rgb" alt="Latest synchronized RGB frame" hidden/><img id="depth" alt="LiDAR depth heatmap" hidden/></div>
    <p class="depth-legend" id="depth-range">Depth: waiting for samples</p>
    <dl>
      <div><dt>Sensor</dt><dd id="device">—</dd></div>
      <div><dt>Tracking</dt><dd id="tracking">—</dd></div>
      <div><dt>Fusion time</dt><dd id="processing">—</dd></div>
      <div><dt>Fused frames</dt><dd id="frames">0</dd></div>
      <div><dt>Skipped / rejected</dt><dd id="dropped">0 / 0</dd></div>
      <div><dt>Session / epoch</dt><dd id="session-id">—</dd></div>
    </dl>
    <div id="xr-slot"></div>
    <button id="reset" class="reset">Clear scan…</button>
    <p class="panel-note" id="panel-note">Observed RGB-D geometry. Unobserved space stays empty.</p>
  </dialog>
  <dialog id="reset-dialog" class="confirm"><h2>Clear the scan?</h2><p>Export first to keep the current point cloud.</p>
    <div class="button-row"><button id="cancel-reset">Cancel</button><button id="confirm-reset">Clear scan</button></div>
  </dialog>
`;

const text = (id: string, value: string) => {
  $(id).textContent = value;
};
const settings = $<HTMLDialogElement>("settings");
let status: Status | undefined;
let view = "3d",
  pointsOnly = false,
  follow = false;
let generation = -1,
  appliedMesh = -1,
  pendingMesh = false;
let connectionVersion = 0;
let socket: WebSocket | null = null,
  reconnectTimer = 0,
  serverOnline = false;
const api = (path: string) => `${location.origin}${path}`;

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setClearColor(0x393939);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.xr.enabled = true;
renderer.xr.setReferenceSpaceType("local-floor");
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 2000);
camera.position.set(9, 6, 9);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.12;
controls.rotateSpeed = 0.7;
controls.panSpeed = 0.8;
controls.zoomSpeed = 0.9;
controls.screenSpacePanning = true;
controls.minDistance = 0.1;
controls.maxDistance = 200;
controls.mouseButtons = {
  LEFT: THREE.MOUSE.PAN,
  RIGHT: THREE.MOUSE.ROTATE,
  MIDDLE: THREE.MOUSE.PAN,
};
controls.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };
controls.saveState();
renderer.domElement.tabIndex = 0;
renderer.domElement.setAttribute("role", "application");
renderer.domElement.setAttribute("aria-label", "Interactive 3D viewport");
renderer.domElement.setAttribute(
  "aria-describedby",
  "navigation-help keyboard-help",
);
renderer.domElement.addEventListener("contextmenu", (event) =>
  event.preventDefault(),
);
renderer.domElement.addEventListener("pointerdown", () =>
  renderer.domElement.focus({ preventScroll: true }),
);
$("scene").append(renderer.domElement);

const root = new THREE.Group();
scene.add(root);
const gridMaterial = new THREE.ShaderMaterial({
  transparent: true,
  depthWrite: false,
  side: THREE.DoubleSide,
  uniforms: {
    minor: { value: new THREE.Color("#4d4d4d") },
    major: { value: new THREE.Color("#616161") },
  },
  vertexShader: `varying vec3 world;
    void main() { vec4 p = modelMatrix * vec4(position, 1.0); world = p.xyz; gl_Position = projectionMatrix * viewMatrix * p; }`,
  fragmentShader: `varying vec3 world; uniform vec3 minor; uniform vec3 major;
    float line(vec2 p, float width) {
      vec2 d = max(fwidth(p), vec2(0.00001));
      vec2 a = abs(fract(p - 0.5) - 0.5) / d;
      return 1.0 - min(min(a.x, a.y) / width, 1.0);
    }
    void main() {
      float fine = line(world.xz, 0.65); float coarse = line(world.xz / 10.0, 0.9);
      float fade = pow(1.0 - smoothstep(15.0, 160.0, distance(cameraPosition, world)), 1.5);
      float alpha = max(fine * 0.65, coarse * 0.8) * fade;
      if (alpha < 0.01) discard;
      gl_FragColor = vec4(mix(minor, major, coarse), alpha);
      #include <colorspace_fragment>
    }`,
});
const grid = new THREE.Mesh(new THREE.PlaneGeometry(2000, 2000), gridMaterial);
grid.rotation.x = -Math.PI / 2;
grid.position.y = -0.002;
root.add(grid);
const axesGeometry = new THREE.BufferGeometry();
axesGeometry.setAttribute(
  "position",
  new THREE.Float32BufferAttribute(
    [-1000, 0, 0, 1000, 0, 0, 0, 0, -1000, 0, 0, 1000],
    3,
  ),
);
const red = new THREE.Color("#985c5c"),
  green = new THREE.Color("#65865c");
axesGeometry.setAttribute(
  "color",
  new THREE.Float32BufferAttribute(
    [
      ...red.toArray(),
      ...red.toArray(),
      ...green.toArray(),
      ...green.toArray(),
    ],
    3,
  ),
);
root.add(
  new THREE.LineSegments(
    axesGeometry,
    new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.8,
    }),
  ),
);
const pointsMaterial = new THREE.PointsMaterial({
  size: 0.025,
  vertexColors: true,
  toneMapped: false,
});
pointsMaterial.onBeforeCompile = (shader) => {
  shader.fragmentShader = shader.fragmentShader.replace(
    "#include <clipping_planes_fragment>",
    "#include <clipping_planes_fragment>\nif (distance(gl_PointCoord, vec2(0.5)) > 0.5) discard;",
  );
};
const cloud = new THREE.Points(new THREE.BufferGeometry(), pointsMaterial);
root.add(cloud);
const surfaces = new THREE.Mesh(
  new THREE.BufferGeometry(),
  new THREE.MeshBasicMaterial({
    vertexColors: true,
    side: THREE.DoubleSide,
    toneMapped: false,
    polygonOffset: true,
    polygonOffsetFactor: -1,
    polygonOffsetUnits: -1,
  }),
);
root.add(surfaces);
const marker = new THREE.Group();
const markerMesh = new THREE.Mesh(
  new THREE.ConeGeometry(0.055, 0.15, 4),
  new THREE.MeshBasicMaterial({ color: "#acbdda" }),
);
markerMesh.rotation.x = -Math.PI / 2;
marker.add(markerMesh);
root.add(marker);
marker.visible = false;

const xrButton = VRButton.createButton(renderer);
xrButton.removeAttribute("style");
$("xr-slot").append(xrButton);
const desktopPosition = new THREE.Vector3(),
  desktopQuaternion = new THREE.Quaternion();
renderer.xr.addEventListener("sessionstart", () => {
  desktopPosition.copy(camera.position);
  desktopQuaternion.copy(camera.quaternion);
  controls.enabled = false;
  settings.close();
  camera.position.set(0, 0, 0);
  camera.quaternion.identity();
});
renderer.xr.addEventListener("sessionend", () => {
  camera.position.copy(desktopPosition);
  camera.quaternion.copy(desktopQuaternion);
  controls.enabled = true;
});
new ResizeObserver(() => {
  const { width, height } = $("scene").getBoundingClientRect();
  renderer.setSize(width, height);
  camera.aspect = width / Math.max(height, 1);
  camera.updateProjectionMatrix();
}).observe($("scene"));
let rover: RoverExperience | undefined;
renderer.setAnimationLoop(() => {
  rover?.update(performance.now());
  root.visible = !rover?.vrCameraView;
  if (!renderer.xr.isPresenting) controls.update();
  renderer.render(scene, camera);
});

rover = new RoverExperience(renderer, scene, camera, () => view, setView);
setView(new URLSearchParams(location.search).get("view") === "map" ? "3d" : "camera");

function replaceGeometry(
  object: THREE.Points | THREE.Mesh,
  data: ReturnType<typeof decodePoints>,
  indices?: Uint32Array,
) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    "position",
    new THREE.BufferAttribute(data.positions, 3),
  );
  geometry.setAttribute("color", new THREE.BufferAttribute(data.colors, 3));
  if (indices) geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.computeBoundingSphere();
  object.geometry.dispose();
  object.geometry = geometry;
}
function emptyGeometry() {
  return {
    positions: new Float32Array(),
    colors: new Float32Array(),
    count: 0,
  };
}
function visibility() {
  cloud.visible = view !== "camera";
  surfaces.visible = !pointsOnly && view === "3d";
}
function frameMap() {
  const positions = cloud.geometry.getAttribute("position");
  if (!positions?.count) return;
  const box = new THREE.Box3().setFromBufferAttribute(
    positions as THREE.BufferAttribute,
  );
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const halfFov = THREE.MathUtils.degToRad(camera.fov / 2);
  const fitAngle = Math.min(
    halfFov,
    Math.atan(Math.tan(halfFov) * camera.aspect),
  );
  const distance = Math.max(0.25, (sphere.radius / Math.sin(fitAngle)) * 1.15);
  const direction =
    view === "map"
      ? new THREE.Vector3(0, 1, 0.0001)
      : camera.position.clone().sub(controls.target).normalize();
  controls.enableDamping = false;
  controls.update();
  controls.target.copy(sphere.center);
  camera.position.copy(sphere.center).addScaledVector(direction, distance);
  controls.update();
  controls.enableDamping = true;
}
function resetView() {
  setView("3d");
  controls.enableDamping = false;
  controls.update();
  controls.reset();
  controls.enableDamping = true;
}
function setView(next: string) {
  if (view !== next) rover?.stop("View changed: release deadman");
  view = next;
  document.querySelectorAll<HTMLElement>("[data-view]").forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  $("full-camera").hidden = view !== "camera";
  $("scene").style.visibility = view === "camera" ? "hidden" : "visible";
  $("navigation-help").hidden = view === "camera";
  ($("navigation-help").firstElementChild as HTMLElement).hidden =
    view === "map";
  camera.up.set(0, view === "map" ? 0 : 1, view === "map" ? -1 : 0);
  controls.enableRotate = view !== "map";
  if (view === "3d" && !renderer.xr.isPresenting)
    camera.position.copy(controls.target).add(new THREE.Vector3(9, 6, 9));
  text(
    "view-label",
    view === "3d"
      ? "User Perspective"
      : view === "map"
        ? "Top Perspective"
        : "Camera Perspective",
  );
  if (!renderer.xr.isPresenting) frameMap();
  visibility();
}
function notify(message: string) {
  text("cloud-status", message);
  $("cloud-status").dataset.live = "false";
  text("panel-note", message);
}
function statusLine(next: Status) {
  const state =
    (!serverOnline ? "Mapper disconnected" : next.reason) ||
    (next.fresh && next.tracking === "normal"
      ? "Live RGB + depth"
      : "Waiting for capture updates");
  const geometry = `${next.points.toLocaleString()} points${!pointsOnly && next.triangles ? ` · ${next.triangles.toLocaleString()} surface triangles` : ""}`;
  text(
    "cloud-status",
    `${next.source === "SIMULATED" ? "SIMULATED · " : ""}${state} · ${geometry}${next.rejected ? ` · ${next.rejected} invalid packets skipped` : ""}`,
  );
  $("cloud-status").dataset.live = String(
    serverOnline && next.fresh && next.tracking === "normal" && !next.paused,
  );
}
async function loadMesh(next: Status) {
  if (!mapNeeded() || pendingMesh || appliedMesh === next.mesh_revision) return;
  pendingMesh = true;
  const wantedGeneration = next.generation,
    wantedRevision = next.mesh_revision,
    wantedConnection = connectionVersion;
  try {
    const response = await fetch(api("/map/mesh.bin"), { cache: "no-store" });
    if (!response.ok)
      throw new Error(`Surface request failed (${response.status})`);
    const data = decodeMesh(await response.arrayBuffer());
    if (
      generation === wantedGeneration &&
      connectionVersion === wantedConnection
    ) {
      replaceGeometry(surfaces, data, data.indices);
      appliedMesh = wantedRevision;
    }
  } catch (error) {
    notify(String(error));
  } finally {
    pendingMesh = false;
  }
}
function acceptStatus(next: Status) {
  if (next.generation < generation) return;
  if (next.generation !== generation) {
    generation = next.generation;
    appliedMesh = -1;
    replaceGeometry(cloud, emptyGeometry());
    replaceGeometry(surfaces, emptyGeometry());
  }
  status = next;
  statusLine(next);
  text(
    "connection",
    `${next.source === "SIMULATED" ? "Simulated sensor" : next.device} · ${next.fresh ? "connected" : next.connected ? "stale" : "disconnected"}`,
  );
  for (const [id, value] of Object.entries({
    device: next.device,
    tracking: next.tracking,
    processing: `${next.processing_ms} ms`,
    frames: String(next.frames),
    dropped: `${next.dropped} / ${next.rejected}`,
    "sensor-age": next.age_ms === null ? "No frames" : `${next.age_ms} ms ago`,
    "session-id": next.session_id
      ? `${next.session_id.slice(0, 16)} / ${next.map_epoch}`
      : "—",
  }))
    text(id, value);
  text(
    "depth-range",
    next.depth_range
      ? `Depth ${next.depth_range[0].toFixed(1)}–${next.depth_range[1].toFixed(1)} m · confidence ≥ 1`
      : "Depth: waiting for samples",
  );
  text("pause", next.paused ? "Resume capture" : "Pause capture");
  marker.visible = next.fresh;
  marker.matrixAutoUpdate = false;
  marker.matrix.fromArray(next.transform);
  if (follow && view !== "camera" && !renderer.xr.isPresenting) {
    const position = new THREE.Vector3(
      ...(next.position as [number, number, number]),
    );
    camera.position.add(position.clone().sub(controls.target));
    controls.target.copy(position);
  }
  void loadMesh(next);
  visibility();
}
function mapNeeded() {
  return view !== "camera" || settings.open || (renderer.xr.isPresenting && !rover?.vrCameraView);
}
function syncMapConnection() {
  if (mapNeeded()) {
    if (!socket || socket.readyState === WebSocket.CLOSED) connect();
  } else {
    if (socket) { socket.onclose = null; socket.close(); socket = null; }
    serverOnline = false;
    text("cloud-status", "Map retained · transfers paused during live camera view");
    $("cloud-status").dataset.live = "false";
  }
}
function connect() {
  if (!mapNeeded() || (socket && socket.readyState !== WebSocket.CLOSED)) return;
  const nextSocket = new WebSocket(api("/ws/map").replace(/^http/, "ws"));
  socket = nextSocket;
  nextSocket.binaryType = "arraybuffer";
  nextSocket.onopen = () => {
    if (socket !== nextSocket) return;
    serverOnline = true;
    connectionVersion += 1;
    generation = -1;
    appliedMesh = -1;
  };
  nextSocket.onmessage = (event) => {
    if (socket !== nextSocket) return;
    try {
      if (typeof event.data === "string") {
        const next = JSON.parse(event.data) as Status;
        if (next.type === "status") acceptStatus(next);
      } else {
        const first = !cloud.geometry.getAttribute("position")?.count;
        replaceGeometry(cloud, decodePoints(event.data));
        if (first && cloud.geometry.getAttribute("position").count && !renderer.xr.isPresenting) frameMap();
      }
    } catch (error) {
      notify(`Map packet rejected: ${String(error)}`);
    }
  };
  nextSocket.onerror = () => nextSocket.close();
  nextSocket.onclose = () => {
    if (socket !== nextSocket) return;
    serverOnline = false;
    marker.visible = false;
    text("connection", "Mapper disconnected");
    notify("Mapper disconnected · Retrying in 2 seconds");
    reconnectTimer = window.setTimeout(connect, 2000);
  };
}
syncMapConnection();
const mapConnectionTimer = window.setInterval(syncMapConnection, 250);

async function command(path: string, body?: object) {
  try {
    const response = await fetch(api(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    acceptStatus(await response.json());
    return true;
  } catch (error) {
    notify(String(error));
    return false;
  }
}
$("settings-toggle").onclick = () => settings.showModal();
$("close-settings").onclick = () => settings.close();
$("frame").onclick = frameMap;
$("home").onclick = resetView;
$("pause").onclick = () =>
  void command("/map/pause", { paused: !status?.paused });
$("export").onclick = () => {
  const link = document.createElement("a");
  link.href = api("/map/export.ply");
  link.download = "farsight-scan.ply";
  link.click();
};
$("points-only").onchange = () => {
  pointsOnly = $<HTMLInputElement>("points-only").checked;
  visibility();
  if (status) statusLine(status);
};
$("follow").onchange = () => {
  follow = $<HTMLInputElement>("follow").checked;
};
const confirmation = $<HTMLDialogElement>("reset-dialog");
$("reset").onclick = () => confirmation.showModal();
$("cancel-reset").onclick = () => confirmation.close();
$("confirm-reset").onclick = async () => {
  if (await command("/map/reset")) confirmation.close();
};
document.querySelectorAll<HTMLButtonElement>("[data-view]").forEach(
  (button) =>
    (button.onclick = () => {
      setView(button.dataset.view!);
      settings.close();
    }),
);

window.addEventListener("keydown", (event) => {
  if (
    event.ctrlKey ||
    event.metaKey ||
    event.altKey ||
    /INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement).tagName)
  )
    return;
  if (event.key === "Escape" && !settings.open && !confirmation.open) {
    event.preventDefault();
    settings.showModal();
    return;
  }
  if (settings.open || confirmation.open) return;
  const key = event.key.toLowerCase();
  if (key === "f") frameMap();
  else if (key === "p") {
    pointsOnly = !pointsOnly;
    $<HTMLInputElement>("points-only").checked = pointsOnly;
    visibility();
    if (status) statusLine(status);
  } else if (key === "home") resetView();
  else if (event.key.startsWith("Arrow") && view !== "camera") {
    controls.enableDamping = false;
    controls.update();
    if (event.shiftKey && view !== "map") {
      const offset = camera.position.clone().sub(controls.target);
      const spherical = new THREE.Spherical().setFromVector3(offset);
      if (event.key === "ArrowLeft") spherical.theta += Math.PI / 36;
      if (event.key === "ArrowRight") spherical.theta -= Math.PI / 36;
      if (event.key === "ArrowUp") spherical.phi -= Math.PI / 36;
      if (event.key === "ArrowDown") spherical.phi += Math.PI / 36;
      spherical.makeSafe();
      camera.position
        .copy(controls.target)
        .add(new THREE.Vector3().setFromSpherical(spherical));
    } else {
      const horizontal = /Left|Right/.test(event.key),
        direction = /Left|Down/.test(event.key) ? -1 : 1;
      const shift = new THREE.Vector3()
        .setFromMatrixColumn(camera.matrix, horizontal ? 0 : 1)
        .multiplyScalar(controls.getDistance() * 0.025 * direction);
      camera.position.add(shift);
      controls.target.add(shift);
    }
    controls.update();
    controls.enableDamping = true;
  } else if (["+", "=", "-", "_"].includes(key)) {
    const factor = key === "+" || key === "=" ? 0.85 : 1 / 0.85;
    camera.position
      .sub(controls.target)
      .multiplyScalar(factor)
      .add(controls.target);
  } else return;
  event.preventDefault();
});

let feedBusy = false;
async function updateFeeds() {
  if (
    feedBusy ||
    !status?.fresh ||
    document.hidden ||
    !settings.open
  )
    return;
  feedBusy = true;
  await Promise.all(
    ["rgb", "depth"].map(async (id) => {
      const image = $<HTMLImageElement>(id);
      try {
        const response = await fetch(
          api(id === "rgb" ? "/camera.jpg" : "/depth.jpg"),
          { cache: "no-store" },
        );
        if (!response.ok) return;
        const url = URL.createObjectURL(await response.blob());
        const old = image.src;
        image.src = url;
        image.hidden = false;
        if (old.startsWith("blob:")) URL.revokeObjectURL(old);
      } catch {
        /* The status line reports freshness; keep the last measured image. */
      }
    }),
  );
  feedBusy = false;
}
window.setInterval(() => void updateFeeds(), 250);
window.addEventListener("beforeunload", () => {
  window.clearTimeout(reconnectTimer);
  window.clearInterval(mapConnectionTimer);
  if (socket) {
    socket.onclose = null;
    socket.close();
  }
  rover?.dispose();
  renderer.dispose();
});
