import test from "node:test";
import assert from "node:assert/strict";
import { AutonomyIntent, autonomyLabel, containPoint, pickTarget, rayPanelPoint } from "../src/autonomy.js";

const person = (id, x, y, w, h) => ({ id, cls: "person", conf: 0.9, x, y, w, h });

test("pickTarget hits the person box under a normalized point", () => {
  const boxes = [person(3, 0.1, 0.1, 0.3, 0.6), person(7, 0.6, 0.2, 0.2, 0.5)];
  assert.equal(pickTarget(boxes, 0.2, 0.4)?.id, 3);
  assert.equal(pickTarget(boxes, 0.7, 0.3)?.id, 7);
  assert.equal(pickTarget(boxes, 0.5, 0.9), null);
});

test("pickTarget prefers the smallest overlapping box and ignores untracked or non-person boxes", () => {
  const boxes = [person(1, 0, 0, 1, 1), person(2, 0.4, 0.4, 0.2, 0.2),
    { cls: "person", conf: 0.9, x: 0.45, y: 0.45, w: 0.05, h: 0.05 },
    { id: 9, cls: "chair", conf: 0.9, x: 0.45, y: 0.45, w: 0.05, h: 0.05 }];
  assert.equal(pickTarget(boxes, 0.47, 0.47)?.id, 2);
  assert.equal(pickTarget([], 0.5, 0.5), null);
  assert.equal(pickTarget(boxes, NaN, 0.5), null);
});

test("containPoint maps a click through object-fit: contain letterboxing", () => {
  const rect = { left: 0, top: 0, width: 1000, height: 500 };
  // 640x480 canvas fills 666.7 px wide, centred: bars of 166.7 px each side.
  assert.deepEqual(containPoint(500, 250, rect, 640, 480), { x: 0.5, y: 0.5 });
  assert.equal(containPoint(100, 250, rect, 640, 480), null);
  const left = containPoint(166.67, 0, rect, 640, 480);
  assert.ok(Math.abs(left.x) < 1e-3 && Math.abs(left.y) < 1e-6);
});

test("rayPanelPoint converts a plane uv hit to canvas coordinates", () => {
  assert.deepEqual(rayPanelPoint({ x: 0.25, y: 0.75 }), { x: 0.25, y: 0.25 });
  assert.equal(rayPanelPoint(null), null);
  assert.equal(rayPanelPoint({ x: 1.2, y: 0.5 }), null);
});

test("selecting a target sends seek only while the deadman is held", () => {
  const intent = new AutonomyIntent();
  assert.deepEqual(intent.fields(true), { mode: "teleop" });
  intent.select(3);
  assert.equal(intent.mode, "seek"); assert.equal(intent.targetId, 3);
  assert.deepEqual(intent.fields(true), { mode: "seek", target_id: 3 });
  assert.deepEqual(intent.fields(false), { mode: "teleop" });
  assert.equal(intent.select(1.5), false);
  assert.equal(intent.targetId, 3);
});

test("explore toggles on and off and replaces a seek target", () => {
  const intent = new AutonomyIntent();
  intent.select(4);
  intent.toggleExplore();
  assert.equal(intent.mode, "explore"); assert.equal(intent.targetId, null);
  assert.deepEqual(intent.fields(true), { mode: "explore" });
  assert.deepEqual(intent.fields(false), { mode: "teleop" });
  intent.toggleExplore();
  assert.equal(intent.mode, "teleop");
});

test("any stick input or stop clears autonomy, matching the server override latch", () => {
  const intent = new AutonomyIntent();
  intent.toggleExplore();
  assert.equal(intent.stick(0, 0), false);
  assert.equal(intent.mode, "explore");
  assert.equal(intent.stick(0.2, 0), true);
  assert.equal(intent.mode, "teleop");
  intent.select(5);
  intent.stick(0, -0.1);
  assert.deepEqual(intent.fields(true), { mode: "teleop" });
  intent.select(5); intent.clear();
  assert.equal(intent.targetId, null);
});

test("autonomyLabel describes server autonomy state", () => {
  assert.equal(autonomyLabel(null), "");
  assert.equal(autonomyLabel({ mode: "teleop", autonomy_state: "idle" }), "");
  assert.equal(autonomyLabel({ mode: "explore", autonomy_state: "forward" }), "Exploring");
  assert.equal(autonomyLabel({ mode: "explore", autonomy_state: "turning" }), "Exploring · turning");
  assert.equal(autonomyLabel({ mode: "seek", autonomy_state: "tracking", target_id: 3 }), "Seeking #3");
  assert.equal(autonomyLabel({ mode: "seek", autonomy_state: "arrived", target_id: 3 }), "Arrived at #3");
  assert.equal(autonomyLabel({ mode: "seek", autonomy_state: "target_lost", target_id: 3 }), "Target #3 lost");
  assert.equal(autonomyLabel({ mode: "seek", autonomy_state: "target_missing", target_id: 3 }), "Target #3 not visible");
  assert.equal(autonomyLabel({ mode: "teleop", autonomy_state: "stick_override" }), "Stick override: teleop");
  assert.equal(autonomyLabel({ mode: "seek", autonomy_state: "estop", target_id: 2 }), "Autonomy stopped: e-stop");
});

test("an accepted go-to goal survives a stop/release but not a stick, cancel or another mode", () => {
  const intent = new AutonomyIntent();
  intent.goto();
  assert.deepEqual(intent.fields(true), { mode: "goto" });
  assert.deepEqual(intent.fields(false), { mode: "teleop" }, "a release never carries autonomy");
  intent.release();
  assert.equal(intent.mode, "goto", "view change or deadman stop keeps the server-owned goal armed");
  assert.equal(intent.stick(0, 0.3), true);
  assert.equal(intent.mode, "teleop");
  intent.goto(); intent.toggleExplore();
  assert.deepEqual(intent.fields(true), { mode: "explore" });
  intent.release();
  assert.equal(intent.mode, "teleop", "release still disarms Explore");
  intent.select(2); intent.release();
  assert.equal(intent.mode, "teleop", "release still disarms Seek");
  intent.goto(); intent.clear();
  assert.equal(intent.mode, "teleop");
});
