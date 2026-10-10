import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { ControlSafety } from '../src/control-safety.js';
import { SourceTruth } from '../src/source-banner.js';
import * as cameraView from '../src/camera-view.js';
import * as frameOrderModule from '../src/frame-order.js';

const code = ts.transpileModule(readFileSync(new URL('../src/rover.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture({ decode = () => Promise.resolve() } = {}) {
  const clock = { now: 0 }, document = { hidden: false, querySelector: () => null }, requests = [];
  const module = { exports: {} };
  const urls = { created: 0, revoked: 0, decoding: 0, peakDecoding: 0 };
  class ImageFixture {
    naturalWidth = 640; naturalHeight = 480; #src = ''; onSrc = null;
    get src() { return this.#src; }
    set src(value) { this.#src = value; this.onSrc?.(value); }
    decode() {
      urls.decoding++; urls.peakDecoding = Math.max(urls.peakDecoding, urls.decoding);
      return decode(this).finally(() => { urls.decoding--; });
    }
  }
  vm.runInNewContext(code, { exports: module.exports, require: name => name === './camera-view.js' ? cameraView : name === './frame-order.js' ? frameOrderModule : {}, document,
    performance: { now: () => clock.now }, AbortController, Image: ImageFixture,
    window: { setTimeout, clearTimeout }, WebSocket: { OPEN: 1 },
    URL: { createObjectURL: () => { urls.created++; return 'blob:fixture'; }, revokeObjectURL() { urls.revoked++; } },
    fetch: (_url, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
      requests.push({ signal, startedAt: clock.now, settled: false, reply(id, age = 100) { this.settled = true; resolve({ ok: true, bodyUsed: false,
        headers: { get: key => ({ 'X-Frame-Id': String(id), 'X-Frame-Age-Ms': String(age), 'X-Frame-Source': 'PHONE RGB' })[key] },
        blob: async () => ({ size: 15, type: 'image/jpeg' }), body: { cancel: async () => {} } }); } });
    }),
  });
  const view = Object.create(module.exports.RoverExperience.prototype);
  const session = { visibilityState: 'visible' };
  Object.assign(view, { h264Enabled: false, stopped: false, frameBusy: false,
    frameRequests: new Map(), frameRequestSeq: 0,
    lastFramePoll: -Infinity, lastHealthPoll: -Infinity, lastPumpAt: -Infinity,
    renderer: { xr: { isPresenting: true, getSession: () => session } }, getView: () => 'camera',
    gate: new ControlSafety(), frame: null, frameId: '', pendingFrame: null,
    detectionFrames: { clear() {} }, sent: new Map(),
    canvas: { width: 640, height: 480 }, ctx: { drawImage() {} }, cameraTexture: {},
    cameraScreen: { scale: { x: 1, y: 1 }, visible: true }, cockpit: { visible: true }, input: { pan: 0 }, digitalPan: false,
    drawDetections() {},
  });
  view.gate.session = 'session';
  const cleanup = async () => { for (const request of view.frameRequests.values()) request.controller.abort(); view.request?.abort(); await flush(); };
  return { view, clock, document, requests, session, cleanup, urls };
}

test('camera pipeline overlaps cellular requests but bounds the window to two', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0);
    f.clock.now = 100; f.view.pollCamera(100);
    f.clock.now = 160; f.view.pollCamera(160);
    assert.equal(f.requests.length, 2, 'two independent pulls should overlap a >160 ms RTT');
    f.clock.now = 180; f.requests[0].reply(1); await flush();
    f.clock.now = 200; f.view.pollCamera(200);
    assert.equal(f.requests.length, 3, 'a full-window tick must not postpone the next available slot');
    assert.equal(f.view.frameRequests.size, 2, 'bounded traffic, regardless of RTT');
  } finally { await f.cleanup(); }
});

test('a late older camera response never replaces newer displayed pixels or age', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0);
    f.clock.now = 100; f.view.pollCamera(100);
    assert.equal(f.requests.length, 2);
    f.clock.now = 180; f.requests[1].reply(12, 80); await flush();
    const newest = f.view.frame, pending = f.view.pendingFrame;
    assert.equal(f.view.frameId, '12');
    assert.equal(pending.age, 160, 'original server age plus this request/decode elapsed time');
    f.clock.now = 220; f.requests[0].reply(11, 100); await flush();
    assert.equal(f.view.frameId, '12');
    assert.equal(f.view.frame, newest);
    assert.equal(f.view.pendingFrame, pending);
  } finally { await f.cleanup(); }
});

test('control session reset cancels every outstanding camera request', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    assert.equal(f.requests.length, 2);
    f.view.gate.session = 'new-session';
    f.view.resetSessionView(); await flush();
    assert.ok(f.requests.every(request => request.signal.aborted));
    assert.equal(f.view.frameRequests.size, 0);
    assert.equal(f.view.frame, null);
    assert.equal(f.view.pendingFrame, null);
  } finally { await f.cleanup(); }
});

test('phone capture can restart its frame counter without changing the control session', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    f.clock.now = 180; f.requests[0].reply(10000); await flush();
    assert.equal(f.view.frameId, '10000');
    f.clock.now = 280; f.requests[1].reply(0); await flush();
    assert.equal(f.view.frameId, '0', 'a restarted camera is fresh evidence, not an old response');
    assert.equal(f.view.gate.session, 'session');
  } finally { await f.cleanup(); }
});

test('an earlier request that arrives later can supply the fresher capture-age bound', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    f.clock.now = 180; f.requests[1].reply(11, 320); await flush();
    assert.equal(f.view.frameId, '11');
    f.clock.now = 220; f.requests[0].reply(12, 0); await flush();
    assert.equal(f.view.frameId, '12', 'request start order is not capture order');
    assert.equal(f.view.pendingFrame.age, 220);
  } finally { await f.cleanup(); }
});

test('invalid capture-age metadata cannot replace validated pixels or freshness', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    f.clock.now = 180; f.requests[0].reply(1, 50); await flush();
    const frame = f.view.frame, pending = f.view.pendingFrame;
    f.clock.now = 200; f.requests[1].reply(2, -1); await flush();
    assert.equal(f.view.frame, frame);
    assert.equal(f.view.pendingFrame, pending);
  } finally { await f.cleanup(); }
});

test('the active display clock times out blocked camera slots even if window timers are throttled', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    f.clock.now = 750; f.view.pollCamera(750); await flush();
    assert.equal(f.requests[0].signal.aborted, true);
    assert.equal(f.requests[1].signal.aborted, false);
    f.view.pollCamera(750);
    assert.equal(f.requests.length, 3, 'the timed-out slot fetches again as soon as its aborted stage settled');
    assert.equal(f.view.frameRequests.size, 2);
  } finally { await f.cleanup(); }
});

test('switching to H264 cancels every outstanding JPEG pull', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    Object.assign(f.view, { bondedJPEG: false, videoToggle: {}, stop() {}, h264: { start() {}, stop() {} } });
    f.view.setVideo(true, false); await flush();
    assert.ok(f.requests.every(request => request.signal.aborted));
    assert.equal(f.view.frameRequests.size, 0);
    assert.equal(f.view.pendingFrame, null);
  } finally { await f.cleanup(); }
});

test('180 ms requests and 160 ms server age keep a 10 fps source continuously fresh after startup', async () => {
  const f = fixture(), truth = new SourceTruth();
  truth.accept({ replay: false, control: { simulated: false }, rover: true, camera: true, camera_source: 'PHONE RGB' }, 0);
  let live = 0, total = 0, id = 0, peakAge = 0;
  f.view.pump = () => {};
  try {
    for (let now = 0; now <= 2200; now += 10) {
      f.clock.now = now;
      for (const request of f.requests) {
        if (!request.settled && !request.signal.aborted && now - request.startedAt >= 180) request.reply(++id, 160);
      }
      await flush();
      // Display loop order: draw the canvas, render it, then commit freshness (afterRender).
      if (f.view.frame) { f.view.drawCamera(now); f.view.afterRender(now); }
      f.view.pollCamera(now);
      if (now < 400) continue;
      const age = f.view.gate.videoAge(now);
      const state = truth.state({ now, frameSource: f.view.frameSource, frameId: f.view.frameId, videoAgeMs: age });
      live += Number(state.kind === 'LIVE'); total++; peakAge = Math.max(peakAge, age);
    }
    assert.ok(live / total > .99, `fresh displayed frames in ${live}/${total} samples; peak age ${peakAge} ms`);
    assert.ok(peakAge < 500, 'the existing age cutoff is met by delivery rather than widened');
  } finally { await f.cleanup(); }
});

test('render-loop health polling keeps immersive live proof renewed without browser timers', async () => {
  const f = fixture(), truth = new SourceTruth();
  const health = { replay: false, control: { simulated: false }, rover: true, camera: true, camera_source: 'PHONE RGB' };
  let polls = 0;
  f.document.hidden = true;
  Object.assign(f.view, { sourceTruth: { refresh: async () => { polls++; truth.accept(health, f.clock.now); } },
    applyRoverMount() {}, applyVoiceAvailability() {}, applyTransportProfile() {}, applyRoverVideo() {} });
  for (let now = 0; now <= 12000; now += 100) {
    f.clock.now = now; f.view.pollHealth(now); await flush();
    assert.equal(truth.state({ now, frameSource: 'PHONE RGB', frameId: '1', videoAgeMs: 0 }).kind, 'LIVE');
  }
  assert.equal(polls, 7, 'health is renewed through the active display loop at two-second intervals');
});

test('visible immersive XR can send a freshly tracked right-trigger command while the 2D page is hidden', () => {
  const f = fixture(), packets = [];
  f.clock.now = 100;
  f.document.hidden = true;
  const status = { session_id: 'session', permit: 'test-only', permit_ms: 250, rover: true };
  f.view.gate.acceptStatus(status, 100); f.view.gate.acceptFrame('1', 0, 100);
  f.view.gate.decide(false, 100, true, true, 100);
  Object.assign(f.view, { socket: { readyState: 1, bufferedAmount: 0, send: value => packets.push(JSON.parse(value)) },
    status, seq: 0, lastSendAt: 100, lastDeadman: false, inputValid: true, xrLive: true,
    actionMenu: { visible: false }, suspended: false, contextLost: false, lastCanvasDraw: 100, lastXRAt: 100,
    input: { held: true, v: .45, w: .2, pan: 0 }, digitalPan: true,
    autonomy: { fields: () => ({}) }, scanning: false, scanPending: false, scanArmed: false });
  f.view.pump();
  assert.equal(packets.at(-1).deadman, true, 'XR visibility and fresh tracked input determine immersive availability');
  f.session.visibilityState = 'hidden'; f.view.suspended = true;
  f.view.pump();
  assert.equal(packets.at(-1).deadman, false, 'a genuinely hidden headset session still stops');
});

test('a decode that outlives its 750 ms timeout keeps its slot: never more than two decoders or blob URLs', async () => {
  // Worst case: decode() ignores the abort and never settles.
  const f = fixture({ decode: () => new Promise(() => {}) });
  try {
    f.view.pollCamera(0); f.clock.now = 100; f.view.pollCamera(100);
    f.clock.now = 150; f.requests[0].reply(1); f.requests[1].reply(2); await flush();
    for (let now = 200; now <= 5000; now += 50) { f.clock.now = now; f.view.pollCamera(now); await flush(); }
    assert.equal(f.requests.length, 2, 'no new pull while both decoders are still alive');
    assert.equal(f.view.frameRequests.size, 2);
    assert.ok(f.urls.created - f.urls.revoked <= 2, `live blob URLs: ${f.urls.created - f.urls.revoked}`);
    assert.ok(f.urls.peakDecoding <= 2, `peak decoders: ${f.urls.peakDecoding}`);
  } finally { await f.cleanup(); }
});

test('abort drops the image source so a pending decode settles, revokes its URL and frees the slot', async () => {
  const f = fixture({ decode: image => new Promise((_resolve, reject) => { image.onSrc = value => { if (!value) reject(new Error('EncodingError')); }; }) });
  try {
    for (let now = 0; now <= 3000; now += 50) {
      f.clock.now = now; f.view.pollCamera(now); await flush();
      for (const request of f.requests) if (!request.settled && !request.signal.aborted) request.reply(now);
      await flush();
      assert.ok(f.urls.peakDecoding <= 2 && f.view.frameRequests.size <= 2, `bounded at ${now}`);
    }
    assert.ok(f.requests.length > 4, `timed-out decodes were replaced: ${f.requests.length} pulls`);
    assert.equal(f.urls.created - f.urls.revoked, f.urls.decoding, 'every settled decode revoked its blob URL');
  } finally { await f.cleanup(); }
});

test('within one capture source the frame ordinal wins over request-delay age bounds', async () => {
  const f = fixture();
  try {
    f.view.pollCamera(0);
    f.clock.now = 300; f.view.pollCamera(300);
    // Frame 12 arrives over a slow request (bound 100 + 400 = 500 ms) ...
    f.clock.now = 400; f.requests[0].reply(12, 100); await flush();
    assert.equal(f.view.frameId, '12');
    // ... then older frame 11 over a fast request (bound 150 + 120 = 270 ms) must not replace it.
    f.clock.now = 420; f.requests[1].reply(11, 150); await flush();
    assert.equal(f.view.frameId, '12', 'older capture never replaces newer displayed pixels');
    assert.equal(f.view.pendingFrame.age, 500, 'and never lends it a fresher age');
  } finally { await f.cleanup(); }
});

test('one held command per permit per 50 ms tick; a release is never coalesced', () => {
  const f = fixture(), packets = [];
  const status = { session_id: 'session', permit: 'p1', permit_ms: 250, rover: true };
  const arm = now => { f.clock.now = now; f.view.gate.acceptStatus(f.view.status, now); f.view.gate.acceptFrame(String(now), 0, now); f.view.lastCanvasDraw = now; f.view.lastXRAt = now; };
  Object.assign(f.view, { socket: { readyState: 1, bufferedAmount: 0, send: value => packets.push(JSON.parse(value)) },
    status, seq: 0, lastSendAt: 0, lastDeadman: false, inputValid: true,
    actionMenu: { visible: false }, suspended: false, contextLost: false,
    input: { held: false, v: .3, w: 0, pan: 0 }, digitalPan: true,
    autonomy: { fields: () => ({}) }, scanning: false, scanPending: false, scanArmed: false });
  arm(0); f.view.gate.decide(false, 0, true, true, 0); f.view.input.held = true;
  arm(10); f.view.pump();
  assert.equal(packets.at(-1).deadman, true);
  // A new permit spends at once (the onmessage path) ...
  f.view.status = { ...status, permit: 'p2' }; arm(49); f.view.pump();
  assert.equal(packets.length, 2);
  // ... and the display-loop pump 1 ms later on the same permit adds nothing.
  arm(50); f.view.pump();
  assert.equal(packets.length, 2, 'redundant held send on the same permit');
  arm(100); f.view.pump();
  assert.equal(packets.length, 3, 'the next tick renews normally');
  f.view.input.held = false; arm(101); f.view.pump();
  assert.equal(packets.at(-1).deadman, false, 'release goes out immediately');
  assert.equal(packets.length, 4);
});

test('control is pumped only after renderer.render() returns, never from update()', () => {
  const rover = readFileSync(new URL('../src/rover.ts', import.meta.url), 'utf8');
  const update = rover.slice(rover.indexOf('  update(now: number'), rover.indexOf('  afterRender(now: number)'));
  assert.ok(update.length > 1000 && !update.includes('pumpControl') && !update.includes('this.pump()'), 'update() must not renew the lease');
  const main = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8');
  const render = main.indexOf('renderer.render(scene, camera);'), after = main.indexOf('rover?.afterRender(');
  assert.ok(render > 0 && after > render, 'afterRender follows a successful render; a throwing render skips it');
  const f = fixture(); let pumps = 0;
  f.view.pump = () => { pumps++; };
  f.view.afterRender(100); f.view.afterRender(120); f.view.afterRender(151);
  assert.equal(pumps, 2, 'afterRender keeps the 50 ms pump cadence');
});

test('drawn camera evidence is not spendable until a visible render commits it; a failed render stops', async () => {
  const f = fixture(), stops = [];
  f.view.pump = () => {}; f.view.stop = reason => stops.push(reason);
  f.view.pollCamera(0);
  f.clock.now = 120; f.requests[0].reply(5, 20); await flush();
  f.view.drawCamera(120);
  assert.equal(f.view.gate.frameId, null, 'a permit callback here sees only the previous evidence');
  assert.notEqual(f.view.lastCanvasDraw, 120, 'nor a fresh canvas-draw time');
  f.view.cameraScreen.visible = false; f.view.afterRender(121);
  assert.equal(f.view.gate.frameId, null, 'a hidden camera surface commits nothing');
  f.view.cameraScreen.visible = true; f.view.afterRender(130);
  assert.equal(f.view.gate.frameId, '5');
  assert.equal(f.view.lastCanvasDraw, 130);
  f.clock.now = 140; f.view.pollCamera(140); f.clock.now = 200; f.requests[1].reply(6, 20); await flush();
  f.view.drawCamera(200); f.view.renderFailed();
  f.view.afterRender(210);
  assert.equal(f.view.gate.frameId, '5', 'pixels from a frame whose render threw never count');
  assert.deepEqual(stops, ['Display render failed: release trigger']);
});
