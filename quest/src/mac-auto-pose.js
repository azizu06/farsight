// Mac render in AUTO: pose math and presentation state for the streamed picture.
// Display only. Nothing here opens /ws/control or changes rover motion.
//
// mapWalk draws an AR-map point p at world = Ry(yaw) * (p - viewer), with
// viewer = (x, floorY + lift, z). The Mac camera lives in AR-map coordinates, so a
// headset pose in world coordinates goes the other way: p = Ry(-yaw) * world + viewer.

export const POSE_VERSION = 1;
export const POSE_HZ = 72;
/** The renderer's camera when it has not echoed a field of view (render.mjs: 50 deg, 1280x720). */
export const DEFAULT_FOV_Y_DEG = 50;
export const DEFAULT_ASPECT = 16 / 9;
/** A picture older than this is no longer shown; the local map returns. */
export const MAC_STALE_MS = 1000;
/** Distance of the flat picture from the pose it was rendered at, metres. */
export const PICTURE_DISTANCE = 3;

/** @param {{x:number,y:number,z:number,yaw:number}} walk @param {number[]} point world xyz */
export function mapPoint(walk, point) {
  const s = Math.sin(-walk.yaw), c = Math.cos(-walk.yaw);
  const [x, y, z] = point;
  return [c * x + s * z + walk.x, y + walk.y, -s * x + c * z + walk.z];
}

/** World direction into map coordinates (rotation only). */
export function mapDirection(walk, direction) {
  const s = Math.sin(-walk.yaw), c = Math.cos(-walk.yaw);
  const [x, y, z] = direction;
  return [c * x + s * z, y, -s * x + c * z];
}

/** World orientation [x, y, z, w] into map coordinates: Ry(-yaw) * q. */
export function mapQuaternion(walk, quaternion) {
  const h = -walk.yaw / 2, ry = Math.sin(h), rw = Math.cos(h);
  const [x, y, z, w] = quaternion;
  // (0, ry, 0, rw) * (x, y, z, w)
  return [rw * x + ry * z, rw * y + ry * w, rw * z - ry * x, rw * w - ry * y];
}

/** Inverse of mapPoint: where map point p is drawn in the headset world. */
export function worldPoint(walk, point) {
  const s = Math.sin(walk.yaw), c = Math.cos(walk.yaw);
  const x = point[0] - walk.x, y = point[1] - walk.y, z = point[2] - walk.z;
  return [c * x + s * z, y, -s * x + c * z];
}

/** Vertical FOV from a WebGL projection matrix (column-major, 16 numbers). */
export function projectionFovY(projection) {
  const f = projection?.[5];
  return Number.isFinite(f) && f > 0 ? (2 * Math.atan(1 / f) * 180) / Math.PI : DEFAULT_FOV_Y_DEG;
}

const finite = values => values.every(Number.isFinite);

/**
 * One pose message. The v1 fields keep the renderer's parser unchanged; the rest are
 * optional additions the renderer may adopt (pose_id echo, per-eye views, FOV request).
 * @param {{
 *   id: number, mono: number, wall: number, walk: {x:number,y:number,z:number,yaw:number},
 *   head: {position: number[], quaternion: number[]},
 *   ray?: {origin: number[], direction: number[]} | null,
 *   eyes?: {position: number[], quaternion: number[], projection: number[]}[],
 *   session: string, mapRevision?: number | null, aspect?: number,
 * }} input
 */
export function mapPoseMessage(input) {
  const { walk, head } = input;
  if (!finite([walk.x, walk.y, walk.z, walk.yaw, ...head.position, ...head.quaternion, input.mono, input.wall])) return null;
  const ray = input.ray && finite([...input.ray.origin, ...input.ray.direction])
    ? { origin: mapPoint(walk, input.ray.origin), direction: mapDirection(walk, input.ray.direction) } : null;
  const eyes = (input.eyes ?? []).filter(eye => finite([...eye.position, ...eye.quaternion, ...eye.projection]))
    .map(eye => ({ position: mapPoint(walk, eye.position), quaternion: mapQuaternion(walk, eye.quaternion), projection: [...eye.projection] }));
  return {
    type: "pose",
    version: POSE_VERSION,
    sent_mono_ms: input.mono,
    sent_wall_ms: input.wall,
    head: { position: mapPoint(walk, head.position), quaternion: mapQuaternion(walk, head.quaternion) },
    ray,
    pose_id: input.id,
    // Alias for renderers that name the view counter frame_id (tools/mac_render/protocol.py accepts both).
    frame_id: input.id,
    space: "ar_map",
    session_id: input.session,
    map_revision: Number.isFinite(input.mapRevision) ? input.mapRevision : null,
    viewer: { x: walk.x, y: walk.y, z: walk.z, yaw: walk.yaw },
    fov_y_deg: eyes.length ? projectionFovY(eyes[0].projection) : DEFAULT_FOV_Y_DEG,
    aspect: Number.isFinite(input.aspect) && input.aspect > 0 ? input.aspect : DEFAULT_ASPECT,
    eyes,
    stereo: false,
  };
}

/** World poses already sent, so a displayed frame can be placed where it was rendered from. */
export class PoseRing {
  /** @param {number} [size] */
  constructor(size = 128) { this.size = size; /** @type {any[]} */ this.items = []; }
  /** @param {{id:number, mono:number, position:number[], quaternion:number[]}} pose */
  push(pose) { this.items.push(pose); if (this.items.length > this.size) this.items.shift(); }
  /** Match the renderer's echo by pose_id when present, else by our own sent_mono_ms. */
  find(echo) {
    if (!echo || typeof echo !== "object") return null;
    if (Number.isFinite(echo.pose_id)) return this.items.find(item => item.id === echo.pose_id) ?? null;
    if (Number.isFinite(echo.sent_mono_ms)) return this.items.find(item => item.mono === echo.sent_mono_ms) ?? null;
    return null;
  }
  clear() { this.items = []; }
}

/** Latest-only pose pacing: at most POSE_HZ, and never queue behind an unsent pose. */
export function shouldSendPose(lastMono, nowMono, bufferedAmount) {
  return bufferedAmount === 0 && nowMono - lastMono >= 1000 / POSE_HZ;
}

/**
 * Whether the Mac picture replaces the local map. It needs a decoded picture; when
 * frames stop for MAC_STALE_MS the local map returns until frames are fresh again.
 */
export function macPresenting(active, lastFrameAt, now) {
  return active && Number.isFinite(lastFrameAt) && now - lastFrameAt < MAC_STALE_MS;
}

/** Picture size at `distance` for a vertical FOV and aspect, metres. */
export function pictureSize(fovYDeg, aspect, distance = PICTURE_DISTANCE) {
  const fov = Number.isFinite(fovYDeg) && fovYDeg > 1 && fovYDeg < 170 ? fovYDeg : DEFAULT_FOV_Y_DEG;
  const height = 2 * distance * Math.tan((fov * Math.PI) / 360);
  return { width: height * (Number.isFinite(aspect) && aspect > 0 ? aspect : DEFAULT_ASPECT), height };
}

/** `?macview=1` opts in; anything else keeps the local map. */
export function macViewEnabled(search) {
  return new URLSearchParams(search).get("macview") === "1";
}
