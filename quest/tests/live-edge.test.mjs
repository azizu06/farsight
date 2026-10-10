import test from "node:test";
import assert from "node:assert/strict";
import { LIVE_EDGE, chaseLiveEdge } from "../src/live-edge.js";

const range = { start: 10, end: 12 };
const at = (currentTime, state) => chaseLiveEdge(currentTime, range, state);

test("normal fragment buffering never seeks: lag is trimmed by playback rate", () => {
  for (const lead of [0.05, 0.15, 0.3, 0.6, 0.85]) {
    assert.equal(at(12 - lead, { now: 5000 }).seekTo, null, `no seek at ${lead * 1000} ms ahead`);
  }
  assert.equal(at(11.95, { now: 0 }).rate, 1, "near the edge plays at 1x");
  assert.equal(at(11.75, { now: 0 }).rate, LIVE_EDGE.gentleRate);
  assert.equal(at(11.5, { now: 0 }).rate, LIVE_EDGE.fastRate);
});

test("a real backlog seeks once, then waits out the cooldown and any in-flight seek", () => {
  const first = at(10.5, { now: 5000, lastSeekAt: -Infinity });
  assert.ok(Math.abs(first.seekTo - (12 - LIVE_EDGE.target)) < 1e-9, "1.5 s behind jumps near the edge");
  assert.equal(at(10.5, { now: 5000 + LIVE_EDGE.cooldownMs - 1, lastSeekAt: 5000 }).seekTo, null, "cooldown");
  assert.equal(at(10.5, { now: 9000, lastSeekAt: 5000, seeking: true }).seekTo, null, "never while seeking");
  assert.equal(at(10.5, { now: 9000, lastSeekAt: 5000, seeking: true }).rate, LIVE_EDGE.fastRate, "rate still trims");
});

test("a gap before the newest range is jumped instead of stalling", () => {
  const jump = chaseLiveEdge(0, { start: 35.6, end: 35.7 }, { now: 0 });
  assert.ok(jump.seekTo >= 35.6 && jump.seekTo <= 35.7, "lands inside the newest range");
});

test("past the newest buffered end is a stall too (Sai's outside-range case)", () => {
  const jump = chaseLiveEdge(12.4, { start: 10, end: 12 }, { now: 0 });
  assert.ok(jump.seekTo !== null && jump.seekTo <= 12);
});

test("a frame due on the next vsync is presented, with that wait in its age", async () => {
  const { presentationWait } = await import("../src/live-edge.js");
  assert.equal(presentationWait(1016, 1000), 16, "expectedDisplayTime one refresh ahead counts");
  assert.equal(presentationWait(990, 1000), 0, "already on screen");
  assert.equal(presentationWait(1200, 1000), null, "implausibly far ahead is not a presented frame");
  assert.equal(presentationWait(NaN, 1000), null);
});

test("nothing buffered or invalid time changes nothing", () => {
  assert.deepEqual(chaseLiveEdge(1, null), { seekTo: null, rate: 1, lead: 0 });
  assert.deepEqual(chaseLiveEdge(NaN, { start: 0, end: 1 }), { seekTo: null, rate: 1, lead: 0 });
});

test("h264.ts guards seeks with the policy, never writing currentTime on its own", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("../src/h264.ts", import.meta.url), "utf8");
  const writes = source.match(/currentTime\s*=(?!=)/g) ?? [];
  assert.equal(writes.length, 1, "the only currentTime write is the policy's seekTo");
  assert.match(source, /seeking: this\.video\.seeking/);
});
