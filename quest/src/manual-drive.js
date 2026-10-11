/** Full manual authority: preserve forward demand while steering.
 * Input is already dead-zoned; the server retains its configured PWM limits. */
export function manualDrive(forward, steering) {
  const v = Number.isFinite(forward) ? Math.max(-1, Math.min(1, forward)) : 0;
  const w = Number.isFinite(steering) ? Math.max(-1, Math.min(1, steering)) : 0;
  return { v, w };
}

/** Quest right-stick steering shape: turn only on purpose. Forward speed is not shaped here. */
export const STEER = {
  /** |x| below this is no turn; past it the rest of the range is rescaled from 0, so there is no jump. */
  deadzone: 0.25,
  /** w = sign(x) * |x|^expo after the deadzone: fine control near center, full pivot at the edge. */
  expo: 2.5,
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

/** Quest right-stick forward ease-in (captain: start ~10% softer, then build up so it never jerks). */
export const DRIVE = {
  /** Gain reached at the end of the onset rise, from rest. */
  onsetGain: 0.9,
  /** Onset rise (s): the gain climbs from 0 to onsetGain, so there is no step at the first push. */
  onsetSeconds: 0.3,
  /** Then, while forward is still held, the gain climbs smoothly from onsetGain to 1 over this long (s). */
  climbSeconds: 2,
  /** Longest frame gap credited to the ease-in (s), so a stalled frame cannot skip it. */
  maxStep: 0.1,
};

/** Forward gain after `held` seconds of continuous forward (or reverse) demand. */
export function forwardGain(held) {
  const t = Number.isFinite(held) && held > 0 ? held : 0;
  if (t < DRIVE.onsetSeconds) return DRIVE.onsetGain * t / DRIVE.onsetSeconds;
  const k = Math.min(1, (t - DRIVE.onsetSeconds) / DRIVE.climbSeconds);
  return DRIVE.onsetGain + (1 - DRIVE.onsetGain) * k * k * (3 - 2 * k);
}

/**
 * Eases forward/back demand in from rest. Output never exceeds the stick's demand, so
 * easing off, release, B, deadman and a reversal (through zero) all apply immediately;
 * returning to center or reversing restarts the ease-in.
 */
export class ForwardRamp {
  constructor() { this.reset(); }
  reset() { this.v = 0; this.held = 0; this.sign = 0; this.at = null; }
  /** @param {number} demand stick demand in [-1, 1] @param {number} now ms */
  step(demand, now) {
    const goal = Number.isFinite(demand) ? Math.max(-1, Math.min(1, demand)) : 0;
    const dt = this.at === null || !Number.isFinite(now) ? 0 : Math.min(Math.max(0, (now - this.at) / 1000), DRIVE.maxStep);
    this.at = Number.isFinite(now) ? now : null;
    const sign = Math.sign(goal);
    if (sign === 0 || sign !== this.sign) { this.held = 0; this.sign = sign; }
    else this.held += dt;
    this.v = goal * forwardGain(this.held) + 0; // never -0
    return this.v;
  }
}
