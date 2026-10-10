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
test("a new logical controller cannot use the preceding session's fresh image", () => {
  const gate = ready();
  gate.acceptStatus({ ...status, session_id: "two" }, 110);
  assert.equal(gate.frameId, null);
  assert.equal(gate.videoAge(110), Infinity);
  gate.decide(false, 111);
  assert.equal(gate.decide(true, 112), false, "release alone does not replace fresh pixels");
  gate.acceptFrame("1", 10, 120);
  gate.decide(false, 121);
  assert.equal(gate.decide(true, 122), true, "a new image may reuse a restarted frame number");
});
test("same logical session status keeps its fresh image during an upstream path change", () => {
  const gate = ready();
  gate.acceptStatus({ ...status, permit: "new-permit" }, 110);
  assert.equal(gate.frameId, "1");
  assert.equal(gate.decide(true, 111), true);
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

test("deadman trigger hysteresis: a held trigger hovering near the pressed threshold never flickers", async () => {
  const { triggerHeld } = await import("../src/control-safety.js");
  let held = false;
  const step = (value, pressed = value > 0.1) => (held = triggerHeld(held, { value, pressed }));
  assert.equal(step(0.5), false, "a light touch does not engage");
  assert.equal(step(0.7), true, "engages above 0.6");
  // The rig log showed 26 deadman flips while the trigger was held: analog wobble around the browser threshold.
  for (const value of [0.45, 0.3, 0.55, 0.26, 0.4, 0.28]) assert.equal(step(value), true, `still held at ${value}`);
  assert.equal(step(0.2), false, "release is immediate below 0.25");
  assert.equal(step(0.5), false, "re-engaging needs the high threshold again");
  assert.equal(triggerHeld(true, undefined), false, "a missing trigger releases");
  assert.equal(triggerHeld(false, { pressed: true }), true, "digital-only buttons still work");
  assert.equal(triggerHeld(true, { pressed: false }), false);
});

test("demo mode: stale video warns but does not block; transient stops resume with one release tick; hard stops still need a re-press", async () => {
  const { demoMode } = await import("../src/demo-config.js");
  assert.equal(demoMode(""), true, "demo is the default build");
  assert.equal(demoMode("?demo=0"), false, "?demo=0 restores every client guard");
  const gate = ready();
  gate.demo = true;
  assert.equal(gate.decide(true, 110, true, true, 110), true);
  gate.acceptStatus(status, 590);
  assert.equal(gate.decide(true, 590, true, true, 590), true, "camera older than 500 ms no longer blocks on the client");
  assert.equal(gate.readiness(590, true, 590), null);
  // A transient stop (link hiccup, menu, tracking) while the trigger stays held.
  gate.invalidate("Control queue stalled: release deadman");
  assert.equal(gate.decide(true, 600, true, true, 600), false, "this tick sends the release the rover requires");
  assert.equal(gate.decide(true, 610, true, true, 610), true, "the next tick resumes without a physical re-press");
  // Input gaps up to 1 s are a blip, not stale input.
  assert.notEqual(gate.readiness(1500, true, 600), "XR input stale: release trigger", "900 ms gap");
  assert.equal(gate.readiness(1600, true, 600), "XR input stale: release trigger", "1 s gap");
  // B, a mode switch or closing the viewer is hard: a real release is still required.
  gate.invalidate("Stopped: release trigger", true);
  assert.equal(gate.decide(true, 620, true, true, 620), false);
  assert.equal(gate.decide(true, 630, true, true, 630), false, "no virtual release after a hard stop");
  assert.equal(gate.decide(false, 640, true, true, 640), false);
  assert.equal(gate.decide(true, 650, true, true, 650), true);
  // Link and lease limits are untouched.
  assert.equal(gate.readiness(650, false, 650), "Control disconnected");
  assert.equal(gate.readiness(1000, true, 1000), "Control status stale");
  gate.demo = false;
  assert.equal(gate.readiness(650, true, 650), "Camera stale: motion stopped", "?demo=0 restores the 500 ms block");
});
