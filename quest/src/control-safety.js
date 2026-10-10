/** Safety decisions use local monotonic time. The rover independently expires its permits. */
function inputFresh(now, sampledAt) {
  return sampledAt === undefined || (Number.isFinite(sampledAt) && sampledAt >= 0 &&
    now >= sampledAt && now - sampledAt < 150);
}

export class ControlSafety {
  constructor() {
    this.needsRelease = true;
    this.session = null;
    this.status = null;
    this.receivedAt = -Infinity;
    this.frameAt = -Infinity;
    this.frameAge = Infinity;
    this.frameId = null;
    this.reason = "Waiting for live camera and rover";
  }
  invalidate(reason = "Release deadman to continue") {
    this.needsRelease = true;
    this.reason = reason;
  }
  acceptStatus(status, now) {
    if (!status || typeof status.session_id !== "string" ||
        typeof status.permit !== "string" || !Number.isFinite(status.permit_ms)) return false;
    if (this.session !== status.session_id) {
      this.invalidate("New control session: release deadman");
      this.session = status.session_id;
    }
    if (this.status?.rover && !status.rover) this.invalidate("Rover unavailable");
    this.status = status;
    this.receivedAt = now;
    return true;
  }
  acceptFrame(id, age, now) {
    // Seeing the same buffered frame again must never renew camera freshness.
    if (id === null || id === "" || id === this.frameId || !Number.isFinite(age) || age < 0) return false;
    this.frameId = id;
    this.frameAge = age;
    this.frameAt = now;
    return true;
  }
  videoAge(now) { return this.frameAge + Math.max(0, now - this.frameAt); }
  readiness(now, connected = true, sampledAt = undefined) {
    if (!connected) return "Control disconnected";
    if (!inputFresh(now, sampledAt)) return "XR input stale: release trigger";
    if (now - this.receivedAt >= 300) return "Control status stale";
    if (!this.status?.rover) return "Rover unavailable";
    if (this.videoAge(now) >= 500) return "Camera stale: motion stopped";
    if (now - this.receivedAt >= Math.min(this.status.permit_ms, 200)) return "Waiting for fresh control permit";
    return null;
  }
  decide(held, now, connected = true, sampledAt = undefined) {
    const unsafe = this.readiness(now, connected, sampledAt);
    if (unsafe) this.invalidate(unsafe);
    // Missing tracking cannot prove a physical trigger release.
    if (!held && inputFresh(now, sampledAt)) this.needsRelease = false;
    const moving = held && !unsafe && !this.needsRelease;
    if (!unsafe) this.reason = this.needsRelease ? "Release deadman to continue" : moving ? "Driving" : "Ready: hold deadman to drive";
    return moving;
  }
}

export function stick(value, deadzone = 0.18) {
  if (!Number.isFinite(value) || Math.abs(value) <= deadzone) return 0;
  return Math.sign(value) * Math.min(1, (Math.abs(value) - deadzone) / (1 - deadzone));
}
