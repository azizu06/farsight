/** Virtual locomotion for AUTO / free roam: the right stick moves the operator through the map,
 * never the rover. Pure math on plain {x, y, z} objects so it is testable without three.js. */

/** Stick deflection inside this radius is drift, not intent. */
export const ROAM_DEADZONE = 0.25;
/** Forward/back speed at full deflection, in map metres per second. */
export const ROAM_SPEED = 1.5;
/** Comfort snap turn per stick flick. */
export const ROAM_SNAP = Math.PI / 6;

function dead(value) {
  if (!Number.isFinite(value) || Math.abs(value) <= ROAM_DEADZONE) return 0;
  return Math.sign(value) * Math.min(1, (Math.abs(value) - ROAM_DEADZONE) / (1 - ROAM_DEADZONE));
}

export class FreeRoam {
  x = 0;
  z = 0;
  yaw = 0;
  turnArmed = true;
  /** Back to the anchored start: no offset, no extra turn. */
  reset() { this.x = 0; this.z = 0; this.yaw = 0; this.turnArmed = true; }
  /**
   * Advance one frame. `stickX` snap-turns (flick and return), `stickY` (forward is negative, as on
   * the Quest) moves along the operator's head heading inside the turned virtual frame.
   * @param {number} dt seconds since the last step (clamped to 0.1)
   * @param {number} stickX right thumbstick x
   * @param {number} stickY right thumbstick y
   * @param {number} headYaw head yaw in world radians (0 faces -z)
   */
  step(dt, stickX, stickY, headYaw) {
    const turn = dead(stickX);
    if (!turn) this.turnArmed = true;
    else if (this.turnArmed && Math.abs(turn) > 0.5) {
      this.yaw -= Math.sign(turn) * ROAM_SNAP;
      this.turnArmed = false;
    }
    const forward = -dead(stickY);
    if (forward && Number.isFinite(dt)) {
      const heading = headYaw + this.yaw, distance = forward * ROAM_SPEED * Math.min(Math.max(dt, 0), 0.1);
      this.x -= Math.sin(heading) * distance;
      this.z -= Math.cos(heading) * distance;
    }
    return { x: this.x, z: this.z, yaw: this.yaw };
  }
}

/**
 * Move a world group (e.g. the map root) opposite to the virtual viewer, pivoting the turn about
 * the operator's real head so turning never swings the room around a far-away origin.
 * Call after any other placement of the group each frame. Works on three.js objects.
 * @param {{ position: any, quaternion: any }} group
 * @param {{ x: number, z: number, yaw: number }} roam
 * @param {{ x: number, z: number }} pivot real head position in world space
 */
export function applyRoam(group, roam, pivot) {
  const c = Math.cos(-roam.yaw), s = Math.sin(-roam.yaw);
  const px = group.position.x - roam.x - pivot.x, pz = group.position.z - roam.z - pivot.z;
  group.position.x = c * px + s * pz + pivot.x;
  group.position.z = -s * px + c * pz + pivot.z;
  // Pre-multiply a world yaw rotation: q = yaw(-roam.yaw) * q.
  const h = -roam.yaw / 2, qy = Math.sin(h), qw = Math.cos(h), q = group.quaternion;
  const { x, y, z, w } = q;
  q.set(qw * x + qy * z, qw * y + qy * w, qw * z - qy * x, qw * w - qy * y);
}
