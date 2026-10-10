/** Flat Mac-rendered picture and pose channel. No rover control endpoint. */
import * as THREE from "three";

export const MACVIEW_PATH = "/ws/macview";
export const POSE_PATH = "/ws/macview-pose";
export const POSE_HZ = 72;
export const LABEL = "MAC RENDER · flat view · not stereo";
export type Vec3 = [number, number, number];
export type PoseMessage = {
  type: "pose"; version: 1; tracked?: true;
  sent_mono_ms: number; sent_wall_ms: number;
  session_id?: string; frame_id?: number; client_nonce?: string;
  head: { position: Vec3; quaternion: [number, number, number, number] };
  ray: { origin: Vec3; direction: Vec3 } | null;
};

/** Diagnostics only: wall clocks on different devices are not an age bound. */
export function frameAgeMs(captureWallMs: number, nowWallMs: number) {
  if (![captureWallMs, nowWallMs].every(Number.isFinite)) throw new Error("frame age needs finite timestamps");
  return nowWallMs - captureWallMs;
}
export function streamOrigin(value: string) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
      url.pathname !== "/" || url.search || url.hash) throw new Error("Mac endpoint must be an HTTP(S) origin without credentials");
  return url.origin;
}
export function streamURL(origin: string, path: string) { return new URL(path, streamOrigin(origin)).href; }
export function shouldSendPose(lastMonoMs: number, nowMonoMs: number) {
  return Number.isFinite(nowMonoMs) && nowMonoMs - lastMonoMs >= 1000 / POSE_HZ;
}
export function poseMessage(now: { mono: number; wall: number }, head: {
  position: THREE.Vector3; quaternion: THREE.Quaternion;
}, ray: { origin: THREE.Vector3; direction: THREE.Vector3 } | null): PoseMessage {
  return { type: "pose", version: 1, sent_mono_ms: now.mono, sent_wall_ms: now.wall,
    head: { position: head.position.toArray() as Vec3,
      quaternion: [head.quaternion.x, head.quaternion.y, head.quaternion.z, head.quaternion.w] },
    ray: ray ? { origin: ray.origin.toArray() as Vec3, direction: ray.direction.toArray() as Vec3 } : null };
}

/** Null is tracking loss; controller index/order never identifies handedness. */
export function trackedXR(frame: XRFrame | undefined, reference: XRReferenceSpace | null, session: XRSession | null) {
  if (!frame || !reference || session?.visibilityState !== "visible") return null;
  try {
    const pose = frame.getViewerPose(reference);
    if (!pose) return null;
    const p = pose.transform.position, q = pose.transform.orientation;
    if (![p.x, p.y, p.z, q.x, q.y, q.z, q.w].every(Number.isFinite)) return null;
    const head = { position: new THREE.Vector3(p.x, p.y, p.z), quaternion: new THREE.Quaternion(q.x, q.y, q.z, q.w) };
    const right = [...session.inputSources].find(source => source.handedness === "right");
    let ray = null;
    if (right?.targetRaySpace) {
      const target = frame.getPose(right.targetRaySpace, reference);
      if (target) {
        const r = target.transform.position, o = target.transform.orientation;
        if ([r.x, r.y, r.z, o.x, o.y, o.z, o.w].every(Number.isFinite)) ray = {
          origin: new THREE.Vector3(r.x, r.y, r.z),
          direction: new THREE.Vector3(0, 0, -1).applyQuaternion(new THREE.Quaternion(o.x, o.y, o.z, o.w)),
        };
      }
    }
    return { head, ray };
  } catch { return null; }
}

type Stream = {
  video: HTMLVideoElement; start: () => void; stop: () => void; update?: () => void; state?: string;
  displayedFrame?: { age: number; source: string; metadata?: unknown } | null;
};

export class MacView {
  readonly viewer: Stream;
  readonly age = document.createElement("div");
  private poseSocket: WebSocket | null = null;
  private retry = 0;
  private generation = 0;
  private lastPoseMono = -Infinity;
  private stopped = true;
  private tracked = false;
  private clearSent = false;
  private session = "";
  private sequence = 0;
  private sent = new Map<number, { nonce: string; at: number }>();
  private source = "waiting for decoded picture";
  private formatOkay = false;
  private formatError = "";
  private panel: HTMLElement;
  private origin: string;
  frameAgeMs: number | null = null;
  poseRoundTripMs: number | null = null;

  constructor(origin = location.origin, panel = document.body, stream?: Stream) {
    this.origin = streamOrigin(origin); this.panel = panel;
    this.viewer = stream ?? { video: document.createElement("video"), start() {}, stop() {} };
    this.age.className = "mac-age"; this.age.textContent = LABEL;
  }
  start() {
    if (!this.stopped) return;
    this.stopped = false; this.generation++; this.resetPoseSession();
    this.panel.append(this.viewer.video, this.age); this.connectPose(); this.viewer.start();
  }
  resetPoseSession() {
    this.session = crypto.randomUUID(); this.sequence = 0; this.sent.clear();
    this.lastPoseMono = -Infinity; this.tracked = false; this.clearSent = false;
    this.poseRoundTripMs = null;
  }
  private connectPose() {
    if (this.stopped) return;
    const token = this.generation;
    const socket = new WebSocket(streamURL(this.origin, POSE_PATH).replace(/^http/, "ws"));
    this.poseSocket = socket;
    socket.onopen = () => { if (token === this.generation && !this.stopped && !this.tracked) this.untracked(); };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (token !== this.generation || this.poseSocket !== socket) return;
      this.poseSocket = null; this.clearSent = false;
      if (!this.stopped) this.retry = window.setTimeout(() => {
        if (!this.stopped && token === this.generation) this.connectPose();
      }, 1000);
    };
  }
  untracked() {
    this.tracked = false; this.poseRoundTripMs = null;
    if (this.stopped || this.clearSent || this.poseSocket?.readyState !== WebSocket.OPEN) return;
    if (this.poseSocket.bufferedAmount > 0) { this.poseSocket.close(); return; }
    this.poseSocket.send(JSON.stringify({ type: "pose_untracked", version: 1, tracked: false,
      session_id: this.session, frame_id: ++this.sequence, client_nonce: crypto.randomUUID() }));
    this.clearSent = true;
  }
  sendPose(now: { mono: number; wall: number }, head: { position: THREE.Vector3; quaternion: THREE.Quaternion },
           ray: { origin: THREE.Vector3; direction: THREE.Vector3 } | null) {
    if (this.stopped) return;
    const packet = poseMessage(now, head, ray);
    if (![now.mono, now.wall, ...packet.head.position, ...packet.head.quaternion,
      ...(packet.ray ? [...packet.ray.origin, ...packet.ray.direction] : [])].every(Number.isFinite) ||
      head.quaternion.lengthSq() < 1e-12) { this.untracked(); return; }
    this.tracked = true;
    if (this.stopped || this.poseSocket?.readyState !== WebSocket.OPEN || this.poseSocket.bufferedAmount > 0 ||
        !shouldSendPose(this.lastPoseMono, now.mono)) return; // newest sample is retried on next RAF, never queued
    this.lastPoseMono = now.mono; this.clearSent = false;
    const frame = ++this.sequence, nonce = crypto.randomUUID();
    this.sent.set(frame, { nonce, at: now.mono });
    if (this.sent.size > 256) this.sent.delete(this.sent.keys().next().value!);
    this.poseSocket.send(JSON.stringify({ ...packet, tracked: true, session_id: this.session, frame_id: frame, client_nonce: nonce }));
  }
  /** Decoder invokes this hook; non-flat formats cannot enter this flat viewer. */
  noteSidecar(message: Record<string, unknown>) {
    if (message.type === "video_init") {
      this.formatOkay = message.stereo === false && message.version === 1;
      this.formatError = this.formatOkay ? "" : "Unsupported video format: requires explicit flat/non-stereo v1";
      this.source = String(message.source ?? "source unreported").slice(0, 120);
      return this.formatOkay;
    }
    if (message.type === "video_segment" && (!this.formatOkay || message.stereo !== false || message.version !== 1)) {
      this.formatError = "Unsupported video format: requires flat segments"; return false;
    }
    return true;
  }
  streamReset() { this.frameAgeMs = null; this.poseRoundTripMs = null; }
  update() {
    this.viewer.update?.(); // request expiry/watchdog must use active display loop
    const frame = this.viewer.displayedFrame;
    this.frameAgeMs = frame && Number.isFinite(frame.age) && frame.age >= 0 ? frame.age : null;
    this.poseRoundTripMs = null;
    if (frame) {
      this.source = String(frame.source ?? "source unreported").slice(0, 120);
      const metadata = frame.metadata as { pose?: { session_id?: string; frame_id?: number; client_nonce?: string } } | undefined;
      const echo = metadata?.pose, sent = typeof echo?.frame_id === "number" ? this.sent.get(echo.frame_id) : null;
      if (sent && echo?.session_id === this.session && echo.client_nonce === sent.nonce) {
        this.poseRoundTripMs = Math.max(0, performance.now() - sent.at);
      }
    }
    const age = this.frameAgeMs === null ? "decoded age unknown" : `${Math.ceil(this.frameAgeMs)} ms${this.frameAgeMs >= 500 ? " · STALE picture" : " · decoded"}`;
    const pose = this.poseRoundTripMs === null ? "pose echo unverified" : `pose round trip ${Math.ceil(this.poseRoundTripMs)} ms (not photon latency)`;
    this.age.textContent = `${LABEL} · ${this.source} · ${age} · ${this.tracked ? "tracked" : "tracking paused"} · ${pose}${this.formatError ? " · " + this.formatError : this.frameAgeMs === null ? " · " + (this.viewer.state ?? "waiting for stream") : ""}`;
  }
  stop() {
    if (this.stopped) return;
    this.untracked(); this.stopped = true; this.generation++;
    window.clearTimeout(this.retry); this.retry = 0;
    const socket = this.poseSocket; this.poseSocket = null;
    if (socket) { socket.onopen = socket.onclose = socket.onerror = null; socket.close(); }
    this.viewer.stop(); this.sent.clear(); this.streamReset();
  }
  dispose() { this.stop(); this.viewer.video.remove(); this.age.remove(); }
}
