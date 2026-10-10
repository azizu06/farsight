import test from "node:test";
import assert from "node:assert/strict";
import { cameraCrop, croppedBox } from "../src/camera-view.js";

test("servo view preserves the full measured frame; digital yaw stays inside it", () => {
  assert.deepEqual(cameraCrop(640, 480, 60), { x: 0, y: 0, width: 640, height: 480 });
  for (const pan of [-180, -80, 0, 80, 180]) {
    const crop = cameraCrop(640, 480, pan, true);
    assert.ok(crop.x >= 0 && crop.y >= 0);
    assert.ok(crop.x + crop.width <= 640 && crop.y + crop.height <= 480);
    assert.equal(crop.width / crop.height, 640 / 480);
  }
  assert.equal(cameraCrop(640, 480, -80, true).x, 0);
  assert.equal(cameraCrop(640, 480, 80, true).x, 192);
  assert.equal(cameraCrop(640, 480, 0, true).x, 96);
  assert.throws(() => cameraCrop(0, 480, 0, true));
});

test("detection coordinates follow the crop, clipped boxes stay bounded", () => {
  const crop = cameraCrop(640, 480, 0, true);
  const box = croppedBox({ x: 0.15, y: 0.15, w: 0.7, h: 0.7 }, crop, 640, 480);
  assert.deepEqual(box, { x: 0, y: 0, w: 1, h: 1 });
  const clipped = croppedBox({ x: 0, y: 0, w: 0.3, h: 0.3 }, crop, 640, 480);
  assert.equal(clipped.x, 0); assert.equal(clipped.y, 0);
  assert.ok(clipped.w > 0 && clipped.w < 1 && clipped.h > 0 && clipped.h < 1);
  assert.equal(croppedBox({ x: 0, y: 0, w: 0.1, h: 0.1 }, crop, 640, 480), null);
  assert.equal(croppedBox({ x: NaN, y: 0, w: 1, h: 1 }, crop, 640, 480), null);
});

test("H264 decoder PTS selects capture identity; uncertain sample metadata omits boxes", async () => {
  const { sampleFrame } = await import("../src/camera-view.js");
  const samples = [
    { media_start_s: 1, media_end_s: 1.066667, frame: 11 },
    { media_start_s: 1.066667, media_end_s: 1.133333, frame: 12 },
  ];
  assert.equal(sampleFrame(samples, 1, 1, 1.133333), 11);
  assert.equal(sampleFrame(samples, 1.066667, 1, 1.133333), 12);
  assert.equal(sampleFrame(samples, 1.066666, 1, 1.133333), 12);
  assert.equal(sampleFrame(samples, 1.133333, 1, 1.133333), null);
  assert.equal(sampleFrame(undefined, 1, 1, 1.133333), null);
  assert.equal(sampleFrame([{ ...samples[0], frame: NaN }], 1, 1, 1.133333), null);
  assert.equal(sampleFrame([{ ...samples[0], media_start_s: 0 }], 1, 1, 1.133333), null);
  assert.equal(sampleFrame([samples[0], { ...samples[1], media_start_s: 1.05 }], 1.06, 1, 1.133333), null);
});
