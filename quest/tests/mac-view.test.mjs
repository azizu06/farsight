import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";
import { MacView, frameAgeMs, poseMessage, shouldSendPose, POSE_HZ, LABEL, streamOrigin, streamURL, trackedXR } from "../src/mac-view.ts";

test("frame age is now minus the capture timestamp", () => {
  assert.equal(frameAgeMs(1_000, 1_040), 40);
  assert.equal(frameAgeMs(1_040, 1_000), -40);
  assert.throws(() => frameAgeMs(Number.NaN, 1), /finite/);
});

test("pose message carries head, right-ray and both clocks", () => {
  const vec = (x, y, z) => ({ x, y, z, toArray: () => [x, y, z] });
  const message = poseMessage(
    { mono: 12.5, wall: 1_700_000_000_000 },
    { position: vec(0, 1.6, 0), quaternion: { x: 0, y: 0, z: 0, w: 1 } },
    { origin: vec(0.2, 1.4, -0.3), direction: vec(0, 0, -1) },
  );
  assert.equal(message.type, "pose");
  assert.equal(message.version, 1);
  assert.equal(message.sent_mono_ms, 12.5);
  assert.deepEqual(message.head.quaternion, [0, 0, 0, 1]);
  assert.deepEqual(message.ray.direction, [0, 0, -1]);
  assert.equal(poseMessage({ mono: 0, wall: 0 }, { position: vec(0, 0, 0), quaternion: { x: 0, y: 0, z: 0, w: 1 } }, null).ray, null);
});

test("poses stay at or under 72 Hz and the label says this is not stereo", () => {
  assert.equal(shouldSendPose(0, 1000 / POSE_HZ - 0.01), false);
  assert.equal(shouldSendPose(0, 1000 / POSE_HZ), true);
  assert.match(LABEL, /not stereo/);
});

const head = () => ({ position: new THREE.Vector3(0, 1.6, 0), quaternion: new THREE.Quaternion() });
function fixture(t) {
  const saved = { document: globalThis.document, window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [], timers = new Map(); let timerId = 0, updates = 0, starts = 0, stops = 0;
  class Socket {
    static OPEN = 1; readyState = 1; bufferedAmount = 0; sent = [];
    constructor(url) { this.url = url; sockets.push(this); }
    send(text) { this.sent.push(JSON.parse(text)); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  globalThis.document = { createElement: () => ({ textContent: '', remove() {} }) };
  globalThis.window = { setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); } };
  globalThis.WebSocket = Socket;
  const stream = { video: { remove() {} }, state: 'waiting', displayedFrame: null,
    start() { starts++; }, stop() { stops++; }, update() { updates++; } };
  const view = new MacView('http://127.0.0.1:8090', { append() {} }, stream);
  t.after(() => { view.dispose(); for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } });
  return { view, stream, sockets, timers, counters: () => ({ updates, starts, stops }) };
}

test('both socket paths use the same selected origin and credentials/paths are rejected', () => {
  assert.equal(streamURL('http://127.0.0.1:8090', '/ws/macview'), 'http://127.0.0.1:8090/ws/macview');
  assert.equal(streamURL('http://127.0.0.1:8090', '/ws/macview-pose'), 'http://127.0.0.1:8090/ws/macview-pose');
  for (const bad of ['ws://localhost:8090', 'http://user:pass@localhost:8090', 'http://localhost:8090/x', 'http://localhost:8090?x=1']) assert.throws(() => streamOrigin(bad));
});

test('latest-only poses never append behind buffered bytes and carry a monotonic nonce ledger', t => {
  const { view, sockets } = fixture(t); view.start(); const socket = sockets[0];
  socket.bufferedAmount = 1;
  view.sendPose({ mono: 100, wall: 1000 }, head(), null);
  assert.equal(socket.sent.length, 0);
  socket.bufferedAmount = 0;
  view.sendPose({ mono: 120, wall: 1020 }, head(), null);
  view.sendPose({ mono: 140, wall: 1040 }, head(), null);
  assert.deepEqual(socket.sent.map(p => p.frame_id), [1, 2]);
  assert.equal(socket.sent[0].session_id, socket.sent[1].session_id);
  assert.notEqual(socket.sent[0].client_nonce, socket.sent[1].client_nonce);
  assert.equal(socket.sent[0].tracked, true);
  assert.equal(socket.sent[0].head.position[1], 1.6);
});

test('tracking loss sends one clear, recovery uses fresh samples, invalid input never restamps', t => {
  const { view, sockets } = fixture(t); view.start(); const socket = sockets[0];
  view.sendPose({ mono: 100, wall: 1000 }, head(), null);
  view.untracked(); view.untracked();
  assert.deepEqual(socket.sent.map(p => p.type), ['pose', 'pose_untracked']);
  assert.equal(socket.sent[1].tracked, false);
  view.sendPose({ mono: 140, wall: 1040 }, head(), null);
  const invalid = head(); invalid.position.x = NaN;
  view.sendPose({ mono: 160, wall: 1060 }, invalid, null);
  assert.deepEqual(socket.sent.map(p => p.type), ['pose', 'pose_untracked', 'pose', 'pose_untracked']);
});

test('stop cancels queued reconnect and old callbacks cannot reopen after restart', t => {
  const { view, sockets, timers, counters } = fixture(t); view.start(); view.start();
  assert.equal(sockets.length, 1); assert.equal(counters().starts, 1);
  sockets[0].close(); const stale = [...timers.values()][0];
  view.stop(); assert.equal(timers.size, 0); stale(); assert.equal(sockets.length, 1);
  view.start(); stale(); assert.equal(sockets.length, 2);
  const sid = sockets[1]; view.sendPose({ mono: 200, wall: 2000 }, head(), null);
  view.stop(); view.sendPose({ mono: 300, wall: 2100 }, head(), null);
  assert.equal(sid.sent.filter(p => p.type === 'pose').length, 1);
  assert.equal(counters().stops, 2);
});

test('actual decoded segment age/source and matching client echo drive status; walls are ignored', t => {
  const { view, stream, sockets, counters } = fixture(t); t.mock.method(performance, 'now', () => 200);
  view.start(); view.sendPose({ mono: 100, wall: -1000000 }, head(), null);
  const sent = sockets[0].sent[0];
  assert.equal(view.noteSidecar({ type: 'video_init', stereo: false, version: 1, source: 'MAC RENDER' }), true);
  stream.displayedFrame = { age: 650, source: 'SIMULATED', metadata: { capture_wall_ms: 999999999, pose: sent } };
  view.update(); assert.equal(counters().updates, 1);
  assert.equal(view.frameAgeMs, 650); assert.equal(view.poseRoundTripMs, 100);
  assert.match(view.age.textContent, /SIMULATED.*STALE.*not photon latency/);
  stream.displayedFrame.metadata.pose = { ...sent, client_nonce: 'not-the-sent-nonce' };
  view.update(); assert.equal(view.poseRoundTripMs, null);
  stream.displayedFrame = null; view.update(); assert.match(view.age.textContent, /age unknown/);
  assert.equal(view.noteSidecar({ type: 'video_init', stereo: true, version: 1 }), false);
});

test('right source uses targetRaySpace minus-Z, and null/hidden tracking produces no pose', () => {
  const right = { handedness: 'right', targetRaySpace: {} }, left = { handedness: 'left', targetRaySpace: {} };
  const transform = { position: { x: 1, y: 2, z: 3 }, orientation: { x: 0, y: 0, z: 0, w: 1 } };
  const frame = { getViewerPose: () => ({ transform }), getPose: space => { assert.equal(space, right.targetRaySpace); return { transform }; } };
  const session = { visibilityState: 'visible', inputSources: [left, right] };
  assert.deepEqual(trackedXR(frame, {}, session).ray.direction.toArray(), [0, 0, -1]);
  assert.equal(trackedXR({ ...frame, getViewerPose: () => null }, {}, session), null);
  assert.equal(trackedXR(frame, {}, { ...session, visibilityState: 'hidden' }), null);
  assert.equal(trackedXR(frame, {}, { ...session, visibilityState: 'visible-blurred' }), null);
  assert.equal(trackedXR({ ...frame, getViewerPose: () => { throw new Error('inactive'); } }, {}, session), null);
  assert.equal(trackedXR({ ...frame, getPose: () => null }, {}, session).ray, null);
});
