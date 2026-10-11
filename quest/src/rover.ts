import * as THREE from "three";
import { SourceTruth } from "./source-banner.js";
import { explainStop } from "./stop-reason.js";
import { ControlSafety, stick } from "./control-safety.js";
import { ForwardRamp, manualDrive, steerCurve } from "./manual-drive.js";
import { H264Viewer } from "./h264";
import { cameraCrop, croppedBox } from "./camera-view.js";
import { VRActionMenu, type VRAction } from "./vr-actions";
import { drawDownMarker, linkHealth, poseSegments, strokeSegments } from "./pose-hud.js";
import { AssistantVoice } from "./assistant-voice";
import { askText } from "./ask-label.js";
import { AutonomyIntent, autonomyLabel, containPoint, pickTarget, rayPanelPoint } from "./autonomy.js";
import { PHRASE_LABELS, sbarLines, speakPhrase } from "./survivor-phrases.js";
import { DRILL_TRAINS, DrillHud, endLines } from "./drill-hud.js";
import { DetectionCache, PACKET_FRESH_MS } from "./detection-match.js";
import { IdentityCards } from "./identity-card.js";
import { defaultH264, defaultMount, loadSettings, pageStorage, saveSettings } from "./viewer-settings.js";
import { PanSmoother } from "./pan-smoothing.js";
import { encounterLine, summaryLines, timelineLines } from "./mission-report.js";
import { floorHit, goalBody, mergeNav, navActive, navLabel, parseNav, postNav } from "./nav-view.js";
import { NavOverlay, type Nav } from "./nav-overlay";
import { frameOrder } from "./frame-order.js";
import { mapWalk } from "./map-walk.js";
import { DEMO_BLIP_MS, DEMO_MODE } from "./demo-config.js";
import { cueFor, drawChip, drawControllerHint, drawCue, motionTone } from "./vr-hud.js";
import { QuestIntercom, type IntercomState } from "./intercom.js";

/** Quest sticks drift; a slightly off-center right stick must not override Explore/Seek/go-to. */
const XR_STICK_DEADZONE = 0.25;
/** Holding right A this long opens Actions instead of switching MANUAL/AUTO on release. */
const A_HOLD_MS = 600;
/** The icon controller diagram shows this long on VR entry (or Y / Controls), then fades out. */
const HINT_MS = 8000, HINT_FADE_MS = 1000;
/** AUTO walk-through: metres per second at full stick, and one comfort snap turn per flick. */
const ROAM_SPEED = 1.5, ROAM_SNAP = Math.PI / 6;

type Box = { id?: number; cls: string; conf: number; x: number; y: number; w: number; h: number; kpts?: number[][]; down?: boolean };
type Detections = { frame: number | string; boxes: Box[]; fps?: number; source?: string; simulated?: boolean };
type ControlStatus = {
  type?: string; session_id: string; permit: string; permit_ms: number; seq?: number;
  rover: boolean; simulated?: boolean; estop?: boolean; range_cm?: number | null;
  requires_release?: boolean; reason?: string | null; link_ms?: number | null;
  mode?: string; autonomy_state?: string; target_id?: number | null;
  nav?: unknown;
};

/** Same-origin only: Quest talks through USB/ADB to the operator relay. */
export class RoverExperience {
  private sourceTruth = new SourceTruth();
  private sourceBanner: HTMLElement;
  private bannerCanvas = document.createElement("canvas");
  private bannerTexture = new THREE.CanvasTexture(this.bannerCanvas);
  private bannerPanel = new THREE.Mesh(new THREE.PlaneGeometry(1.0, 0.125), new THREE.MeshBasicMaterial({ map: this.bannerTexture, transparent: true, toneMapped: false, depthTest: false }));
  private stopCanvas = document.createElement("canvas");
  private stopTexture = new THREE.CanvasTexture(this.stopCanvas);
  private stopPanel = new THREE.Mesh(new THREE.PlaneGeometry(1.0, 0.15), new THREE.MeshBasicMaterial({ map: this.stopTexture, transparent: true, toneMapped: false, depthTest: false }));
  private stopKey = "";
  private bannerKey = "";
  private bannerPending: { source: ReturnType<SourceTruth["state"]>; mode: "manual" | "auto"; tone: string; at: number } | null = null;
  private hintCanvas = document.createElement("canvas");
  private hintTexture = new THREE.CanvasTexture(this.hintCanvas);
  private hintPanel = new THREE.Mesh(new THREE.PlaneGeometry(1.6, 1.0), new THREE.MeshBasicMaterial({ map: this.hintTexture, transparent: true, toneMapped: false, depthTest: false, depthWrite: false }));
  private hintUntil = -Infinity;
  private hintKey = "";
  /** AUTO / free roam: Explore drives the rover, the right stick only moves the operator through the map. */
  private auto = false;
  /** A mode change sends a release before autonomous heartbeats begin. */
  private autoArming = false;
  private forwardRampState?: ForwardRamp;
  /** Lazily created so prototype-built test fixtures need no extra field. */
  private get forwardRamp() { return this.forwardRampState ??= new ForwardRamp(); }
  private lastRoamAt = -Infinity;
  private roamTurnArmed = true;
  private voiceCanvas = document.createElement("canvas");
  private voiceTexture = new THREE.CanvasTexture(this.voiceCanvas);
  private voicePanel = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 0.8), new THREE.MeshBasicMaterial({ map: this.voiceTexture, transparent: true, toneMapped: false, depthTest: false }));
  private talkDown = false;
  private gate = new ControlSafety({ demo: DEMO_MODE });
  private socket: WebSocket | null = null;
  private detectionsSocket: WebSocket | null = null;
  private timers: number[] = [];
  private stopped = false;
  private frameRequests = new Map<number, { controller: AbortController; startedAt: number }>();
  private frameRequestSeq = 0;
  private lastFramePoll = -Infinity;
  private lastHealthPoll = -Infinity;
  private lastPumpAt = -Infinity;
  private frameId = "";
  private pendingFrame: { id: string | null; age: number; at: number } | null = null;
  private lastCanvasDraw = -Infinity;
  /** A real camera picture has been drawn; later gaps keep it instead of a placeholder. */
  private pictured = false;
  private inputValid = false;
  private contextLost = false;
  private frameSource = "Waiting for camera";
  private frame: HTMLImageElement | null = null;
  private detections: Detections | null = null;
  private detectionsAt = -Infinity;
  private detectionFrames = new DetectionCache();
  private identityCanvas = document.createElement("canvas");
  private identityTexture = new THREE.CanvasTexture(this.identityCanvas);
  private identityPanel = new THREE.Mesh(new THREE.PlaneGeometry(.84, .448),
    new THREE.MeshBasicMaterial({ map: this.identityTexture, transparent: true,
      toneMapped: false, depthTest: false, depthWrite: false }));
  private identityDirty = true;
  private lastIdentityPaint = -Infinity;
  private identityPaintSource = "";
  private identityCards = new IdentityCards({ onChange: () => { this.identityDirty = true; } });
  private status: ControlStatus | null = null;
  private seq = 0;
  private sent = new Map<number, number>();
  private ackMs: number | null = null;
  private decodeMs: number | null = null;
  private drillHud = new DrillHud();
  private drillTexture = new THREE.CanvasTexture(this.drillHud.canvas);
  private drillPanel = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 1.2), new THREE.MeshBasicMaterial({ map: this.drillTexture, transparent: true, toneMapped: false, depthTest: false }));
  private drillDown = false;
  private voice: AssistantVoice | null = null;
  private autonomy = new AutonomyIntent();
  private drawnBoxes: Box[] = [];
  private raycaster = new THREE.Raycaster();
  private exploreDown = false;
  private navOverlay = new NavOverlay();
  private nav: Nav | null = null;
  private navMessage = "";
  private navMessageAt = -Infinity;
  private navBusy = false;
  private goalDown = false;
  /** Trigger edge inside the actions menu: it selects, like the grip, and never drives (the menu blocks input). */
  private menuTriggerDown = false;
  private menuCloseAt = -Infinity;
  private modeFlashUntil = -Infinity;
  private screenFitX = 1;
  private screenFitY = 1;
  private cancelDown = false;
  private navStatus: HTMLElement;
  private lastSendAt = 0;
  private lastDeadman = false;
  private lastSentPermit: string | null = null;
  /** Camera evidence drawn into the canvas but not yet shown by a successful render (see afterRender). */
  private stagedView: { id: string | null; age: number; at: number } | null = null;
  private stagedRepaint = false;
  private input = { held: false, v: 0, w: 0, pan: 0 };
  private keys = new Set<string>();
  private pointerHeld = false;
  private suspended = false;
  private recenter = true;
  private baseYaw = 0;
  private panSmoother = new PanSmoother();
  private lastPanAt = 0;
  private panDt() { const now = performance.now(), dt = this.lastPanAt ? (now - this.lastPanAt) / 1000 : 0; this.lastPanAt = now; return Math.min(dt, 0.1); }
  private toggleDown = false;
  /** When an observed (unlatched) A press began: a tap toggles the view, a long hold opens Actions. */
  private aPressedAt: number | null = null;
  private recenterDown = false;
  private scanPending = false;
  private scanGeneration = 0;
  private scanReserved = false;
  private scanPendingCancelled = false;
  private scanArmed = false;
  private scanning = false;
  private scanCompleting = false;
  private scanDown = false;
  private scanMessage = "Hold A for actions; pick Scan, then hold the trigger";
  private scanPollAt = 0;
  private lastXRAt = -Infinity;
  private rightSource: XRInputSource | null = null;
  private leftSource: XRInputSource | null = null;
  private canvas = document.createElement("canvas");
  private ctx: CanvasRenderingContext2D;
  private cameraTexture: THREE.CanvasTexture;
  private cameraMaterial: THREE.MeshBasicMaterial;
  private cockpit = new THREE.Group();
  private cockpitUp = new THREE.Vector3(0, 1, 0);
  private mapTilt = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 6);
  private mapOffset = new THREE.Vector3();
  private actionMenu = new VRActionMenu();
  private pointer = new THREE.Mesh(new THREE.RingGeometry(0.008, 0.016, 20),
    new THREE.MeshBasicMaterial({ color: 0x7fe8ff, depthTest: false, depthWrite: false }));
  private message: HTMLElement;
  private details: HTMLElement;
  private holdButton: HTMLButtonElement;
  private h264: H264Viewer;
  private h264Enabled = false;
  private bondedJPEG = false;
  private roverVideoAt = -Infinity;
  private digitalPan = false;
  private settings = loadSettings();
  private mountSelect!: HTMLSelectElement;
  private videoToggle!: HTMLInputElement;
  private drawnPan = 0;
  private cameraScreen: THREE.Mesh;
  private intercomCanvas = document.createElement("canvas");
  private intercomTexture = new THREE.CanvasTexture(this.intercomCanvas);
  private intercomPanel = new THREE.Mesh(new THREE.PlaneGeometry(.9, .3), new THREE.MeshBasicMaterial({
    map: this.intercomTexture, transparent: true, toneMapped: false, depthTest: false, depthWrite: false,
  }));
  private intercomContext!: CanvasRenderingContext2D;
  private intercom!: QuestIntercom;
  private intercomButton!: HTMLButtonElement;
  private intercomStatus!: HTMLElement;
  private aimedIntercom = false;
  private xrSession: XRSession | null = null;
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
      <button id="rover-intercom" aria-pressed="false">Talk to iPhone · Off</button><p id="rover-intercom-status" role="status">Connect iPhone capture for two-way audio.</p>
      <button id="rover-hold" class="hold-button">Hold to enable · WASD to drive</button>
      <button id="rover-explore" aria-pressed="false">Explore (E)</button>
      <button id="rover-nav-cancel">Cancel nav (C)</button>
      <p id="rover-nav" class="rover-nav" role="status" hidden></p>
      <p class="rover-help">Quest: move the right joystick to drive. Press right A to switch to Autonomous; the rover explores while the joystick roams the 3D map. Press A again for Manual. Center the stick to stop manual motion. B stops the rover.<br>Desktop: hold Space + WASD in camera view; click a person to seek; E explores; double-click the map floor for go-to, C cancels. Stick/WASD takes back control.</p>`;
    document.querySelector("main")!.append(panel);
    this.sourceBanner = document.createElement("div");
    this.sourceBanner.className = "source-banner"; this.sourceBanner.setAttribute("role", "status");
    this.sourceBanner.innerHTML = '<strong></strong><span></span><p class="stop-reason" hidden><b></b><em></em></p>';
    document.querySelector("main")!.append(this.sourceBanner);
    this.message = panel.querySelector("#rover-message")!;
    this.details = panel.querySelector("#rover-details")!;
    this.intercomButton = panel.querySelector<HTMLButtonElement>("#rover-intercom")!;
    this.intercomStatus = panel.querySelector<HTMLElement>("#rover-intercom-status")!;
    this.intercom = new QuestIntercom(state => this.updateIntercomState(state));
    this.intercomButton.onclick = () => { void this.intercom.toggle(); };
    this.navStatus = panel.querySelector("#rover-nav")!;
    this.holdButton = panel.querySelector("#rover-hold")!;
    panel.querySelector<HTMLButtonElement>("#live-rover")!.onclick = () => this.setView("camera");
    panel.querySelector<HTMLButtonElement>("#rover-map")!.onclick = () => { this.stop("Map view: release deadman"); this.setView("3d"); };
    panel.querySelector<HTMLButtonElement>("#rover-stop")!.onclick = () => {
      this.stop("Stopped: release deadman", true);
      if (this.autonomy.mode === "goto" || navActive(this.nav)) void this.cancelNav("Stopped: navigation cancelled");
    };
    panel.querySelector<HTMLButtonElement>("#rover-nav-cancel")!.onclick = () => void this.cancelNav();
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
    this.renderer.domElement.addEventListener("dblclick", this.mapGoal);
    document.addEventListener("visibilitychange", this.visibility);
    document.addEventListener("keydown", this.escapeStop);

    this.cameraTexture = this.createCameraTexture();
    this.cameraMaterial = new THREE.MeshBasicMaterial({ map: this.cameraTexture, toneMapped: false, depthTest: false });
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 2.1), this.cameraMaterial);
    screen.position.set(0, 0, -2.35); screen.renderOrder = 10;
    this.drillTexture.colorSpace = THREE.SRGBColorSpace;
    this.drillPanel.position.set(2.15, 0.15, -2.1); this.drillPanel.rotation.y = -0.35; this.drillPanel.renderOrder = 12; this.drillPanel.visible = false;
    document.querySelector("#full-camera")!.append(this.drillHud.canvas); this.drillHud.canvas.hidden = true;
    this.bannerCanvas.width = 1024; this.bannerCanvas.height = 128;
    this.bannerTexture.colorSpace = THREE.SRGBColorSpace;
    this.bannerPanel.position.set(0, 1.12, -2.32); this.bannerPanel.renderOrder = 13;
    this.stopCanvas.width = 640; this.stopCanvas.height = 96;
    this.stopTexture.colorSpace = THREE.SRGBColorSpace;
    this.stopPanel.position.set(0, -0.92, -2.30); this.stopPanel.renderOrder = 13; this.stopPanel.visible = false;
    this.hintCanvas.width = 1024; this.hintCanvas.height = 640;
    this.hintTexture.colorSpace = THREE.SRGBColorSpace;
    this.hintPanel.position.set(0, 0.05, -2.2); this.hintPanel.renderOrder = 21; this.hintPanel.visible = false;
    this.voiceCanvas.width = 768; this.voiceCanvas.height = 512;
    this.voiceTexture.colorSpace = THREE.SRGBColorSpace;
    this.voicePanel.position.set(-2.15, 0.15, -2.1); this.voicePanel.rotation.y = 0.35; this.voicePanel.renderOrder = 12; this.voicePanel.visible = false;
    this.pointer.visible = false; this.pointer.renderOrder = 30;
    this.intercomCanvas.width = 640; this.intercomCanvas.height = 192;
    this.intercomContext = this.intercomCanvas.getContext("2d")!;
    this.intercomTexture.colorSpace = THREE.SRGBColorSpace;
    this.intercomPanel.position.set(.96, -.78, -2.31); this.intercomPanel.renderOrder = 14;
    this.cockpit.add(screen, this.bannerPanel, this.intercomPanel, this.pointer); this.cockpit.visible = false;
    this.cameraScreen = screen;
    this.drawIntercomPanel({ enabled: false, phoneConnected: false, status: "Enter VR or connect iPhone capture" });
    this.identityCanvas.width = 300; this.identityCanvas.height = 160;
    this.identityCanvas.hidden = true;
    this.identityCanvas.setAttribute("aria-label", "Recently recognized faces");
    Object.assign(this.identityCanvas.style, { position: "absolute", top: "12px", right: "12px",
      width: "min(300px, 42vw)", height: "auto", pointerEvents: "none" });
    document.querySelector("#full-camera")!.append(this.identityCanvas);
    this.identityTexture.colorSpace = THREE.SRGBColorSpace;
    this.identityPanel.position.set(.93, .79, .025);
    this.identityPanel.renderOrder = 15; this.identityPanel.visible = false;
    this.cameraScreen.add(this.identityPanel);
    this.scene.add(this.cockpit);
    this.h264 = new H264Viewer(() => {
      if (performance.now() - this.lastVideoAt > 60) { this.dirty = true; this.lastVideoAt = performance.now(); }
    }, () => this.stop("H.264 restarted: motion disabled"));
    const videoOption = document.createElement("label"); videoOption.className = "check";
    videoOption.innerHTML = '<input type="checkbox"/> H.264 video (experimental; requires decoded freshness)';
    document.querySelector("#settings")!.append(videoOption);
    this.videoToggle = videoOption.querySelector("input")!;
    this.videoToggle.onchange = () => this.setVideo(this.videoToggle.checked, true);
    this.renderer.xr.addEventListener("sessionstart", () => {
      this.stop("VR session started: release trigger", true);
      this.lastXRAt = -Infinity;
      this.rightSource = null; this.leftSource = null;
      this.inputValid = false; this.keys.clear(); this.pointerHeld = false;
      this.recenter = true; this.actionMenu.visible = false; this.setView("camera");
      this.auto = false; this.autoArming = false; mapWalk.setMode("overview"); mapWalk.reset(); this.lastRoamAt = -Infinity; this.showHint();
      this.intercom.connectIfNeeded();
      this.xrSession = this.renderer.xr.getSession();
      this.xrSession?.addEventListener("selectstart", this.onXRSelectStart);
      this.renderer.xr.getSession()?.addEventListener("visibilitychange", () => {
        this.suspended = this.renderer.xr.getSession()?.visibilityState !== "visible";
        if (this.suspended) { this.intercom.disable(true); this.voice?.stopListening("Headset inactive: voice question cancelled"); this.stop("Headset inactive: release trigger"); }
      });
    });
    this.renderer.xr.addEventListener("sessionend", () => {
      this.xrSession?.removeEventListener("selectstart", this.onXRSelectStart); this.xrSession = null;
      this.aimedIntercom = false; this.intercom.disconnect();
      this.voice?.stopListening("VR session ended: voice question cancelled");
      this.stop("VR session ended: release deadman", true); this.suspended = false;
      this.lastXRAt = -Infinity;
      this.rightSource = null; this.leftSource = null;
      this.inputValid = false; this.keys.clear(); this.pointerHeld = false;
      this.actionMenu.visible = false; this.pointer.visible = false;
    });
    this.renderer.domElement.addEventListener("webglcontextlost", () => { this.contextLost = true; this.stop("Display lost: release deadman"); });
    this.renderer.domElement.addEventListener("webglcontextrestored", () => { this.contextLost = false; this.stop("Display restored: release deadman"); });
    const cameraOption = document.createElement("label"); cameraOption.className = "camera-mount-option";
    cameraOption.innerHTML = '<span>Camera mounting</span><select aria-label="Camera mounting mode"><option value="servo">Camera mounted on servo</option><option value="fixed">Fixed camera · full frame</option></select><small>Fixed mode shows the entire camera image and keeps the physical pan command centered.</small>';
    document.querySelector("#settings")!.append(cameraOption);
    this.mountSelect = cameraOption.querySelector("select")!;
    this.mountSelect.onchange = () => this.setMount(this.mountSelect.value === "fixed", true);
    this.setMount(this.settings.values.mount === "fixed", false);
    this.connectControl(); this.connectDetections();
    void this.connectIdentity();
    if (this.settings.values.h264) this.setVideo(true, false);
    this.pollHealth(performance.now());
    void this.fetchFrame();
    this.addAssistant();
  }

  private keyDown = (event: KeyboardEvent) => {
    if (/INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement)?.tagName ?? "") || event.metaKey || event.ctrlKey || event.altKey || document.querySelector("dialog[open]")) return;
    if (event.code === "KeyC" && !event.repeat && !this.renderer.xr.isPresenting) { event.preventDefault(); void this.cancelNav(); return; }
    if (this.getView() !== "camera" || this.renderer.xr.isPresenting) return;
    const key = event.code;
    if (event.repeat && !this.keys.has(key)) return;
    if (["Space", "KeyW", "KeyA", "KeyS", "KeyD"].includes(key)) {
      event.preventDefault(); this.keys.add(key);
    }
    if (key === "KeyE" && !event.repeat) { event.preventDefault(); this.toggleExplore(); }
  };
  private async connectIdentity() {
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 2000);
    try {
      const response = await fetch("/phone/identity/status", { cache: "no-store", signal: abort.signal });
      if (!response.ok) return;
      const status = await response.json();
      if (!this.stopped && status.enabled && status.ready)
        this.identityCards.connect(this.endpoint("/ws/identity"));
    } catch { /* Recognition is optional; its absence never changes camera or control. */ }
    finally { window.clearTimeout(timer); }
  }
  // The 3D scene canvas sits above the camera canvas, so map window clicks onto the camera image.
  private click = (event: MouseEvent) => {
    if (this.getView() !== "camera" || this.renderer.xr.isPresenting || !(event.target instanceof HTMLCanvasElement)) return;
    const point = containPoint(event.clientX, event.clientY, this.canvas.getBoundingClientRect(), this.canvas.width, this.canvas.height);
    if (point) this.selectAt(point);
  };
  /** Desktop go-to: double-click the floor in the 3D or 2D map. */
  private mapGoal = (event: MouseEvent) => {
    if (this.renderer.xr.isPresenting || this.getView() === "camera") return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    this.raycaster.setFromCamera(new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1), this.camera);
    const point = this.mapFloorPoint();
    if (point) void this.setGoal(point);
    else this.noteNav("Go-to: double-click on the floor");
  };
  private keyUp = (event: KeyboardEvent) => { this.keys.delete(event.code); };
  private blur = () => { this.keys.clear(); this.pointerHeld = false; this.stop("Window inactive: release deadman"); };
  private visibility = () => {
    if (document.hidden && !(this.renderer.xr.isPresenting && this.renderer.xr.getSession()?.visibilityState === "visible")) {
      this.intercom?.disable(true); this.blur();
    }
  };
  /** Fixed: show the full measured frame and command pan 0. Servo: send head yaw as pan. */
  private setMount(fixed: boolean, save: boolean) {
    if (save) { this.settings.origin.mount = "saved"; saveSettings(pageStorage(), { mount: fixed ? "fixed" : "servo" }); }
    this.mountSelect.value = fixed ? "fixed" : "servo";
    if (fixed === this.digitalPan) return;
    this.digitalPan = fixed;
    this.stop("Camera mount changed: release deadman"); this.dirty = true;
  }
  /** With no query or saved mount, a rover reporting no physical pan selects fixed. */
  private applyRoverMount() {
    if (this.stopped || this.settings.origin.mount !== "default") return;
    const mount = defaultMount(this.sourceTruth.health);
    if (!mount) return;
    this.settings.origin.mount = "rover";
    this.setMount(mount === "fixed", false);
  }
  /** Prefer H.264 when the rover offers it and the operator has not chosen; bonded JPEG still wins. */
  private applyRoverVideo() {
    if (this.stopped) return;
    if (this.settings.origin.h264 === "rover" && this.h264Enabled && !this.h264.hasVerifiedFrame &&
        !this.h264.displayedFrame && performance.now() - this.roverVideoAt > 6000) {
      // Auto-selected H.264 never produced a verified frame here: fall back to JPEG for this page.
      this.settings.origin.h264 = "fallback";
      this.setVideo(false, false);
      return;
    }
    if (this.settings.origin.h264 !== "default") return;
    const h264 = defaultH264(this.sourceTruth.health);
    if (h264 === null) return;
    this.settings.origin.h264 = "rover"; this.roverVideoAt = performance.now();
    this.setVideo(h264, false);
  }
  /** Voice needs the rover's Whisper: without it hide the controls so the left trigger cannot stop the rover for a VOICE ERROR. */
  private applyVoiceAvailability() {
    if (this.stopped || !this.voice) return;
    const health = this.sourceTruth.health as { assistant?: { whisper?: boolean } } | null;
    const whisper = health?.assistant?.whisper === true;
    if (whisper === this.voice.voiceAvailable) return;
    this.voice.setAvailable(whisper);
    // The help line lost its voice hint in the two-mode controls (b7fb39e); a missing hint must not
    // abort the rest of the health handling (transport profile, video auto-selection).
    const help = document.querySelector<HTMLElement>("#rover-voice-help");
    if (help) help.hidden = !whisper;
    if (whisper && this.settings.values.voice) this.voice.restoreVR();
    this.dirty = true;
  }
  private applyTransportProfile() {
    if (this.stopped || !this.sourceTruth.health) return;
    const health = this.sourceTruth.health as { relay_transport?: { profile?: string } };
    this.bondedJPEG = health.relay_transport?.profile === "bonded-jpeg";
    this.videoToggle.disabled = this.bondedJPEG;
    this.videoToggle.title = this.bondedJPEG ? "Dual-route prototype uses redundant JPEG frames" : "";
    if (this.bondedJPEG) this.setVideo(false, false);
  }
  private setVideo(enabled: boolean, save: boolean) {
    if (enabled && this.bondedJPEG) { this.videoToggle.checked = false; return; }
    if (save) saveSettings(pageStorage(), { h264: enabled });
    this.videoToggle.checked = enabled;
    if (enabled === this.h264Enabled) return;
    this.h264Enabled = enabled;
    // Aborted pulls keep their slot until their own fetch/decode settles (see fetchFrame).
    if (this.h264Enabled) for (const request of this.frameRequests.values()) request.controller.abort();
    this.stop("Video format changed: release deadman");
    this.gate.frameAt = -Infinity; this.gate.frameAge = Infinity; this.pendingFrame = null; this.stagedView = null; this.stagedRepaint = false; this.lastCanvasDraw = -Infinity;
    if (this.h264Enabled) this.h264.start(); else this.h264.stop();
    this.dirty = true;
  }

  private escapeStop = (event: KeyboardEvent) => { if (event.key === "Escape") this.stop("Stopped: release deadman", true); };

  /** `hard`: a deliberate stop (B, MANUAL/AUTO switch, session start/end, viewer closed) that needs a real
   * trigger release even in demo mode. Every other stop is transient. */
  stop(reason: string, hard = false) {
    this.forwardRamp.reset();
    // An accepted go-to goal stays armed (see AutonomyIntent.release); motion still needs a fresh deadman press.
    this.autonomy.release();
    this.scanGeneration++; this.scanReserved = false;
    if (this.scanPending) this.scanPendingCancelled = true;
    this.scanArmed = false;
    this.scanning = false;
    this.scanCompleting = false;
    this.input = this.gate.stop(this.input, reason, hard);
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
        const newPermit = next.permit !== this.status?.permit;
        const newSession = next.session_id !== this.gate.session;
        if (this.gate.acceptStatus(next, performance.now())) {
          this.status = next;
          if (newSession) this.resetSessionView();
          this.nav = mergeNav(next.nav, this.nav, !newSession);
          // The next held heartbeat after end_scan reports release_required before most status ticks; pollScan decides complete vs aborted.
          this.observeScanStatus(next);
          if (typeof next.seq === "number" && this.sent.has(next.seq)) this.ackMs = performance.now() - this.sent.get(next.seq)!;
          if (next.requires_release) this.gate.invalidate(next.reason ?? "Release deadman to continue");
          // Spend a new permit at once while driving: waiting for the next 50 ms
          // tick costs up to a fifth of its 250 ms lease (docs/link-lab.md).
          if (newPermit && this.lastDeadman) this.pump();
        }
      } catch { this.stop("Invalid control status"); socket.close(); }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      this.gate.invalidate("Control disconnected: release deadman");
      this.status = null; this.nav = null;
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
          this.detectionFrames.add(next, this.detectionsAt);
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

  private resetSessionView() {
    for (const request of this.frameRequests.values()) request.controller.abort();
    this.pendingFrame = null; this.stagedView = null; this.stagedRepaint = false;
    this.frame = null; this.frameId = ""; this.lastCanvasDraw = -Infinity;
    this.detections = null; this.detectionFrames.clear();
    this.sent.clear(); this.lastDeadman = false; this.lastSendAt = performance.now();
    if (this.h264Enabled) { this.h264.stop(); this.h264.start(); }
    this.dirty = true;
  }

  /** Poll from the active display loop so immersive browsers cannot throttle the camera timer. */
  private pollCamera(now: number) {
    // A timed-out pull is aborted (which also cancels its decode) but keeps its slot until that stage
    // has settled and its blob URL is revoked, so at most two decoders ever exist.
    for (const request of this.frameRequests.values()) {
      if (now - request.startedAt >= 750) request.controller.abort();
    }
    if (now - this.lastFramePoll < 100 || this.frameRequests.size >= 2) return;
    this.lastFramePoll = now;
    void this.fetchFrame();
  }

  private pollHealth(now: number) {
    if (this.stopped || now - this.lastHealthPoll < 2000) return;
    this.lastHealthPoll = now;
    void this.sourceTruth.refresh().then(() => {
      if (this.stopped) return;
      this.applyRoverMount(); this.applyVoiceAvailability(); this.applyTransportProfile(); this.applyRoverVideo();
    });
  }

  private pumpControl(now: number) {
    if (now - this.lastPumpAt < 50) return;
    this.lastPumpAt = now;
    this.pump();
  }

  private async fetchFrame() {
    const session = this.renderer.xr.isPresenting ? this.renderer.xr.getSession() : null;
    const xrVisible = !!session && session.visibilityState !== "hidden";
    if (this.h264Enabled || this.frameRequests.size >= 2 || (document.hidden && !xrVisible) || this.stopped ||
        (!this.renderer.xr.isPresenting && this.getView() !== "camera")) return;
    const start = performance.now();
    const controlSession = this.gate.session;
    const requestId = this.frameRequestSeq++;
    const controller = new AbortController(); this.frameRequests.set(requestId, { controller, startedAt: start });
    const timeout = window.setTimeout(() => controller.abort(), 750);
    let url = "";
    let response: Response | undefined;
    try {
      response = await fetch("/camera.jpg", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`Camera HTTP ${response.status}`);
      const id = response.headers.get("X-Frame-Id");
      const ageHeader = response.headers.get("X-Frame-Age-Ms");
      const source = response.headers.get("X-Frame-Source") ?? "Unverified camera source";
      const serverAge = ageHeader === null || !ageHeader.trim() ? Infinity : Number(ageHeader);
      if (!Number.isFinite(serverAge) || serverAge < 0) return;
      const waitHeader = response.headers.get("X-Frame-Server-Wait-Ms");
      const reportedWait = waitHeader?.trim() ? Number(waitHeader) : NaN;
      const elapsed = performance.now() - start;
      // The server's age already includes this measured handler residence.
      // Missing/invalid metadata retains the complete request bound; network,
      // body transfer, decode and later display delay are still counted.
      const serverWait = Number.isFinite(reportedWait) && reportedWait >= 0 && reportedWait <= elapsed ? reportedWait : 0;
      const frameAge = (now: number) => serverAge + now - start - serverWait;
      // Request start order is not capture order: rank by capture ordinal within one source, and only
      // fall back to the conservative capture-age bound across sources or after a counter restart.
      const retainDisplayed = (now: number) => {
        if (!this.frame) return false;
        const order = frameOrder(source, id, this.frameSource, this.frameId);
        if (order !== 0) return order < 0;
        const retainedAge = this.pendingFrame ? this.pendingFrame.age + Math.max(0, now - this.pendingFrame.at) : this.gate.videoAge(now);
        return frameAge(now) >= retainedAge;
      };
      if (controller.signal.aborted || this.gate.session !== controlSession || retainDisplayed(performance.now())) return;
      const blob = await response.blob();
      if (blob.size > 2_000_000 || !blob.type.startsWith("image/")) throw new Error("Invalid camera image");
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(blob);
      const image = new Image(); image.src = url;
      // Abort (timeout, session reset, H.264) drops the image source, which rejects a pending decode.
      const release = () => { image.src = ""; };
      controller.signal.addEventListener("abort", release, { once: true });
      const decodeStart = performance.now();
      try { await image.decode(); } finally { controller.signal.removeEventListener("abort", release); }
      const now = performance.now();
      this.decodeMs = now - decodeStart;
      if (this.stopped || controller.signal.aborted || this.h264Enabled || this.gate.session !== controlSession ||
          retainDisplayed(now)) return;
      this.frame = image; this.frameSource = source;
      this.frameId = id ?? "";
      // Subtract only measured server residence, never the original capture age.
      this.pendingFrame = { id, age: frameAge(now), at: now };
      this.dirty = true;
    } catch {
      // Keep the last frame visible; its monotonically growing age stops motion.
    } finally {
      if (url) URL.revokeObjectURL(url);
      if (response?.body && !response.bodyUsed) void response.body.cancel().catch(() => {});
      controller.abort(); window.clearTimeout(timeout); this.frameRequests.delete(requestId);
    }
  }

  update(now: number, frame?: XRFrame) {
    const xr = this.renderer.xr.isPresenting;
    this.pollCamera(now);
    this.pollHealth(now);
    const session = this.renderer.xr.getSession();
    // Demo relaxations (demo-config.js) apply only while immersive; the desktop page keeps every guard.
    this.gate.demo = DEMO_MODE && xr;
    this.gate.handsFree = this.gate.demo && this.auto && !this.autoArming;
    if (xr && session) {
      const sources = [...session.inputSources];
      const right = sources.find(source => source.handedness === "right" && source.gamepad);
      const left = sources.find(source => source.handedness === "left" && source.gamepad);
      // All driving is on the right controller. The left one only carries optional buttons, so an
      // idle, sleeping or swapped left controller never stops the rover or cancels autonomy.
      if ((right ?? null) !== this.rightSource) {
        this.voice?.stopListening("Controller changed: voice question cancelled");
        // Demo: a re-enumerated right controller is not a stop; a missing one is (after DEMO_BLIP_MS, below).
        if (!this.gate.demo) this.stop("Controller changed: release trigger");
        this.rightSource = right ?? null;
        this.latchButtons();
      }
      if ((left ?? null) !== this.leftSource) {
        this.voice?.stopListening("Left controller changed: voice question cancelled");
        this.leftSource = left ?? null;
        this.latchLeftButtons();
      }
      const reference = this.renderer.xr.getReferenceSpace();
      let pose: XRViewerPose | null = null;
      let tracked = false;
      let leftTracked = false;
      try {
        if (session.visibilityState === "visible" && frame && reference) pose = frame.getViewerPose(reference) ?? null;
        tracked = Boolean(pose && frame && reference &&
          right?.gamepad?.connected && right.gripSpace && frame.getPose(right.gripSpace, reference));
        leftTracked = Boolean(pose && frame && reference && left?.gamepad?.connected &&
          left.gripSpace && frame.getPose(left.gripSpace, reference));
      } catch { /* An ended/inactive frame cannot provide current tracking. */ }
      if (pose) {
        const orientation = pose.transform.orientation, position = pose.transform.position;
        this.quat.set(orientation.x, orientation.y, orientation.z, orientation.w);
        this.position.set(position.x, position.y, position.z);
        if (this.recenter) this.anchorCockpit();
      }
      if (!tracked && this.gate.demo && pose && session.visibilityState === "visible" && now - this.lastXRAt < DEMO_BLIP_MS) {
        // Demo: ride out a short right-controller tracking blip without a stop or re-press. The deadman
        // state carries over, but manual v/w go to 0 until tracking returns; > DEMO_BLIP_MS still stops.
        this.input = { ...this.input, v: 0, w: 0 }; this.forwardRamp.reset();
        this.navOverlay.setReticle(null); this.pointer.visible = false;
        this.cockpit.visible = true; this.cameraScreen.visible = !this.actionMenu.visible;
        this.paint(now);
        return;
      }
      if (!tracked) {
        this.forwardRamp.reset();
        this.inputValid = false;
        this.voice?.stopListening("XR tracking lost: voice question cancelled");
        if (Number.isFinite(this.lastXRAt) || this.input.held)
          this.stop("XR tracking unavailable: release trigger");
        else this.gate.invalidate("XR tracking unavailable: release trigger");
        this.lastXRAt = -Infinity;
        this.latchButtons();
        this.navOverlay.setReticle(null);
        this.pointer.visible = false;
        // Keep the camera and stop reason visible when only a controller is lost.
        this.cockpit.visible = !!pose;
        this.cameraScreen.visible = !this.actionMenu.visible;
        this.paint(now);
        return;
      }
      if (!leftTracked) {
        this.voice?.stopListening("Left controller tracking unavailable: voice question cancelled");
        this.latchLeftButtons();
      }
      this.lastXRAt = now;
      this.updatePointer(frame, reference, right);
      const axes = right?.gamepad?.axes ?? [];
      const stickX = axes.length >= 4 ? axes[2] : axes[0], stickY = axes.length >= 4 ? axes[3] : axes[1];
      const toggle = !!right?.gamepad?.buttons[4]?.pressed;
      const stop = !!right?.gamepad?.buttons[5]?.pressed;
      // A has one meaning: switch mode on the press edge. Holding it never opens a menu.
      if (toggle && !this.toggleDown) this.setAuto(!this.auto);
      if (stop && !this.exploreDown) {
        this.setAuto(false);
        this.stop("Stopped: center the joystick", true);
      }
      this.toggleDown = toggle; this.exploreDown = stop;
      this.actionMenu.visible = false; this.pointer.visible = false;
      this.navOverlay.setReticle(null);
      if (this.autoArming && !this.gate.needsRelease && this.status && !this.status.requires_release &&
          this.socket?.readyState === WebSocket.OPEN && now - this.gate.receivedAt < 200)
        this.autoArming = false;
      // Moving the stick is the manual motion intent. Centering it releases immediately.
      // In autonomous mode the stick only moves the viewer, never the rover.
      const held = this.auto ? !this.autoArming : !!(stick(stickX, XR_STICK_DEADZONE) || stick(stickY, XR_STICK_DEADZONE));
      this.euler.setFromQuaternion(this.quat, "YXZ");
      if (this.recenter && pose) this.anchorCockpit();
      const yaw = Math.atan2(Math.sin(this.euler.y - this.baseYaw), Math.cos(this.euler.y - this.baseYaw));
      this.inputValid = true;
      if (this.auto) {
        const leftAxes = leftTracked ? left?.gamepad?.axes ?? [] : [];
        this.roamStep(now, stickX, stickY, leftAxes.length >= 4 ? leftAxes[2] : leftAxes[0],
          leftAxes.length >= 4 ? leftAxes[3] : leftAxes[1]);
      }
      this.lastRoamAt = this.auto ? now : -Infinity;
      // Forward eases in from rest (~90% after 0.3 s, full after ~2 s held); easing off or releasing is
      // immediate. Steering gets a deadzone, expo curve and straight-ahead lock.
      if (this.auto) this.forwardRamp.reset();
      const forward = this.auto ? 0 : this.forwardRamp.step(-stick(stickY, XR_STICK_DEADZONE), now);
      const drive = manualDrive(forward, steerCurve(stickX, stickY));
      this.input = { held,
        v: this.auto ? 0 : drive.v,
        w: this.auto ? 0 : drive.w,
        pan: this.digitalPan ? 0 : this.panSmoother.step(-THREE.MathUtils.radToDeg(yaw), this.panDt()) };
      if (this.auto && this.autonomy.mode === "teleop") this.autonomy.toggleExplore();

    } else {
      this.inputValid = true;
      const drive = manualDrive(Number(this.keys.has("KeyW")) - Number(this.keys.has("KeyS")),
        Number(this.keys.has("KeyD")) - Number(this.keys.has("KeyA")));
      this.input = {
        held: this.keys.has("Space") || this.pointerHeld,
        v: drive.v,
        w: drive.w,
        pan: 0,
      };
    }
    const wasGoto = this.autonomy.mode === "goto";
    if (this.autonomy.stick(this.input.v, this.input.w)) {
      this.dirty = true;
      if (wasGoto) void this.cancelNav("Stick override: go-to cancelled");
    }
    if (!xr) this.navOverlay.setReticle(null);
    this.navOverlay.update(this.nav, now);
    this.navOverlay.group.visible = !xr;
    if (this.input.v || this.input.w) this.scanArmed = false;
    this.cockpit.visible = xr;
    this.cameraScreen.visible = !this.actionMenu.visible;
    // AUTO keeps the live camera painted as a small window: it is the motion freshness source and the
    // rover's sensor provenance while the operator roams the map.
    this.placeCameraScreen();
    if (Number.isFinite(this.menuCloseAt) && now >= this.menuCloseAt) { this.actionMenu.visible = false; this.menuCloseAt = -Infinity; }
    // A mode switch briefly enlarges the chip so MANUAL/AUTO is unmistakable.
    this.bannerPanel.scale.setScalar(1);
    this.updateHint(now);
    if (this.h264Enabled) {
      this.h264.update();
    }
    this.paint(now);
  }
  /** Periodic control renewal, called by the display loop only after renderer.render() returned: a lease is
   * never renewed for a camera texture that has not reached a successful frame. Stops are sent at once by stop(). */
  afterRender(now: number) {
    // Freshness drawn into the camera canvas counts only once that canvas reached a rendered, visible frame,
    // so a permit callback can never spend provisional pixels. The 500 ms and lease limits are unchanged.
    const visible = !this.renderer.xr.isPresenting || (this.cockpit.visible && this.cameraScreen.visible);
    if (visible) {
      const staged = this.stagedView;
      if (staged && this.gate.acceptFrame(staged.id, staged.age + Math.max(0, now - staged.at), now)) this.lastCanvasDraw = now;
      if (this.stagedRepaint) this.lastCanvasDraw = now;
      this.stagedView = null; this.stagedRepaint = false;
    }
    this.pumpControl(now);
  }
  /** renderer.render() threw: nothing drawn this frame may renew freshness; stop and require a release. */
  renderFailed() {
    this.stagedView = null; this.stagedRepaint = false;
    this.stop("Display render failed: release trigger");
  }
  /** A level screen anchored on entry/recenter, so looking around works naturally. */
  private anchorCockpit() {
    this.euler.setFromQuaternion(this.quat, "YXZ");
    this.baseYaw = this.euler.y;
    this.cockpit.position.copy(this.position);
    this.cockpit.quaternion.setFromAxisAngle(this.cockpitUp, this.baseYaw);
    this.panSmoother.reset(); this.recenter = false;
  }
  private updatePointer(frame: XRFrame | undefined, reference: XRReferenceSpace | null, right: XRInputSource | undefined) {
    if (!this.aimRay(frame, reference, right)) {
      this.pointer.visible = false; this.aimedIntercom = false; this.actionMenu.hover(null); return null;
    }
    this.cockpit.updateMatrixWorld(true);
    const targets = this.actionMenu.visible ? [this.actionMenu.panel] : [this.intercomPanel, this.cameraScreen];
    const hit = this.raycaster.intersectObjects(targets, false)[0];
    this.aimedIntercom = hit?.object === this.intercomPanel;
    this.pointer.visible = !!hit;
    if (hit) { this.pointer.position.copy(this.cockpit.worldToLocal(hit.point.clone())); this.pointer.position.z += .015; }
    const action = this.actionMenu.hit(hit?.uv); this.actionMenu.hover(action);
    return action;
  }

  private onXRSelectStart = (event: XRInputSourceEvent) => {
    if (event.inputSource.handedness !== "right" || !this.aimedIntercom) return;
    void this.intercom.toggle();
  };

  private updateIntercomState(state: IntercomState) {
    this.intercomButton.textContent = `Talk to iPhone · ${state.enabled ? "On" : "Off"}`;
    this.intercomButton.setAttribute("aria-pressed", String(state.enabled));
    this.intercomStatus.textContent = state.status;
    this.drawIntercomPanel(state);
  }

  private drawIntercomPanel(state: IntercomState) {
    const c = this.intercomContext;
    if (!c) return;
    c.clearRect(0, 0, 640, 192);
    c.fillStyle = state.enabled ? "#07583e" : "#10232f";
    c.strokeStyle = state.enabled ? "#53f3ae" : state.phoneConnected ? "#58d7ef" : "#8295a0";
    c.lineWidth = 6;
    c.beginPath(); c.roundRect(4, 4, 632, 184, 26); c.fill(); c.stroke();
    c.textAlign = "center"; c.textBaseline = "middle";
    c.fillStyle = "#f4fbff"; c.font = "bold 48px system-ui";
    c.fillText(`TALK ${state.enabled ? "ON" : "OFF"}`, 320, 69);
    c.fillStyle = "#c8d8df"; c.font = "22px system-ui";
    c.fillText("QUEST MIC → iPHONE · iPHONE MIC → QUEST", 320, 118);
    c.fillStyle = state.enabled ? "#a7f2d2" : "#c8d8df"; c.font = "20px system-ui";
    c.fillText(state.status, 320, 158);
    this.intercomTexture.needsUpdate = true;
  }
  private chooseAction(action: VRAction, now: number) {
    // Flash the chosen card, then close; the stop below already latches a fresh trigger release.
    this.actionMenu.confirm(action); this.menuCloseAt = now + 250;
    this.stop("Action selected: release trigger");
    if (action === "help") this.showHint();
    else if (action === "scan") {
      this.auto = false; mapWalk.setMode("overview");
      this.autonomy.clear(); this.scanArmed = true;
      this.scanMessage = "Scan armed: center the stick, then hold the right trigger";
    } else if (action === "drill") void this.startDrill();
  }
  /** Every canvas repaint is a texture upload; spread camera, HUD and banner over separate frames.
   * Liveness and freshness timing are unchanged: the camera still repaints first, every frame it is due. */
  private paint(now: number) {
    let painted = (!this.renderer.xr.isPresenting || !this.actionMenu.visible) &&
      (this.dirty || now - this.lastCanvasDraw > 100) && this.drawCamera(now) ? 1 : 0;
    this.paintIdentity(now);
    // An overdue HUD draws even when fresh camera frames are arriving.
    if (now - this.lastHudAt > (painted ? 200 : 100)) { this.drawHUD(now); this.lastHudAt = now; painted++; }
    if (this.bannerPending && (!painted || now - this.bannerPending.at > 100)) this.drawBanner();
  }
  private paintIdentity(now: number) {
    if (this.renderer.xr.isPresenting) { this.identityPanel.visible = false; this.identityCanvas.hidden = true; return; }
    if (!this.identityCanvas || (!this.identityDirty && this.identityPaintSource === this.frameSource
        && now - this.lastIdentityPaint < 100)) return;
    const context = this.identityCanvas.getContext("2d")!;
    context.clearRect(0, 0, 300, 160);
    const count = this.identityCards.draw(context, { now, source: this.frameSource, x: 0, y: 0, width: 300 });
    // This layer expires even when H.264 cannot confirm another decoded frame.
    // Updating identity pixels never marks camera presentation or renews its age.
    if (count || this.identityPanel.visible || this.identityDirty) this.identityTexture.needsUpdate = true;
    this.identityPanel.visible = count > 0;
    this.identityCanvas.hidden = count === 0;
    this.identityDirty = false; this.lastIdentityPaint = now;
    this.identityPaintSource = this.frameSource;
  }
  /** A tracked controller ray onto the camera panel, in canvas-normalized coordinates. */
  private panelPoint(frame: XRFrame | undefined, reference: XRReferenceSpace | null, right: XRInputSource | undefined) {
    if (!this.aimRay(frame, reference, right)) return null;
    this.cockpit.updateMatrixWorld(true);
    return rayPanelPoint(this.raycaster.intersectObject(this.cameraScreen, false)[0]?.uv ?? null);
  }
  /** A tracked controller ray onto the map floor, in map {x, z}. */
  private aimFloor(frame: XRFrame | undefined, reference: XRReferenceSpace | null, right: XRInputSource | undefined) {
    if (!this.aimRay(frame, reference, right)) return null;
    return this.mapFloorPoint();
  }
  /** Where the raycaster's ray meets the nav layer's floor (y = 0 in map coordinates). */
  private mapFloorPoint() {
    const layer = this.navOverlay.group;
    layer.updateWorldMatrix(true, false);
    const inverse = layer.matrixWorld.clone().invert();
    const origin = this.raycaster.ray.origin.clone().applyMatrix4(inverse);
    const direction = this.raycaster.ray.direction.clone().transformDirection(inverse);
    return floorHit(origin, direction);
  }
  private aimRay(frame: XRFrame | undefined, reference: XRReferenceSpace | null, right: XRInputSource | undefined) {
    let ray: XRPose | null = null;
    try { if (frame && reference && right) ray = frame.getPose(right.targetRaySpace, reference) ?? null; } catch { /* Fall back to gaze. */ }
    if (ray?.transform) {
      const o = ray.transform.orientation, p = ray.transform.position;
      this.raycaster.ray.origin.set(p.x, p.y, p.z);
      this.raycaster.ray.direction.set(0, 0, -1).applyQuaternion(new THREE.Quaternion(o.x, o.y, o.z, o.w));
      return true;
    } else {
      this.raycaster.ray.origin.copy(this.position);
      this.raycaster.ray.direction.set(0, 0, -1).applyQuaternion(this.quat);
      return false;
    }
  }
  private selectAt(point: { x: number; y: number }) {
    const box = pickTarget(this.drawnBoxes, point.x, point.y);
    if (!box) return false;
    if (this.scanning || this.scanPending) this.stop("Scan cancelled: Seek selected");
    if (!this.autonomy.select(box.id!)) return false;
    this.dirty = true;
    return true;
  }
  /** POST /nav/goal; only a 202 arms go-to heartbeats. The deadman still gates every motion. */
  private async setGoal(point: { x: number; z: number }) {
    if (this.navBusy) return;
    this.navBusy = true;
    const controllerSession = this.status?.session_id, controllerSocket = this.socket;
    const body = goalBody(point);
    this.noteNav(`Sending go-to (${body.x.toFixed(2)}, ${body.z.toFixed(2)})…`);
    try {
      const result = await postNav("/nav/goal", body);
      if (this.stopped || this.status?.session_id !== controllerSession || this.socket !== controllerSocket) return;
      if (result.nav) this.nav = result.nav;
      if (!result.ok) { this.noteNav(`Go-to rejected: ${result.error}`); return; }
      if (this.scanning || this.scanPending) this.stop("Scan cancelled: go-to selected");
      this.autonomy.goto();
      this.noteNav(this.renderer.xr.isPresenting ? "Go-to set: A for live view, then hold the right trigger" : "Go-to set: Live rover, then hold Space");
    } finally { this.navBusy = false; }
  }
  /** Disarm go-to locally first (fail toward teleop), then POST /nav/cancel. */
  private async cancelNav(reason = "Navigation cancelled") {
    const controllerSession = this.status?.session_id, controllerSocket = this.socket;
    if (this.autonomy.mode === "goto") this.autonomy.clear();
    this.dirty = true;
    const result = await postNav("/nav/cancel");
    if (this.stopped || this.status?.session_id !== controllerSession || this.socket !== controllerSocket) return;
    if (result.nav) this.nav = result.nav;
    this.noteNav(result.ok ? reason : `${reason} locally; server: ${result.error}`);
  }
  private noteNav(message: string) { this.navMessage = message; this.navMessageAt = performance.now(); this.dirty = true; }
  get navLayer() { return this.navOverlay.group; }
  placeMapOverview(group: THREE.Group, center: THREE.Vector3, span: number) {
    if (!this.renderer.xr.isPresenting) {
      group.position.set(0, 0, 0); group.quaternion.identity(); group.scale.setScalar(1); return;
    }
    const scale = Math.min(.25, 1.8 / Math.max(span, 2));
    group.scale.setScalar(scale); group.quaternion.copy(this.cockpit.quaternion).multiply(this.mapTilt);
    this.mapOffset.set(0, -.75, -2.9).applyQuaternion(this.cockpit.quaternion).add(this.cockpit.position);
    group.position.set(-center.x * scale, 0, -center.z * scale)
      .applyQuaternion(group.quaternion).add(this.mapOffset);
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
  get vrCameraView() { return this.renderer.xr.isPresenting && !this.auto; }
  /** AUTO walk-through state for diagnostics/tests; main.ts renders mapWalk itself. */
  get freeRoam() { return { active: this.renderer.xr.isPresenting && this.auto, x: mapWalk.x, z: mapWalk.z, yaw: mapWalk.yaw }; }
  /** AUTO locomotion on mapWalk: stick forward/back walks along the gaze, a sideways flick snap-turns. */
  private roamStep(now: number, stickX: number, stickY: number, leftX = 0, leftY = 0) {
    const dt = Number.isFinite(this.lastRoamAt) ? Math.min(0.1, Math.max(0, (now - this.lastRoamAt) / 1000)) : 0;
    const strafe = stick(leftX, XR_STICK_DEADZONE), leftForward = -stick(leftY, XR_STICK_DEADZONE);
    const forward = leftForward || -stick(stickY, XR_STICK_DEADZONE), side = stick(stickX, XR_STICK_DEADZONE);
    const length = Math.max(1, Math.hypot(forward, strafe));
    let turn = 0;
    if (!side) this.roamTurnArmed = true;
    else if (this.roamTurnArmed && Math.abs(side) > 0.5) { turn = -Math.sign(side) * ROAM_SNAP; this.roamTurnArmed = false; }
    mapWalk.moveViewer(forward / length * ROAM_SPEED * dt, strafe / length * ROAM_SPEED * dt, 0, turn);
  }
  /** A switches between direct joystick driving and autonomous exploration with map roaming.
   * Returning to manual requires centering the joystick before it can drive the rover. */
  private setAuto(auto: boolean) {
    if (auto === this.auto) return;
    this.actionMenu.visible = false; this.auto = auto; this.dirty = true; this.modeFlashUntil = performance.now() + 1500;
    mapWalk.setMode(auto ? "walk" : "overview"); this.lastRoamAt = -Infinity; this.roamTurnArmed = true;
    // Send a release during the transition; AUTO starts only after the release latch clears.
    this.autoArming = auto;
    this.stop(auto ? "Autonomous: arming" : "Manual: center the joystick", true);
    this.autonomy.clear();
    if (!auto && navActive(this.nav)) void this.cancelNav("Manual: navigation cancelled");
    if (auto) this.autonomy.toggleExplore();
  }
  /** Full camera view in MANUAL; a small upper-right window over the map in AUTO. */
  private get cameraWindowed() { return !!this.renderer?.xr.isPresenting && this.auto; }
  private placeCameraScreen() {
    const windowed = this.cameraWindowed, k = windowed ? 0.3 : 1;
    this.cameraScreen.scale.set(this.screenFitX * k, this.screenFitY * k, 1);
    this.cameraScreen.position.set(windowed ? 0.95 : 0, windowed ? 0.7 : 0, windowed ? -2.2 : -2.35);
    this.intercomPanel.position.set(windowed ? 0.95 : .96, windowed ? .43 : -.78, windowed ? -2.16 : -2.31);
  }
  private showHint(visible = true) { this.hintUntil = visible ? performance.now() + HINT_MS : -Infinity; }
  private updateHint(_now: number) { this.hintPanel.visible = false; }

  get deadmanHeld() { return this.input.held; }
  /** Newest detections packet and when its frame arrived, for 3D person markers (display only). */
  get latestDetections(): { packet: Detections | null; at: number } { return { packet: this.detections, at: this.detectionsAt }; }
  get videoStreaming() { return this.h264Enabled; }
  /** Head-locked panel beside the camera, e.g. the diagnostics overlay. */
  addCockpitPanel(panel: THREE.Object3D) { panel.visible = false; }
  /** Read-only figures for the diagnostics overlay. */
  diagnostics() {
    const now = performance.now();
    return { source: this.sourceTruth.state({ now, frameSource: this.frameSource, frameId: this.frameId, videoAgeMs: this.gate.videoAge(now) }).kind,
      video: this.h264Enabled ? "H.264" : "JPEG", videoAgeMs: this.gate.videoAge(now),
      decodeMs: this.h264Enabled ? this.h264.decodeMs : this.decodeMs, ackMs: this.ackMs, linkMs: this.status?.link_ms, xr: this.renderer.xr.isPresenting,
      // Per-hop H.264 latency: server capture-to-send age, player lead behind newest data, live-edge corrections.
      h264Latency: this.h264Enabled ? { ...this.h264.latency } : null };
  }

  private pump() {
    const now = performance.now();
    const connected = this.socket?.readyState === WebSocket.OPEN;
    // XR motion needs the live camera painted (full view in MANUAL, the window in AUTO) and no menu in front.
    const contextActive = this.renderer.xr.isPresenting ? !this.actionMenu.visible && !this.suspended : this.getView() === "camera" && !document.querySelector("dialog[open]");
    const displayVisible = this.renderer.xr.isPresenting ? this.renderer.xr.getSession()?.visibilityState === "visible" : !document.hidden;
    const available = this.inputValid && contextActive && displayVisible && !this.contextLost && (this.gate.demo || now - this.lastCanvasDraw < 250);
    // No motion while the microphone is requested or open; release is required afterwards.
    if (this.voice?.capturing) this.gate.invalidate("Voice question: release trigger");
    const moving = this.gate.decide(this.input.held, now, connected, available,
      this.renderer.xr.isPresenting ? this.lastXRAt : undefined);
    // Any tick that does not move restarts the forward ease-in, so resuming never jumps to full.
    if (!moving) this.forwardRamp.reset();
    // Never add another command behind buffered traffic. Reset the session instead.
    if (connected && this.socket!.bufferedAmount > 0) {
      // A permit callback and the 50 ms timer can run in the same browser turn.
      // Coalesce that fresh handoff instead of treating one packet as a stall.
      // Unsafe motion still closes immediately; nothing is queued for replay.
      if (now - this.lastSendAt < 50 && (!this.lastDeadman || moving)) return;
      this.gate.invalidate("Control queue stalled: release deadman"); this.socket!.close(); return;
    }
    // Current input, display and permit evidence above decide renewal after a
    // scheduling gap. A historical send gap alone must not invent a release;
    // the rover independently enforces its original permit/command deadline.
    if ((this.scanning || this.scanPending) && this.scanCompleting) {
      this.send(false);
      if (now - this.scanPollAt > 250) { this.scanPollAt = now; void this.pollScan(); }
      return;
    }
    if (this.scanPending) {
      if (this.scanPendingCancelled) { this.send(false); return; }
      if (!moving || this.input.v || this.input.w) {
        this.scanMessage = "Scan cancelled: release, stale view, or stick override";
        this.stop(this.scanMessage);
        return;
      }
      // POST delivery may outlast a lease. A scan-mode status proves the server
      // reserved the scan even while its HTTP response is still in flight.
      this.send(moving && !this.gate.needsRelease);
      return;
    }
    if (this.scanning && (!moving || this.input.v || this.input.w)) {
      this.scanMessage = "Scan cancelled: release, stale view, or stick override";
      this.stop(this.scanMessage);
    }
    // Demo AUTO hold: keep Explore heartbeats while the last permit may still be valid, then wait
    // for a fresh one without sending a release (B or leaving AUTO is the only stop).
    if (this.gate.holding && !this.gate.permitLive(now)) return;
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
    if (!this.status || typeof this.status.session_id !== "string" || this.status.session_id !== this.gate.session) return;
    // A permit callback and the display-loop pump may both fire within one 50 ms tick: one held
    // command per permit per tick. A release or STOP (deadman false) is never delayed.
    if (deadman && this.lastDeadman && this.status.permit === this.lastSentPermit && performance.now() - this.lastSendAt < 50) return;
    const age = this.gate.videoAge(performance.now());
    const seq = this.seq++;
    this.sent.set(seq, performance.now());
    if (this.sent.size > 100) this.sent.delete(this.sent.keys().next().value!);
    this.socket.send(JSON.stringify({
      seq, session_id: this.status?.session_id ?? null, permit: this.status?.permit ?? null,
      nav_lane: "revision",
      deadman, v: deadman ? this.input.v : 0, w: deadman ? this.input.w : 0,
      pan: this.digitalPan ? 0 : this.input.pan, video_fresh: age < 500,
      ...((this.scanning || (this.scanPending && this.status?.mode === "scan")) ? { mode: "scan" } : this.autonomy.fields(deadman)),
      video_age_ms: Number.isFinite(age) ? Math.ceil(age) : 999999,
    }));
    this.lastSendAt = performance.now();
    this.lastDeadman = deadman; this.lastSentPermit = this.status.permit;
  }
  private observeScanStatus(next: ControlStatus) {
    if (this.scanPending && !this.scanPendingCancelled && next.mode === "scan") this.scanReserved = true;
    if ((this.scanning || (this.scanPending && !this.scanPendingCancelled && this.scanReserved)) &&
        next.requires_release && (next.reason === "scan_finished" || next.reason === "release_required")) this.scanCompleting = true;
  }
  private async startScan() {
    if (this.scanPending || this.scanning) return;
    if (this.autonomy.mode !== "teleop" || !this.input.held || this.input.v || this.input.w || this.gate.needsRelease ||
        this.gate.readiness(performance.now(), this.socket?.readyState === WebSocket.OPEN)) {
      this.scanMessage = "Scan: stop autonomy first; hold deadman with sticks centered";
      return;
    }
    this.send(true);
    const generation = ++this.scanGeneration;
    this.scanReserved = false; this.scanCompleting = false;
    this.scanPendingCancelled = false;
    this.scanPending = true;
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 1000);
    try {
      const response = await fetch("/scan", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: this.status?.session_id, permit: this.status?.permit }), signal: abort.signal });
      if (!response.ok) throw new Error(String((await response.json()).detail ?? response.status));
      const result = await response.json();
      if (generation !== this.scanGeneration || this.scanPendingCancelled) return; // a late reply cannot rearm a cancelled reservation
      this.scanning = true;
      this.scanMessage = `${result.simulated ? "SIMULATED · " : ""}Scanning${result.approximate ? " · approximate rover turn" : " · physical pan"}; keep holding deadman`;
    } catch (error) {
      if (generation !== this.scanGeneration) return;
      this.scanMessage = `Scan unavailable: ${String(error)}`; this.stop(this.scanMessage);
    }
    finally { this.scanPending = false; window.clearTimeout(timer); }
  }
  private async pollScan() {
    if (this.scanPending && !this.scanReserved) return;
    const generation = this.scanGeneration;
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 1000);
    try {
      const response = await fetch("/scan", { cache: "no-store", signal: abort.signal });
      if (!response.ok) throw new Error(`Scan status ${response.status}`);
      const result = await response.json();
      if (generation !== this.scanGeneration || (!this.scanning && !this.scanPending) || result.running) return;
      this.scanMessage = result.state === "complete" ?
        `${result.points.toLocaleString()} points · ${(result.scan_ms / 1000).toFixed(1)} s${result.approximate ? " · approximate" : ""}` : `Scan aborted: ${result.error}`;
      this.stop(this.scanMessage);
      if (result.state === "complete" && !this.renderer.xr.isPresenting) this.setView("3d");
    } catch (error) {
      if (generation !== this.scanGeneration) return;
      this.scanMessage = String(error); this.stop("Scan status unavailable");
    }
    finally { window.clearTimeout(timer); }
  }
  private drawCamera(now: number) {
    const context = this.ctx;
    if (this.h264Enabled && this.h264.video.readyState >= 2) {
      const decoded = this.h264.displayedFrame;
      // Metadata can be ahead of presentation. Retain the validated canvas
      // and its boxes until the decoder confirms the next displayed identity.
      // Keep dirty so the next RAF retries; retained pixels never renew age.
      if (!decoded) return false;
      const video = this.h264.video;
      this.resizeCameraCanvas(video.videoWidth, video.videoHeight);
      const crop = cameraCrop(video.videoWidth, video.videoHeight, this.input.pan, this.digitalPan);
      context.drawImage(video, crop.x, crop.y, crop.width, crop.height, 0, 0, this.canvas.width, this.canvas.height);
      this.pictured = true;
      if (decoded) {
        this.stagedView = { id: decoded.id, age: decoded.age, at: now };
        this.frameSource = decoded.source; this.frameId = decoded.frame === null ? "" : String(decoded.frame);
        this.drawDetections(crop, video.videoWidth, video.videoHeight, now);
      }
    } else if (!this.h264Enabled && this.frame) {
      this.resizeCameraCanvas(this.frame.naturalWidth, this.frame.naturalHeight);
      const crop = cameraCrop(this.frame.naturalWidth, this.frame.naturalHeight, this.input.pan, this.digitalPan);
      context.drawImage(this.frame, crop.x, crop.y, crop.width, crop.height, 0, 0, this.canvas.width, this.canvas.height);
      this.pictured = true;
      if (this.pendingFrame) {
        const pending = this.pendingFrame;
        this.stagedView = { id: pending.id, age: pending.age + now - pending.at, at: now };
        this.pendingFrame = null;
      }
      // Repainting a retained frame proves display-loop liveness, while its
      // original capture age continues growing; duplicate IDs never renew it.
      if (this.gate.frameId === this.frameId) this.stagedRepaint = true;
      this.drawDetections(crop, this.frame.naturalWidth, this.frame.naturalHeight, now);
    } else if (this.pictured) {
      // A decoder restart or a gap between frames keeps the last picture on screen; its
      // age keeps growing and the stale badge says so. Never swap in a full blocking panel.
      return false;
    } else {
      context.fillStyle = "#141c22"; context.fillRect(0, 0, this.canvas.width, this.canvas.height);
      context.fillStyle = "#dce5ee"; context.font = "22px system-ui"; context.textAlign = "center";
      context.fillText("Waiting for rover camera", this.canvas.width / 2, this.canvas.height / 2);
      context.textAlign = "left";
    }
    this.drawnPan = this.input.pan;
    const aspect = this.canvas.width / this.canvas.height;
    const width = Math.min(3, 1.9 * aspect), height = width / aspect;
    this.screenFitX = width / 2.8; this.screenFitY = height / 2.1;
    const k = this.cameraWindowed ? 0.3 : 1;
    this.cameraScreen.scale.x = this.screenFitX * k; this.cameraScreen.scale.y = this.screenFitY * k;
    this.cameraTexture.needsUpdate = true; this.dirty = false;
    return true;
  }
  /** Nearest same-source packet within a few frames of the displayed one; see detection-match.js. */
  private matchingDetections(now: number): { packet: Detections; lag: number; alpha: number } | null {
    return this.detectionFrames.match(this.frameSource, this.frameId, now);
  }
  private drawDetections(crop: ReturnType<typeof cameraCrop>, width: number, height: number, now: number) {
    const matched = this.matchingDetections(now);
    this.drawnBoxes = [];
    if (!matched) return;
    const { packet, alpha } = matched;
    const context = this.ctx;
    // Boxes from a nearby, not exact, frame fade with frame distance.
    context.save(); context.globalAlpha = alpha;
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
    context.restore();
  }
  /** Held buttons from a lost or replaced controller act only after an observed release. */
  private latchButtons() {
    this.aPressedAt = null;
    this.toggleDown = this.exploreDown = this.goalDown = this.cancelDown = this.menuTriggerDown = true;
    this.latchLeftButtons();
  }
  private latchLeftButtons() {
    this.recenterDown = this.scanDown = this.drillDown = this.talkDown = true;
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
    const detection = matching ? `${matching.packet.boxes.length} detections${matching.lag ? ` (${Math.abs(matching.lag)} frame${Math.abs(matching.lag) === 1 ? "" : "s"} ${matching.lag > 0 ? "old" : "ahead"})` : ""}` : !this.detections ? "detector unavailable" : now - this.detectionsAt > PACKET_FRESH_MS ? "detections stale" : "detections awaiting matching frame";
    const link = linkHealth({ now, receivedAt: this.gate.receivedAt, connected: this.socket?.readyState === WebSocket.OPEN,
      linkMs: this.status?.link_ms, detFps: this.detections?.fps });
    const drill = this.drillLine(now);
    const intent = this.autonomy.mode === "seek" ? `Seek #${this.autonomy.targetId} armed` : this.autonomy.mode === "explore" ? "Explore armed" : "";
    const autonomy = [this.autonomy.mode === "goto" ? "Go-to armed" : intent, autonomyLabel(this.status)].filter(Boolean).join(" → ");
    const navText = [navLabel(this.nav), now - this.navMessageAt < 8000 ? this.navMessage : ""].filter(Boolean).join(" · ");
    this.navStatus.hidden = !navText;
    this.navStatus.textContent = navText;
    this.navStatus.dataset.state = this.nav?.state ?? "none";
    this.navStatus.dataset.backend = this.nav?.backend ?? "none";
    this.navStatus.dataset.pathPoints = String(this.navOverlay.path.visible ? this.navOverlay.path.userData.points : 0);
    const panMode = this.digitalPan ? "Fixed camera · full frame" : `Servo pan ${Math.round(this.input.pan)}°`;
    const detail = `${link.text} · ${this.frameSource} · ${panMode} · ${this.h264Enabled ? "H.264" : "JPEG"} video ${age} · ${ack} · ${range} · ${detection}${drill ? " · " + drill : ""}${this.h264Enabled && !this.h264.displayedFrame ? " · " + this.h264.state : ""} · ${this.scanMessage}`;
    this.message.textContent = headline;
    this.message.dataset.state = moving ? "moving" : ready ? "unsafe" : "ready";
    this.details.textContent = `${autonomy ? autonomy + " · " : ""}${navText ? navText + " · " : ""}${detail}${this.status?.estop ? " · FORWARD BLOCKED" : ""}`;
    const exploreButton = document.querySelector<HTMLButtonElement>("#rover-explore");
    exploreButton?.setAttribute("aria-pressed", String(this.autonomy.mode === "explore"));
    this.holdButton.classList.toggle("engaged", moving);
    const stop = explainStop({ unsafe: ready, reason: this.gate.reason, latched: this.gate.needsRelease, cause: this.gate.cause, status: this.status,
      xr: this.renderer.xr.isPresenting, held: this.input.held, autonomyArmed: this.autonomy.mode !== "teleop",
      detectorStale: !this.detections || now - this.detectionsAt > 500 });
    // Demo mode lifts the client stale-camera block, so stale video becomes a warning instead of a stop.
    const cue = cueFor(stop, !!ready) ?? (this.gate.demo && videoAge >= 500 ? { tone: "wait" as const, icon: "camera", words: "VIDEO STALE" } : null);
    this.drawSource(source, now, motionTone({ moving, unsafe: !!ready, cue }));
    this.drawStop(stop, cue);
    this.drawVoice();
    const drawn = this.drillHud.draw(now);
    this.drillPanel.visible = false; this.drillHud.canvas.hidden = !drawn;
    if (drawn) this.drillTexture.needsUpdate = true;
  }

  private drawSource(source: ReturnType<SourceTruth["state"]>, now: number, tone: string) {
    this.sourceBanner.dataset.kind = source.kind;
    this.sourceBanner.querySelector("strong")!.textContent = source.title;
    this.sourceBanner.querySelector("span")!.textContent = source.detail;
    // The headset chip repaints on a later frame than the HUD (see paint), and only when it changed.
    const mode = this.auto ? "auto" : "manual";
    const key = `${source.kind}|${mode}|${tone}`;
    if (key === this.bannerKey) { this.bannerPending = null; return; }
    if (!this.bannerPending) this.bannerPending = { source, mode, tone, at: now };
    else Object.assign(this.bannerPending, { source, mode, tone });
  }
  /** Tiny headset chip: motion tone, MANUAL/AUTO and the truthful source label. */
  private drawBanner() {
    const { source, mode, tone } = this.bannerPending!;
    this.bannerPending = null;
    this.bannerKey = `${source.kind}|${mode}|${tone}`;
    drawChip(this.bannerCanvas.getContext("2d")!, this.bannerCanvas.width, this.bannerCanvas.height, { mode, source: source.kind, tone });
    this.bannerTexture.needsUpdate = true;
  }
  /** Full cause and recovery on the 2D page; one icon and 1-3 words in the headset, only when relevant. */
  private drawStop(stop: ReturnType<typeof explainStop>, cue: ReturnType<typeof cueFor>) {
    this.stopPanel.visible = false;
    const key = `${stop ? `${stop.tone}|${stop.cause}|${stop.recovery}` : ""}|${cue ? `${cue.tone}|${cue.icon}|${cue.words}` : ""}`;
    if (key === this.stopKey) return;
    this.stopKey = key;
    const line = this.sourceBanner.querySelector<HTMLElement>(".stop-reason")!;
    line.hidden = !stop;
    if (stop) {
      line.dataset.tone = stop.tone;
      line.querySelector("b")!.textContent = stop.cause;
      line.querySelector("em")!.textContent = stop.recovery;
    }
    drawCue(this.stopCanvas.getContext("2d")!, this.stopCanvas.width, this.stopCanvas.height, cue);
    this.stopTexture.needsUpdate = true;
  }
  private drawVoice() { this.voicePanel.visible = false; }
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
      reply => { result.textContent = `${reply.question ? "Heard: " + reply.question + "\n" : ""}${askText(reply)}`; },
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
    this.voice.onVREnabled = () => saveSettings(pageStorage(), { voice: true });
    this.voice.setAvailable(false);  // until /health reports the rover's Whisper
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
        const answer = await response.json(); result.textContent = askText(answer); this.voice?.setReply(answer);
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
      const evidenceLines = summaryLines(report);
      if (evidenceLines.length) {
        const telemetry = document.createElement("ul"); telemetry.className = "mission-summary";
        for (const line of evidenceLines) { const item = document.createElement("li"); item.textContent = line; telemetry.append(item); }
        content.append(telemetry);
      }
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
        item.textContent = encounterLine(entry, report.started_at);
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
      const flagged = timelineLines(report);
      if (flagged.length) {
        const heading = document.createElement("h3"); heading.textContent = "Timeline";
        const timeline = document.createElement("ol"); timeline.className = "mission-timeline";
        for (const line of flagged) { const item = document.createElement("li"); item.textContent = line; timeline.append(item); }
        content.append(heading, timeline);
      }
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
    this.stop("Viewer closed", true); this.stopped = true;
    this.intercom.dispose();
    this.timers.forEach(timer => { window.clearInterval(timer); window.clearTimeout(timer); });
    this.socket?.close(); this.detectionsSocket?.close();
    this.identityCards.close();
    this.identityCanvas.remove(); this.identityTexture.dispose();
    this.identityPanel.geometry.dispose(); (this.identityPanel.material as THREE.Material).dispose();
    for (const request of this.frameRequests.values()) request.controller.abort();
    this.frameRequests.clear(); this.h264.stop(); this.voice?.dispose();
    window.removeEventListener("keydown", this.keyDown); window.removeEventListener("keyup", this.keyUp);
    window.removeEventListener("blur", this.blur); window.removeEventListener("click", this.click);
    this.renderer.domElement.removeEventListener("dblclick", this.mapGoal); this.navOverlay.dispose(); document.removeEventListener("visibilitychange", this.visibility);
    document.removeEventListener("keydown", this.escapeStop);
    this.cameraTexture.dispose(); this.intercomPanel.geometry.dispose(); this.intercomTexture.dispose(); (this.intercomPanel.material as THREE.Material).dispose(); this.hintTexture.dispose(); this.hintPanel.geometry.dispose(); (this.hintPanel.material as THREE.Material).dispose(); this.drillTexture.dispose(); this.drillHud.canvas.remove();
    this.bannerTexture.dispose(); this.stopTexture.dispose(); this.voiceTexture.dispose(); this.sourceBanner.remove();
    this.actionMenu.dispose(); this.pointer.geometry.dispose(); this.pointer.material.dispose();
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
