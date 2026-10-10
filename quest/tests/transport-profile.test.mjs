import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/rover.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} }).outputText;
const module = { exports: {} };
let decode;
class ImageFixture {
  decode() { return new Promise(resolve => { decode = resolve; }); }
}
vm.runInNewContext(code, { exports: module.exports, require: () => ({}),
  document: { hidden: false }, performance: { now: () => 100 },
  AbortController, Image: ImageFixture, WebSocket: { OPEN: 1 }, window: { setTimeout, clearTimeout },
  URL: { createObjectURL: () => 'blob:fixture', revokeObjectURL: () => {} },
  fetch: async () => ({ ok: true, headers: { get: key => ({
    'X-Frame-Id': '7', 'X-Frame-Age-Ms': '10', 'X-Frame-Source': 'SIMULATED',
  })[key] }, blob: async () => ({ size: 15, type: 'image/jpeg' }) }),
});
const prototype = module.exports.RoverExperience.prototype;

test('bonded health forces JPEG without changing the saved user preference', () => {
  const selected = [];
  const view = Object.create(prototype);
  Object.assign(view, { stopped: false, videoToggle: {},
    sourceTruth: { health: { relay_transport: { profile: 'bonded-jpeg' } } },
    setVideo: (...args) => selected.push(args) });
  view.applyTransportProfile();
  assert.equal(view.bondedJPEG, true);
  assert.equal(view.videoToggle.disabled, true);
  assert.deepEqual(selected, [[false, false]]);
  // Missing health on disconnect cannot re-enable a nonredundant stream.
  view.sourceTruth.health = null;
  view.applyTransportProfile();
  assert.equal(view.videoToggle.disabled, true);
  view.sourceTruth.health = { video: { ready: true } };
  view.applyTransportProfile();
  assert.equal(view.bondedJPEG, false);
  assert.equal(view.videoToggle.disabled, false);
});

test('H264 enable cannot bypass the explicit bonded profile or write preferences', () => {
  const view = Object.create(prototype);
  Object.assign(view, { bondedJPEG: true, h264Enabled: false, videoToggle: {} });
  prototype.setVideo.call(view, true, true);
  assert.equal(view.h264Enabled, false);
  assert.equal(view.videoToggle.checked, false);
});

test('JPEG decode begun in an old control session cannot become the new session image', async () => {
  const view = Object.create(prototype);
  let cleared = 0;
  Object.assign(view, { h264Enabled: false, frameBusy: false, stopped: false,
    renderer: { xr: { isPresenting: false } }, getView: () => 'camera',
    gate: { session: 'old' }, frame: { retained: true }, frameId: 'old-frame',
    pendingFrame: { id: 'old-frame' }, detectionFrames: { clear: () => cleared++ }, sent: new Map() });
  const request = view.fetchFrame();
  while (!decode) await new Promise(resolve => setImmediate(resolve));
  view.gate.session = 'new';
  view.resetSessionView();
  decode();
  await request;
  assert.equal(view.frame, null);
  assert.equal(view.pendingFrame, null);
  assert.equal(view.frameId, '');
  assert.equal(view.lastCanvasDraw, -Infinity);
  assert.equal(view.frameBusy, false);
  assert.equal(cleared, 1);
});

test('open socket waits for an authoritative session before sending stop or motion', () => {
  const view = Object.create(prototype);
  const sent = [];
  Object.assign(view, { socket: { readyState: 1, bufferedAmount: 0, send: x => sent.push(x) },
    status: null, gate: { session: null }, seq: 4 });
  view.send(false); view.send(true);
  assert.deepEqual(sent, []);
  assert.equal(view.seq, 4, 'unknown-session commands are not constructed or queued');
  view.status = { session_id: 'old' }; view.gate.session = 'new';
  view.send(false);
  assert.deepEqual(sent, []);
});
