import test from "node:test";
import assert from "node:assert/strict";
import { cueFor, motionTone, controllerHintLabels, SOURCE_CHIP, drawChip } from "../src/vr-hud.js";
import { explainStop, STOP_CODES } from "../src/stop-reason.js";

test("legacy cue helpers preserve their no-stop behavior", () => {
  assert.equal(cueFor(null), null);
  assert.equal(motionTone({ moving: true, unsafe: false, cue: null }), "go");
  assert.equal(motionTone({ moving: false, unsafe: false, cue: null }), "wait");
});

/** Records calls from the actual painter; no browser/GPU or parallel label implementation. */
function pillCanvas() {
  const calls = [], states = [];
  const context = {
    font: "10px system-ui", textAlign: "left", textBaseline: "alphabetic",
    save() { states.push({ font: this.font, textAlign: this.textAlign, textBaseline: this.textBaseline }); },
    restore() { Object.assign(this, states.pop()); },
    measureText(text) { return { width: text.length * Number(this.font.match(/([\d.]+)px/)[1]) * .58 }; },
    clearRect(...args) { calls.push({ op: "clear", args }); },
    beginPath() {},
    roundRect(...args) { calls.push({ op: "pill", args }); },
    arc(...args) { calls.push({ op: "badge", args }); },
    fill() {}, stroke() {},
    fillText(text, x, y) { calls.push({ op: "text", text, x, y, font: this.font, align: this.textAlign }); },
  };
  return { context, calls };
}

test('the active head-steering mode is visible without extra telemetry',()=>{
  const {context,calls}=pillCanvas();drawChip(context,1024,128,{mode:'head'});
  assert.ok(calls.some(call=>call.op==='text'&&call.text==='HEAD STEERING'));
});

test("actual mode pill draws only MANUAL/AUTONOMOUS and the small A button", () => {
  for (const [mode, expected] of [["manual", "MANUAL"], ["auto", "AUTONOMOUS"]]) {
    const { context, calls } = pillCanvas();
    drawChip(context, 1024, 128, { mode, source: "SIMULATED", tone: "stop" });
    assert.deepEqual(calls.filter(call => call.op === "text").map(call => call.text), [expected, "A"]);
    assert.equal(calls.filter(call => call.op === "pill").length, 1, "one pill, no separate telemetry/source tag");
    assert.equal(calls.filter(call => call.op === "badge").length, 1, "one A affordance, no mode/telemetry icons");
    assert.deepEqual(calls[0], { op: "clear", args: [0, 0, 1024, 128] }, "old source/telemetry pixels cleared");
    const [label, button] = calls.filter(call => call.op === "text");
    assert.ok(Number(button.font.match(/([\d.]+)px/)[1]) < Number(label.font.match(/([\d.]+)px/)[1]), "A is a smaller affordance");
  }
});

test("source/tone changes never add any rendered mode-pill telemetry", () => {
  for (const mode of ["manual", "auto"]) {
    const reference = pillCanvas(); drawChip(reference.context, 1024, 128, { mode });
    for (const source of [...Object.keys(SOURCE_CHIP), "unknown"]) for (const tone of ["go", "wait", "stop"]) {
      const actual = pillCanvas(); drawChip(actual.context, 1024, 128, { mode, source, tone });
      assert.deepEqual(actual.calls, reference.calls, `${mode}/${source}/${tone}`);
    }
  }
});

test("pill defaults to manual and fits narrow canvases without leaking text state", () => {
  const manual = pillCanvas(); drawChip(manual.context, 1024, 128);
  assert.deepEqual(manual.calls.filter(call => call.op === "text").map(call => call.text), ["MANUAL", "A"]);
  const small = pillCanvas(); drawChip(small.context, 180, 64, { mode: "auto" });
  const [x, y, width, height] = small.calls.find(call => call.op === "pill").args;
  assert.ok(x >= 0 && x + width <= 180 && y >= 0 && y + height <= 64);
  assert.equal(small.context.font, "10px system-ui"); assert.equal(small.context.textBaseline, "alphabetic");
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
