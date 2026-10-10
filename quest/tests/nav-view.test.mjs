import test from "node:test";
import assert from "node:assert/strict";
import { MAX_FRONTIERS, MAX_PATH, floorHit, goalBody, navActive, navColor, navLabel, parseNav, pathLength, postNav, ribbon } from "../src/nav-view.js";

const close = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-5, `${message}: ${actual} != ${expected}`);
const vertex = (geometry, index) => [...geometry.positions.slice(index * 3, index * 3 + 3)];

test("parseNav accepts the viewer contract and rejects unknown states", () => {
  const nav = parseNav({ state: "driving", mode: "goto", goal: { x: 2, z: -1 }, path: [[0, 0], [1, 0], [2, -1]],
    frontiers: [[4, 4]], backend: "nav2", message: "replanned" });
  assert.deepEqual(nav, { state: "driving", mode: "goto", goal: { x: 2, z: -1 }, path: [[0, 0], [1, 0], [2, -1]],
    frontiers: [[4, 4]], backend: "nav2", message: "replanned" });
  assert.equal(parseNav(null), null);
  assert.equal(parseNav({ state: "teleporting" }), null);
  assert.deepEqual(parseNav({ state: "idle", mode: "dance", backend: "ros1", goal: { x: "1", z: 0 } }),
    { state: "idle", mode: null, goal: null, path: [], frontiers: [], backend: null, message: "" });
});

test("parseNav drops a path with any bad point instead of bridging the gap, and caps lengths", () => {
  assert.deepEqual(parseNav({ state: "driving", path: [[0, 0], [NaN, 1], [2, 2]] }).path, []);
  assert.deepEqual(parseNav({ state: "driving", path: [[0, 0], [1e9, 1]] }).path, []);
  const long = Array.from({ length: MAX_PATH + 50 }, (_, i) => [i * 0.1, 0]);
  assert.equal(parseNav({ state: "driving", path: long }).path.length, MAX_PATH);
  const frontiers = Array.from({ length: MAX_FRONTIERS + 9 }, (_, i) => [i, i]);
  frontiers.splice(3, 0, [null, 1]);
  assert.equal(parseNav({ state: "planning", frontiers }).frontiers.length, MAX_FRONTIERS - 1);
});

test("ribbon lays a straight path flat on the floor at the requested width", () => {
  const geometry = ribbon([[0, 0], [1, 0], [3, 0]], 0.05, 0.02);
  assert.equal(geometry.points, 3);
  assert.equal(geometry.positions.length, 3 * 2 * 3);
  assert.deepEqual([...geometry.indices], [0, 1, 2, 1, 3, 2, 2, 3, 4, 3, 5, 4]);
  for (let i = 0; i < 6; i++) {
    const [x, y, z] = vertex(geometry, i);
    close(y, 0.02, "floor height");
    close(Math.abs(z), 0.05, "half width");
    close(x, [0, 0, 1, 1, 3, 3][i], "x follows path");
  }
  assert.deepEqual([...geometry.side], [1, -1, 1, -1, 1, -1]);
  assert.deepEqual([...geometry.along], [0, 0, 1, 1, 3, 3]);
  close(geometry.length, 3, "length");
});

test("ribbon mitres corners, skips duplicates and survives a reversal", () => {
  const corner = ribbon([[0, 0], [1, 0], [1, 0], [1, 1]], 0.1);
  assert.equal(corner.points, 3, "duplicate point skipped");
  // Right-angle corner: the miter offset is half-width × √2 along the bisector.
  const [ax, , az] = vertex(corner, 2), [bx, , bz] = vertex(corner, 3);
  close(Math.hypot(ax - 1, az), 0.1 * Math.SQRT2, "outer miter");
  close(Math.hypot(bx - 1, bz), 0.1 * Math.SQRT2, "inner miter");
  const hairpin = ribbon([[0, 0], [1, 0], [0, 0.001]], 0.1);
  for (let i = 0; i < hairpin.positions.length; i += 3) {
    assert.ok(Number.isFinite(hairpin.positions[i]) && Number.isFinite(hairpin.positions[i + 2]), "finite vertices");
  }
  const [hx, , hz] = vertex(hairpin, 2);
  assert.ok(Math.hypot(hx - 1, hz) <= 0.2 + 1e-6, "hairpin miter capped at twice the half-width");
  assert.equal(ribbon([[1, 1], [1, 1]], 0.1), null);
  assert.equal(ribbon([], 0.1), null);
});

test("floorHit picks the floor point under a downward ray and refuses level, upward or distant rays", () => {
  // Controller at 1.2 m pointing 45° down along -z lands 1.2 m ahead.
  const down45 = { x: 0, y: -Math.SQRT1_2, z: -Math.SQRT1_2 };
  const hit = floorHit({ x: 0.5, y: 1.2, z: 0 }, down45);
  close(hit.x, 0.5, "x"); close(hit.z, -1.2, "z");
  const raised = floorHit({ x: 0, y: 1.2, z: 0 }, down45, 0.2);
  close(raised.z, -1, "raised floor");
  assert.equal(floorHit({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 }), null, "level");
  assert.equal(floorHit({ x: 0, y: 1, z: 0 }, { x: 0, y: 0.5, z: -0.5 }), null, "upward");
  assert.equal(floorHit({ x: 0, y: -1, z: 0 }, { x: 0, y: -1, z: 0 }), null, "below the floor already");
  assert.equal(floorHit({ x: 0, y: 1, z: 0 }, { x: 0, y: -0.01, z: -1 }), null, "beyond 40 m");
  assert.equal(floorHit({ x: NaN, y: 1, z: 0 }, down45), null);
});

test("goalBody sends exactly x and z rounded to millimetres", () => {
  assert.deepEqual(goalBody({ x: 1.23456, z: -0.00049, y: 9 }), { x: 1.235, z: 0 });
  assert.deepEqual(Object.keys(goalBody({ x: 1, z: 2 })), ["x", "z"]);
});

test("navLabel and navColor describe backend, mode, state, path and frontiers", () => {
  const nav = parseNav({ state: "driving", mode: "goto", path: [[0, 0], [3, 4]], frontiers: [[1, 1], [2, 2]], backend: "nav2" });
  assert.equal(navLabel(nav), "Nav2 · Go-to driving · 5.0 m path · 2 frontiers");
  assert.equal(navLabel(parseNav({ state: "unavailable", backend: "fallback", message: "ROS not running" })),
    "Fallback planner · unavailable · ROS not running");
  assert.equal(navLabel(parseNav({ state: "arrived", mode: "goto", path: [[0, 0], [1, 0]] })), "Nav · Go-to arrived");
  assert.equal(navLabel(null), "");
  assert.equal(navActive(nav), true);
  assert.equal(navActive(parseNav({ state: "failed" })), false);
  assert.equal(pathLength([[0, 0], [3, 4], [3, 5]]), 6);
  assert.notEqual(navColor("driving"), navColor("failed"));
  assert.equal(navColor("idle"), navColor("unavailable"));
});

test("postNav returns the nav object on 202 and the reason on 409 or network failure", async () => {
  const calls = [];
  const reply = (status, body) => async (url, init) => { calls.push({ url, init }); return { ok: status < 300, status, json: async () => body }; };
  const accepted = await postNav("/nav/goal", { x: 1, z: 2 }, reply(202, { state: "planning", mode: "goto", goal: { x: 1, z: 2 }, backend: "nav2" }));
  assert.equal(accepted.ok, true);
  assert.equal(accepted.nav.state, "planning");
  assert.equal(calls[0].url, "/nav/goal");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { x: 1, z: 2 });
  const conflict = await postNav("/nav/goal", { x: 1, z: 2 }, reply(409, { state: "unavailable", message: "Nav2 not running" }));
  assert.deepEqual([conflict.ok, conflict.status, conflict.error, conflict.nav.state], [false, 409, "Nav2 not running", "unavailable"]);
  const cancel = await postNav("/nav/cancel", undefined, reply(200, { nav: { state: "idle" } }));
  assert.equal(calls[2].init.body, undefined);
  assert.equal(cancel.nav.state, "idle");
  const down = await postNav("/nav/goal", { x: 0, z: 0 }, async () => { throw new Error("connection refused"); });
  assert.deepEqual([down.ok, down.status, down.error], [false, 0, "connection refused"]);
  const missing = await postNav("/nav/goal", { x: 0, z: 0 }, async () => ({ ok: false, status: 404, json: async () => { throw new SyntaxError("html"); } }));
  assert.equal(missing.error, "HTTP 404");
});
