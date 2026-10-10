/**
 * Standalone Mac visual view. Plays /ws/macview on a flat panel (desktop and a
 * WebXR quad) and sends head + right-controller ray back. Never opens /ws/control.
 * This is a flat, pose-driven picture. It is not stereo.
 */
import * as THREE from "three";

export const MACVIEW_PATH = "/ws/macview";
export const POSE_PATH = "/ws/macview-pose";
export const POSE_HZ = 72;
export const LABEL = "MAC RENDER · flat view · not stereo";

export type Vec3 = [number, number, number];
export type PoseMessage = {
  type: "pose";
  version: 1;
  sent_mono_ms: number;
  sent_wall_ms: number;
  head: { position: Vec3; quaternion: [number, number, number, number] };
  ray: { origin: Vec3; direction: Vec3 } | null;
};

export function frameAgeMs(captureWallMs: number, nowWallMs: number): number {
  if (!Number.isFinite(captureWallMs) || !Number.isFinite(nowWallMs)) {
    throw new Error("frame age needs finite timestamps");
  }
  return nowWallMs - captureWallMs;
}

export function poseMessage(now: { mono: number; wall: number }, head: {
  position: THREE.Vector3; quaternion: THREE.Quaternion;
}, ray: { origin: THREE.Vector3; direction: THREE.Vector3 } | null): PoseMessage {
  return {
    type: "pose",
    version: 1,
    sent_mono_ms: now.mono,
    sent_wall_ms: now.wall,
    head: {
      position: head.position.toArray() as Vec3,
      quaternion: [head.quaternion.x, head.quaternion.y, head.quaternion.z, head.quaternion.w],
    },
    ray: ray ? {
      origin: ray.origin.toArray() as Vec3,
      direction: ray.direction.toArray() as Vec3,
    } : null,
  };
}

/** Drop poses faster than 72 Hz. A held trigger still does not send motion. */
export function shouldSendPose(lastMonoMs: number, nowMonoMs: number): boolean {
  return nowMonoMs - lastMonoMs >= 1000 / POSE_HZ;
}

type Stream = {
  video: HTMLVideoElement;
  start: () => void;
  stop: () => void;
};

export class MacView {
  readonly viewer: Stream;
  readonly age = document.createElement("div");
  private poseSocket: WebSocket | null = null;
  private lastPoseMono = -Infinity;
  private stopped = true;
  private origin: string;
  private panel: HTMLElement;
  frameAgeMs: number | null = null;

  constructor(origin?: string, panel?: HTMLElement, stream?: Stream) {
    this.origin = origin ?? location.origin;
    this.panel = panel ?? document.body;
    this.viewer = stream ?? { video: document.createElement("video"), start() {}, stop() {} };
    this.age.className = "mac-age";
    this.age.textContent = LABEL;
  }

  start() {
    this.stopped = false;
    this.panel.append(this.viewer.video, this.age);
    this.connectPose();
    this.viewer.start();
  }

  private connectPose() {
    const socket = new WebSocket(new URL(POSE_PATH, this.origin).href.replace(/^http/, "ws"));
    this.poseSocket = socket;
    socket.onclose = () => {
      if (!this.stopped) window.setTimeout(() => this.connectPose(), 1000);
    };
  }

  sendPose(now: { mono: number; wall: number }, head: {
    position: THREE.Vector3; quaternion: THREE.Quaternion;
  }, ray: { origin: THREE.Vector3; direction: THREE.Vector3 } | null) {
    if (this.stopped || this.poseSocket?.readyState !== WebSocket.OPEN) return;
    if (!shouldSendPose(this.lastPoseMono, now.mono)) return;
    this.lastPoseMono = now.mono;
    this.poseSocket.send(JSON.stringify(poseMessage(now, head, ray)));
  }

  noteSidecar(message: { type?: string; macview?: string; capture_wall_ms?: number }) {
    const kind = message.macview ?? message.type;
    if (kind !== "macview_segment" || !Number.isFinite(message.capture_wall_ms)) return;
    this.frameAgeMs = frameAgeMs(message.capture_wall_ms as number, Date.now());
    this.paintAge();
  }

  private paintAge() {
    const age = this.frameAgeMs;
    this.age.textContent = age === null
      ? LABEL
      : `${LABEL} · frame age ${Math.max(0, Math.round(age))} ms`;
  }

  stop() {
    this.stopped = true;
    this.poseSocket?.close();
    this.viewer.stop();
  }
}
