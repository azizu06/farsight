import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import * as viewerSettings from '../src/viewer-settings.js';

const source = readFileSync(new URL('../src/rover.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} }).outputText;
const module = { exports: {} };
let decode;
let fetchRequests = 0;
const documentFixture = { hidden: false };
class ImageFixture {
  decode() { return new Promise(resolve => { decode = resolve; }); }
}
vm.runInNewContext(code, { exports: module.exports,
  require: name => name === './viewer-settings.js' ? viewerSettings : ({}),
  document: documentFixture, performance: { now: () => 100 },
  AbortController, Image: ImageFixture, WebSocket: { OPEN: 1 }, window: { setTimeout, clearTimeout },
  URL: { createObjectURL: () => 'blob:fixture', revokeObjectURL: () => {} },
  fetch: async () => { fetchRequests++; return ({ ok: true, headers: { get: key => ({
    'X-Frame-Id': '7', 'X-Frame-Age-Ms': '10', 'X-Frame-Source': 'SIMULATED',
  })[key] }, blob: async () => ({ size: 15, type: 'image/jpeg' }) }); },
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

test('rover-offered H.264 becomes the unsaved default, never overrides a choice, and falls back to JPEG', () => {
  const selected = [];
  const view = Object.create(prototype);
  Object.assign(view, { stopped: false, h264Enabled: false, h264: { displayedFrame: null },
    settings: { origin: { h264: 'default' } }, sourceTruth: { health: { video: { enabled: true } } },
    setVideo(enabled, save) { selected.push([enabled, save]); this.h264Enabled = enabled; } });
  view.applyRoverVideo();
  assert.deepEqual(selected, [[true, false]]);
  assert.equal(view.settings.origin.h264, 'rover');
  view.applyRoverVideo();  // still within the grace period
  assert.equal(selected.length, 1);
  view.roverVideoAt = -10_000;  // no verified decoded frame for >6 s
  view.applyRoverVideo();
  assert.deepEqual(selected.at(-1), [false, false]);
  assert.equal(view.settings.origin.h264, 'fallback');
  view.applyRoverVideo();
  assert.equal(selected.length, 2);
  for (const origin of ['saved', 'query']) {
    const chosen = Object.create(prototype);
    Object.assign(chosen, { stopped: false, settings: { origin: { h264: origin } },
      sourceTruth: { health: { video: { enabled: true } } }, setVideo: () => assert.fail('explicit choice overridden') });
    chosen.applyRoverVideo();
  }
});

test('H264 enable cannot bypass the explicit bonded profile or write preferences', () => {
  const view = Object.create(prototype);
  Object.assign(view, { bondedJPEG: true, h264Enabled: false, videoToggle: {} });
  prototype.setVideo.call(view, true, true);
  assert.equal(view.h264Enabled, false);
  assert.equal(view.videoToggle.checked, false);
});

test('immersive camera fetch continues when the browser hides the page surface', async () => {
  documentFixture.hidden = true;
  decode = undefined;
  fetchRequests = 0;
  const view = Object.create(prototype);
  Object.assign(view, { h264Enabled: false, frameRequests: new Map(), frameRequestSeq: 0, displayedFrameRequest: -1, stopped: false,
    renderer: { xr: { isPresenting: true, getSession: () => ({ visibilityState: 'visible' }) } }, getView: () => 'camera',
    gate: { session: 'immersive-session' }, frame: null, frameId: 'old-frame' });
  try {
    const request = view.fetchFrame();
    assert.equal(fetchRequests, 1, 'XR-visible camera polling must not use document.hidden as a stop signal');
    while (!decode) await new Promise(resolve => setImmediate(resolve));
    decode();
    await request;
    assert.equal(view.frameId, '7');
    assert.equal(view.frameSource, 'SIMULATED');
    assert.equal(view.frameRequests.size, 0);
  } finally {
    documentFixture.hidden = false;
  }
});

test('camera polling cadence uses the active render-loop clock', () => {
  const view = Object.create(prototype);
  let requests = 0;
  Object.assign(view, { lastFramePoll: -Infinity, frameRequests: new Map(), fetchFrame: () => { requests++; } });
  view.pollCamera(100);
  view.pollCamera(199);
  view.pollCamera(200);
  assert.equal(requests, 2, 'poll at most every 100 ms, independently of throttled window timers');
});

test('JPEG decode begun in an old control session cannot become the new session image', async () => {
  decode = undefined;
  const view = Object.create(prototype);
  let cleared = 0;
  Object.assign(view, { h264Enabled: false, frameRequests: new Map(), frameRequestSeq: 0, displayedFrameRequest: -1, stopped: false,
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
  assert.equal(view.frameRequests.size, 0);
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
