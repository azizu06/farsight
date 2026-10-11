/** Full manual authority: preserve forward demand while steering.
 * Input is already dead-zoned; the server retains its configured PWM limits. */
export function manualDrive(forward, steering) {
  const v = Number.isFinite(forward) ? Math.max(-1, Math.min(1, forward)) : 0;
  const w = Number.isFinite(steering) ? Math.max(-1, Math.min(1, steering)) : 0;
  return { v, w };
}

/** Quest joystick pace cap (Dechante's measured manual run: v=0.6 straight is PWM 181/181, ~0.20 m/s;
 * w=0.6 spins at ±181; v=0.6 + w=0.6 is 255/0). Keyboard/desktop driving is not capped. */
export const JOYSTICK_PACE = 0.6;

/** Joystick drive: the shaped stick (deadzone, expo, lock, ease-in) scaled to the manual pace. */
export function joystickDrive(forward, steering) {
  const { v, w } = manualDrive(forward, steering);
  return { v: v * JOYSTICK_PACE + 0, w: w * JOYSTICK_PACE + 0 };
}

/** Quest right-stick steering shape: turn only on purpose. Forward speed is not shaped here. */
export const STEER = {
  /** |x| below this is no turn; past it the rest of the range is rescaled from 0, so there is no jump. */
  deadzone: 0.25,
  /** w = sign(x) * |x|^expo after the deadzone: fine control near center, full pivot at the edge. */
  expo: 1, // Dechante's arrow-key run turned at full w instantly; linear stick (was 2.5)
  /** A push within this angle of straight forward/back drives straight (w = 0). */
  lockDeg: 20,
};

/**
 * Steering demand from the raw right-stick axes. A mostly-forward push that drifts a little
 * sideways stays straight (axis lock); a deliberate sideways push still reaches w = ±1.
 * @param {number} x raw stick x (right positive) @param {number} y raw stick y (forward negative)
 */
export function steerCurve(x, y) {
  const ax = Number.isFinite(x) ? Math.abs(x) : 0, ay = Number.isFinite(y) ? Math.abs(y) : 0;
  if (ax <= STEER.deadzone) return 0;
  if (ay > STEER.deadzone && Math.atan2(ax, ay) < STEER.lockDeg * Math.PI / 180) return 0;
  const shaped = Math.min(1, (ax - STEER.deadzone) / (1 - STEER.deadzone));
  return Math.sign(x) * shaped ** STEER.expo;
}

/** Quest right-stick acceleration (captain, 057): speed builds instead of jumping 0 -> full, and
 * releasing the stick slows down quickly but not instantly. Overrides Dechante's 0.1 s ease-in (79fef73).
 * Rates are for full-scale demand ([-1, 1], before JOYSTICK_PACE); a partial push gets there sooner. */
export const DRIVE = {
  /** Rest to full forward (or reverse) demand (s). */
  accelSeconds: 0.7,
  /** Full demand to rest when the stick is eased off, released or reversed (s). */
  decelSeconds: 0.25,
  /** Steering: no turn to a full pivot (s), and a full pivot back to straight (s). */
  steerSeconds: 0.3,
  steerReleaseSeconds: 0.15,
  /** Longest frame gap credited to the ramps (s), so a stalled frame cannot skip them. */
  maxStep: 0.1,
};

/** Move `value` toward `goal`: speeding up (same sign, larger) at `up` per s, anything else at `down` per s. */
function slew(value, goal, dt, upSeconds, downSeconds) {
  const speedingUp = Math.sign(goal) !== 0 && Math.sign(goal) === Math.sign(value || goal) && Math.abs(goal) > Math.abs(value);
  // Slowing toward rest, or reversing: never cross zero faster than the decel rate allows.
  const limit = dt / (speedingUp ? upSeconds : downSeconds);
  const target = !speedingUp && Math.sign(goal) !== 0 && Math.sign(goal) !== Math.sign(value) && value !== 0 ? 0 : goal;
  const next = value + Math.max(-limit, Math.min(limit, target - value));
  return Math.abs(next) < 1e-9 ? 0 : next; // snap float residue (and -0) to rest
}

/**
 * Rate-limits Quest stick demand: forward/reverse builds over DRIVE.accelSeconds, easing off or
 * releasing decays over DRIVE.decelSeconds, and steering changes over DRIVE.steerSeconds. A reversal
 * slows through rest first. reset() is the hard stop (B, deadman, tracking loss, faults): output 0 at once.
 */
export class DriveRamp {
  constructor() { this.reset(); }
  reset() { this.v = 0; this.w = 0; this.at = null; }
  /** Still moving (decelerating after a release keeps the motion intent until it reaches rest). */
  get moving() { return this.v !== 0 || this.w !== 0; }
  /** @param {number} forward stick demand in [-1, 1] @param {number} steering in [-1, 1] @param {number} now ms */
  step(forward, steering, now) {
    const v = Number.isFinite(forward) ? Math.max(-1, Math.min(1, forward)) : 0;
    const w = Number.isFinite(steering) ? Math.max(-1, Math.min(1, steering)) : 0;
    const dt = this.at === null || !Number.isFinite(now) ? 0 : Math.min(Math.max(0, (now - this.at) / 1000), DRIVE.maxStep);
    this.at = Number.isFinite(now) ? now : null;
    this.v = slew(this.v, v, dt, DRIVE.accelSeconds, DRIVE.decelSeconds);
    this.w = slew(this.w, w, dt, DRIVE.steerSeconds, DRIVE.steerReleaseSeconds);
    return { v: this.v, w: this.w };
  }
}
