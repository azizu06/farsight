import test from "node:test";
import assert from "node:assert/strict";
import { DRILL_TRAINS, NO_FACE_MODEL, faceWarning, DrillHud, drawDrill, drillLine, endLines, parseDrill, routeArrows } from "../src/drill-hud.js";

const running = {
  active: true, remaining_s: 60, elapsed_s: 30, duration_s: 90, found: 1, total: 3, estops: 2,
  found_ids: [4], pose: { x: 1, y: 0, heading_rad: 0 }, summary: null, route_approximate: true,
  breadcrumb: [{ t: 0, x: 0, y: 0 }, { t: 3, x: 1, y: 0 }],
  route_home: [{ x: 1, y: 0 }, { x: 0, y: 0 }],
};
const ended = {
  ...running, active: false, remaining_s: 0, elapsed_s: 90, found: 2,
  summary: { time_s: 90, found: 2, total: 3, estops_avoided: 2, found_at_s: [12.5, 40], route_approximate: true },
};

test("parses server drill status and rejects malformed packets", () => {
  assert.equal(parseDrill(running).found, 1);
  assert.deepEqual(parseDrill({ ...running, route_home: "bad" }).route_home, []);
  assert.throws(() => parseDrill({ ...running, found: "1" }), /Invalid drill status/);
  assert.throws(() => parseDrill(null), /Invalid drill status/);
  // Older servers without scoring fields still render a timer.
  const legacy = parseDrill({ active: true, remaining_s: 10, person_encounters: 2 });
  assert.equal(legacy.found, 0);
  assert.equal(legacy.total, null);
});

test("HUD line counts down locally and flags stale status", () => {
  const drill = parseDrill(running);
  assert.equal(drillLine(drill, 0), "Rescue drill 60s · victims 1/3 · e-stops 2");
  assert.equal(drillLine(drill, 2500), "Rescue drill 58s · victims 1/3 · e-stops 2");
  assert.equal(drillLine(drill, 4000), "Rescue drill status stale");
  assert.equal(drillLine(parseDrill(ended), 0), "Rescue drill complete · victims 2/3 · e-stops 2");
  assert.equal(drillLine(parseDrill({ ...running, remaining_s: null, active: false }), 0), "");
  assert.equal(drillLine(null, 0), "");
});

test("end screen lists time, found/total, collisions avoided and approximate route", () => {
  assert.equal(endLines(parseDrill(running)), null);
  const lines = endLines(parseDrill(ended));
  assert.deepEqual(lines.slice(0, 3), ["Time 1:30", "Victims found 2/3", "Collisions avoided (e-stops) 2"]);
  assert.match(lines.join(" "), /approximate/);
});

test("who-this-trains sentence names responders practising room search", () => {
  assert.match(DRILL_TRAINS, /responders practising room search/);
});

test("route arrows run from the rover back to the start, forward drawn up", () => {
  const box = { x: 0, y: 0, w: 100, h: 100 };
  const arrows = routeArrows([{ x: 1, y: 0 }, { x: 0, y: 0 }], box, 20);
  assert.ok(arrows.length >= 2);
  // The rover (x=1, forward) sits above the start; arrows point down the screen (+y).
  assert.ok(arrows[0].y < arrows.at(-1).y);
  for (const arrow of arrows) assert.ok(Math.abs(arrow.angle - Math.PI / 2) < 1e-6);
  // Left of the rover (+y in rover frame) is screen left (-x).
  const left = routeArrows([{ x: 0, y: 1 }, { x: 0, y: 0 }], box, 20);
  for (const arrow of left) assert.ok(Math.abs(arrow.angle) < 1e-6);
  assert.deepEqual(routeArrows([{ x: 0, y: 0 }], box, 20), []);
});

function fakeContext() {
  const texts = [];
  const noop = () => {};
  return {
    texts,
    fillText: text => texts.push(text),
    clearRect: noop, fillRect: noop, beginPath: noop, moveTo: noop, lineTo: noop, stroke: noop, fill: noop,
    closePath: noop, save: noop, restore: noop, translate: noop, rotate: noop, arc: noop, setLineDash: noop,
    set fillStyle(_) {}, set strokeStyle(_) {}, set lineWidth(_) {}, set font(_) {},
  };
}

test("panel draws counter, route label and end screen", () => {
  const live = fakeContext();
  drawDrill(live, parseDrill(running), 0, 512, 512);
  assert.ok(live.texts.some(text => /1\/3/.test(text)));
  assert.ok(live.texts.some(text => /60s/.test(text)));
  assert.ok(live.texts.some(text => /Route home \(approximate\)/.test(text)));
  assert.ok(live.texts.includes(DRILL_TRAINS) || live.texts.some(text => DRILL_TRAINS.startsWith(text)));
  const done = fakeContext();
  drawDrill(done, parseDrill(ended), 0, 512, 512);
  assert.ok(done.texts.includes("Victims found 2/3"));
  assert.ok(done.texts.includes("Collisions avoided (e-stops) 2"));
});

test("DrillHud starts and polls through the mission drill API", async () => {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body) : null });
    return { ok: true, status: 200, json: async () => (init.method === "POST" ? running : ended) };
  };
  const hud = new DrillHud({ fetchImpl, now: () => 0, canvas: null });
  await hud.start();
  assert.deepEqual(calls[0], { url: "/mission/drill", method: "POST", body: { duration_s: 90, victims: 3 } });
  assert.equal(hud.line(0), "Rescue drill 60s · victims 1/3 · e-stops 2");
  assert.equal(hud.visible(), true);
  await hud.refresh();
  assert.equal(hud.drill.summary.found, 2);
  assert.equal(hud.visible(), true);
});

test("DrillHud reports server errors without throwing", async () => {
  const hud = new DrillHud({ fetchImpl: async () => ({ ok: false, status: 422, json: async () => ({}) }), now: () => 0, canvas: null });
  assert.match(await hud.start(), /Drill unavailable/);
  assert.equal(hud.visible(), false);
});

test("missing face model is flagged in the line, idle settings text and canvas", async () => {
  assert.equal(NO_FACE_MODEL, "No face model: victims cannot be found");
  const noFace = parseDrill({ ...running, face_detector: false, face_error: null });
  assert.equal(faceWarning(noFace), NO_FACE_MODEL);
  assert.equal(drillLine(noFace, 0), `Rescue drill 60s · victims 1/3 · e-stops 2 · ${NO_FACE_MODEL}`);
  // Loaded model, or an older rover without the field: no warning.
  assert.equal(faceWarning(parseDrill({ ...running, face_detector: true })), "");
  assert.equal(drillLine(parseDrill(running), 0), "Rescue drill 60s · victims 1/3 · e-stops 2");
  const texts = [];
  const c = new Proxy({}, { get: (_, key) => key === "fillText" ? text => texts.push(text) : () => {}, set: () => true });
  drawDrill(c, noFace, 0, 512, 512);
  assert.ok(texts.includes(NO_FACE_MODEL));
  texts.length = 0;
  drawDrill(c, parseDrill({ ...running, face_detector: true }), 0, 512, 512);
  assert.ok(!texts.includes(NO_FACE_MODEL));
  const idle = { active: false, remaining_s: null, found: 0, total: null, estops: 0, face_detector: false, face_error: "Face model unavailable: x" };
  const hud = new DrillHud({ canvas: null, fetchImpl: async () => ({ ok: true, json: async () => idle }) });
  assert.equal(await hud.refresh(), `Ready to start · ${NO_FACE_MODEL}`);
});
