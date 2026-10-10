// Walk-through map: in XR the reconstructed map is drawn 1:1 with its floor on the
// headset floor, anchored so the operator starts where the rover started (map origin).
// Head tracking stays free; locomotion (owned by the controls/HUD layer) moves a
// virtual viewer through the map with moveViewer(). Display only: nothing here
// sends commands or gates motion.
//
// Map point p (metres, map frame, Y up) is drawn at world = Ry(yaw) * (p - viewer).

/** @typedef {"overview" | "walk"} MapMode */
export const MAP_MODES = /** @type {const} */ (["overview", "walk"]);

export class MapWalk {
  /** @type {MapMode} */ mode = "overview";
  /** Viewer anchor in map coordinates; y is the map height drawn at the headset floor. */
  x = 0; y = 0; z = 0;
  /** Extra map rotation from locomotion turns, radians about +Y. */
  yaw = 0;
  /** Latest headset pose in world coordinates (from the XR camera). */
  headX = 0; headZ = 0; headYaw = 0;
  floorY = 0;
  #lift = 0;

  /** @param {MapMode} mode */
  setMode(mode) {
    if (!MAP_MODES.includes(mode)) throw new Error(`Unknown map mode ${mode}`);
    this.mode = mode;
  }
  get walking() { return this.mode === "walk"; }

  /** Stand at the rover start pose again (map origin, floor height). */
  reset() {
    this.x = this.z = this.yaw = this.#lift = 0;
    this.y = this.floorY;
  }

  /** Floor of the current map; keeps any vertical locomotion offset. */
  setFloor(floorY) {
    if (!Number.isFinite(floorY)) return;
    this.floorY = floorY;
    this.y = floorY + this.#lift;
  }

  /** @param {number} x @param {number} z @param {number} yaw headset world pose */
  observeHead(x, z, yaw) {
    if ([x, z, yaw].every(Number.isFinite)) { this.headX = x; this.headZ = z; this.headYaw = yaw; }
  }

  /** Move the viewer relative to where the headset faces.
   * @param {number} forward metres along the gaze heading
   * @param {number} [right] metres to the right
   * @param {number} [up] metres up
   * @param {number} [turn] radians, positive turns the viewer left */
  moveViewer(forward, right = 0, up = 0, turn = 0) {
    if (![forward, right, up, turn].every(Number.isFinite)) return;
    const s = Math.sin(this.headYaw), c = Math.cos(this.headYaw);
    // World displacement of the gaze frame, then into map coordinates via Ry(-yaw).
    const wx = -s * forward + c * right, wz = -c * forward - s * right;
    const ys = Math.sin(-this.yaw), yc = Math.cos(-this.yaw);
    this.x += yc * wx + ys * wz;
    this.z += -ys * wx + yc * wz;
    this.#lift += up; this.y = this.floorY + this.#lift;
    if (turn) {
      // Turn about the head, not the world origin: keep the map point under the head fixed.
      const under = this.#toMap(this.headX, this.headZ);
      this.yaw -= turn;
      const after = this.#toMap(this.headX, this.headZ);
      this.x += under[0] - after[0]; this.z += under[1] - after[1];
    }
  }

  /** Map point currently under a world (x, z) position. */
  #toMap(wx, wz) {
    const s = Math.sin(-this.yaw), c = Math.cos(-this.yaw);
    return [c * wx + s * wz + this.x, -s * wx + c * wz + this.z];
  }

  /** Root group pose: rotate by yaw about +Y, then position = -Ry(yaw) * viewer. */
  rootTransform() {
    const s = Math.sin(this.yaw), c = Math.cos(this.yaw);
    return { yaw: this.yaw, position: [-(c * this.x + s * this.z), -this.y, -(-s * this.x + c * this.z)] };
  }
}

/** Shared instance: main.ts renders it, the controls layer drives it. */
export const mapWalk = new MapWalk();

/** Low percentile of map heights as the floor, from at most `samples` evenly strided points.
 * @param {ArrayLike<number>} positions xyz triples */
export function floorHeight(positions, samples = 4096) {
  const count = Math.floor(positions.length / 3);
  if (!count) return NaN;
  const step = Math.max(1, Math.floor(count / samples)), heights = [];
  for (let i = 0; i < count; i += step) {
    const y = positions[i * 3 + 1];
    if (Number.isFinite(y)) heights.push(y);
  }
  if (!heights.length) return NaN;
  heights.sort((a, b) => a - b);
  return heights[Math.floor(heights.length * 0.03)];
}
