import test from "node:test";
import assert from "node:assert/strict";
import { DetectionCache, MAX_LAG_FRAMES, PACKET_FRESH_MS } from "../src/detection-match.js";

const packet = (frame, source = "PHONE RGB", boxes = [{ cls: "person", conf: 0.9, x: 0.1, y: 0.1, w: 0.2, h: 0.2 }]) => ({ frame, source, boxes });

test("exact displayed frame wins at full opacity", () => {
  const cache = new DetectionCache();
  cache.add(packet(10), 0); cache.add(packet(12), 10);
  const match = cache.match("PHONE RGB", "12", 20);
  assert.equal(match.packet.frame, 12);
  assert.equal(match.lag, 0);
  assert.equal(match.alpha, 1);
});

test("detector slower than camera: every displayed frame between detections keeps the newest box", () => {
  // --demo cadence: camera ~15 Hz, detector ~3 fps, so packets every 5 frames.
  const cache = new DetectionCache();
  let shown = 0;
  for (let frame = 0; frame < 60; frame++) {
    const now = frame * 66;
    if (frame % 5 === 0) cache.add(packet(frame), now + 30);
    if (cache.match("PHONE RGB", String(frame), now + 40)) shown++;
  }
  assert.equal(shown, 60);
});

test("older packets fade with frame lag and are dropped past the bound", () => {
  const cache = new DetectionCache();
  cache.add(packet(100), 0);
  const one = cache.match("PHONE RGB", "101", 10), four = cache.match("PHONE RGB", "104", 10);
  assert.equal(one.lag, 1); assert.equal(four.lag, 4);
  assert.ok(one.alpha < 1 && four.alpha < one.alpha && four.alpha > 0);
  assert.ok(cache.match("PHONE RGB", String(100 + MAX_LAG_FRAMES), 10));
  assert.equal(cache.match("PHONE RGB", String(101 + MAX_LAG_FRAMES), 10), null);
});

test("prefers the nearest packet, older over newer on a tie; newer frames ahead of a lagging video still count", () => {
  const cache = new DetectionCache();
  cache.add(packet(20), 0); cache.add(packet(24), 0); cache.add(packet(28), 0);
  assert.equal(cache.match("PHONE RGB", "25", 1).packet.frame, 24);
  assert.equal(cache.match("PHONE RGB", "26", 1).packet.frame, 24);
  assert.equal(cache.match("PHONE RGB", "27", 1).packet.frame, 28);
  // Detections can reach the viewer before the decoder presents that frame.
  const ahead = new DetectionCache();
  ahead.add(packet(50), 0);
  const match = ahead.match("PHONE RGB", "48", 1);
  assert.equal(match.packet.frame, 50); assert.equal(match.lag, -2);
});

test("never draws boxes from a different camera source", () => {
  const cache = new DetectionCache();
  cache.add(packet(7, "USB CAMERA"), 0);
  assert.equal(cache.match("PHONE RGB", "7", 1), null);
  assert.equal(cache.match("PHONE RGB", "8", 1), null);
  assert.ok(cache.match("USB CAMERA", "8", 1));
  // Legacy packets without a source still match only their exact frame id.
  const legacy = new DetectionCache();
  legacy.add({ frame: 7, boxes: [] }, 0);
  assert.ok(legacy.match("PHONE RGB", "7", 1));
  assert.equal(legacy.match("PHONE RGB", "8", 1), null);
});

test("packets older than the freshness window and unusable ids never match", () => {
  const cache = new DetectionCache();
  cache.add(packet(5), 0);
  assert.ok(cache.match("PHONE RGB", "5", PACKET_FRESH_MS - 1));
  assert.equal(cache.match("PHONE RGB", "5", PACKET_FRESH_MS), null);
  assert.equal(cache.match("PHONE RGB", "", 1), null);
  // Non-numeric ids cannot measure lag, so only exact matches render.
  const named = new DetectionCache();
  named.add(packet("a7"), 0);
  assert.ok(named.match("PHONE RGB", "a7", 1));
  assert.equal(named.match("PHONE RGB", "a8", 1), null);
});

test("a camera restart (frame counter going back) does not keep showing old-run boxes", () => {
  const cache = new DetectionCache();
  cache.add(packet(5000), 0);
  assert.equal(cache.match("PHONE RGB", "3", 1), null);
});

test("cache stays bounded and clear() forgets everything", () => {
  const cache = new DetectionCache();
  for (let frame = 0; frame < 200; frame++) cache.add(packet(frame), frame);
  assert.ok(cache.size <= 40);
  assert.ok(cache.match("PHONE RGB", "199", 200));
  cache.clear();
  assert.equal(cache.match("PHONE RGB", "199", 200), null);
});
