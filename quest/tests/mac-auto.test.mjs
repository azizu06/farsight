import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as THREE from "three";
import { MapWalk } from "../src/map-walk.js";
import {
  MAC_STALE_MS, PoseRing, macPresenting, macViewEnabled, mapPoint, mapPoseMessage, mapQuaternion, pictureSize,
  projectionFovY, shouldSendPose, worldPoint,
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
