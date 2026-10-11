import test from "node:test";
import assert from "node:assert/strict";
import { encounterLine, missionTime, summaryLines, timelineLines } from "../src/mission-report.js";

const report = {
  started_at: 1000,
  summary: {
    samples: 40, min_range_cm: 18, min_range_at: 1065, estop_count: 2,
    stop_reasons: { forward_blocked: { count: 2, first_ts: 1010, last_ts: 1065 }, deadman_released: { count: 1, first_ts: 1070, last_ts: 1070 } },
    lost: { rover_link: { count: 1, total_s: 3.5, intervals: [{ start: 1080, end: 1083.5, duration_s: 3.5 }] },
      camera: { count: 1, total_s: 2, intervals: [{ start: 1090, end: null, duration_s: 2 }] }, controller: { count: 0, total_s: 0, intervals: [] } },
    detections_by_class: { person: 2, dog: 1 },
  },
  timeline: [
    { kind: "detection", ts: 1005, cls: "person", key: "USB CAMERA:1", range_cm: 120 },
    { kind: "estop", ts: 1065, range_cm: 18, reason: "forward_blocked" },
    { kind: "stop", ts: 1070, reason: "deadman_released", range_cm: 60, requires_release: false },
    { kind: "rover_link_restored", ts: 1083.5, lost_s: 3.5 },
  ],
};

test("summary lines show min range with time, e-stops, stop histogram, losses and classes", () => {
  assert.deepEqual(summaryLines(report), [
    "Min range 18 cm at +1:05",
    "E-stops 2",
    "Stops: forward_blocked ×2 (+0:10–+1:05), deadman_released ×1 (+1:10–+1:10)",
    "Motor link lost 1× · 3.5s total",
    "Camera lost 1× · 2s total · still lost",
    "Detections: person 2, dog 1",
  ]);
});

test("older servers without summary render nothing extra", () => {
  assert.deepEqual(summaryLines({ mission_id: "x" }), []);
  assert.deepEqual(timelineLines({}), []);
  assert.equal(encounterLine({ cls: "person", id: 3, conf: 0.87, source: "USB CAMERA" }, 1000), "person · track 3 · 87% · USB CAMERA");
  assert.deepEqual(summaryLines({ started_at: 1, summary: { estop_count: 0 } }), ["Min range: no ultrasonic reading", "E-stops 0"]);
});

test("encounter line carries time first seen, dwell and range at first sight", () => {
  const entry = { cls: "person", id: 3, conf: 0.9, source: "USB CAMERA", first_seen: 1005, last_seen: 1017, first_range_cm: 120 };
  assert.equal(encounterLine(entry, 1000), "person · track 3 · 90% · USB CAMERA · seen +0:05 for 12s · range 120 cm at first sight");
  assert.match(encounterLine({ ...entry, first_range_cm: null }, 1000), /range no range at first sight$/);
});

test("encounter line omits the down-specific suffix", () => {
  const entry = { cls: "person", id: 3, conf: 0.9, source: "USB CAMERA", down: true };
  assert.equal(encounterLine(entry, 1000), "person · track 3 · 90% · USB CAMERA");
  assert.doesNotMatch(encounterLine({ ...entry, down: false }, 1000), /down/);
});

test("timeline is newest first with mission time, range and recovery time", () => {
  assert.deepEqual(timelineLines(report), [
    "+1:24 Motor link back after 3.5s",
    "+1:10 Stop deadman_released · 60 cm",
    "+1:05 E-stop · 18 cm",
    "+0:05 Seen person · 120 cm",
  ]);
  assert.equal(timelineLines(report, 1).length, 1);
  assert.equal(missionTime(undefined, 1000), "+?");
});
