import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";
import { MAX_FRONTIERS, MAX_PATH, floorHit, goalBody, mergeNav, navActive, navColor, navLabel, parseNav, pathLength, postNav, ribbon } from "../src/nav-view.js";

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

const previousNav = () => parseNav({ state: "driving", mode: "goto", backend: "nav2", revision: 9,
  goal: { x: 3, z: 4 }, path: [[0, 0], [3, 4]], frontiers: [[2, 2]] });

test("revision lane retains omitted arrays while replacing scalar metadata", () => {
  const before = previousNav();
  const next = mergeNav({ state: "recovering", mode: "explore", backend: "nav2", revision: 10,
    message: "replanning", goal: null }, before);
  assert.deepEqual(next.path, before.path);
  assert.deepEqual(next.frontiers, before.frontiers);
  assert.equal(next.state, "recovering");
  assert.equal(next.mode, "explore");
  assert.equal(next.revision, 10);
  assert.equal(next.goal, null);
  assert.equal(next.message, "replanning");
  assert.match(navLabel(next), /5\.0 m path · 1 frontier/);
  // Normalized copies cannot mutate the prior status or share raw inputs.
  next.path[0][0] = 99;
  assert.equal(before.path[0][0], 0);
});

test("full arrays replace independently and explicit empty arrays clear", () => {
  const before = previousNav();
  const full = mergeNav({ state: "driving", path: [[4, 4], [5, 5]], frontiers: [[6, 6]], revision: 11 }, before);
  assert.deepEqual(full.path, [[4, 4], [5, 5]]);
  assert.deepEqual(full.frontiers, [[6, 6]]);
  const pathOnly = mergeNav({ state: "driving", path: [] }, before);
  assert.deepEqual(pathOnly.path, []);
  assert.deepEqual(pathOnly.frontiers, before.frontiers);
  const cleared = mergeNav({ state: "idle", path: [], frontiers: [] }, before);
  assert.deepEqual(cleared.path, []);
  assert.deepEqual(cleared.frontiers, []);
});

test("a new controller session never inherits old paths/frontiers", () => {
  const reset = mergeNav({ state: "planning", revision: 0 }, previousNav(), false);
  assert.deepEqual(reset.path, []);
  assert.deepEqual(reset.frontiers, []);
  assert.equal(reset.revision, 0);
  assert.deepEqual(mergeNav({ state: "planning" }).path, []);
  const fresh = mergeNav({ state: "driving", path: [[1, 1], [2, 2]], frontiers: [[3, 3]] }, previousNav(), false);
  assert.deepEqual(fresh.path, [[1, 1], [2, 2]]);
});

test("malformed or absent nav hides it; malformed present arrays never retain old routes", () => {
  for (const raw of [null, undefined, {}, { state: "teleporting" }]) {
    assert.equal(mergeNav(raw, previousNav()), null);
  }
  for (const path of [null, "unchanged", [[0, 0], [NaN, 1], [2, 2]]]) {
    assert.deepEqual(mergeNav({ state: "driving", path }, previousNav()).path, []);
  }
  for (const frontiers of [null, "unchanged", [[NaN, 1]]]) {
    assert.deepEqual(mergeNav({ state: "driving", frontiers }, previousNav()).frontiers, []);
  }
  assert.deepEqual(mergeNav({ state: "driving" }, { state: "driving", path: [[Infinity, 0]] }).path, []);
});

test("legacy full nav remains compatible and invalid revisions are not published", () => {
  const raw = { state: "driving", path: [[0, 0], [1, 1]], frontiers: [], backend: "nav2" };
  assert.deepEqual(mergeNav(raw, previousNav()), parseNav(raw));
  for (const revision of [-1, NaN, 1.5, "10", Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(Object.hasOwn(mergeNav({ state: "idle", revision }, previousNav()), "revision"), false);
  }
});

// Compile the actual production HTTP methods, so these regressions cannot pass
// merely because a test copied a session check absent from RoverExperience.
function navHttpFixture(post) {
  const source = ts.createSourceFile("rover.ts", readFileSync(new URL("../src/rover.ts", import.meta.url), "utf8"), ts.ScriptTarget.Latest, true);
  const klass = source.statements.find(node => ts.isClassDeclaration(node) && node.name.text === "RoverExperience");
  const methods = ["setGoal", "cancelNav", "send"].map(name => klass.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(source) === name).getText(source));
  const js = ts.transpileModule(`class Fixture { ${methods.join('\n')} }`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const Fixture = new Function("postNav", "goalBody", "WebSocket", `${js}; return Fixture;`)(post, goalBody, { OPEN: 1 });
  const fixture = new Fixture();
  Object.assign(fixture, { stopped: false, status: { session_id: "old-controller" }, socket: {}, navBusy: false,
    nav: previousNav(), scanning: false, scanPending: false, dirty: false, renderer: { xr: { isPresenting: false } },
    noteNav() {}, stop() {}, autonomy: { mode: "goto", clears: 0, gotos: 0,
      clear() { this.clears++; this.mode = "teleop"; }, goto() { this.gotos++; this.mode = "goto"; } } });
  return fixture;
}

test("late POST responses from an old controller cannot replace new nav or action mode", async () => {
  for (const method of ["setGoal", "cancelNav"]) {
    let finish;
    const fixture = navHttpFixture(() => new Promise(resolve => { finish = resolve; }));
    const pending = method === "setGoal" ? fixture.setGoal({ x: 1, z: 2 }) : fixture.cancelNav();
    const newNav = parseNav({ state: "planning", path: [[7, 7], [8, 8]], frontiers: [[9, 9]] });
    fixture.status = { session_id: "new-controller" }; fixture.socket = {}; fixture.nav = newNav;
    fixture.autonomy.mode = "explore";
    finish({ ok: true, nav: previousNav(), error: null });
    await pending;
    assert.equal(fixture.nav, newNav);
    assert.equal(fixture.autonomy.mode, "explore");
    assert.equal(fixture.autonomy.gotos, 0);
  }
});

test("same-session POST updates still work, but replacing its socket rejects old responses", async () => {
  const nav = parseNav({ state: "planning", path: [[0, 0], [2, 2]] });
  const current = navHttpFixture(async () => ({ ok: true, nav, error: null }));
  await current.setGoal({ x: 1, z: 2 });
  assert.equal(current.nav, nav); assert.equal(current.autonomy.gotos, 1);
  let finish;
  const changed = navHttpFixture(() => new Promise(resolve => { finish = resolve; }));
  const original = changed.nav, pending = changed.setGoal({ x: 1, z: 2 });
  changed.socket = {}; // Same textual session cannot authorize another socket's late result.
  finish({ ok: true, nav, error: null }); await pending;
  assert.equal(changed.nav, original); assert.equal(changed.autonomy.gotos, 0);
});

test("production heartbeat and release opt into revision nav without changing permit or sequence", () => {
  const fixture = navHttpFixture(async () => null), commands = [];
  Object.assign(fixture, { seq: 10, sent: new Map(), digitalPan: false,
    status: { session_id: "controller", permit: "server-issued" },
    gate: { session: "controller", videoAge: () => 80.2 }, input: { v: .35, w: -.2, pan: 5 },
    socket: { readyState: 1, bufferedAmount: 0, send: text => commands.push(JSON.parse(text)) } });
  fixture.autonomy.fields = () => ({ mode: "teleop" });
  fixture.send(true); fixture.send(false);
  assert.deepEqual(commands, [
    { seq: 10, session_id: "controller", permit: "server-issued", nav_lane: "revision",
      deadman: true, v: .35, w: -.2, pan: 5, video_fresh: true, mode: "teleop", video_age_ms: 81 },
    { seq: 11, session_id: "controller", permit: "server-issued", nav_lane: "revision",
      deadman: false, v: 0, w: 0, pan: 5, video_fresh: true, mode: "teleop", video_age_ms: 81 },
  ]);
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
