import type { ControlCommand, ControlReply } from "./roverProtocol";

const COMMAND_PERIOD_MS = 50;
const CADENCE_JITTER_MS = 0.5;

type AuthorizedStatus = ControlReply & {
  type: "control_status";
  session_id: string;
  permit: string;
  permit_ms: number;
  rover: boolean;
  simulated: boolean;
  requires_release: boolean;
  reason: string;
};

export type ControlState =
  "disconnected" | "connecting" | "waiting" | "healthy" | "stale" | "disposed";
export interface ControlSocket {
  readonly readyState: number;
  readonly bufferedAmount?: number;
  send(data: string): void;
  close(): void;
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
}
export interface ControlCallbacks {
  onReply?: (reply: ControlReply, receivedAt: number) => void;
  onState?: (state: string) => void;
  onCommand?: (command: ControlCommand, at: number) => void;
}
export interface ControlRuntime {
  createSocket?: (url: string) => ControlSocket;
  now?: () => number;
  setInterval?: (callback: () => void, delay: number) => number;
  clearInterval?: (id: number) => void;
}

export class ControlLink {
  private socket: ControlSocket | null = null;
  private listeners: [string, EventListener][] = [];
  private timer: number | null = null;
  private linkState: ControlState = "disconnected";
  private latestReply: AuthorizedStatus | null = null;
  private receivedAt: number | null = null;
  private openedAt = 0;
  private sequence = 0;
  private seenPermits = new Set<string>();
  private latestCommand: ControlCommand | null = null;
  private lastSentAt = -Infinity;
  private intent = { deadman: false, v: 0, w: 0 };
  private armed = false;
  private observedNeutral = false;
  private reason = "release controls to arm";
  private source: "keyboard" | "xr" = "keyboard";
  private xrSession: XRSession | null = null;
  private xrSource: XRInputSource | null = null;
  private lastXRAt: number | null = null;
  private initialYaw: number | null = null;
  private pan = 0;
  private panSampleAt = 0;
  private videoFrameId: string | null = null;
  private videoSourceAge = Infinity;
  private videoReceivedAt = -Infinity;
  private readonly now: () => number;
  private readonly startTimer: (callback: () => void, delay: number) => number;
  private readonly clearTimer: (id: number) => void;

  constructor(
    private url: string,
    private callbacks: ControlCallbacks = {},
    private runtime: ControlRuntime = {},
  ) {
    this.now = runtime.now ?? (() => performance.now());
    this.startTimer =
      runtime.setInterval ??
      ((callback, delay) => globalThis.setInterval(callback, delay));
    this.clearTimer =
      runtime.clearInterval ?? ((id) => globalThis.clearInterval(id));
  }

  get state(): ControlState {
    return this.linkState;
  }
  get reply(): ControlReply | null {
    return this.latestReply;
  }
  get replyReceivedAt(): number | null {
    return this.receivedAt;
  }
  get command(): ControlCommand | null {
    return this.latestCommand;
  }
  get stopReason(): string {
    return this.reason;
  }
  get inputStatus(): "ready" | "release-to-arm" | "keyboard" | "xr" {
    if (!this.armed) return "release-to-arm";
    return this.intent.deadman ? this.source : "ready";
  }

  /** Explicit reconnect only: a new socket never restores the old drive intent. */
  connect(): void {
    if (this.linkState === "disposed" || this.socket) return;
    this.stop("connecting");
    this.latestReply = null;
    this.receivedAt = null;
    this.seenPermits.clear();
    this.setState("connecting");
    let socket: ControlSocket;
    try {
      socket = (this.runtime.createSocket ?? ((url) => new WebSocket(url)))(
        this.url,
      );
    } catch {
      this.setState("disconnected");
      return;
    }
    this.socket = socket;
    this.listen("open", () => {
      this.openedAt = this.now();
      this.setState("waiting");
    });
    this.listen("message", (event) =>
      this.receive((event as MessageEvent).data),
    );
    this.listen("close", () => this.disconnect("socket closed"));
    this.listen("error", () => this.disconnect("socket error"));
    if (this.timer === null)
      this.timer = this.startTimer(() => this.tick(), COMMAND_PERIOD_MS);
  }

  /** Supply metadata only after this exact JPEG has decoded and been displayed. */
  setVideoFrame(
    frameId: string,
    sourceAgeMs: number,
    receivedAt: number,
  ): void {
    if (this.linkState === "disposed" || frameId === this.videoFrameId) return;
    if (
      !frameId ||
      frameId.length > 256 ||
      !Number.isFinite(sourceAgeMs) ||
      sourceAgeMs < 0 ||
      !Number.isFinite(receivedAt) ||
      receivedAt > this.now()
    ) {
      this.videoFrameId = null;
      this.videoSourceAge = Infinity;
      this.videoReceivedAt = -Infinity;
      this.stop("video metadata unavailable");
      return;
    }
    this.videoFrameId = frameId;
    this.videoSourceAge = sourceAgeMs;
    this.videoReceivedAt = receivedAt;
    if (this.videoAge(this.now()) >= 500) this.stop("video stale");
  }

  get videoAgeMs(): number {
    return this.videoAge(this.now());
  }

  dispose(): void {
    if (this.linkState === "disposed") return;
    this.stop("disposed");
    this.disconnect("disposed");
    this.detachSession();
    if (this.timer !== null) {
      this.clearTimer(this.timer);
      this.timer = null;
    }
    this.setState("disposed");
  }

  /** Uses KeyboardEvent.code; opposite directions cancel, Space is the deadman. */
  setKeyboard(keys: ReadonlySet<string>): void {
    if (this.linkState === "disposed" || this.xrSession) return;
    this.source = "keyboard";
    this.pan = 0;
    const v = Number(keys.has("KeyW")) - Number(keys.has("KeyS"));
    const w = Number(keys.has("KeyD")) - Number(keys.has("KeyA"));
    const held = keys.has("Space");
    const neutral =
      !held && !["KeyW", "KeyS", "KeyA", "KeyD"].some((key) => keys.has(key));
    this.observe(held, v, w, neutral, this.now());
  }

  /**
   * Standard right trigger + thumbstick only. W3C reserves axes 0/1 for a
   * touchpad; missing axes 2/3 safely disable drive rather than guessing.
   * https://www.w3.org/TR/webxr-gamepads-module-1/#xr-standard-gamepad-mapping
   * Three.js positive head yaw looks left, so camera pan is its negative.
   */
  sampleXR(session: XRSession, yawRadians: number): void {
    if (this.linkState === "disposed") return;
    const now = this.now();
    if (session !== this.xrSession) {
      this.stop("XR session changed");
      this.detachSession();
      this.xrSession = session;
      this.xrSource = null;
      this.source = "xr";
      this.initialYaw = Number.isFinite(yawRadians) ? yawRadians : null;
      this.pan = 0;
      this.panSampleAt = now;
      session.addEventListener("end", this.onXREnd);
      session.addEventListener("visibilitychange", this.onXRVisibility);
    }
    if (session.visibilityState !== "visible" || !Number.isFinite(yawRadians)) {
      this.stop("XR tracking unavailable");
      return;
    }
    if (this.initialYaw === null) {
      this.initialYaw = yawRadians;
      this.panSampleAt = now;
    }
    const source = Array.from(session.inputSources).find(
      (input) => input.handedness === "right",
    );
    const gamepad = source?.gamepad;
    const trigger = gamepad?.buttons[0];
    if (
      !source ||
      source.targetRayMode !== "tracked-pointer" ||
      !source.gripSpace ||
      !gamepad?.connected ||
      gamepad.mapping !== "xr-standard" ||
      !trigger ||
      typeof trigger.pressed !== "boolean" ||
      !Number.isFinite(trigger.value) ||
      gamepad.axes.length < 4 ||
      !Number.isFinite(gamepad.axes[2]) ||
      !Number.isFinite(gamepad.axes[3])
    ) {
      this.xrSource = null;
      this.stop("XR right controller unavailable");
      return;
    }
    if (this.xrSource !== source) {
      this.stop("XR controller changed");
      this.xrSource = source;
    }
    this.lastXRAt = now;
    const delta = Math.atan2(
      Math.sin(yawRadians - this.initialYaw),
      Math.cos(yawRadians - this.initialYaw),
    );
    const yawDegrees = (-delta * 180) / Math.PI;
    const targetPan = Math.abs(yawDegrees) < 2 ? 0 : clamp(yawDegrees, -80, 80);
    // A 120 ms time constant makes smoothing independent of the headset frame rate.
    const weight = 1 - Math.exp(-Math.max(0, now - this.panSampleAt) / 120);
    this.pan = clamp(this.pan + (targetPan - this.pan) * weight, -80, 80);
    this.panSampleAt = now;
    const w = stick(gamepad.axes[2]);
    const v = -stick(gamepad.axes[3]);
    const held = trigger.pressed && trigger.value >= 0.5;
    this.observe(held, v, w, !held && v === 0 && w === 0, now);
  }

  /** Call on window blur, hidden page, XR exit, or any input/tracking loss. */
  stop(reason = "stopped"): void {
    const wasHeld = this.intent.deadman || this.latestCommand?.deadman === true;
    this.intent = { deadman: false, v: 0, w: 0 };
    this.armed = false;
    this.observedNeutral = false;
    this.reason = reason;
    if (wasHeld) this.send(false, this.now());
  }

  tick(now = this.now()): void {
    if (
      this.linkState === "disposed" ||
      !this.socket ||
      this.socket.readyState !== 1
    )
      return;
    const healthy = this.healthy(now);
    if (!this.socket) return;
    const readiness = this.readiness(now);
    if (readiness && (this.armed || this.intent.deadman)) this.stop(readiness);
    if (
      this.xrSession &&
      (this.lastXRAt === null || now - this.lastXRAt >= 150)
    ) {
      this.stop("XR input stale");
      return;
    }
    if (now - this.lastSentAt + CADENCE_JITTER_MS < COMMAND_PERIOD_MS) return;
    // Open/release can occur between interval deadlines. Let that send drain
    // for a command period before inspecting pending bytes, allowing only
    // 0.5 ms of clock jitter so an early scheduled tick cannot halve drive Hz.
    if (!this.writable() || !healthy || readiness) return;
    if (this.armed && this.intent.deadman) this.send(true, now);
    else if (
      this.armed &&
      Math.abs(this.pan - (this.latestCommand?.pan ?? 0)) >= 0.5
    )
      this.send(false, now);
  }

  private observe(
    held: boolean,
    v: number,
    w: number,
    neutral: boolean,
    now: number,
  ): void {
    const healthy = this.healthy(now);
    this.observedNeutral = neutral;
    const readiness = this.readiness(now);
    if (readiness && this.intent.deadman) this.stop(readiness);
    if (
      neutral &&
      this.latestReply?.rover &&
      this.latestReply.requires_release &&
      (!this.latestCommand || this.latestCommand.seq <= this.latestReply.seq)
    )
      this.send(false, now);
    if (neutral && healthy && !readiness) {
      this.armed = true;
      this.reason = "";
    }
    const wasHeld = this.intent.deadman;
    this.intent =
      this.armed && healthy && !readiness && held
        ? { deadman: true, v, w }
        : { deadman: false, v: 0, w: 0 };
    if (wasHeld && !this.intent.deadman) this.send(false, now);
  }

  private healthy(now: number): boolean {
    if (!this.socket || this.socket.readyState !== 1) return false;
    const last = this.receivedAt ?? this.openedAt;
    if (now - last >= 300) {
      if (this.linkState !== "stale") {
        this.stop("control replies stale");
        if (this.socket) this.setState("stale");
      }
      return false;
    }
    return this.linkState === "healthy" && this.receivedAt !== null;
  }

  private receive(data: unknown): void {
    const reply = parseReply(data);
    if (!reply || reply.seq > this.sequence) return;
    const changedSession = reply.session_id !== this.latestReply?.session_id;
    if (
      !changedSession &&
      (this.seenPermits.has(reply.permit) ||
        (this.latestReply && reply.seq < this.latestReply.seq))
    )
      return;
    const now = this.now();
    this.healthy(now); // A late reply cannot preserve the old held intent.
    if (!this.socket) return; // A failed stop send may have disconnected the link.
    this.latestReply = reply;
    if (changedSession) this.seenPermits.clear();
    this.seenPermits.add(reply.permit);
    if (this.seenPermits.size > 32)
      this.seenPermits.delete(this.seenPermits.values().next().value!);
    this.receivedAt = now;
    this.setState("healthy");
    if (changedSession || !reply.rover || reply.requires_release) {
      const beforeStop = this.sequence;
      this.stop(changedSession ? "control session changed" : reply.reason);
      // Ordered neutral unlocks the server. Local input must still be neutral
      // before a new press can arm this client after a session change.
      if (changedSession && this.sequence === beforeStop) this.send(false, now);
    } else if (this.observedNeutral && !this.readiness(now)) {
      this.armed = true;
      this.reason = "";
    }
    this.callbacks.onReply?.(reply, now);
  }

  private send(deadman: boolean, at: number): void {
    if (!this.latestReply || !this.writable()) return;
    const readiness = this.readiness(at);
    if (deadman && readiness) {
      this.stop(readiness);
      return;
    }
    const age = this.videoAge(at);
    const command: ControlCommand = {
      seq: this.sequence + 1,
      deadman,
      v: deadman ? this.intent.v : 0,
      w: deadman ? this.intent.w : 0,
      pan: this.pan,
      mode: "teleop",
      session_id: this.latestReply.session_id,
      permit: this.latestReply.permit,
      video_fresh: Number.isFinite(age) && age < 500,
      video_age_ms: Number.isFinite(age) ? Math.ceil(age) : 999999,
    };
    try {
      this.socket!.send(JSON.stringify(command));
    } catch {
      this.disconnect("socket send failed");
      return;
    }
    this.sequence = command.seq;
    this.latestCommand = command;
    this.lastSentAt = at;
    this.callbacks.onCommand?.(command, at);
  }

  private readiness(now: number): string | null {
    if (!this.latestReply) return "waiting for control session";
    if (!this.latestReply.rover)
      return this.latestReply.reason || "rover unavailable";
    if (this.latestReply.requires_release)
      return this.latestReply.reason || "server release required";
    if (
      this.receivedAt === null ||
      now - this.receivedAt >= Math.min(this.latestReply.permit_ms, 200)
    )
      return "control permit stale";
    if (this.videoAge(now) >= 500) return "video stale or metadata unavailable";
    return null;
  }

  private videoAge(now: number): number {
    return this.videoSourceAge + Math.max(0, now - this.videoReceivedAt);
  }

  private writable(): boolean {
    if (!this.socket || this.socket.readyState !== 1) return false;
    if ((this.socket.bufferedAmount ?? 0) > 0) {
      // Do not grow a motion queue or append a stop behind it. close() may still
      // flush already queued bytes; the rover's 250 ms command lease is required.
      this.disconnect("socket outbound backlog");
      return false;
    }
    return true;
  }

  private listen(type: string, listener: EventListener): void {
    this.listeners.push([type, listener]);
    this.socket?.addEventListener(type, listener);
  }

  private disconnect(reason: string): void {
    const socket = this.socket;
    this.socket = null;
    this.intent = { deadman: false, v: 0, w: 0 };
    this.armed = false;
    this.observedNeutral = false;
    this.reason = reason;
    for (const [type, listener] of this.listeners)
      socket?.removeEventListener(type, listener);
    this.listeners = [];
    if (socket && socket.readyState < 2) socket.close();
    if (this.linkState !== "disposed") this.setState("disconnected");
  }

  private setState(state: ControlState): void {
    if (this.linkState === state) return;
    this.linkState = state;
    this.callbacks.onState?.(state);
  }

  private readonly onXREnd = (): void => {
    this.stop("XR session ended");
    this.detachSession();
    this.source = "keyboard";
    this.pan = 0;
  };
  private readonly onXRVisibility = (): void => {
    if (this.xrSession?.visibilityState !== "visible")
      this.stop("XR session hidden");
  };
  private detachSession(): void {
    this.xrSession?.removeEventListener("end", this.onXREnd);
    this.xrSession?.removeEventListener(
      "visibilitychange",
      this.onXRVisibility,
    );
    this.xrSession = null;
    this.xrSource = null;
    this.lastXRAt = null;
  }
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}
function stick(value: number): number {
  const clipped = clamp(value, -1, 1);
  return Math.abs(clipped) <= 0.18
    ? 0
    : clamp((Math.sign(clipped) * (Math.abs(clipped) - 0.18)) / 0.82, -1, 1);
}
function parseReply(data: unknown): AuthorizedStatus | null {
  if (typeof data !== "string" || data.length > 4096) return null;
  try {
    const value: unknown = JSON.parse(data);
    if (!value || typeof value !== "object") return null;
    const packet = value as Partial<ControlReply>;
    if (
      !Number.isSafeInteger(packet.seq) ||
      packet.seq! < -1 ||
      !(
        packet.range_cm === null ||
        (typeof packet.range_cm === "number" &&
          Number.isFinite(packet.range_cm) &&
          packet.range_cm >= 0)
      ) ||
      typeof packet.estop !== "boolean" ||
      packet.mode !== "teleop" ||
      !(
        packet.link_ms === null ||
        (typeof packet.link_ms === "number" &&
          Number.isFinite(packet.link_ms) &&
          packet.link_ms >= 0)
      ) ||
      packet.type !== "control_status" ||
      typeof packet.session_id !== "string" ||
      !packet.session_id ||
      packet.session_id.length > 128 ||
      typeof packet.permit !== "string" ||
      !packet.permit ||
      packet.permit.length > 128 ||
      typeof packet.permit_ms !== "number" ||
      !Number.isFinite(packet.permit_ms) ||
      packet.permit_ms <= 0 ||
      packet.permit_ms > 250 ||
      typeof packet.rover !== "boolean" ||
      typeof packet.simulated !== "boolean" ||
      typeof packet.requires_release !== "boolean" ||
      typeof packet.reason !== "string" ||
      packet.reason.length > 256
    )
      return null;
    return {
      seq: packet.seq!,
      range_cm: packet.range_cm,
      estop: packet.estop,
      mode: packet.mode,
      link_ms: packet.link_ms,
      type: packet.type,
      session_id: packet.session_id,
      permit: packet.permit,
      permit_ms: packet.permit_ms,
      rover: packet.rover,
      simulated: packet.simulated,
      requires_release: packet.requires_release,
      reason: packet.reason,
      ...(typeof packet.left_pwm === "number" &&
      Number.isFinite(packet.left_pwm)
        ? { left_pwm: packet.left_pwm }
        : {}),
      ...(typeof packet.right_pwm === "number" &&
      Number.isFinite(packet.right_pwm)
        ? { right_pwm: packet.right_pwm }
        : {}),
      ...(typeof packet.pan_deg === "number" && Number.isFinite(packet.pan_deg)
        ? { pan_deg: packet.pan_deg }
        : {}),
      ...(typeof packet.max_pwm === "number" && Number.isFinite(packet.max_pwm)
        ? { max_pwm: packet.max_pwm }
        : {}),
    };
  } catch {
    return null;
  }
}
