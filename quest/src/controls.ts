import type { ControlCommand, ControlReply } from "./protocol";

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
  private latestReply: ControlReply | null = null;
  private receivedAt: number | null = null;
  private openedAt = 0;
  private sequence = 0;
  private firstSocketSequence = 1;
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
    this.firstSocketSequence = this.sequence + 1;
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
      this.send(false, this.openedAt);
    });
    this.listen("message", (event) =>
      this.receive((event as MessageEvent).data),
    );
    this.listen("close", () => this.disconnect("socket closed"));
    this.listen("error", () => this.disconnect("socket error"));
    if (this.timer === null)
      this.timer = this.startTimer(() => this.tick(), 50);
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
    if (this.linkState === "disposed" || !this.socket || this.socket.readyState !== 1) return;
    const healthy = this.healthy(now);
    if (!this.socket) return;
    if (
      this.xrSession &&
      (this.lastXRAt === null || now - this.lastXRAt >= 150)
    ) {
      this.stop("XR input stale");
      return;
    }
    if (now - this.lastSentAt < 50) return;
    // Open/release can occur between interval deadlines. Let that send drain
    // for a full command period before inspecting the socket's pending bytes.
    if (!this.writable() || !healthy) return;
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
    if (neutral && healthy) {
      this.armed = true;
      this.reason = "";
    }
    const wasHeld = this.intent.deadman;
    this.intent =
      this.armed && healthy && held
        ? { deadman: true, v, w }
        : { deadman: false, v: 0, w: 0 };
    if (wasHeld && !this.intent.deadman) this.send(false, now);
  }

  private healthy(now: number): boolean {
    if (!this.socket || this.socket.readyState !== 1) return false;
    const last = this.receivedAt ?? this.openedAt;
    if (now - last >= 500) {
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
    if (
      !reply ||
      reply.seq < this.firstSocketSequence ||
      reply.seq > this.sequence
    )
      return;
    if (
      this.latestReply &&
      (reply.seq < this.latestReply.seq ||
        (reply.seq === this.latestReply.seq &&
          (this.latestCommand?.deadman ||
            reply.seq !== this.latestCommand?.seq)))
    )
      return;
    const now = this.now();
    this.healthy(now); // A late reply cannot preserve the old held intent.
    if (!this.socket) return; // A failed stop send may have disconnected the link.
    this.latestReply = reply;
    this.receivedAt = now;
    this.setState("healthy");
    if (this.observedNeutral) {
      this.armed = true;
      this.reason = "";
    }
    this.callbacks.onReply?.(reply, now);
  }

  private send(deadman: boolean, at: number): void {
    if (!this.writable()) return;
    const command: ControlCommand = {
      seq: this.sequence + 1,
      deadman,
      v: deadman ? this.intent.v : 0,
      w: deadman ? this.intent.w : 0,
      pan: this.pan,
      mode: "teleop",
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
function parseReply(data: unknown): ControlReply | null {
  if (typeof data !== "string" || data.length > 4096) return null;
  try {
    const value: unknown = JSON.parse(data);
    if (!value || typeof value !== "object") return null;
    const packet = value as Partial<ControlReply>;
    if (
      !Number.isSafeInteger(packet.seq) ||
      packet.seq! < 0 ||
      typeof packet.range_cm !== "number" ||
      !Number.isFinite(packet.range_cm) ||
      packet.range_cm < 0 ||
      typeof packet.estop !== "boolean" ||
      (packet.mode !== "teleop" && packet.mode !== "seek") ||
      typeof packet.link_ms !== "number" ||
      !Number.isFinite(packet.link_ms) ||
      packet.link_ms < 0
    )
      return null;
    return {
      seq: packet.seq!,
      range_cm: packet.range_cm,
      estop: packet.estop,
      mode: packet.mode,
      link_ms: packet.link_ms,
    };
  } catch {
    return null;
  }
}
