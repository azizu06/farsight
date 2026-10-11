/** Full manual authority: prioritize steering torque instead of dragging one wheel.
 * Input is already dead-zoned; the server retains its configured PWM limits. */
export function manualDrive(forward, steering) {
  const v = Number.isFinite(forward) ? Math.max(-1, Math.min(1, forward)) : 0;
  const w = Number.isFinite(steering) ? Math.max(-1, Math.min(1, steering * 2)) : 0;
  return { v: v * (1 - Math.abs(w)), w };
}
