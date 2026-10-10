import * as THREE from "three";
import { ControlSafety, stick } from "./control-safety.js";
import { H264Viewer } from "./h264";
import { cameraCrop, croppedBox } from "./camera-view.js";
import { AssistantVoice } from "./assistant-voice";

type Box = { id?: number; cls: string; conf: number; x: number; y: number; w: number; h: number };
type Detections = { frame: number | string; boxes: Box[]; fps?: number; source?: string; simulated?: boolean };
type ControlStatus = {
  type?: string; session_id: string; permit: string; permit_ms: number; seq?: number;
  rover: boolean; simulated?: boolean; estop?: boolean; range_cm?: number | null;
  requires_release?: boolean; reason?: string | null; link_ms?: number | null;
};

/** Same-origin only: Quest talks through USB/ADB to the operator relay. */
export class RoverExperience {
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
  private drill: { active: boolean; remaining_s: number | null; person_encounters: number } | null = null;
  private drillAt = -Infinity;
  private drillBusy = false;
  private voice: AssistantVoice | null = null;
  private seekTarget: number | null = null;
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
  private canvas = document.createElement("canvas");
  private ctx: CanvasRenderingContext2D;
  private hudCanvas = document.createElement("canvas");
  private hudContext: CanvasRenderingContext2D;
  private hudTexture: THREE.CanvasTexture;
  private cameraTexture: THREE.CanvasTexture;
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
    panel.innerHTML = `<div class="rover-tabs"><button id="live-rover">Live rover</button><button id="rover-map">3D map</button><button id="rover-stop">STOP</button></div>
      <p id="rover-message" role="status">Waiting for live camera and rover</p>
      <p id="rover-details">Motion disabled until camera and control are fresh.</p>
      <button id="rover-hold" class="hold-button">Hold to enable · WASD to drive</button>
      <p class="rover-help">Quest: hold right trigger + left stick. A: camera/map. B: center pan.<br>Desktop: hold Space + WASD in camera view. Release to stop.</p>`;
    document.querySelector("main")!.append(panel);
    this.message = panel.querySelector("#rover-message")!;
    this.details = panel.querySelector("#rover-details")!;
    this.holdButton = panel.querySelector("#rover-hold")!;
    panel.querySelector<HTMLButtonElement>("#live-rover")!.onclick = () => this.setView("camera");
    panel.querySelector<HTMLButtonElement>("#rover-map")!.onclick = () => { this.stop("Map view: release deadman"); this.setView("3d"); };
    panel.querySelector<HTMLButtonElement>("#rover-stop")!.onclick = () => this.stop("Stopped: release deadman");
    this.holdButton.onpointerdown = event => {
      this.holdButton.setPointerCapture(event.pointerId);
      this.pointerHeld = true;
    };
    this.holdButton.onpointerup = this.holdButton.onpointercancel = () => { this.pointerHeld = false; };
    this.holdButton.onlostpointercapture = () => { this.pointerHeld = false; };
    window.addEventListener("keydown", this.keyDown);
    window.addEventListener("keyup", this.keyUp);
    window.addEventListener("blur", this.blur);
    document.addEventListener("visibilitychange", this.visibility);
    document.addEventListener("keydown", this.escapeStop);

    this.hudCanvas.width = 1024; this.hudCanvas.height = 220;
    this.hudContext = this.hudCanvas.getContext("2d")!;
    this.cameraTexture = new THREE.CanvasTexture(this.canvas);
    this.cameraTexture.colorSpace = THREE.SRGBColorSpace;
    this.hudTexture = new THREE.CanvasTexture(this.hudCanvas);
    this.hudTexture.colorSpace = THREE.SRGBColorSpace;
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 2.1), new THREE.MeshBasicMaterial({ map: this.cameraTexture, toneMapped: false, depthTest: false }));
    screen.position.set(0, 0.15, -2.35); screen.renderOrder = 10;
    const hud = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 0.60), new THREE.MeshBasicMaterial({ map: this.hudTexture, transparent: true, toneMapped: false, depthTest: false }));
    hud.position.set(0, -1.10, -2.30); hud.renderOrder = 11;
    this.cockpit.add(screen, hud); this.cockpit.visible = false;
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
      this.inputValid = false; this.keys.clear(); this.pointerHeld = false;
      this.recenter = true; this.xrLive = true; this.setView("camera");
      this.renderer.xr.getSession()?.addEventListener("visibilitychange", () => {
        this.suspended = this.renderer.xr.getSession()?.visibilityState !== "visible";
        if (this.suspended) this.stop("Headset inactive: release trigger");
      });
    });
    this.renderer.xr.addEventListener("sessionend", () => {
      this.stop("VR session ended: release deadman"); this.suspended = false;
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
  };
  private keyUp = (event: KeyboardEvent) => { this.keys.delete(event.code); };
  private blur = () => { this.keys.clear(); this.pointerHeld = false; this.stop("Window inactive: release deadman"); };
  private visibility = () => { if (document.hidden) this.blur(); };
  private escapeStop = (event: KeyboardEvent) => { if (event.key === "Escape") this.stop("Stopped: release deadman"); };

  stop(reason: string) {
    this.seekTarget = null;
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

  update(now: number) {
    const xr = this.renderer.xr.isPresenting;
    const session = this.renderer.xr.getSession();
    if (xr && session) {
      const sources = [...session.inputSources];
      const right = sources.find(source => source.handedness === "right" && source.gamepad);
      const left = sources.find(source => source.handedness === "left" && source.gamepad);
      const gamepad = (left ?? right)?.gamepad;
      const axes = gamepad?.axes ?? [];
      const held = !!right?.gamepad?.buttons[0]?.pressed;
      const toggle = !!right?.gamepad?.buttons[4]?.pressed;
      const center = !!right?.gamepad?.buttons[5]?.pressed;
      if (toggle && !this.toggleDown) { this.xrLive = !this.xrLive; this.stop("View changed: release trigger"); this.setView(this.xrLive ? "camera" : "3d"); }
      if (center && !this.recenterDown) this.recenter = true;
      this.toggleDown = toggle; this.recenterDown = center;
      const xrCamera = this.renderer.xr.getCamera();
      xrCamera.getWorldQuaternion(this.quat); xrCamera.getWorldPosition(this.position);
      this.euler.setFromQuaternion(this.quat, "YXZ");
      if (this.recenter) { this.baseYaw = this.euler.y; this.recenter = false; }
      const yaw = Math.atan2(Math.sin(this.euler.y - this.baseYaw), Math.cos(this.euler.y - this.baseYaw));
      this.inputValid = !!right?.gamepad;
      this.input = { held,
        v: -stick(axes.length >= 4 ? axes[3] : axes[1]) * 0.45,
        w: stick(axes.length >= 4 ? axes[2] : axes[0]) * 0.55,
        pan: THREE.MathUtils.clamp(-THREE.MathUtils.radToDeg(yaw), -80, 80) };
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
    if (this.input.v || this.input.w) this.seekTarget = null;
    this.cockpit.visible = xr;
    this.cameraScreen.visible = this.xrLive;
    if (this.h264Enabled) {
      this.h264.update();
    }
    if (this.digitalPan && Math.abs(this.input.pan - this.drawnPan) > 0.1) this.dirty = true;
    if (this.dirty || now - this.lastCanvasDraw > 100) this.drawCamera(now);
    if (now - this.lastHudAt > 100) { this.drawHUD(now); this.lastHudAt = now; }
  }
  get vrCameraView() { return this.renderer.xr.isPresenting && this.xrLive; }

  private pump() {
    const now = performance.now();
    const connected = this.socket?.readyState === WebSocket.OPEN;
    const contextActive = this.renderer.xr.isPresenting ? this.xrLive && !this.suspended : this.getView() === "camera" && !document.querySelector("dialog[open]");
    const available = this.inputValid && contextActive && !document.hidden && !this.contextLost && now - this.lastCanvasDraw < 250;
    const moving = this.gate.decide(this.input.held, now, connected, available);
    // Never add another command behind buffered traffic. Reset the session instead.
    if (connected && this.socket!.bufferedAmount > 0) {
      this.gate.invalidate("Control queue stalled: release deadman"); this.socket!.close(); return;
    }
    if (now - this.lastSendAt > 200) this.gate.invalidate("Browser paused: release deadman");
    this.send(moving && !this.gate.needsRelease);
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
      ...(this.seekTarget === null ? { mode: "teleop" } : { mode: "seek", target_id: this.seekTarget }),
      video_age_ms: Number.isFinite(age) ? Math.ceil(age) : 999999,
    }));
    this.lastSendAt = performance.now();
  }
  private drawCamera(now: number) {
    const context = this.ctx;
    if (this.h264Enabled && this.h264.video.readyState >= 2) {
      const video = this.h264.video;
      if (this.canvas.width !== video.videoWidth || this.canvas.height !== video.videoHeight) {
        this.canvas.width = video.videoWidth; this.canvas.height = video.videoHeight;
      }
      const crop = cameraCrop(video.videoWidth, video.videoHeight, this.input.pan, this.digitalPan);
      context.drawImage(video, crop.x, crop.y, crop.width, crop.height, 0, 0, this.canvas.width, this.canvas.height);
      const decoded = this.h264.displayedFrame;
      if (decoded) {
        this.gate.acceptFrame(decoded.id, decoded.age, now);
        this.frameSource = decoded.source; this.frameId = decoded.frame === null ? "" : String(decoded.frame);
        this.lastCanvasDraw = now;
        this.drawDetections(crop, video.videoWidth, video.videoHeight, now);
      }
    } else if (!this.h264Enabled && this.frame) {
      if (this.canvas.width !== this.frame.naturalWidth || this.canvas.height !== this.frame.naturalHeight) {
        this.canvas.width = this.frame.naturalWidth; this.canvas.height = this.frame.naturalHeight;
      }
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
    if (!packet) return;
    const context = this.ctx;
    context.strokeStyle = "#8dffa9"; context.fillStyle = "#8dffa9";
    context.lineWidth = Math.max(2, this.canvas.width / 320);
    context.font = `${Math.max(14, this.canvas.width / 40)}px system-ui`;
    for (const box of packet.boxes) {
      const visibleBox = croppedBox(box, crop, width, height);
      if (!visibleBox) continue;
      const x = visibleBox.x * this.canvas.width, y = visibleBox.y * this.canvas.height;
      context.strokeRect(x, y, visibleBox.w * this.canvas.width, visibleBox.h * this.canvas.height);
      context.fillText(`${box.cls.slice(0, 30)} ${Math.round(box.conf * 100)}%`, x + 4, Math.max(18, y - 5));
    }
  }
  private drawHUD(now: number) {
    const ready = this.gate.readiness(now, this.socket?.readyState === WebSocket.OPEN);
    const videoAge = this.gate.videoAge(now);
    const age = Number.isFinite(videoAge) ? `${Math.ceil(videoAge)} ms` : "unavailable";
    const simulation = this.status?.simulated || this.frameSource === "SIMULATED";
    const moving = this.input.held && !this.gate.needsRelease && !ready;
    const headline = `${simulation ? "SIMULATED · " : ""}${ready ?? this.gate.reason}`;
    const ack = this.ackMs === null ? "ack pending" : `control ack ${Math.round(this.ackMs)} ms`;
    const range = this.status?.range_cm == null ? "range unknown" : `${Math.round(this.status.range_cm)} cm`;
    const matching = this.matchingDetections(now);
    const detection = matching ? `${matching.boxes.length} detections` : !this.detections ? "detector unavailable" : now - this.detectionsAt > 500 ? "detections stale" : "detections awaiting matching frame";
    const drill = this.drillLine(now);
    const panMode = `${this.digitalPan ? "Digital pan (fixed camera)" : "Servo pan"} ${Math.round(this.input.pan)}°`;
    const detail = `${this.frameSource} · ${panMode} · ${this.h264Enabled ? "H.264" : "JPEG"} video ${age} · ${ack} · ${range} · ${detection}${drill ? " · " + drill : ""}${this.h264Enabled && !this.h264.displayedFrame ? " · " + this.h264.state : ""}`;
    this.message.textContent = headline;
    this.message.dataset.state = moving ? "moving" : ready ? "unsafe" : "ready";
    this.details.textContent = `${detail}${this.status?.estop ? " · FORWARD BLOCKED" : ""}`;
    this.holdButton.classList.toggle("engaged", moving);
    const c = this.hudContext;
    c.clearRect(0, 0, 1024, 220); c.fillStyle = "#111b25ee"; c.fillRect(0, 0, 1024, 220);
    c.fillStyle = simulation ? "#ffc779" : ready ? "#ffb3a8" : "#a6edc4";
    c.font = "bold 32px system-ui"; c.fillText(headline.slice(0, 58), 28, 46);
    c.font = "24px system-ui"; c.fillStyle = "#dce5ee";
    c.fillText(`${this.h264Enabled ? "H.264" : "JPEG"} video ${age} · ${range} · ${panMode}`, 28, 90);
    c.fillText(`${ack} · ${detection}${this.status?.estop ? " · FORWARD BLOCKED" : ""}`, 28, 126);
    if (drill) { c.font = "18px system-ui"; c.fillText(drill, 28, 150); }
    c.font = "22px system-ui";
    c.fillText("Right trigger: deadman · Left stick: drive · A: map · B: center pan", 28, 170);
    c.fillText("Release trigger after a stop or reconnect", 28, 202);
    this.hudTexture.needsUpdate = true;
  }

  private addAssistant() {
    const parent = document.querySelector("#settings")!;
    const block = document.createElement("section");
    block.className = "local-assistant";
    block.innerHTML = `<h2>Local scene assistant</h2><form><input aria-label="Question for rover" placeholder="What do you see?" maxlength="500"/><button>Ask</button></form><p role="status">Uses observed detections; runs on rover.</p><button type="button" id="start-drill">Start 90-second rescue drill</button><p id="drill-status">Counts observed person tracks as encounters.</p><button type="button" id="open-mission-report">Open mission report</button>`;
    parent.append(block);
    const form = block.querySelector("form")!, input = block.querySelector("input")!, result = block.querySelector("p")!, button = form.querySelector("button")!;
    block.querySelector<HTMLButtonElement>("#open-mission-report")!.onclick = () => void this.showReport();
    this.voice = new AssistantVoice(block, document.querySelector<HTMLDialogElement>("#settings")!, button,
      () => this.stop("Assistant active: release deadman"),
      reply => { result.textContent = `${reply.question ? "Heard: " + reply.question + "\n" : ""}${String(reply.answer ?? "No answer returned")}`; },
      action => {
        // A confirmed proposal still needs the held deadman; any stick input or stop cancels it.
        if (action.type !== "seek") { result.textContent = "Scan confirmed, but room scan is not wired to the rover yet."; return; }
        this.stop(`Seek person #${action.target_id}: release, then hold deadman`);
        this.seekTarget = action.target_id;
        result.textContent = `Seek person #${action.target_id} armed: hold the deadman to approach; move the stick to cancel.`;
      });
    const drillButton = block.querySelector<HTMLButtonElement>("#start-drill")!;
    drillButton.onclick = () => void this.updateDrill(true);
    this.timers.push(window.setInterval(() => {
      if (document.querySelector<HTMLDialogElement>("#settings")!.open || this.drill?.active) void this.updateDrill(false);
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
        drill.textContent = `Rescue drill: ${report.drill.active ? "running" : report.drill.remaining_s === null ? "not started" : "complete"} · ${Number(report.drill.person_encounters) || 0} person encounters`;
        content.append(drill);
      }
      const list = document.createElement("ul"); list.className = "mission-tracks";
      for (const entry of encounters.slice(-20).reverse()) {
        const item = document.createElement("li");
        item.textContent = `${String(entry.cls ?? "object")} · track ${entry.id ?? "?"} · ${Math.round((Number(entry.conf) || 0) * 100)}% · ${entry.source ?? "unknown source"}`;
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
  private drillLine(now: number) {
    if (!this.drill || this.drill.remaining_s === null) return "";
    if (now - this.drillAt > 3000) return "Rescue drill status stale";
    const left = Math.max(0, this.drill.remaining_s - (now - this.drillAt) / 1000);
    return `${left > 0 ? "Rescue drill " + Math.ceil(left) + "s" : "Rescue drill complete"} · ${this.drill.person_encounters} person encounters`;
  }
  private async updateDrill(start: boolean) {
    if (this.drillBusy || this.stopped) return;
    this.drillBusy = true;
    const label = document.querySelector("#drill-status");
    const abort = new AbortController(), timer = window.setTimeout(() => abort.abort(), 2000);
    try {
      if (start) this.stop("Drill started: release deadman");
      const response = await fetch("/mission/drill", start ? {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration_s: 90 }), signal: abort.signal,
      } : { cache: "no-store", signal: abort.signal });
      if (!response.ok) throw new Error(`Drill HTTP ${response.status}`);
      const drill = await response.json();
      if (typeof drill.active !== "boolean" || !Number.isFinite(drill.person_encounters) ||
          !(drill.remaining_s === null || Number.isFinite(drill.remaining_s))) throw new Error("Invalid drill status");
      this.drill = drill; this.drillAt = performance.now();
      if (label) label.textContent = `${this.drillLine(this.drillAt) || "Ready to start"}. Tracks are encounters, not verified distinct people.`;
    } catch (error) { if (label) label.textContent = `Drill unavailable: ${String(error)}`; }
    finally { window.clearTimeout(timer); this.drillBusy = false; }
  }
  dispose() {
    this.stop("Viewer closed"); this.stopped = true;
    this.timers.forEach(timer => { window.clearInterval(timer); window.clearTimeout(timer); });
    this.socket?.close(); this.detectionsSocket?.close(); this.request?.abort(); this.h264.stop(); this.voice?.dispose();
    window.removeEventListener("keydown", this.keyDown); window.removeEventListener("keyup", this.keyUp);
    window.removeEventListener("blur", this.blur); document.removeEventListener("visibilitychange", this.visibility);
    document.removeEventListener("keydown", this.escapeStop);
    this.cameraTexture.dispose(); this.hudTexture.dispose();
  }
}
