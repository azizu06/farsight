/** Manual drive at the operator's recorded pace: 0.6 of full on both axes (PWM 181 of 255,
 * about 0.20 m/s), arcs and pivots unchanged in shape (Dechante's manual run, 2026-10-11).
 * Input is already dead-zoned; the server retains its configured PWM limits. */
export const MANUAL_SPEED = 0.6;

export function manualDrive(forward, steering) {
  const v = Number.isFinite(forward) ? Math.max(-1, Math.min(1, forward)) : 0;
  const w = Number.isFinite(steering) ? Math.max(-1, Math.min(1, steering)) : 0;
  return { v: v * MANUAL_SPEED, w: w * MANUAL_SPEED };
}
