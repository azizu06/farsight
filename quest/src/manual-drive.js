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
