import * as THREE from "three";
import { SourceTruth } from "./source-banner.js";
import { ControlSafety, stick } from "./control-safety.js";
import { H264Viewer } from "./h264";
import { cameraCrop, croppedBox } from "./camera-view.js";
import { drawDownMarker, linkHealth, poseSegments, strokeSegments } from "./pose-hud.js";
import { AssistantVoice } from "./assistant-voice";
import { AutonomyIntent, autonomyLabel, containPoint, pickTarget, rayPanelPoint } from "./autonomy.js";
import { PHRASE_LABELS, sbarLines, speakPhrase } from "./survivor-phrases.js";
import { DRILL_TRAINS, DrillHud, endLines } from "./drill-hud.js";

type Box = { id?: number; cls: string; conf: number; x: number; y: number; w: number; h: number; kpts?: number[][]; down?: boolean };
type Detections = { frame: number | string; boxes: Box[]; fps?: number; source?: string; simulated?: boolean };
type ControlStatus = {
  type?: string; session_id: string; permit: string; permit_ms: number; seq?: number;
  rover: boolean; simulated?: boolean; estop?: boolean; range_cm?: number | null;
  requires_release?: boolean; reason?: string | null; link_ms?: number | null;
  mode?: string; autonomy_state?: string; target_id?: number | null;
};

/** Same-origin only: Quest talks through USB/ADB to the operator relay. */
export class RoverExperience {
  private sourceTruth = new SourceTruth();
  private sourceBanner: HTMLElement;
  private bannerCanvas = document.createElement("canvas");
  private bannerTexture = new THREE.CanvasTexture(this.bannerCanvas);
  private bannerPanel = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 0.35), new THREE.MeshBasicMaterial({ map: this.bannerTexture, transparent: true, toneMapped: false, depthTest: false }));
  private voiceCanvas = document.createElement("canvas");
  private voiceTexture = new THREE.CanvasTexture(this.voiceCanvas);
  private voicePanel = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 0.8), new THREE.MeshBasicMaterial({ map: this.voiceTexture, transparent: true, toneMapped: false, depthTest: false }));
  private talkDown = false;
  private gate = new ControlSafety();
  private socket: WebSocket | null = null;
  private detectionsSocket: WebSocket | null = null;
  private timers: number[] = [];
  private stopped = false;
  private frameBusy = false;
  private request: AbortController | null = null;
  private frameId = "";
  private pendingFrame: { id: string | null; age: number; at: number } | null = null;
  private lastCanvasDraw = -Infinity;
  private inputValid = false;
  private contextLost = false;
  private frameSource = "Waiting for camera";
  private frame: HTMLImageElement | null = null;
  private detections: Detections | null = null;
  private detectionsAt = -Infinity;
  private detectionFrames = new Map<string, { packet: Detections; at: number }>();
  private status: ControlStatus | null = null;
  private seq = 0;
  private sent = new Map<number, number>();
  private ackMs: number | null = null;
  private drillHud = new DrillHud();
  private drillTexture = new THREE.CanvasTexture(this.drillHud.canvas);
  private drillPanel = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 1.2), new THREE.MeshBasicMaterial({ map: this.drillTexture, transparent: true, toneMapped: false, depthTest: false }));
  private drillDown = false;
  private voice: AssistantVoice | null = null;
  private autonomy = new AutonomyIntent();
  private drawnBoxes: Box[] = [];
  private raycaster = new THREE.Raycaster();
  private exploreDown = false;
  private lastSendAt = 0;
  private input = { held: false, v: 0, w: 0, pan: 0 };
  private keys = new Set<string>();
  private pointerHeld = false;
  private suspended = false;
  private recenter = true;
  private baseYaw = 0;
  private toggleDown = false;
  private recenterDown = false;
  private xrLive = true;
  private scanPending = false;
  private scanArmed = false;
  private scanning = false;
  private scanCompleting = false;
  private scanDown = false;
  private scanMessage = "Hold trigger + Y to scan; keep sticks centered";
  private scanPollAt = 0;
  private lastXRAt = -Infinity;
  private rightSource: XRInputSource | null = null;
  private movementSource: XRInputSource | null = null;
  private canvas = document.createElement("canvas");
  private ctx: CanvasRenderingContext2D;
  private hudCanvas = document.createElement("canvas");
  private hudContext: CanvasRenderingContext2D;
  private hudTexture: THREE.CanvasTexture;
  private cameraTexture: THREE.CanvasTexture;
  private cameraMaterial: THREE.MeshBasicMaterial;
  private cockpit = new THREE.Group();
  private message: HTMLElement;
  private details: HTMLElement;
  private holdButton: HTMLButtonElement;
  private h264: H264Viewer;
  private h264Enabled = false;
  private digitalPan = false;
  private drawnPan = 0;
  private cameraScreen: THREE.Mesh;
  private lastVideoAt = 0;
  private dirty = true;
  private lastHudAt = 0;
  private quat = new THREE.Quaternion();
  private position = new THREE.Vector3();
  private euler = new THREE.Euler(0, 0, 0, "YXZ");

  constructor(
    private renderer: THREE.WebGLRenderer,
    private scene: THREE.Scene,
    private camera: THREE.PerspectiveCamera,
    private getView: () => string,
    private setView: (view: string) => void,
  ) {
    this.canvas.id = "rover-camera";
    this.canvas.width = 640; this.canvas.height = 480;
    this.ctx = this.canvas.getContext("2d")!;
    document.querySelector("#full-camera")!.append(this.canvas);
    const panel = document.createElement("section");
    panel.className = "rover-controls";
    panel.setAttribute("aria-label", "Rover driving controls");
    panel.innerHTML = `<div class="rover-tabs"><button id="live-rover">Live rover</button><button id="rover-map">3D map</button><button id="rover-scan">Scan</button><button id="rover-stop">STOP</button></div>
      <p id="rover-message" role="status">Waiting for live camera and rover</p>
      <p id="rover-details">Motion disabled until camera and control are fresh.</p>
      <button id="rover-hold" class="hold-button">Hold to enable · WASD to drive</button>
      <button id="rover-explore" aria-pressed="false">Explore (E)</button>
      <p class="rover-help">Quest: hold right trigger + left stick. A: seek pointed person (else camera/map). B: explore. X: center pan. Y: scan. Left grip: start rescue drill. Hold left trigger: ask by voice (enable in Settings first).<br>Desktop: hold Space + WASD in camera view; click a person to seek; E explores. Stick/WASD takes back control.</p>`;
    document.querySelector("main")!.append(panel);
    this.sourceBanner = document.createElement("div");
    this.sourceBanner.className = "source-banner"; this.sourceBanner.setAttribute("role", "status");
    this.sourceBanner.innerHTML = "<strong></strong><span></span>";
    document.querySelector("main")!.append(this.sourceBanner);
    this.message = panel.querySelector("#rover-message")!;
    this.details = panel.querySelector("#rover-details")!;
    this.holdButton = panel.querySelector("#rover-hold")!;
    panel.querySelector<HTMLButtonElement>("#live-rover")!.onclick = () => this.setView("camera");
    panel.querySelector<HTMLButtonElement>("#rover-map")!.onclick = () => { this.stop("Map view: release deadman"); this.setView("3d"); };
    panel.querySelector<HTMLButtonElement>("#rover-stop")!.onclick = () => this.stop("Stopped: release deadman");
    panel.querySelector<HTMLButtonElement>("#rover-explore")!.onclick = () => this.toggleExplore();
    panel.querySelector<HTMLButtonElement>("#rover-scan")!.onclick = () => void this.startScan();
    this.holdButton.onpointerdown = event => {
      this.holdButton.setPointerCapture(event.pointerId);
      this.pointerHeld = true;
    };
    this.holdButton.onpointerup = this.holdButton.onpointercancel = () => { this.pointerHeld = false; };
    this.holdButton.onlostpointercapture = () => { this.pointerHeld = false; };
    window.addEventListener("keydown", this.keyDown);
    window.addEventListener("keyup", this.keyUp);
    window.addEventListener("blur", this.blur);
    window.addEventListener("click", this.click);
    document.addEventListener("visibilitychange", this.visibility);
    document.addEventListener("keydown", this.escapeStop);

    this.hudCanvas.width = 1024; this.hudCanvas.height = 220;
    this.hudContext = this.hudCanvas.getContext("2d")!;
    this.cameraTexture = this.createCameraTexture();
    this.hudTexture = new THREE.CanvasTexture(this.hudCanvas);
    this.hudTexture.colorSpace = THREE.SRGBColorSpace;
    this.cameraMaterial = new THREE.MeshBasicMaterial({ map: this.cameraTexture, toneMapped: false, depthTest: false });
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 2.1), this.cameraMaterial);
    screen.position.set(0, 0.15, -2.35); screen.renderOrder = 10;
    const hud = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 0.60), new THREE.MeshBasicMaterial({ map: this.hudTexture, transparent: true, toneMapped: false, depthTest: false }));
    hud.position.set(0, -1.10, -2.30); hud.renderOrder = 11;
    this.drillTexture.colorSpace = THREE.SRGBColorSpace;
    this.drillPanel.position.set(2.15, 0.15, -2.1); this.drillPanel.rotation.y = -0.35; this.drillPanel.renderOrder = 12; this.drillPanel.visible = false;
    document.querySelector("#full-camera")!.append(this.drillHud.canvas); this.drillHud.canvas.hidden = true;
    this.bannerCanvas.width = 1024; this.bannerCanvas.height = 128;
    this.bannerTexture.colorSpace = THREE.SRGBColorSpace;
    this.bannerPanel.position.set(0, 1.45, -2.32); this.bannerPanel.renderOrder = 13;
    this.voiceCanvas.width = 768; this.voiceCanvas.height = 512;
    this.voiceTexture.colorSpace = THREE.SRGBColorSpace;
    this.voicePanel.position.set(-2.15, 0.15, -2.1); this.voicePanel.rotation.y = 0.35; this.voicePanel.renderOrder = 12; this.voicePanel.visible = false;
    this.cockpit.add(screen, hud, this.drillPanel, this.bannerPanel, this.voicePanel); this.cockpit.visible = false;
    this.cameraScreen = screen;
    this.scene.add(this.cockpit);
    this.h264 = new H264Viewer(() => {
      if (performance.now() - this.lastVideoAt > 60) { this.dirty = true; this.lastVideoAt = performance.now(); }
    }, () => this.stop("H.264 restarted: motion disabled"));
    const videoOption = document.createElement("label"); videoOption.className = "check";
    videoOption.innerHTML = '<input type="checkbox"/> H.264 video (experimental; requires decoded freshness)';
    document.querySelector("#settings")!.append(videoOption);
    videoOption.querySelector("input")!.onchange = event => {
      this.h264Enabled = (event.target as HTMLInputElement).checked;
      this.stop("Video format changed: release deadman");
      this.gate.frameAt = -Infinity; this.gate.frameAge = Infinity; this.pendingFrame = null; this.lastCanvasDraw = -Infinity;
      if (this.h264Enabled) this.h264.start(); else this.h264.stop();
      this.dirty = true;
    };
    this.renderer.xr.addEventListener("sessionstart", () => {
      this.stop("VR session started: release trigger");
      this.lastXRAt = -Infinity;
      this.rightSource = null; this.movementSource = null;
      this.inputValid = false; this.keys.clear(); this.pointerHeld = false;
      this.recenter = true; this.xrLive = true; this.setView("camera");
      this.renderer.xr.getSession()?.addEventListener("visibilitychange", () => {
        this.suspended = this.renderer.xr.getSession()?.visibilityState !== "visible";
        if (this.suspended) { this.voice?.stopListening("Headset inactive: voice question cancelled"); this.stop("Headset inactive: release trigger"); }
      });
    });
    this.renderer.xr.addEventListener("sessionend", () => {
      this.voice?.stopListening("VR session ended: voice question cancelled");
      this.stop("VR session ended: release deadman"); this.suspended = false;
      this.lastXRAt = -Infinity;
      this.rightSource = null; this.movementSource = null;
      this.inputValid = false; this.keys.clear(); this.pointerHeld = false;
    });
    this.renderer.domElement.addEventListener("webglcontextlost", () => { this.contextLost = true; this.stop("Display lost: release deadman"); });
    this.renderer.domElement.addEventListener("webglcontextrestored", () => { this.contextLost = false; this.stop("Display restored: release deadman"); });
    const cameraOption = document.createElement("label"); cameraOption.className = "camera-mount-option";
    cameraOption.innerHTML = '<span>Camera mounting</span><select aria-label="Camera mounting mode"><option value="servo">Camera mounted on servo</option><option value="fixed">Fixed camera · digital look-around</option></select><small>Fixed mode crops measured pixels and keeps the physical pan command centered.</small>';
    document.querySelector("#settings")!.append(cameraOption);
    cameraOption.querySelector("select")!.onchange = event => {
      this.digitalPan = (event.target as HTMLSelectElement).value === "fixed";
      this.stop("Camera mount changed: release deadman"); this.dirty = true;
    };
    this.connectControl(); this.connectDetections();
    void this.sourceTruth.refresh();
    this.timers.push(window.setInterval(() => void this.sourceTruth.refresh(), 2000));
    this.timers.push(window.setInterval(() => this.pump(), 50));
    this.timers.push(window.setInterval(() => void this.fetchFrame(), 160));
    void this.fetchFrame();
    this.addAssistant();
  }

  private keyDown = (event: KeyboardEvent) => {
    if (/INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement)?.tagName ?? "") || event.metaKey || event.ctrlKey || event.altKey || document.querySelector("dialog[open]")) return;
    if (this.getView() !== "camera" || this.renderer.xr.isPresenting) return;
    const key = event.code;
    if (event.repeat && !this.keys.has(key)) return;
    if (["Space", "KeyW", "KeyA", "KeyS", "KeyD"].includes(key)) {
      event.preventDefault(); this.keys.add(key);
    }
    if (key === "KeyE" && !event.repeat) { event.preventDefault(); this.toggleExplore(); }
  };
  // The 3D scene canvas sits above the camera canvas, so map window clicks onto the camera image.
  private click = (event: MouseEvent) => {
    if (this.getView() !== "camera" || this.renderer.xr.isPresenting || !(event.target instanceof HTMLCanvasElement)) return;
    const point = containPoint(event.clientX, event.clientY, this.canvas.getBoundingClientRect(), this.canvas.width, this.canvas.height);
    if (point) this.selectAt(point);
  };
  private keyUp = (event: KeyboardEvent) => { this.keys.delete(event.code); };
  private blur = () => { this.keys.clear(); this.pointerHeld = false; this.stop("Window inactive: release deadman"); };
  private visibility = () => { if (document.hidden) this.blur(); };
  private escapeStop = (event: KeyboardEvent) => { if (event.key === "Escape") this.stop("Stopped: release deadman"); };

  stop(reason: string) {
    this.autonomy.clear();
    this.scanArmed = false;
    this.scanning = false;
    this.scanCompleting = false;
    this.input = this.gate.stop(this.input, reason);
    this.send(false);
  }
  private endpoint(path: string) { return new URL(path, location.origin).href.replace(/^http/, "ws"); }
  private connectControl() {
    if (this.stopped) return;
    const socket = new WebSocket(this.endpoint("/ws/control"));
    this.socket = socket;
    socket.onopen = () => { this.gate.invalidate("Connected: release deadman"); this.send(false); };
    socket.onmessage = event => {
      try {
        const next = JSON.parse(event.data) as ControlStatus;
        if (this.gate.acceptStatus(next, performance.now())) {
          this.status = next;
          if (this.scanning && next.requires_release && next.reason === "scan_finished") this.scanCompleting = true;
          if (typeof next.seq === "number" && this.sent.has(next.seq)) this.ackMs = performance.now() - this.sent.get(next.seq)!;
          if (next.requires_release) this.gate.invalidate(next.reason ?? "Release deadman to continue");
        }
      } catch { this.stop("Invalid control status"); socket.close(); }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      this.gate.invalidate("Control disconnected: release deadman");
      this.status = null;
      if (!this.stopped) this.timers.push(window.setTimeout(() => this.connectControl(), 1000));
    };
  }
  private connectDetections() {
    if (this.stopped) return;
    const socket = new WebSocket(this.endpoint("/ws/detections"));
    this.detectionsSocket = socket;
    socket.onmessage = event => {
      try {
        const next = JSON.parse(event.data) as Detections;
        if (!Array.isArray(next.boxes) || next.boxes.length > 100) return;
        next.boxes = next.boxes.filter(box => typeof box.cls === "string" &&
          [box.conf, box.x, box.y, box.w, box.h].every(Number.isFinite) && box.w > 0 && box.h > 0 && box.conf >= 0 && box.conf <= 1);
        if (!this.detections || next.frame !== this.detections.frame || next.source !== this.detections.source) {
          this.detectionsAt = performance.now();
          this.detectionFrames.set(`${next.source ?? ""}:${next.frame}`, { packet: next, at: this.detectionsAt });
          if (this.detectionFrames.size > 40) this.detectionFrames.delete(this.detectionFrames.keys().next().value!);
        }
        this.detections = next; this.dirty = true;
      } catch { /* Malformed detections are omitted from HUD; never synthesize boxes. */ }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      this.detections = null; this.dirty = true;
      if (!this.stopped) this.timers.push(window.setTimeout(() => this.connectDetections(), 2000));
    };
  }

  private async fetchFrame() {
    if (this.h264Enabled || this.frameBusy || document.hidden || this.stopped ||
        (!this.renderer.xr.isPresenting && this.getView() !== "camera")) return;
    this.frameBusy = true;
    const start = performance.now();
    const controller = new AbortController(); this.request = controller;
    const timeout = window.setTimeout(() => controller.abort(), 750);
    let url = "";
    try {
      const response = await fetch("/camera.jpg", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`Camera HTTP ${response.status}`);
      const id = response.headers.get("X-Frame-Id");
      const ageHeader = response.headers.get("X-Frame-Age-Ms");
      const source = response.headers.get("X-Frame-Source") ?? "Unverified camera source";
      if (id === this.frameId) return;
      const blob = await response.blob();
      if (blob.size > 2_000_000 || !blob.type.startsWith("image/")) throw new Error("Invalid camera image");
      url = URL.createObjectURL(blob);
      const image = new Image(); image.src = url;
      await image.decode();
      const now = performance.now();
      if (this.stopped || controller.signal.aborted || this.h264Enabled) return;
      this.frame = image; this.frameSource = source;
      this.frameId = id ?? "";
      // Server capture age + full request/decode time bounds transit age without synchronized clocks.
      this.pendingFrame = { id, age: ageHeader === null ? Infinity : Number(ageHeader) + now - start, at: now };
      this.dirty = true;
    } catch {
      // Keep the last frame visible; its monotonically growing age stops motion.
    } finally {
      if (url) URL.revokeObjectURL(url);
      window.clearTimeout(timeout); this.frameBusy = false; this.request = null;
    }
  }

  update(now: number, frame?: XRFrame) {
    const xr = this.renderer.xr.isPresenting;
    const session = this.renderer.xr.getSession();
    if (xr && session) {
      const sources = [...session.inputSources];
      const right = sources.find(source => source.handedness === "right" && source.gamepad);
      const left = sources.find(source => source.handedness === "left" && source.gamepad);
      const movement = left ?? right;
      if ((right ?? null) !== this.rightSource || (movement ?? null) !== this.movementSource) {
        this.voice?.stopListening("Controller changed: voice question cancelled");
        this.stop("Controller changed: release trigger");
        this.rightSource = right ?? null; this.movementSource = movement ?? null;
        this.latchButtons();
      }
      const reference = this.renderer.xr.getReferenceSpace();
      let pose: XRViewerPose | null = null;
      let tracked = false;
      try {
        if (session.visibilityState === "visible" && frame && reference) pose = frame.getViewerPose(reference) ?? null;
        tracked = Boolean(pose && frame && reference &&
          right?.gamepad?.connected && movement?.gamepad?.connected &&
          right.gripSpace && movement.gripSpace &&
          frame.getPose(right.gripSpace, reference) && frame.getPose(movement.gripSpace, reference));
      } catch { /* An ended/inactive frame cannot provide current tracking. */ }
      if (pose) {
        const orientation = pose.transform.orientation, position = pose.transform.position;
        this.quat.set(orientation.x, orientation.y, orientation.z, orientation.w);
        this.position.set(position.x, position.y, position.z);
        this.cockpit.position.copy(this.position); this.cockpit.quaternion.copy(this.quat);
      }
      if (!tracked) {
        this.inputValid = false;
        this.voice?.stopListening("XR tracking lost: voice question cancelled");
        if (Number.isFinite(this.lastXRAt) || this.input.held)
          this.stop("XR tracking unavailable: release trigger");
        else this.gate.invalidate("XR tracking unavailable: release trigger");
        this.lastXRAt = -Infinity;
        this.latchButtons();
        // Keep the camera and stop reason visible when only a controller is lost.
        this.cockpit.visible = !!pose;
        this.cameraScreen.visible = this.xrLive;
        if (this.dirty || now - this.lastCanvasDraw > 100) this.drawCamera(now);
        if (now - this.lastHudAt > 100) { this.drawHUD(now); this.lastHudAt = now; }
        return;
      }
      this.lastXRAt = now;
      const gamepad = movement?.gamepad;
      const axes = gamepad?.axes ?? [];
      const held = !!right?.gamepad?.buttons[0]?.pressed;
      const scan = !!left?.gamepad?.buttons[5]?.pressed;
      const toggle = !!right?.gamepad?.buttons[4]?.pressed;
      const explore = !!right?.gamepad?.buttons[5]?.pressed;
      const center = !!left?.gamepad?.buttons[4]?.pressed;
      const drill = !!left?.gamepad?.buttons[1]?.pressed;
      const talk = !!left?.gamepad?.buttons[0]?.pressed;
      if (talk && !this.talkDown) this.talkPressed();
      else if (!talk && this.talkDown) this.voice?.pushEnd();
      this.talkDown = talk;
      if (center && !this.recenterDown && this.voice?.pendingAction) this.voice.dismissAction();
      if (drill && !this.drillDown) void this.startDrill();
      this.drillDown = drill;
      if (toggle && !this.toggleDown) {
        // A on a tracked person selects Seek; A anywhere else keeps the camera/map toggle.
        const point = this.xrLive ? this.panelPoint(frame, reference, right) : null;
        if (!point || !this.selectAt(point)) { this.xrLive = !this.xrLive; this.stop("View changed: release trigger"); this.setView(this.xrLive ? "camera" : "3d"); }
      }
      if (explore && !this.exploreDown && this.xrLive) this.toggleExplore();
      if (center && !this.recenterDown) this.recenter = true;
      this.toggleDown = toggle; this.exploreDown = explore; this.recenterDown = center;
      this.euler.setFromQuaternion(this.quat, "YXZ");
      if (this.recenter && pose) { this.baseYaw = this.euler.y; this.recenter = false; }
      const yaw = Math.atan2(Math.sin(this.euler.y - this.baseYaw), Math.cos(this.euler.y - this.baseYaw));
      this.inputValid = true;
      this.input = { held,
        v: -stick(axes.length >= 4 ? axes[3] : axes[1]) * 0.45,
        w: stick(axes.length >= 4 ? axes[2] : axes[0]) * 0.55,
        pan: THREE.MathUtils.clamp(-THREE.MathUtils.radToDeg(yaw), -80, 80) };
      if (scan && !this.scanDown) void this.startScan();
      this.scanDown = scan;
      this.cockpit.position.copy(this.position); this.cockpit.quaternion.copy(this.quat);
    } else {
      this.inputValid = true;
      this.input = {
        held: this.keys.has("Space") || this.pointerHeld,
        v: (Number(this.keys.has("KeyW")) - Number(this.keys.has("KeyS"))) * 0.35,
        w: (Number(this.keys.has("KeyD")) - Number(this.keys.has("KeyA"))) * 0.45,
        pan: 0,
      };
    }
    if (this.autonomy.stick(this.input.v, this.input.w)) this.dirty = true;
    if (this.input.v || this.input.w) this.scanArmed = false;
    this.cockpit.visible = xr;
    this.cameraScreen.visible = this.xrLive;
    if (this.h264Enabled) {
      this.h264.update();
    }
    if (this.digitalPan && Math.abs(this.input.pan - this.drawnPan) > 0.1) this.dirty = true;
    if (this.dirty || now - this.lastCanvasDraw > 100) this.drawCamera(now);
    if (now - this.lastHudAt > 100) { this.drawHUD(now); this.lastHudAt = now; }
  }
  /** Controller ray (or head gaze) onto the camera panel, in canvas-normalized coordinates. */
  private panelPoint(frame: XRFrame | undefined, reference: XRReferenceSpace | null, right: XRInputSource | undefined) {
    let ray: XRPose | null = null;
    try { if (frame && reference && right) ray = frame.getPose(right.targetRaySpace, reference) ?? null; } catch { /* Fall back to gaze. */ }
    if (ray) {
      const o = ray.transform.orientation, p = ray.transform.position;
      this.raycaster.ray.origin.set(p.x, p.y, p.z);
      this.raycaster.ray.direction.set(0, 0, -1).applyQuaternion(new THREE.Quaternion(o.x, o.y, o.z, o.w));
    } else {
      this.raycaster.ray.origin.copy(this.position);
      this.raycaster.ray.direction.set(0, 0, -1).applyQuaternion(this.quat);
    }
    this.cockpit.updateMatrixWorld(true);
    return rayPanelPoint(this.raycaster.intersectObject(this.cameraScreen, false)[0]?.uv ?? null);
  }
  private selectAt(point: { x: number; y: number }) {
    const box = pickTarget(this.drawnBoxes, point.x, point.y);
    if (!box) return false;
    if (this.scanning || this.scanPending) this.stop("Scan cancelled: Seek selected");
    if (!this.autonomy.select(box.id!)) return false;
    this.dirty = true;
    return true;
  }
  private toggleExplore() {
    if (this.scanning || this.scanPending) this.stop("Scan cancelled: Explore selected");
    this.autonomy.toggleExplore(); this.dirty = true;
  }
  /** Left trigger: confirm a pending rover proposal, otherwise start push-to-talk. */
  private talkPressed() {
    if (!this.voice) return;
    if (this.voice.pendingAction) { this.voice.confirmAction(); this.dirty = true; return; }
    if (this.voice.pushStart()) this.stop("Voice question: release trigger");
  }
  get vrCameraView() { return this.renderer.xr.isPresenting && this.xrLive; }

  private pump() {
    const now = performance.now();
    const connected = this.socket?.readyState === WebSocket.OPEN;
    const contextActive = this.renderer.xr.isPresenting ? this.xrLive && !this.suspended : this.getView() === "camera" && !document.querySelector("dialog[open]");
    const available = this.inputValid && contextActive && !document.hidden && !this.contextLost && now - this.lastCanvasDraw < 250;
    // No motion while the microphone is requested or open; release is required afterwards.
    if (this.voice?.capturing) this.gate.invalidate("Voice question: release trigger");
    const moving = this.gate.decide(this.input.held, now, connected, available,
      this.renderer.xr.isPresenting ? this.lastXRAt : undefined);
    // Never add another command behind buffered traffic. Reset the session instead.
    if (connected && this.socket!.bufferedAmount > 0) {
      this.gate.invalidate("Control queue stalled: release deadman"); this.socket!.close(); return;
    }
    if (now - this.lastSendAt > 200) this.gate.invalidate("Browser paused: release deadman");
    if (this.scanPending) return; // POST reservation must precede scan-mode heartbeats
    if (this.scanning && this.scanCompleting) {
      this.send(false);
      if (now - this.scanPollAt > 250) { this.scanPollAt = now; void this.pollScan(); }
      return;
    }
    if (this.scanning && (!moving || this.input.v || this.input.w)) {
      this.scanMessage = "Scan cancelled: release, stale view, or stick override";
      this.stop(this.scanMessage);
    }
    this.send(moving && !this.gate.needsRelease);
    if (this.scanArmed && moving && !this.input.v && !this.input.w) {
      this.scanArmed = false;
      void this.startScan();
    }
    if (this.scanning && now - this.scanPollAt > 250) {
      this.scanPollAt = now;
      void this.pollScan();
    }
  }
  private send(deadman: boolean) {
    if (this.socket?.readyState !== WebSocket.OPEN || this.socket.bufferedAmount > 0) return;
    const age = this.gate.videoAge(performance.now());
    const seq = this.seq++;
    this.sent.set(seq, performance.now());
    if (this.sent.size > 100) this.sent.delete(this.sent.keys().next().value!);
    this.socket.send(JSON.stringify({
      seq, session_id: this.status?.session_id ?? null, permit: this.status?.permit ?? null,
      deadman, v: deadman ? this.input.v : 0, w: deadman ? this.input.w : 0,
      pan: this.digitalPan ? 0 : this.input.pan, video_fresh: age < 500,
      ...(this.scanning ? { mode: "scan" } : this.autonomy.fields(deadman)),
      video_age_ms: Number.isFinite(age) ? Math.ceil(age) : 999999,
    }));
    this.lastSendAt = performance.now();
  }
  private async startScan() {
    if (this.scanPending || this.scanning) return;
    if (this.autonomy.mode !== "teleop" || !this.input.held || this.input.v || this.input.w || this.gate.needsRelease ||
        this.gate.readiness(performance.now(), this.socket?.readyState === WebSocket.OPEN)) {
      this.scanMessage = "Scan: stop autonomy first; hold deadman with sticks centered";
      return;
    }
    this.send(true);
    this.scanPending = true;
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 1000);
    try {
      const response = await fetch("/scan", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: this.status?.session_id, permit: this.status?.permit }), signal: abort.signal });
      if (!response.ok) throw new Error(String((await response.json()).detail ?? response.status));
      const result = await response.json();
      this.scanning = true;
      this.scanMessage = `${result.simulated ? "SIMULATED · " : ""}Scanning${result.approximate ? " · approximate rover turn" : " · physical pan"}; keep holding deadman`;
    } catch (error) { this.scanMessage = `Scan unavailable: ${String(error)}`; this.stop(this.scanMessage); }
    finally { this.scanPending = false; window.clearTimeout(timer); }
  }
  private async pollScan() {
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 1000);
    try {
      const response = await fetch("/scan", { cache: "no-store", signal: abort.signal });
      if (!response.ok) throw new Error(`Scan status ${response.status}`);
      const result = await response.json();
      if (!this.scanning || result.running) return;
      this.scanMessage = result.state === "complete" ?
        `${result.points.toLocaleString()} points · ${(result.scan_ms / 1000).toFixed(1)} s${result.approximate ? " · approximate" : ""}` : `Scan aborted: ${result.error}`;
      this.stop(this.scanMessage);
      if (result.state === "complete") { this.xrLive = false; this.setView("3d"); }
    } catch (error) { this.scanMessage = String(error); this.stop("Scan status unavailable"); }
    finally { window.clearTimeout(timer); }
  }
  private drawCamera(now: number) {
    const context = this.ctx;
    if (this.h264Enabled && this.h264.video.readyState >= 2) {
      const decoded = this.h264.displayedFrame;
      // Metadata can be ahead of presentation. Retain the validated canvas
      // and its boxes until the decoder confirms the next displayed identity.
      // Keep dirty so the next RAF retries; retained pixels never renew age.
      if (!decoded) return;
      const video = this.h264.video;
      this.resizeCameraCanvas(video.videoWidth, video.videoHeight);
      const crop = cameraCrop(video.videoWidth, video.videoHeight, this.input.pan, this.digitalPan);
      context.drawImage(video, crop.x, crop.y, crop.width, crop.height, 0, 0, this.canvas.width, this.canvas.height);
      if (decoded) {
        this.gate.acceptFrame(decoded.id, decoded.age, now);
        this.frameSource = decoded.source; this.frameId = decoded.frame === null ? "" : String(decoded.frame);
        this.lastCanvasDraw = now;
        this.drawDetections(crop, video.videoWidth, video.videoHeight, now);
      }
    } else if (!this.h264Enabled && this.frame) {
      this.resizeCameraCanvas(this.frame.naturalWidth, this.frame.naturalHeight);
      const crop = cameraCrop(this.frame.naturalWidth, this.frame.naturalHeight, this.input.pan, this.digitalPan);
      context.drawImage(this.frame, crop.x, crop.y, crop.width, crop.height, 0, 0, this.canvas.width, this.canvas.height);
      if (this.pendingFrame) {
        const pending = this.pendingFrame;
        if (this.gate.acceptFrame(pending.id, pending.age + now - pending.at, now)) this.lastCanvasDraw = now;
        this.pendingFrame = null;
      }
      // Repainting a retained frame proves display-loop liveness, while its
      // original capture age continues growing; duplicate IDs never renew it.
      if (this.gate.frameId === this.frameId) this.lastCanvasDraw = now;
      this.drawDetections(crop, this.frame.naturalWidth, this.frame.naturalHeight, now);
    } else {
      context.fillStyle = "#141c22"; context.fillRect(0, 0, this.canvas.width, this.canvas.height);
      context.fillStyle = "#dce5ee"; context.font = "22px system-ui"; context.textAlign = "center";
      context.fillText("Waiting for rover camera", this.canvas.width / 2, this.canvas.height / 2);
      context.textAlign = "left";
    }
    this.drawnPan = this.input.pan;
    this.cameraScreen.scale.y = (this.canvas.height / this.canvas.width) / 0.75;
    this.cameraTexture.needsUpdate = true; this.dirty = false;
  }
  private matchingDetections(now: number) {
    if (!this.frameId) return null;
    const cached = this.detectionFrames.get(`${this.frameSource}:${this.frameId}`) ?? this.detectionFrames.get(`:${this.frameId}`);
    return cached && now - cached.at < 500 ? cached.packet : null;
  }
  private drawDetections(crop: ReturnType<typeof cameraCrop>, width: number, height: number, now: number) {
    const packet = this.matchingDetections(now);
    this.drawnBoxes = [];
    if (!packet) return;
    const context = this.ctx;
    context.strokeStyle = "#8dffa9"; context.fillStyle = "#8dffa9";
    context.lineWidth = Math.max(2, this.canvas.width / 320);
    context.font = `${Math.max(14, this.canvas.width / 40)}px system-ui`;
    const segments: number[] = [];
    for (const box of packet.boxes) {
      const visibleBox = croppedBox(box, crop, width, height);
      if (!visibleBox) continue;
      this.drawnBoxes.push({ ...box, ...visibleBox });
      const target = box.id !== undefined && box.id === this.autonomy.targetId;
      context.strokeStyle = context.fillStyle = target ? "#ffc779" : "#8dffa9";
      context.lineWidth = Math.max(2, this.canvas.width / 320) * (target ? 2 : 1);
      const x = visibleBox.x * this.canvas.width, y = visibleBox.y * this.canvas.height;
      context.strokeRect(x, y, visibleBox.w * this.canvas.width, visibleBox.h * this.canvas.height);
      const id = Number.isSafeInteger(box.id) ? ` #${box.id}` : "";
      context.fillText(`${target ? "SEEK " : ""}${box.cls.slice(0, 30)}${id} ${Math.round(box.conf * 100)}%`, x + 4, Math.max(18, y - 5));
      if (box.down === true) {
        drawDownMarker(context, { x, y, w: visibleBox.w * this.canvas.width, h: visibleBox.h * this.canvas.height }, this.canvas.width, Math.max(16, this.canvas.width / 32));
      }
      if (box.kpts) segments.push(...poseSegments(box.kpts, crop, width, height));
    }
    strokeSegments(context, segments, this.canvas.width, this.canvas.height, "#7fd4ff", Math.max(2, this.canvas.width / 240));
  }
  /** Held buttons from a lost or replaced controller act only after an observed release. */
  private latchButtons() {
    this.toggleDown = this.exploreDown = this.recenterDown = this.scanDown = this.drillDown = this.talkDown = true;
  }
  private createCameraTexture() {
    const texture = new THREE.CanvasTexture(this.canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  }
  private resizeCameraCanvas(width: number, height: number) {
    if (this.canvas.width === width && this.canvas.height === height) return;
    this.canvas.width = width; this.canvas.height = height;
    // Immutable WebGL texture storage cannot retain the previous image size.
    this.cameraTexture.dispose();
    this.cameraTexture = this.createCameraTexture();
    this.cameraMaterial.map = this.cameraTexture;
    this.cameraMaterial.needsUpdate = true;
  }
  private drawHUD(now: number) {
    const ready = this.gate.readiness(now, this.socket?.readyState === WebSocket.OPEN,
      this.renderer.xr.isPresenting ? this.lastXRAt : undefined);
    const videoAge = this.gate.videoAge(now);
    const age = Number.isFinite(videoAge) ? `${Math.ceil(videoAge)} ms` : "unavailable";
    this.sourceTruth.observe(this.frameSource, this.status);
    const source = this.sourceTruth.state({ now, frameSource: this.frameSource, frameId: this.frameId, videoAgeMs: videoAge });
    const simulation = source.kind === "SIMULATED" || source.kind === "REPLAY";
    const moving = this.input.held && !this.gate.needsRelease && !ready;
    const headline = `${source.kind === "LIVE" ? "" : source.kind + " · "}${ready ?? this.gate.reason}`;
    const ack = this.ackMs === null ? "ack pending" : `control ack ${Math.round(this.ackMs)} ms`;
    const range = this.status?.range_cm == null ? "range unknown" : `${Math.round(this.status.range_cm)} cm`;
    const matching = this.matchingDetections(now);
    const detection = matching ? `${matching.boxes.length} detections` : !this.detections ? "detector unavailable" : now - this.detectionsAt > 500 ? "detections stale" : "detections awaiting matching frame";
    const link = linkHealth({ now, receivedAt: this.gate.receivedAt, connected: this.socket?.readyState === WebSocket.OPEN,
      linkMs: this.status?.link_ms, detFps: this.detections?.fps });
    const drill = this.drillLine(now);
    const intent = this.autonomy.mode === "seek" ? `Seek #${this.autonomy.targetId} armed` : this.autonomy.mode === "explore" ? "Explore armed" : "";
    const autonomy = [intent, autonomyLabel(this.status)].filter(Boolean).join(" → ");
    const panMode = `${this.digitalPan ? "Digital pan (fixed camera)" : "Servo pan"} ${Math.round(this.input.pan)}°`;
    const detail = `${link.text} · ${this.frameSource} · ${panMode} · ${this.h264Enabled ? "H.264" : "JPEG"} video ${age} · ${ack} · ${range} · ${detection}${drill ? " · " + drill : ""}${this.h264Enabled && !this.h264.displayedFrame ? " · " + this.h264.state : ""} · ${this.scanMessage}`;
    this.message.textContent = headline;
    this.message.dataset.state = moving ? "moving" : ready ? "unsafe" : "ready";
    this.details.textContent = `${autonomy ? autonomy + " · " : ""}${detail}${this.status?.estop ? " · FORWARD BLOCKED" : ""}`;
    const exploreButton = document.querySelector<HTMLButtonElement>("#rover-explore");
    exploreButton?.setAttribute("aria-pressed", String(this.autonomy.mode === "explore"));
    this.holdButton.classList.toggle("engaged", moving);
    const c = this.hudContext;
    c.clearRect(0, 0, 1024, 220); c.fillStyle = "#111b25ee"; c.fillRect(0, 0, 1024, 220);
    c.fillStyle = simulation ? "#ffc779" : ready ? "#ffb3a8" : "#a6edc4";
    c.font = "bold 32px system-ui"; c.fillText(headline.slice(0, 58), 28, 46);
    c.font = "24px system-ui"; c.fillStyle = "#dce5ee";
    c.fillText(`${this.h264Enabled ? "H.264" : "JPEG"} video ${age} · ${range} · ${panMode}`, 28, 90);
    c.font = "bold 24px system-ui"; c.textAlign = "right"; c.fillStyle = link.color;
    c.fillText(link.text, this.hudCanvas.width - 28, 90); c.textAlign = "left";
    c.font = "24px system-ui"; c.fillStyle = "#dce5ee";
    c.fillText(`${ack} · ${detection}${this.status?.estop ? " · FORWARD BLOCKED" : ""}`, 28, 126);
    if (drill || autonomy) { c.font = "18px system-ui"; c.fillText([autonomy, drill].filter(Boolean).join(" · "), 28, 150); }
    const hint = "R trigger: deadman · Stick: drive · A: seek/map · B: explore · X: center · Y: scan · L grip: drill · L trigger: ask";
    c.font = "22px system-ui";
    if (c.measureText(hint).width > this.hudCanvas.width - 56) c.font = "18px system-ui";
    c.fillText(hint, 28, 170);
    c.fillText(this.scanMessage.slice(0, 84), 28, 202);
    this.hudTexture.needsUpdate = true;
    this.drawSource(source);
    this.drawVoice();
    const drawn = this.drillHud.draw(now);
    this.drillPanel.visible = drawn; this.drillHud.canvas.hidden = !drawn;
    if (drawn) this.drillTexture.needsUpdate = true;
  }

  private drawSource(source: ReturnType<SourceTruth["state"]>) {
    const colors = { LIVE: ["#14532d", "#bbf7d0"], SIMULATED: ["#7c4a03", "#ffe3a3"], REPLAY: ["#4c1d95", "#e9d5ff"], UNVERIFIED: ["#5b1f1f", "#ffd2ca"] }[source.kind];
    this.sourceBanner.dataset.kind = source.kind;
    this.sourceBanner.querySelector("strong")!.textContent = source.title;
    this.sourceBanner.querySelector("span")!.textContent = source.detail;
    const c = this.bannerCanvas.getContext("2d")!;
    c.clearRect(0, 0, 1024, 128); c.fillStyle = colors[0]; c.fillRect(0, 0, 1024, 128);
    c.strokeStyle = colors[1]; c.lineWidth = 6; c.strokeRect(3, 3, 1018, 122);
    c.fillStyle = colors[1]; c.font = "bold 54px system-ui"; c.fillText(source.title, 28, 62);
    c.font = "26px system-ui"; c.fillText(source.detail.slice(0, 72), 28, 106);
    this.bannerTexture.needsUpdate = true;
  }
  private drawVoice() {
    const voice = this.voice, action = voice?.pendingAction ?? null;
    const visible = !!voice && this.renderer.xr.isPresenting && (voice.phase !== "idle" || !!action);
    this.voicePanel.visible = visible;
    if (!visible) return;
    const c = this.voiceCanvas.getContext("2d")!;
    const accent = { idle: "#dce5ee", permission: "#ffc779", listening: "#ff8a7a", processing: "#a6c9ec", answer: "#a6edc4", error: "#ffb3a8" }[voice.phase];
    c.clearRect(0, 0, 768, 512); c.fillStyle = "#111b25ee"; c.fillRect(0, 0, 768, 512);
    c.strokeStyle = accent; c.lineWidth = 6; c.strokeRect(3, 3, 762, 506);
    c.fillStyle = accent; c.font = "bold 44px system-ui";
    c.fillText({ idle: "VOICE", permission: "OPENING MIC", listening: "● LISTENING", processing: "PROCESSING", answer: "ANSWER", error: "VOICE ERROR" }[voice.phase], 28, 60);
    c.fillStyle = "#dce5ee"; c.font = "28px system-ui";
    const body = voice.phase === "answer" && voice.answerText ? voice.answerText : voice.statusText;
    let y = 110;
    for (const line of wrapLines(c, body, 712).slice(0, action ? 8 : 11)) { c.fillText(line, 28, y); y += 36; }
    if (action) {
      c.fillStyle = "#ffc779"; c.font = "bold 30px system-ui";
      c.fillText(action.type === "seek" ? `Seek person #${action.target_id}?` : "Scan the room?", 28, 432);
      c.font = "26px system-ui"; c.fillText("Left trigger: confirm · X: dismiss", 28, 476);
    }
    this.voiceTexture.needsUpdate = true;
  }

  private addAssistant() {
    const parent = document.querySelector("#settings")!;
    const block = document.createElement("section");
    block.className = "local-assistant";
    block.innerHTML = `<h2>Local scene assistant</h2><form><input aria-label="Question for rover" placeholder="What do you see?" maxlength="500"/><button>Ask</button></form><p role="status">Uses observed detections; runs on rover.</p><button type="button" id="start-drill">Start 90-second rescue drill (left grip in VR)</button><p id="drill-status">${DRILL_TRAINS} A victim is a face held in view for 1 s.</p><button type="button" id="open-mission-report">Open mission report</button><div class="survivor-phrases"><select aria-label="Survivor phrase">${Object.entries(PHRASE_LABELS).map(([id, label]) => `<option value="${id}">${label}</option>`).join("")}</select><button type="button" data-lang="en">Say EN</button><button type="button" data-lang="es">Say ES</button></div><p id="phrase-status">Speaks on the rover speaker, or here if it has none.</p>`;
    parent.append(block);
    const form = block.querySelector("form")!, input = block.querySelector("input")!, result = block.querySelector("p")!, button = form.querySelector("button")!;
    block.querySelector<HTMLButtonElement>("#open-mission-report")!.onclick = () => void this.showReport();
    this.voice = new AssistantVoice(block, document.querySelector<HTMLDialogElement>("#settings")!, button,
      () => this.stop("Assistant active: release deadman"),
      reply => { result.textContent = `${reply.question ? "Heard: " + reply.question + "\n" : ""}${String(reply.answer ?? "No answer returned")}`; },
      action => {
        // A confirmed proposal still needs the held deadman; any stick input or stop cancels it.
        if (action.type === "scan") {
          this.stop("Scan confirmed: release, then hold deadman in live view");
          this.scanArmed = true;
          this.scanMessage = "Scan armed: release, then hold the deadman with sticks centered";
          result.textContent = "Scan armed: close Settings, release, then hold the deadman with sticks centered. Release or move a stick to cancel.";
          return;
        }
        this.stop(`Seek person #${action.target_id}: release, then hold deadman`);
        this.autonomy.select(action.target_id);
        result.textContent = `Seek person #${action.target_id} armed: hold the deadman to approach; move the stick to cancel.`;
      });
    const phrase = block.querySelector<HTMLSelectElement>(".survivor-phrases select")!, phraseStatus = block.querySelector<HTMLElement>("#phrase-status")!;
    for (const say of block.querySelectorAll<HTMLButtonElement>(".survivor-phrases button")) {
      say.onclick = async () => {
        say.disabled = true; phraseStatus.textContent = "Speaking…";
        phraseStatus.textContent = await speakPhrase(phrase.value, say.dataset.lang!);
        say.disabled = false;
      };
    }
    const drillButton = block.querySelector<HTMLButtonElement>("#start-drill")!;
    drillButton.onclick = () => void this.startDrill();
    this.timers.push(window.setInterval(async () => {
      if (this.stopped || !(document.querySelector<HTMLDialogElement>("#settings")!.open || this.drillHud.drill?.active)) return;
      const line = await this.drillHud.refresh();
      const label = document.querySelector("#drill-status");
      if (label && document.querySelector<HTMLDialogElement>("#settings")!.open) label.textContent = line;
    }, 1000));
    form.onsubmit = async event => {
      event.preventDefault(); if (!input.value.trim() || button.disabled) return;
      this.voice?.cancelCapture(); this.stop("Assistant open: release deadman"); button.disabled = true; result.textContent = "Asking rover…";
      const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 15000);
      try {
        const response = await fetch("/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: input.value.trim() }), signal: abort.signal });
        if (!response.ok) throw new Error(`Rover returned ${response.status}`);
        const answer = await response.json(); result.textContent = String(answer.answer ?? "No answer returned"); this.voice?.setReply(answer);
      } catch (error) { result.textContent = `Assistant unavailable: ${String(error)}`; }
      finally { window.clearTimeout(timer); button.disabled = false; }
    };
  }
  private async showReport() {
    this.voice?.cancelCapture(); this.stop("Mission report open: release deadman");
    const dialog = document.createElement("dialog"); dialog.className = "mission-report confirm";
    dialog.innerHTML = '<header><h2>Mission report</h2><button aria-label="Close mission report">Close</button></header><div class="mission-content" role="status">Loading rover evidence…</div>';
    document.body.append(dialog); dialog.showModal();
    const content = dialog.querySelector<HTMLElement>(".mission-content")!;
    dialog.querySelector("button")!.onclick = () => dialog.close();
    dialog.onclose = () => dialog.remove();
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 5000);
    try {
      const response = await fetch("/mission/report", { cache: "no-store", signal: abort.signal });
      if (!response.ok) throw new Error(`Report HTTP ${response.status}`);
      const report = await response.json();
      const encounters = Array.isArray(report.encounters) ? report.encounters.slice(-500) : [];
      const events = Array.isArray(report.events) ? report.events.slice(-500) : [];
      const sources = [...new Set(encounters.map((entry: { source?: string }) => String(entry.source ?? "unknown")))];
      const summary = document.createElement("p");
      summary.textContent = `${report.mission_id ?? "Mission"} · ${Number(report.duration_s) || 0}s · ${encounters.length} observed tracks · ${Number(report.saved_snapshots) || 0} saved snapshots · ${report.persisted ? "Saved on rover" : "In-memory evidence only"}`;
      const source = document.createElement("p"); source.textContent = `Sources: ${sources.length ? sources.join(", ") : this.frameSource}. Tracks are encounters, not verified distinct people.`;
      content.replaceChildren(summary, source);
      if (sources.includes("SIMULATED") || this.status?.simulated) {
        const label = document.createElement("p"); label.className = "mission-simulated"; label.textContent = "SIMULATED: these observations do not prove a physical rescue."; content.append(label);
      }
      if (report.drill) {
        const drill = document.createElement("p");
        const end = report.drill.summary ? endLines({ summary: report.drill.summary }) : null;
        drill.textContent = `Rescue drill: ${report.drill.active ? "running" : report.drill.remaining_s === null ? "not started" : "complete"}${end ? " · " + end.join(" · ") : ""} · ${Number(report.drill.person_encounters) || 0} person tracks`;
        content.append(drill);
      }
      const list = document.createElement("ul"); list.className = "mission-tracks";
      for (const entry of encounters.slice(-20).reverse()) {
        const item = document.createElement("li");
        item.textContent = `${String(entry.cls ?? "object")} · track ${entry.id ?? "?"} · ${Math.round((Number(entry.conf) || 0) * 100)}% · ${entry.source ?? "unknown source"}`;
        const sbar = sbarLines(entry.sbar);
        if (sbar.length) {
          const note = document.createElement("p"); note.className = "mission-sbar";
          note.textContent = `${sbar.join("\n")}\n(${entry.sbar.model === "template" ? "template" : "local vision model"}; not a medical assessment)`;
          item.append(note);
        }
        list.append(item);
      }
      if (!encounters.length) { const empty = document.createElement("p"); empty.textContent = "No detection encounters recorded."; content.append(empty); }
      else content.append(list);
      const evidence = document.createElement("div"); evidence.className = "mission-images";
      for (const event of events.filter((entry: { snapshot_url?: string }) => typeof entry.snapshot_url === "string" && /^\/mission\/snapshots\/detection-[A-Za-z0-9._-]+\.jpg$/.test(entry.snapshot_url)).slice(-6)) {
        const image = document.createElement("img"); image.src = event.snapshot_url; image.loading = "lazy";
        image.alt = `${event.box?.cls ?? "Detection"} evidence, frame ${event.frame}, ${event.source}`; evidence.append(image);
      }
      content.append(evidence);
      const download = document.createElement("button"); download.textContent = "Download report JSON";
      download.onclick = () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
        const link = document.createElement("a"); link.href = url; link.download = "farsight-mission-report.json"; link.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      };
      content.append(download);
    } catch (error) { content.textContent = `Mission report unavailable: ${String(error)}`; }
    finally { window.clearTimeout(timer); }
  }
  private drillLine(now: number) { return this.drillHud.line(now); }
  private async startDrill() {
    if (this.stopped) return;
    this.stop("Drill started: release deadman");
    const line = await this.drillHud.start();
    const label = document.querySelector("#drill-status");
    if (label) label.textContent = line;
  }
  dispose() {
    this.stop("Viewer closed"); this.stopped = true;
    this.timers.forEach(timer => { window.clearInterval(timer); window.clearTimeout(timer); });
    this.socket?.close(); this.detectionsSocket?.close(); this.request?.abort(); this.h264.stop(); this.voice?.dispose();
    window.removeEventListener("keydown", this.keyDown); window.removeEventListener("keyup", this.keyUp);
    window.removeEventListener("blur", this.blur); window.removeEventListener("click", this.click); document.removeEventListener("visibilitychange", this.visibility);
    document.removeEventListener("keydown", this.escapeStop);
    this.cameraTexture.dispose(); this.hudTexture.dispose(); this.drillTexture.dispose(); this.drillHud.canvas.remove();
    this.bannerTexture.dispose(); this.voiceTexture.dispose(); this.sourceBanner.remove();
  }
}

function wrapLines(context: CanvasRenderingContext2D, text: string, width: number) {
  const lines: string[] = [];
  for (const paragraph of text.split("\n")) {
    let line = "";
    for (const word of paragraph.split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word;
      if (line && context.measureText(next).width > width) { lines.push(line); line = word; } else line = next;
    }
    lines.push(line);
  }
  return lines;
}
