import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as THREE from "three";
import { MapWalk } from "../src/map-walk.js";
import * as macPose from "../src/mac-auto-pose.js";
import {
  MAC_STALE_MS, PoseRing, frameSeenAt, frustumRect, macPresenting, macStereoEnabled, macViewEnabled, mapPoint, mapPoseMessage,
  mapQuaternion, pictureSize, projectionFovY, segmentKind, shouldSendPose, viewerEyes, worldPoint,
} from "../src/mac-auto-pose.js";

const close = (actual, expected, label) => actual.forEach((value, i) => assert.ok(Math.abs(value - expected[i]) < 1e-9, `${label}[${i}] ${value} vs ${expected[i]}`));

function walked() {
  const walk = new MapWalk();
  walk.setMode("walk"); walk.setFloor(-0.4);
  walk.observeHead(0.3, -0.2, 0.7);
  walk.moveViewer(1.2, -0.5, 0.1, 0.4);
  walk.moveViewer(0.6, 0.2, 0, -1.1);
  return walk;
}

test("map pose is the inverse of the walk transform main.ts applies to the map root", () => {
  const walk = walked();
  const { yaw, position } = walk.rootTransform();
  const root = new THREE.Object3D();
  root.quaternion.setFromAxisAngle(new THREE.Vector3(0, 1, 0), yaw);
  root.position.fromArray(position);
  root.updateMatrixWorld();
  for (const point of [[0, 0, 0], [1.5, 0.2, -3], [-2, 1.1, 0.7]]) {
    const drawn = new THREE.Vector3(...point).applyMatrix4(root.matrixWorld).toArray();
    close(worldPoint(walk, point), drawn, "world");
    close(mapPoint(walk, drawn), point, "map");
  }
});

test("the Mac camera at the mapped head pose sees each map point where the headset sees it", () => {
  const walk = walked();
  const head = new THREE.Vector3(0.4, 1.6, -0.3);
  const gaze = new THREE.Quaternion().setFromEuler(new THREE.Euler(-0.2, 0.9, 0.05, "YXZ"));
  const macPosition = new THREE.Vector3(...mapPoint(walk, head.toArray()));
  const macQuaternion = new THREE.Quaternion(...mapQuaternion(walk, gaze.toArray()));
  for (const point of [[1, 0, -2], [-0.5, 1.4, 0.3], [3, 0.9, 2]]) {
    const inHeadset = new THREE.Vector3(...worldPoint(walk, point)).sub(head).applyQuaternion(gaze.clone().invert());
    const inMac = new THREE.Vector3(...point).sub(macPosition).applyQuaternion(macQuaternion.clone().invert());
    close(inMac.toArray(), inHeadset.toArray(), "eye-space");
  }
});

test("pose message keeps the v1 renderer fields and adds identity, eyes and FOV", () => {
  const walk = walked();
  const projection = new THREE.PerspectiveCamera(90, 1, 0.1, 100).projectionMatrix.elements;
  const message = mapPoseMessage({
    id: 7, mono: 1234.5, wall: 1_700_000_000_000, walk,
    head: { position: [0, 1.6, 0], quaternion: [0, 0, 0, 1] },
    ray: { origin: [0, 1.2, 0], direction: [0, 0, -1] },
    eyes: [{ position: [-0.03, 1.6, 0], quaternion: [0, 0, 0, 1], projection: [...projection] }],
    session: "s1", mapRevision: 42,
  });
  assert.equal(message.type, "pose");
  assert.equal(message.version, 1);
  assert.equal(message.sent_mono_ms, 1234.5);
  assert.equal(message.pose_id, 7);
  assert.equal(message.frame_id, 7, "alias for frame_id parsers");
  assert.equal(message.space, "ar_map");
  assert.equal(message.session_id, "s1");
  assert.equal(message.map_revision, 42);
  assert.equal(message.stereo, false);
  close(message.head.position, mapPoint(walk, [0, 1.6, 0]), "head");
  close(message.ray.origin, mapPoint(walk, [0, 1.2, 0]), "ray");
  assert.equal(message.eyes.length, 1);
  assert.ok(Math.abs(message.fov_y_deg - 90) < 1e-6);
  assert.ok(Math.abs(projectionFovY([...projection]) - 90) < 1e-6);
  assert.equal(mapPoseMessage({ id: 1, mono: NaN, wall: 0, walk, head: { position: [0, 0, 0], quaternion: [0, 0, 0, 1] }, session: "s" }), null);
});

test("a displayed frame finds the world pose it was rendered from, by pose_id or our send time", () => {
  const ring = new PoseRing(3);
  for (let id = 1; id <= 5; id += 1) ring.push({ id, mono: id * 14, position: [id, 0, 0], quaternion: [0, 0, 0, 1] });
  assert.equal(ring.find({ pose_id: 4 }).id, 4);
  assert.equal(ring.find({ sent_mono_ms: 70 }).id, 5);
  assert.equal(ring.find({ pose_id: 1 }), null, "bounded");
  assert.equal(ring.find(null), null);
});

test("poses are latest-only and capped at 72 Hz", () => {
  assert.equal(shouldSendPose(-Infinity, 0, 0), true);
  assert.equal(shouldSendPose(0, 10, 0), false);
  assert.equal(shouldSendPose(0, 14, 0), true);
  assert.equal(shouldSendPose(0, 100, 512), false, "never queue behind an unsent pose");
});

test("the local map returns when Mac frames stop", () => {
  assert.equal(macPresenting(true, -Infinity, 0), false, "no picture yet");
  assert.equal(macPresenting(true, 100, 100 + MAC_STALE_MS - 1), true);
  assert.equal(macPresenting(true, 100, 100 + MAC_STALE_MS), false);
  assert.equal(macPresenting(false, 100, 101), false, "only in AUTO");
});

test("a frozen picture goes stale: only a new decoded frame renews it", () => {
  let at = -Infinity, id = null;
  for (const [now, shown] of [[0, "h264:1:1"], [400, "h264:1:1"], [800, "h264:1:1"], [1200, "h264:1:1"]]) {
    at = frameSeenAt(at, id, shown, now); id = shown;
  }
  assert.equal(at, 0, "the same frame id never renews");
  assert.equal(macPresenting(true, at, 1200), false, "local map returns");
  assert.equal(frameSeenAt(at, id, "h264:1:2", 1250), 1250, "a new frame does");
  assert.equal(frameSeenAt(at, id, null, 1250), at, "no frame changes nothing");
});

// Run the production class without allocating DOM, decoder, or WebGL resources.
// The frame input is controlled; update(), placement, and presentation use real code.
const module = { exports: {} };
const compiled = ts.transpileModule(readFileSync(new URL("../src/mac-auto.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
vm.runInNewContext(compiled, { exports: module.exports, WebSocket: { OPEN: 1 },
  require: name => name === "three" ? THREE : name === "./mac-auto-pose.js" ? macPose : {} });

function presentation() {
  const view = Object.create(module.exports.MacAutoView.prototype), camera = new THREE.Object3D();
  const context = { clearRect() {}, beginPath() {}, roundRect() {}, fill() {}, fillText() {} };
  Object.assign(view, {
    active: true, viewer: { update() {}, displayedFrame: null, video: { videoWidth: 640, videoHeight: 360 } },
    renderer: { xr: { getCamera: () => camera, getReferenceSpace: () => null } }, poseSocket: null,
    head: { position: new THREE.Vector3(), quaternion: new THREE.Quaternion() }, offset: new THREE.Vector3(),
    lastFrameAt: -Infinity, lastFrameId: null, ring: new PoseRing(),
    picture: new THREE.Object3D(), label: new THREE.Object3D(), group: new THREE.Group(),
    eyeQuads: [new THREE.Object3D(), new THREE.Object3D()], stereoShown: false,
    labelCanvas: { getContext: () => context }, labelTexture: {}, labelText: "",
  });
  const show = (now, id, age, metadata = null, pose = null) => {
    view.viewer.displayedFrame = id === null ? null : { id, age, metadata, pose };
    return view.update(now, true);
  };
  return { view, show };
}

test("AUTO returns to the local map when new decoded IDs keep arriving with stale capture age", () => {
  const { view, show } = presentation();
  assert.equal(show(10000, "fresh", 20), true);
  for (let now = 10100; now <= 15100; now += 100) {
    assert.equal(show(now, `old:${now}`, MAC_STALE_MS + 200), false, "decode progress cannot renew old captures");
    assert.equal(view.picture.visible, false);
  }
  assert.equal(show(15200, "recovered", 20), true, "a fresh picture restores Mac presentation");
  assert.equal(view.picture.visible, true);
});

test("AUTO counts the frame's existing capture age while the same decoded picture remains", () => {
  const { show } = presentation();
  assert.equal(show(10000, "almost-stale", MAC_STALE_MS - 100), true);
  assert.equal(show(10050, "almost-stale", MAC_STALE_MS - 50), true);
  assert.equal(show(10100, null), false, "capture age reaches the bound even without another decoder callback");
});

test("AUTO invalidates a picture whose capture age is missing, nonfinite, or in the future", () => {
  for (const age of [undefined, NaN, Infinity, -Infinity, -1]) {
    const { view, show } = presentation();
    assert.equal(show(10000, "fresh", 20), true);
    assert.equal(show(10001, "invalid", age), false, `invalid age ${String(age)} clears the previous picture`);
    assert.equal(view.picture.visible, false);
    assert.equal(show(10002, "recovered", 0), true);
  }
});

test("flat picture is sized so it subtends the renderer's field of view", () => {
  const { width, height } = pictureSize(50, 16 / 9, 3);
  assert.ok(Math.abs(height - 2 * 3 * Math.tan(25 * Math.PI / 180)) < 1e-9);
  assert.ok(Math.abs(width / height - 16 / 9) < 1e-9);
  assert.deepEqual(pictureSize(NaN, NaN, 3), pictureSize(50, 16 / 9, 3));
});

test("Mac view is opt-in and never owns rover control", () => {
  assert.equal(macViewEnabled(""), false);
  assert.equal(macViewEnabled("?macview=1"), true);
  assert.equal(macViewEnabled("?macview=0"), false);
  const source = readFileSync(new URL("../src/mac-auto.ts", import.meta.url), "utf8");
  const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
  assert.doesNotMatch(code, /ws\/control/);
  const sockets = code.match(/new WebSocket\([^)]*\)/g);
  assert.deepEqual(sockets, ["new WebSocket(socketUrl(location.origin, MACVIEW_POSE_PATH)"], "only the pose socket; video goes through H264Viewer");
});

const stereoMeta = { stereo: true, layout: "sbs-left-right", eye_width: 960, eye_height: 1080 };

test("AUTO hides packed stereo when the exact eye pair is unavailable, then recovers", () => {
  const { view, show } = presentation();
  assert.equal(show(10000, "mono", 20), true);
  assert.equal(show(10001, "unknown-eyes", 20, stereoMeta, { pose_id: 99 }), false);
  assert.equal(view.picture.visible, false, "packed stereo cannot become a mono picture");
  assert.ok(view.eyeQuads.every(quad => !quad.visible));
  const projection = new THREE.PerspectiveCamera(90, 1, .1, 100).projectionMatrix.elements;
  view.ring.push({ id: 7, mono: 10002, position: [0, 1.6, 0], quaternion: [0, 0, 0, 1],
    eyes: [-.032, .032].map(x => ({ position: [x, 1.6, 0], quaternion: [0, 0, 0, 1], projection })) });
  assert.equal(show(10003, "paired", 20, stereoMeta, { pose_id: 7 }), true);
  assert.equal(view.picture.visible, false);
  assert.ok(view.eyeQuads.every(quad => quad.visible));
  assert.equal(show(10004, "invalid-layout", 20, { ...stereoMeta, layout: "top-bottom" }, { pose_id: 7 }), false);
  assert.ok(view.eyeQuads.every(quad => !quad.visible));
  assert.equal(show(10005, "paired-again", 20, stereoMeta, { pose_id: 7 }), true);
  assert.equal(show(10006, "stale-pair", MAC_STALE_MS, stereoMeta, { pose_id: 7 }), false);
  assert.ok(view.eyeQuads.every(quad => !quad.visible));
});

test("stereo is a separate opt-in on top of the Mac view and asks the renderer for packed pairs", () => {
  assert.equal(macStereoEnabled("?macview=1"), false);
  assert.equal(macStereoEnabled("?macview=1&stereo=1"), true);
  const source = readFileSync(new URL("../src/mac-auto.ts", import.meta.url), "utf8");
  assert.match(source, /stereo \? `\$\{MACVIEW_PATH\}\?layout=\$\{STEREO_LAYOUT\}` : MACVIEW_PATH/);
  const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  assert.match(main, /new MacAutoView\([^\n]*macStereoEnabled\(location\.search\)\)/);
});

test("segments are flat, a valid side-by-side pair, or invalid; invalid stereo never shows as mono", () => {
  assert.equal(segmentKind(null), "flat");
  assert.equal(segmentKind({ pose: {} }), "flat");
  assert.equal(segmentKind(stereoMeta), "stereo");
  assert.equal(segmentKind({ ...stereoMeta, layout: "top-bottom" }), "invalid");
  assert.equal(segmentKind({ ...stereoMeta, eye_width: 0 }), "invalid");
  assert.equal(segmentKind({ ...stereoMeta, stereo: false }), "invalid");
  assert.equal(segmentKind({ layout: "sbs-left-right" }), "invalid");
});

test("an eye quad covers exactly that eye's frustum at the picture distance, asymmetric included", () => {
  const symmetric = new THREE.PerspectiveCamera(90, 1, 0.1, 100).projectionMatrix.elements;
  const rect = frustumRect(symmetric, 3);
  close([rect.cx, rect.cy, rect.width, rect.height], [0, 0, 6, 6], "symmetric");
  // Off-axis frustum, like each Quest eye.
  const [left, right, top, bottom, near] = [-0.9, 0.5, 0.8, -0.95, 0.1];
  const asymmetric = new THREE.Matrix4().makePerspective(left * near, right * near, top * near, bottom * near, near, 100).elements;
  const skewed = frustumRect(asymmetric, 3);
  close([skewed.cx - skewed.width / 2, skewed.cx + skewed.width / 2], [3 * left, 3 * right], "horizontal edges");
  close([skewed.cy - skewed.height / 2, skewed.cy + skewed.height / 2], [3 * bottom, 3 * top], "vertical edges");
  assert.equal(frustumRect([], 3), null);
  assert.equal(frustumRect(new Array(16).fill(NaN), 3), null);
});

test("eye poses come from the XRFrame views, ordered and labelled, or not at all", () => {
  const view = (eye, x) => ({
    eye, projectionMatrix: Float32Array.from({ length: 16 }, (_, i) => i === 0 ? 1.2 : i === 5 ? 1.1 : 0),
    transform: { position: { x, y: 1.6, z: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } },
  });
  const eyes = viewerEyes([view("right", 0.032), view("left", -0.032)]);
  assert.deepEqual(eyes.map(eye => eye.eye), ["left", "right"]);
  assert.deepEqual(eyes[0].position, [-0.032, 1.6, 0]);
  assert.equal(eyes[1].projection.length, 16);
  assert.deepEqual(viewerEyes([view("none", 0)]), []);
  assert.deepEqual(viewerEyes(undefined), []);
  const message = mapPoseMessage({
    id: 7, mono: 1, wall: 2, walk: new MapWalk(), head: { position: [0, 1.6, 0], quaternion: [0, 0, 0, 1] }, ray: null,
    eyes, session: "s", mapRevision: 1, aspect: 16 / 9,
  });
  assert.deepEqual(message.eyes.map(eye => eye.eye), ["left", "right"]);
});
