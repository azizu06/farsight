import test from "node:test";
import assert from "node:assert/strict";
import { cueFor, motionTone, controllerHintLabels, SOURCE_CHIP } from "../src/vr-hud.js";
import { explainStop, STOP_CODES } from "../src/stop-reason.js";

test("no stop, no cue: the default headset view has nothing but the chip", () => {
  assert.equal(cueFor(null), null);
  assert.equal(motionTone({ moving: true, unsafe: false, cue: null }), "go");
  assert.equal(motionTone({ moving: false, unsafe: false, cue: null }), "wait");
});

test("a live unsafe condition is red with its cause; a cleared one asks for a release", () => {
  const live = explainStop({ unsafe: "Camera stale: motion stopped", xr: true });
  assert.deepEqual(cueFor(live, true), { tone: "stop", icon: "camera", words: "NO VIDEO" });
  const latched = explainStop({ latched: true, cause: "Camera stale: motion stopped", xr: true });
  assert.deepEqual(cueFor(latched, false), { tone: "wait", icon: "camera", words: "RELEASE TRIGGER" });
  assert.equal(motionTone({ moving: false, unsafe: true, cue: cueFor(live, true) }), "stop");
});

test("link, tracking and motor faults get their own icon", () => {
  assert.equal(cueFor(explainStop({ unsafe: "Control disconnected", xr: true }), true).icon, "link");
  assert.equal(cueFor(explainStop({ latched: true, cause: "XR tracking unavailable: release trigger", xr: true }), false).icon, "controller");
  assert.equal(cueFor(explainStop({ unsafe: "Rover unavailable", xr: true }), true).icon, "motor");
  assert.equal(cueFor(explainStop({ held: true, status: { estop: true }, xr: true })).icon, "blocked");
});

test("mode switches only ask for a trigger release", () => {
  for (const reason of ["Auto: release trigger", "Manual: release trigger", "Stopped: release trigger"])
    assert.deepEqual(cueFor(explainStop({ latched: true, cause: reason, xr: true })), { tone: "wait", icon: "trigger", words: "RELEASE TRIGGER" });
});

test("every known stop fits in three words", () => {
  const reasons = [...STOP_CODES.server.map(code => explainStop({ latched: true, status: { requires_release: true, reason: code }, xr: true })),
    ...STOP_CODES.unsafe.map(code => explainStop({ unsafe: code, xr: true })),
    ...STOP_CODES.client.map(code => explainStop({ latched: true, cause: code, xr: true }))];
  for (const stop of reasons) {
    const cue = cueFor(stop, false);
    assert.ok(cue.words.split(" ").length <= 3, `${stop.code}: ${cue.words}`);
  }
});

test("source chip keeps the truthful labels and the hint is words, not sentences", () => {
  assert.deepEqual(Object.fromEntries(Object.entries(SOURCE_CHIP).map(([kind, chip]) => [kind, chip.text])),
    { LIVE: "LIVE", UNVERIFIED: "NOT VERIFIED", SIMULATED: "SIMULATED", REPLAY: "REPLAY" });
  for (const mode of ["manual", "auto"]) for (const label of controllerHintLabels(mode)) assert.ok(label.words.split(" ").length <= 4, label.words);
  assert.equal(controllerHintLabels("auto").find(label => label.part === "stick").words, "FLY");
  assert.equal(controllerHintLabels("manual").find(label => label.part === "stick").words, "DRIVE");
});
