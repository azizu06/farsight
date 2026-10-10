import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as cameraView from "../src/camera-view.js";
import * as poseHud from "../src/pose-hud.js";
import { POSE_EDGES, drawDownMarker, linkHealth, parseKpts, poseSegments, strokeSegments } from "../src/pose-hud.js";

const full = { x: 0, y: 0, width: 640, height: 480 };
const standing = Array.from({ length: 17 }, (_, i) => [0.1 + i * 0.04, 0.2 + i * 0.03, 0.9]);

test("parseKpts accepts 17 finite triples only", () => {
  assert.equal(parseKpts(standing), standing);
  assert.equal(parseKpts(standing.slice(0, 16)), null);
  assert.equal(parseKpts(undefined), null);
  assert.equal(parseKpts([...standing.slice(0, 16), [0.1, NaN, 0.9]]), null);
});

test("segments map keypoints into the view and drop weak or out-of-crop limbs", () => {
  assert.equal(poseSegments(standing, full, 640, 480).length, POSE_EDGES.length * 4);
  const weak = standing.map(p => [...p]); weak[0][2] = 0.1;
  assert.equal(poseSegments(weak, full, 640, 480).length, (POSE_EDGES.length - 2) * 4); // nose has two limbs
  // Digital pan crop: x in [0.1,0.9] of the source maps to 0..1 of the view.
  const crop = { x: 64, y: 0, width: 448, height: 480 };
  const point = standing.map(() => [0.5, 0.5, 0.9]);
  const [x1, y1] = poseSegments(point, crop, 640, 480);
  assert.ok(Math.abs(x1 - (320 - 64) / 448) < 1e-9);
  assert.equal(y1, 0.5);
  const outside = standing.map(() => [0.01, 0.5, 0.9]);
  assert.equal(poseSegments(outside, crop, 640, 480).length, 0);
});

test("all limbs stroke in a single path", () => {
  const calls = [];
  const ctx = new Proxy({}, { get: (_, k) => (...a) => calls.push([k, ...a]), set: (_, k, v) => { calls.push([k, v]); return true; } });
  strokeSegments(ctx, poseSegments(standing, full, 640, 480), 640, 480, "#fff", 2);
  assert.equal(calls.filter(c => c[0] === "stroke").length, 1);
  assert.equal(calls.filter(c => c[0] === "beginPath").length, 1);
  assert.equal(calls.filter(c => c[0] === "lineTo").length, POSE_EDGES.length);
  calls.length = 0;
  strokeSegments(ctx, [], 640, 480, "#fff", 2);
  assert.equal(calls.length, 0);
});

test("down marker draws a red labelled tag", () => {
  const texts = [], fills = [];
  const ctx = { measureText: t => ({ width: t.length * 10 }), strokeRect() {}, fillRect: (...a) => fills.push([ctx.fillStyle, ...a]), fillText: t => texts.push(t) };
  drawDownMarker(ctx, { x: 10, y: 5, w: 100, h: 80 }, 320, 16);
  assert.deepEqual(texts, ["PERSON DOWN"]);
  assert.equal(fills[0][0], "#ff4d4d");
  assert.ok(fills[0][2] >= 0, "tag stays on canvas when the box touches the top");
});

test("link health goes red at 500 ms without a reply and shows ms and fps", () => {
  const ok = linkHealth({ now: 1000, receivedAt: 700, connected: true, linkMs: 12.4, detFps: 24.84 });
  assert.equal(ok.ok, true);
  assert.equal(ok.text, "LINK OK · 12 ms · det 24.8 fps");
  assert.equal(linkHealth({ now: 1000, receivedAt: 500, connected: true }).ok, false);
  assert.equal(linkHealth({ now: 1000, receivedAt: 900, connected: false }).text, "LINK LOST");
  assert.equal(linkHealth({ now: 1000, receivedAt: undefined, connected: true }).ok, false);
});

test("production drawDetections draws pose and down only for matching cached frame", () => {
  const source = readFileSync(new URL("../src/rover.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
  const mod = { exports: {} };
  vm.runInNewContext(compiled.outputText, { exports: mod.exports,
    require: n => n === "./camera-view.js" ? cameraView : n === "./pose-hud.js" ? poseHud : n === "three" ? {} : {} });
  const view = Object.create(mod.exports.RoverExperience.prototype);
  const ops = [];
  const ctx = new Proxy({ measureText: t => ({ width: t.length * 8 }) }, {
    get: (t, k) => k in t ? t[k] : (...a) => ops.push([k, ...a]), set: (t, k, v) => { t[k] = v; return true; } });
  const box = { id: 1, cls: "person", conf: .9, x: .2, y: .2, w: .3, h: .5, kpts: standing, down: true };
  Object.assign(view, { ctx, canvas: { width: 640, height: 480 }, frameId: "8", frameSource: "S", autonomy: { targetId: null },
    detectionFrames: new Map([["S:8", { packet: { boxes: [box] }, at: 90 }]]), drawnBoxes: [] });
  view.drawDetections(full, 640, 480, 100);
  assert.equal(view.drawnBoxes.length, 1);
  assert.ok(ops.some(o => o[0] === "fillText" && o[1] === "PERSON DOWN"));
  assert.equal(ops.filter(o => o[0] === "lineTo").length, POSE_EDGES.length);
  ops.length = 0;
  view.drawDetections(full, 640, 480, 700); // cached packet older than 500 ms
  assert.equal(ops.length, 0);
  assert.equal(view.drawnBoxes.length, 0);
});
