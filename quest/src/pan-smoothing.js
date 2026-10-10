export const PAN_LIMIT_DEG = 80;

/** EMA plus slew-rate limit for the head-yaw pan command, in degrees. Pan only; never touches drive or deadman. */
export class PanSmoother {
  constructor({ tauSec = 0.12, maxRateDegPerSec = 240, limit = PAN_LIMIT_DEG } = {}) {
    Object.assign(this, { tauSec, maxRateDegPerSec, limit });
    this.value = 0;
  }
  reset(value = 0) { this.value = Math.max(-this.limit, Math.min(this.limit, value)); return this.value; }
  step(target, dtSec) {
    const goal = Math.max(-this.limit, Math.min(this.limit, Number.isFinite(target) ? target : 0));
    const dt = Number.isFinite(dtSec) ? Math.max(0, dtSec) : 0;
    const ema = this.value + (goal - this.value) * (1 - Math.exp(-dt / this.tauSec));
    const maxStep = this.maxRateDegPerSec * dt;
    this.value += Math.max(-maxStep, Math.min(maxStep, ema - this.value));
    return this.value;
  }
}
