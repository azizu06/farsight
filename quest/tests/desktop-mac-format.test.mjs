import test from "node:test";
import assert from "node:assert/strict";
import { macFormat, macRectangle } from "../src/desktop-mac-format.js";

test("only declared flat or correctly packed SBS frames enter the desktop canvas", () => {
  assert.deepEqual(macFormat({ version: 1, stereo: false }), { stereo: false });
  assert.equal(macFormat({ version: 1 }), null);
  assert.equal(macFormat({ version: 2, stereo: false }), null);
  const init = { type: "video_init", version: 1, stereo: true, layout: "sbs-left-right",
    width: 1920, height: 1080, eye_width: 960, eye_height: 1080 };
  assert.deepEqual(macFormat(init), { stereo: true, eyeWidth: 960, eyeHeight: 1080 });
  assert.equal(macFormat({ ...init, width: 960 }), null);
  const eye = name => ({ eye: name, position: [0, 0, 0], quaternion: [0, 0, 0, 1], projection: Array(16).fill(0) });
  const segment = { ...init, type: "video_segment", pose: { eyes: [eye("left"), eye("right")] } };
  assert.ok(macFormat(segment));
  assert.equal(macFormat({ ...segment, pose: null }), null);
  assert.equal(macFormat({ ...segment, pose: { eyes: [eye("right"), eye("left")] } }), null);
  segment.pose.eyes[1].position[0] = NaN;
  assert.equal(macFormat(segment), null);
});

test("SBS crops exactly one eye and keeps its real aspect on the canvas", () => {
  const rect = macRectangle(1920, 1080, 1280, 720, true);
  assert.equal(rect.sourceWidth, 960);
  assert.equal(rect.width / rect.height, 960 / 1080);
  assert.equal(rect.height, 720);
  assert.equal(rect.y, 0);
  assert.equal(rect.x, (1280 - 640) / 2);
  assert.deepEqual(macRectangle(1280, 720, 1280, 720, false), { sourceWidth: 1280, width: 1280, height: 720, x: 0, y: 0 });
});
