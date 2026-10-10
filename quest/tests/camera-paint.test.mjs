import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as cameraView from "../src/camera-view.js";
import { ControlSafety } from "../src/control-safety.js";

// Exercise the production method without constructing DOM/WebGL resources.
// Compile the complete class, replace constructor dependencies, and invoke its
// actual prototype; camera geometry and the freshness gate remain production.
const source = readFileSync(new URL("../src/rover.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} });
const module = { exports: {} };
vm.runInNewContext(compiled.outputText, {
  exports: module.exports,
  require: name => name === "./camera-view.js" ? cameraView : {},
});

function fixture() {
  const view = Object.create(module.exports.RoverExperience.prototype);
  const paints = [], strokes = [];
  const box = { id: 1, cls: "person", conf: .9, x: .2, y: .2, w: .3, h: .5 };
  const gate = new ControlSafety();
  gate.acceptFrame("PHONE RGB:7", 100, 0);
  Object.assign(view, {
    ctx: { drawImage: (...args) => paints.push(args), strokeRect: (...args) => strokes.push(args),
      fillText() {} },
    canvas: { width: 320, height: 240 },
    h264Enabled: true,
    h264: { video: { readyState: 2, videoWidth: 640, videoHeight: 480 }, displayedFrame: null },
    input: { pan: 0 }, digitalPan: false, drawnPan: 0,
    frameId: "7", frameSource: "PHONE RGB", gate, dirty: true, lastCanvasDraw: 0,
    cameraTexture: { needsUpdate: false }, cameraScreen: { scale: { y: 1 } },
    drawnBoxes: [box], autonomy: { targetId: null },
    detectionFrames: new Map([["PHONE RGB:8", { packet: { boxes: [box] }, at: 0 }]]),
  });
  return { view, paints, strokes };
}

test("unverified H264 presentation retains validated pixels, boxes and pending repaint without renewing age", () => {
  const { view, paints, strokes } = fixture();
  view.drawCamera(100);
  assert.equal(paints.length, 0);
  assert.equal(strokes.length, 0);
  assert.equal(view.canvas.width, 320, "resizing would also erase the retained image");
  assert.equal(view.frameId, "7");
  assert.equal(view.drawnBoxes.length, 1);
  assert.equal(view.dirty, true);
  assert.equal(view.cameraTexture.needsUpdate, false);
  assert.equal(view.lastCanvasDraw, 0);
  assert.equal(view.gate.videoAge(100), 200);
});

test("the next verified H264 frame paints matching boxes and completes the pending repaint", () => {
  const { view, paints, strokes } = fixture();
  view.drawCamera(100);
  view.h264.displayedFrame = { id: "PHONE RGB:8", frame: 8, source: "PHONE RGB", age: 150 };
  view.drawCamera(120);
  assert.equal(paints.length, 1);
  assert.equal(strokes.length, 1);
  assert.equal(view.canvas.width, 640);
  assert.equal(view.frameId, "8");
  assert.equal(view.drawnBoxes.length, 1);
  assert.equal(view.dirty, false);
  assert.equal(view.cameraTexture.needsUpdate, true);
  assert.equal(view.gate.videoAge(120), 150);
});
