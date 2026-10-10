import test from "node:test";
import assert from "node:assert/strict";
import { askLabel, askText } from "../src/ask-label.js";

test("offline answer names its source, timing and that it needs no internet", () => {
  const reply = { answer: "The usb camera view is fresh.", model: "local-observation-summary", ms: 12.4, simulated: false };
  assert.equal(askLabel(reply), "Offline observation summary · 12 ms · on rover, no internet");
  assert.equal(askText(reply), "The usb camera view is fresh.\n(Offline observation summary · 12 ms · on rover, no internet)");
});

test("vision model fallback shows the warning and the simulated flag", () => {
  const reply = { answer: "A.", model: "local-observation-summary", ms: 40, simulated: true, assistant_warning: "Local vision model unavailable" };
  assert.equal(askText(reply), "A.\nLocal vision model unavailable\n(Offline observation summary · 40 ms · on rover, no internet · simulated view)");
  assert.match(askLabel({ model: "local-vision-model", ms: 900 }), /^Local vision model · 900 ms/);
});

test("older rovers without timing or model still get a label", () => {
  assert.equal(askLabel({}), "Rover assistant · on rover, no internet");
});
