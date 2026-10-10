import test from "node:test";
import assert from "node:assert/strict";
import { FrameStats, QUEST2_BUDGET_MS, budgetTone, diagnosticsLines, percentile } from "../src/diagnostics.js";

test("frame stats report fps and p95 from a bounded ring, ignoring pauses", () => {
  const stats = new FrameStats(8);
  assert.deepEqual(stats.summary(), { fps: 0, frameP95: 0, workP95: 0, frames: 0 });
  let now = 0;
  stats.record(now, 1); // first frame has no interval
  for (let i = 0; i < 20; i++) stats.record(now += 1000 / 72, 2);
  stats.record(now += 5000, 50); // headset asleep: not a frame time
  const summary = stats.summary();
  assert.equal(summary.frames, 8);
  assert.ok(Math.abs(summary.fps - 72) < 0.01);
  assert.ok(Math.abs(summary.frameP95 - 1000 / 72) < 0.01);
  assert.equal(summary.workP95, 2);
  stats.record(now += 40, 9); // one long frame shows in p95 of a small window
  assert.equal(stats.summary().frameP95, 40);
});

test("percentile and budget tone against the Quest 2 72 Hz budget", () => {
  assert.equal(percentile([], 0.95), 0);
  assert.equal(percentile([5, 1, 3], 0.5), 3);
  assert.ok(Math.abs(QUEST2_BUDGET_MS[72] - 13.89) < 0.01);
  assert.equal(budgetTone(13.9), "ok");
  assert.equal(budgetTone(18), "warn");
  assert.equal(budgetTone(30), "bad");
  assert.equal(budgetTone(0), "warn");
  assert.equal(budgetTone(12, QUEST2_BUDGET_MS[90]), "ok");
});

test("lines name source, video path and unknowns explicitly", () => {
  const lines = diagnosticsLines({ fps: 71.94, frameP95: 14.2, workP95: 2.06, frames: 200 },
    { source: "SIMULATED", video: "H.264", videoAgeMs: 120.4, decodeMs: 3.44, ackMs: 22.6, linkMs: 18, xr: true });
  assert.deepEqual(lines, [
    "71.9 fps · frame p95 14.2 ms · JS p95 2.1 ms",
    "H.264 age 120 ms · decode 3.4 ms",
    "control RTT 23 ms · server link 18 ms",
    "SIMULATED · immersive VR · budget 72 Hz 13.9 ms",
  ]);
  const empty = diagnosticsLines({ fps: 0, frameP95: 0, workP95: 0, frames: 0 },
    { source: "UNVERIFIED", video: "JPEG", videoAgeMs: Infinity, decodeMs: null, ackMs: null, linkMs: undefined, xr: false });
  assert.match(empty[0], /^-- fps/);
  assert.equal(empty[1], "JPEG age n/a · decode n/a");
  assert.equal(empty[2], "control RTT n/a · server link n/a");
  assert.match(empty[3], /^UNVERIFIED · desktop/);
});
