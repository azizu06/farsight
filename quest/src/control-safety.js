/** Safety decisions use local monotonic time. The rover independently expires its permits. */
function inputFresh(now, sampledAt, limit = 150) {
  return sampledAt === undefined || (Number.isFinite(sampledAt) && sampledAt >= 0 &&
    now >= sampledAt && now - sampledAt < limit);
}
/** Demo mode tolerates XR input gaps this long (a tracking blip) before treating input as stale. */
const DEMO_INPUT_MS = 1000;

/** Server-side stalls that demo hands-free AUTO rides out instead of releasing (see `handsFree`). */
const HOLDABLE = new Set(["Control status stale", "Waiting for fresh control permit"]);

/** Latch causes that only restate "release first"; a specific cause may replace them. */
const GENERIC_CAUSES = new Set(["Waiting for live camera and rover", "Release deadman to continue",
  "Connected: release deadman", "New control session: release deadman", "release_required", "deadman_released"]);

/** Optional diagnostics receive copied primitives after a decision is committed. */
function traceSafety(value) {
  try {
    const trace = globalThis.__farsightControlSafetyTrace;
    if (typeof trace === "function") trace(value);
  } catch { /* Diagnostics cannot affect stopping or recovery. */ }
}

export class ControlSafety {
  /** @param {{ demo?: boolean }} [options] demo: see demo-config.js; the owner may toggle `demo` per frame. */
  constructor({ demo = false } = {}) {
    this.demo = demo;
    /**
     * Demo hands-free AUTO (set by the owner per frame). A late status or permit then
     * holds Explore armed and sends no release: only B or leaving AUTO stops it. The
     * rover still expires its own permits and lease.
     */
    this.handsFree = false;
    /** This tick is riding out a late status/permit; nothing was invalidated. */
    this.holding = false;
    /** A deliberate stop (B, mode change, viewer closed) always needs a real release, even in demo mode. */
    this.hardLatch = false;
    this.needsRelease = true;
    this.session = null;
    this.status = null;
    this.receivedAt = -Infinity;
    this.frameAt = -Infinity;
    this.frameAge = Infinity;
    this.frameId = null;
    this.reason = "Waiting for live camera and rover";
    /** First specific cause of the current release latch, kept for display only. */
    this.cause = this.reason;
  }
  invalidate(reason = "Release deadman to continue", hard = false) {
    if (hard) this.hardLatch = true;
    if (!this.needsRelease || this.cause === null || GENERIC_CAUSES.has(this.cause)) this.cause = reason;
    this.needsRelease = true;
    this.reason = reason;
    traceSafety({ event: "stop", reason, cause: this.cause,
      needsRelease: this.needsRelease, hardLatch: this.hardLatch });
  }
  stop(input, reason, hard = false) {
    // Zero commands without inventing a release of the physical trigger/key.
    this.invalidate(reason, hard);
    return { ...input, v: 0, w: 0 };
  }
  acceptStatus(status, now) {
    if (!status || typeof status.session_id !== "string" ||
        typeof status.permit !== "string" || !Number.isFinite(status.permit_ms)) return false;
    if (this.session !== status.session_id) {
      this.invalidate("New control session: release deadman");
      this.session = status.session_id;
      this.frameAt = -Infinity;
      this.frameAge = Infinity;
      this.frameId = null;
    }
    if (this.status?.rover && !status.rover) this.invalidate("Rover unavailable");
    if (status.requires_release) this.invalidate(status.reason ?? "Release deadman to continue");
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
    traceSafety({ event: "frame", age });
    return true;
  }
  videoAge(now) { return this.frameAge + Math.max(0, now - this.frameAt); }
  /** @param {number} [sampledAt] Most recent verified XR input sample. */
  readiness(now, connected = true, sampledAt = undefined) {
    if (!connected) return "Control disconnected";
    if (!inputFresh(now, sampledAt, this.demo ? DEMO_INPUT_MS : 150)) return "XR input stale: release trigger";
    if (now - this.receivedAt >= 300) return "Control status stale";
    if (!this.status?.rover) return "Rover unavailable";
    if (!this.demo && this.videoAge(now) >= 500) return "Camera stale: motion stopped";
    if (now - this.receivedAt >= Math.min(this.status.permit_ms, 200)) return "Waiting for fresh control permit";
    return null;
  }
  /** @param {number} [sampledAt] Most recent verified XR input sample. */
  decide(held, now, connected = true, inputAvailable = true, sampledAt = undefined) {
    if (!inputAvailable) {
      this.invalidate("Input or display inactive: release deadman");
      return false;
    }
    let unsafe = this.readiness(now, connected, sampledAt);
    this.holding = !!unsafe && this.demo && this.handsFree && HOLDABLE.has(unsafe);
    if (this.holding) { this.reason = "Holding: waiting for rover status"; unsafe = null; }
    if (unsafe) this.invalidate(unsafe);
    // Missing tracking cannot prove a physical trigger release.
    if (!held && inputFresh(now, sampledAt)) { this.needsRelease = false; this.cause = null; this.hardLatch = false; }
    else if (this.demo && held && this.needsRelease && !this.hardLatch && !unsafe) {
      // Demo: a transient stop needs no re-press. This tick sends the release the rover requires; the next resumes.
      this.needsRelease = false; this.cause = null; this.reason = "Resuming";
      return false;
    }
    const moving = held && !unsafe && !this.needsRelease;
    if (!unsafe && !this.holding) this.reason = this.needsRelease ? "Release deadman to continue" : moving ? "Driving" : "Ready: hold deadman to drive";
    return moving;
  }
  /** Whether the last permit can still be valid on the rover; an expired one would only make it stop. */
  permitLive(now) { return Number.isFinite(this.status?.permit_ms) && now - this.receivedAt < this.status.permit_ms; }
}

/** Deadman trigger with hysteresis: engage above 0.6, release below 0.25. An analog trigger resting
 * near the browser's own `pressed` threshold no longer flickers deadman on/off while it is held.
 * Release stays immediate once the finger lets go below the low threshold; a missing button releases.
 * @param {boolean} wasHeld @param {{ pressed?: boolean, value?: number } | undefined} button */
export const TRIGGER_ENGAGE = 0.6, TRIGGER_RELEASE = 0.25;
export function triggerHeld(wasHeld, button) {
  if (!button) return false;
  const value = Number.isFinite(button.value) && (button.value > 0 || !button.pressed) ? button.value : button.pressed ? 1 : 0;
  return wasHeld ? value >= TRIGGER_RELEASE : value > TRIGGER_ENGAGE;
}

export function stick(value, deadzone = 0.18) {
  if (!Number.isFinite(value) || Math.abs(value) <= deadzone) return 0;
  return Math.sign(value) * Math.min(1, (Math.abs(value) - deadzone) / (1 - deadzone));
}
