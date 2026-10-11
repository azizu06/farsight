const wrap = value => Math.atan2(Math.sin(value), Math.cos(value));
const DEADBAND = 5 * Math.PI / 180;
const FULL_TURN = 45 * Math.PI / 180;

/** Relative head-to-chassis alignment. Both yaws are left-positive; motor w is right-positive.
 * No command integration: only current, measured phone orientation closes the loop. */
export class HeadSteering {
  constructor() { this.enabled = false; this.reference = null; }
  setEnabled(enabled) { this.enabled = enabled; this.recenter(); }
  recenter() { this.reference = null; }
  step(headYaw, heading, now, receivedAt, override = 0) {
    const idle = {w:override, ready:false, cameraYaw:headYaw};
    if (!this.enabled) return idle;
    // Same one-second pose freshness contract as the existing navigation lane.
    if (!Number.isFinite(headYaw) || !heading || !Number.isFinite(heading.yaw_rad) ||
        !Number.isFinite(heading.age_ms) || heading.age_ms < 0 ||
        heading.age_ms + Math.max(0, now - receivedAt) >= 1000 ||
        !['arkit','simulated'].includes(heading.source) || typeof heading.session_id !== 'string' ||
        !Number.isSafeInteger(heading.map_epoch) || !Number.isSafeInteger(heading.socket_generation)) {
      this.recenter(); return idle;
    }
    const world = JSON.stringify([heading.session_id,heading.map_epoch,heading.socket_generation]);
    if (!this.reference || this.reference.world !== world || override) {
      this.reference = {head:headYaw, rover:heading.yaw_rad, world};
    }
    const cameraYaw = wrap(this.reference.head + wrap(heading.yaw_rad - this.reference.rover));
    const error = wrap(headYaw - cameraYaw);
    const turn = Math.max(0, Math.abs(error) - DEADBAND) / (FULL_TURN - DEADBAND);
    return {w:override || -Math.sign(error) * Math.min(1, turn) + 0, ready:true, cameraYaw};
  }
}
