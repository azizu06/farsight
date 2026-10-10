import assert from "node:assert/strict";
import test from "node:test";
import { frameAgeMs, poseMessage, shouldSendPose, POSE_HZ, LABEL } from "../src/mac-view.ts";

test("frame age is now minus the capture timestamp", () => {
  assert.equal(frameAgeMs(1_000, 1_040), 40);
  assert.equal(frameAgeMs(1_040, 1_000), -40);
  assert.throws(() => frameAgeMs(Number.NaN, 1), /finite/);
});

test("pose message carries head, right-ray and both clocks", () => {
  const vec = (x, y, z) => ({ x, y, z, toArray: () => [x, y, z] });
  const message = poseMessage(
    { mono: 12.5, wall: 1_700_000_000_000 },
    { position: vec(0, 1.6, 0), quaternion: { x: 0, y: 0, z: 0, w: 1 } },
    { origin: vec(0.2, 1.4, -0.3), direction: vec(0, 0, -1) },
  );
  assert.equal(message.type, "pose");
  assert.equal(message.version, 1);
  assert.equal(message.sent_mono_ms, 12.5);
  assert.deepEqual(message.head.quaternion, [0, 0, 0, 1]);
  assert.deepEqual(message.ray.direction, [0, 0, -1]);
  assert.equal(poseMessage({ mono: 0, wall: 0 }, { position: vec(0, 0, 0), quaternion: { x: 0, y: 0, z: 0, w: 1 } }, null).ray, null);
});

test("poses stay at or under 72 Hz and the label says this is not stereo", () => {
  assert.equal(shouldSendPose(0, 1000 / POSE_HZ - 0.01), false);
  assert.equal(shouldSendPose(0, 1000 / POSE_HZ), true);
  assert.match(LABEL, /not stereo/);
});
