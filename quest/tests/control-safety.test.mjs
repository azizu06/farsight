import test from "node:test";
import assert from "node:assert/strict";
import { ControlSafety, stick } from "../src/control-safety.js";
const status = { session_id: "one", permit: "issued-on-rover", permit_ms: 250, rover: true };
function ready() {
  const gate = new ControlSafety();
  gate.acceptStatus(status, 100);
  gate.acceptFrame("1", 10, 100);
  gate.decide(false, 100);
  return gate;
}
test("fresh camera and echoed permit still require deadman release", () => {
  const gate = new ControlSafety();
  gate.acceptStatus(status, 100);
  gate.acceptFrame("1", 10, 100);
  assert.equal(gate.decide(true, 110), false);
  assert.equal(gate.decide(false, 120), false);
  assert.equal(gate.decide(true, 130), true);
});
test("stale video stops and recovery while held never resumes", () => {
  const gate = ready();
  assert.equal(gate.decide(true, 110), true);
  gate.acceptStatus(status, 590);
  assert.equal(gate.decide(true, 590), false);
  gate.acceptFrame("2", 10, 600);
  gate.acceptStatus(status, 600);
  assert.equal(gate.decide(true, 601), false);
  gate.decide(false, 610);
  assert.equal(gate.decide(true, 611), true);
});
test("duplicate decoded images and malformed freshness cannot renew video", () => {
  const gate = ready();
  assert.equal(gate.acceptFrame("1", 0, 1000), false);
  assert.equal(gate.acceptFrame("2", NaN, 1000), false);
  assert.equal(gate.acceptFrame(null, 0, 1000), false);
  assert.equal(gate.videoAge(1000), 910);
});
test("disconnect, server session changes, rover loss, and permit silence require release", () => {
  for (const fault of [
    gate => gate.decide(true, 110, false),
    gate => gate.acceptStatus({ ...status, session_id: "two" }, 110),
    gate => gate.acceptStatus({ ...status, rover: false }, 110),
    gate => gate.decide(true, 310),
  ]) {
    const gate = ready();
    fault(gate);
    gate.acceptStatus({ ...status, session_id: gate.session }, 320);
    gate.acceptFrame("2", 0, 320);
    assert.equal(gate.decide(true, 320), false);
    gate.decide(false, 321);
    assert.equal(gate.decide(true, 322), true);
  }
});
test("stick ignores invalid values and drift, scales bounded travel", () => {
  assert.equal(stick(NaN), 0);
  assert.equal(stick(0.1), 0);
  assert.equal(stick(-1), -1);
  assert.equal(stick(2), 1);
  assert.ok(stick(0.5) > 0 && stick(0.5) < 0.5);
});

test("XR samples expire at 150 ms even while video and control permits remain fresh", () => {
  const gate = ready();
  assert.equal(gate.decide(true, 110, true, true, 100), true);
  gate.acceptStatus(status, 249);
  gate.acceptFrame("2", 0, 249);
  assert.equal(gate.decide(true, 249, true, true, 100), true);
  assert.equal(gate.decide(true, 250, true, true, 100), false);
  assert.match(gate.reason, /XR input stale/);
});

test("missing XR tracking cannot masquerade as a released trigger", () => {
  const gate = ready();
  assert.equal(gate.decide(true, 110, true, true, 100), true);
  assert.equal(gate.decide(false, 120, true, true, -Infinity), false);
  assert.equal(gate.decide(true, 130, true, true, 130), false);
  assert.equal(gate.decide(false, 140, true, true, 140), false);
  assert.equal(gate.decide(true, 150, true, true, 150), true);
});

test("invalid or future XR sample timestamps cannot authorize motion", () => {
  for (const sampledAt of [NaN, Infinity, -1, 201]) {
    const gate = ready();
    assert.equal(gate.decide(true, 200, true, true, sampledAt), false);
    assert.equal(gate.needsRelease, true);
  }
});

test("explicit stop preserves physical deadman across timer/render ordering", () => {
  const gate = ready();
  const physicalInput = { held: true, v: 0.4, w: 0.2, pan: 10 };
  assert.equal(gate.decide(physicalInput.held, 110), true);
  // Fault/STOP runs between input sampling and the next 20 Hz command pump.
  const stoppedInput = gate.stop(physicalInput, "STOP pressed");
  assert.equal(stoppedInput.held, true);
  assert.equal(stoppedInput.v, 0);
  assert.equal(stoppedInput.w, 0);
  assert.equal(gate.decide(stoppedInput.held, 120), false);
  // Rendering samples the still-held trigger again; it cannot resume motion.
  assert.equal(gate.decide(physicalInput.held, 130), false);
  gate.decide(false, 140);
  assert.equal(gate.decide(true, 150), true);
});

test("server release requirement overrides a locally fresh connection", () => {
  const gate = ready();
  assert.equal(gate.decide(true, 110), true);
  gate.acceptStatus({ ...status, requires_release: true, reason: "expired_permit" }, 120);
  assert.equal(gate.decide(true, 121), false);
  gate.acceptStatus({ ...status, requires_release: false }, 130);
  assert.equal(gate.decide(true, 131), false);
  gate.decide(false, 140);
  assert.equal(gate.decide(true, 150), true);
});

test("inactive headset or unobserved input cannot invent a deadman release", () => {
  for (const reportedHeld of [true, false]) {
    const gate = ready();
    assert.equal(gate.decide(true, 110), true);
    // A hidden headset, absent controller, or frozen display may report false;
    // that is unavailable input, rather than evidence of a physical release.
    assert.equal(gate.decide(reportedHeld, 120, true, false), false);
    gate.acceptStatus(status, 130);
    gate.acceptFrame("2", 0, 130);
    assert.equal(gate.decide(true, 131, true, true), false);
    gate.decide(false, 140, true, true);
    assert.equal(gate.decide(true, 150, true, true), true);
  }
});

test("XR input age uses monotonic observation time, not the predicted XR display time", async () => {
  const { readFileSync } = await import("node:fs");
  const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  // The frame start may be held in a local (diagnostics timing) as long as it is performance.now().
  assert.match(main, /setAnimationLoop\(\(_time, frame\) => \{\s*(?:rover\?\.update\(performance\.now\(\), frame\)|const (\w+) = performance\.now\(\);\s*rover\?\.update\(\1, frame\))/);
});
