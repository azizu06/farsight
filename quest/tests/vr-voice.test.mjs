import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as THREE from "three";
import * as safety from "../src/control-safety.js";
import * as drive from "../src/manual-drive.js";
import { AutonomyIntent } from "../src/autonomy.js";
import * as walk from "../src/map-walk.js";

// Actual RoverExperience.update/pump; only browser, media and rendering boundaries are faked.
const exports = {};
let now = 1000;
const context = { exports, THREE, performance: { now: () => now }, WebSocket: { OPEN: 1 },
  document: { hidden: false, querySelector: () => null },
  require: name => name === "three" ? THREE : name === "./control-safety.js" ? safety :
    name === "./manual-drive.js" ? drive : name === "./map-walk.js" ? walk :
      name === "./demo-config.js" ? { DEMO_MODE: true, DEMO_BLIP_MS: 1000 } : {} };
vm.runInNewContext(ts.transpileModule(readFileSync(new URL("../src/rover.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, context);

function fixture() {
  const rover = Object.create(exports.RoverExperience.prototype);
  const controller = handedness => ({ handedness, gripSpace: {}, gamepad: { connected: true,
    axes: [0, 0, 0, 0], buttons: Array.from({ length: 6 }, () => ({ pressed: false, value: 0 })) } });
  const right = controller("right"), left = controller("left");
  const session = { visibilityState: "visible", inputSources: [right, left] };
  const starts = [], ends = [], stops = [], sent = [];
  const voice = { phase: "idle", capturing: false, pendingAction: null,
    get busy() { return this.capturing || this.phase === "processing"; },
    pushStart() { starts.push(1); this.phase = "listening"; this.capturing = true; return true; },
    pushEnd() { ends.push(1); this.phase = "processing"; this.capturing = false; },
    stopListening(reason) { if (this.capturing) { stops.push(reason); this.phase = "error"; this.capturing = false; } } };
  Object.assign(rover, { voice, talkDown: false, voiceReleaseRequired: false, rightSource: right, leftSource: left,
    renderer: { xr: { isPresenting: true, getSession: () => session, getReferenceSpace: () => ({}) } },
    gate: new safety.ControlSafety(), input: { held: false, v: 0, w: 0, pan: 0 },
    quat: new THREE.Quaternion(), position: new THREE.Vector3(), euler: new THREE.Euler(), baseYaw: 0,
    digitalPan: true, auto: false, autoArming: false, recenter: false, suspended: false,
    toggleDown: false, exploreDown: false, actionMenu: { visible: false }, pointer: {},
    navOverlay: { setReticle() {}, update() {}, group: {} }, cockpit: {}, cameraScreen: {}, bannerPanel: { scale: { setScalar() {} } },
    autonomy: new AutonomyIntent(), socket: { readyState: 1, bufferedAmount: 0 }, lastCanvasDraw: now,
    menuCloseAt: -Infinity, pollCamera() {}, pollHealth() {}, paint() {}, placeCameraScreen() {},
    updateHint() {}, send(value) { sent.push(value); } });
  let leftTracked = true, headTracked = true;
  const frame = { getViewerPose: () => headTracked ? { transform: { orientation: { x: 0, y: 0, z: 0, w: 1 }, position: { x: 0, y: 0, z: 0 } } } : null,
    getPose: space => space === left.gripSpace && !leftTracked ? null : {} };
  const tick = () => {
    now += 60;
    rover.status = { session_id: "test", permit: `p${now}`, rover: true, permit_ms: 250 };
    rover.gate.acceptStatus(rover.status, now);
    rover.gate.acceptFrame(String(now), 0, now); rover.lastCanvasDraw = now;
    rover.update(now, frame); rover.pump();
    return sent.at(-1);
  };
  tick();
  return { rover, voice, left, right, starts, ends, stops, sent, tick,
    loseLeft() { leftTracked = false; }, restoreLeft() { leftTracked = true; },
    removeHeadset() { headTracked = false; session.visibilityState = "hidden"; } };
}

test("left trigger starts once, hold stays stopped, release submits and processing stays stopped", () => {
  const f = fixture();
  f.right.gamepad.axes[3] = -1;
  f.right.gamepad.buttons[0].pressed = true;
  f.left.gamepad.buttons[0].pressed = true;
  assert.equal(f.tick(), false);
  assert.equal(f.starts.length, 1, "the actual XR update must call pushStart");
  for (let i = 0; i < 5; i++) assert.equal(f.tick(), false, "demo mode cannot resume while asking");
  assert.equal(f.starts.length, 1);
  f.left.gamepad.buttons[0].pressed = false;
  assert.equal(f.tick(), false);
  assert.equal(f.ends.length, 1);
  for (let i = 0; i < 5; i++) assert.equal(f.tick(), false, "wait for /ask too");
  f.voice.phase = "answer";
  assert.equal(f.tick(), false, "a held right trigger cannot resume after an answer");
  f.right.gamepad.buttons[0].pressed = false;
  f.right.gamepad.axes[3] = 0;
  assert.equal(f.tick(), false);
  f.right.gamepad.axes[3] = -1;
  assert.equal(f.tick(), true, "release and center rearm the existing joystick controls");
});

test("tracking loss cancels the mic and a held left trigger cannot restart on recovery", () => {
  const f = fixture();
  f.left.gamepad.buttons[0].pressed = true; f.tick();
  assert.equal(f.starts.length, 1);
  f.loseLeft(); f.tick();
  assert.equal(f.stops.length, 1);
  f.restoreLeft(); f.tick();
  assert.equal(f.starts.length, 1);
  f.left.gamepad.buttons[0].pressed = false; f.tick();
  f.left.gamepad.buttons[0].pressed = true; f.tick();
  assert.equal(f.starts.length, 2);
  f.removeHeadset(); f.tick();
  assert.equal(f.stops.length, 2, "removing the headset cancels capture");
});

test("hands-free demo AUTO stays stopped through a question and rearms after right-trigger release", () => {
  const f = fixture();
  f.rover.auto = true; f.rover.autonomy.toggleExplore();
  f.right.gamepad.buttons[0].pressed = true;
  f.left.gamepad.buttons[0].pressed = true;
  for (let i = 0; i < 5; i++) assert.equal(f.tick(), false);
  f.left.gamepad.buttons[0].pressed = false;
  for (let i = 0; i < 5; i++) assert.equal(f.tick(), false);
  f.voice.phase = "answer";
  assert.equal(f.tick(), false);
  f.right.gamepad.buttons[0].pressed = false;
  assert.equal(f.tick(), false);
  assert.equal(f.tick(), true);
});

test("in-VR panel shows Heard text and the answer, and stable text uploads no new texture", () => {
  const f = fixture(), labels = [];
  const canvasContext = { clearRect() {}, fillRect() {}, fillText: text => labels.push(text), measureText: text => ({ width: text.length * 12 }) };
  Object.assign(f.rover, { voicePanel: {}, voiceCanvas: { getContext: () => canvasContext }, voiceTexture: {}, voiceKey: "" });
  Object.assign(f.voice, { phase: "answer", statusText: "Rover answered.", questionText: "What do you see?", answerText: "A doorway ahead" });
  f.rover.drawVoice();
  assert.equal(f.rover.voicePanel.visible, true);
  assert.ok(labels.includes("Heard: What do you see?"));
  assert.ok(labels.includes("A doorway ahead"));
  const count = labels.length;
  f.rover.voiceTexture.needsUpdate = false;
  f.rover.drawVoice();
  assert.equal(labels.length, count);
  assert.equal(f.rover.voiceTexture.needsUpdate, false);
  f.voice.phase = "idle"; f.rover.drawVoice();
  assert.equal(f.rover.voicePanel.visible, false);
});
