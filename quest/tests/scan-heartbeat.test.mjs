import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { ControlSafety } from '../src/control-safety.js';
import { AutonomyIntent } from '../src/autonomy.js';

// Run the real production methods and safety gate, without DOM/WebGL construction.
const source = readFileSync(new URL('../src/rover.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} });
const module = { exports: {} };
let clock = 100;
let fetchImpl;
vm.runInNewContext(compiled.outputText, { exports: module.exports, require: () => ({}),
  performance: { now: () => clock }, WebSocket: { OPEN: 1 },
  document: { hidden: false, querySelector: () => null },
  AbortController, window: { setTimeout, clearTimeout }, fetch: (...args) => fetchImpl(...args),
});
function fixture() {
  clock = 100;
  const view = Object.create(module.exports.RoverExperience.prototype);
  const gate = new ControlSafety();
  const status = { session_id: 'sim-only', permit: 'fresh-one', permit_ms: 250,
    rover: true, simulated: true, requires_release: false, mode: 'teleop' };
  gate.acceptStatus(status, clock);
  gate.acceptFrame('SIMULATED:1', 100, clock);
  gate.decide(false, clock, true, true);
  const sent = [];
  const socket = { readyState: 1, bufferedAmount: 0, closed: false,
    send: message => sent.push(JSON.parse(message)), close() { this.closed = true; } };
  Object.assign(view, { gate, status, socket, renderer: { xr: { isPresenting: false } },
    getView: () => 'camera', inputValid: true, contextLost: false, lastCanvasDraw: clock,
    input: { held: true, v: 0, w: 0, pan: 0 }, lastSendAt: clock - 25, lastDeadman: true,
    scanPending: false, scanPendingCancelled: false, scanning: false, scanCompleting: false, scanArmed: false,
    scanPollAt: clock, digitalPan: true, seq: 1, sent: new Map(), autonomy: new AutonomyIntent() });
  return { view, gate, socket, sent };
}

test('timer/new-permit double pump coalesces one fresh handoff without another send or session reset', () => {
  const { view, socket, sent } = fixture();
  view.pump(); assert.equal(sent.length, 1);
  socket.bufferedAmount = 187;
  clock += 4; view.pump();
  assert.equal(sent.length, 1); assert.equal(socket.closed, false);
  socket.bufferedAmount = 0;
  clock += 21; view.status.permit = 'fresh-two'; view.pump();
  assert.equal(sent.length, 2, 'a new 40 Hz permit remains immediately spendable');
  assert.equal(sent[1].permit, 'fresh-two');
});

test('a true pending backlog at the regular heartbeat cadence still closes and requires release', () => {
  const { view, gate, socket, sent } = fixture();
  socket.bufferedAmount = 187; view.lastSendAt = clock - 50;
  view.pump();
  assert.equal(socket.closed, true); assert.equal(sent.length, 0);
  assert.equal(gate.needsRelease, true); assert.match(gate.reason, /queue stalled/);
});

test('stale camera or display still closes immediately even behind a recent motion packet', () => {
  for (const cause of ['camera', 'display']) {
    const { view, gate, socket, sent } = fixture();
    socket.bufferedAmount = 187; view.lastSendAt = clock - 4;
    if (cause === 'camera') gate.frameAge = 500;
    else view.lastCanvasDraw = clock - 250;
    view.pump();
    assert.equal(socket.closed, true, cause); assert.equal(sent.length, 0);
    assert.equal(gate.needsRelease, true);
  }
});

test('pending POST continues fresh zero-stick heartbeats and uses scan mode only after server reservation', () => {
  const { view, sent } = fixture(); view.scanPending = true;
  view.pump(); assert.equal(sent[0].mode, 'teleop'); assert.equal(sent[0].deadman, true);
  clock += 25; view.status = { ...view.status, permit: 'reserved', mode: 'scan' };
  view.gate.acceptStatus(view.status, clock);
  view.pump(); assert.equal(sent[1].mode, 'scan'); assert.equal(sent[1].deadman, true);
  assert.equal(sent[1].v, 0); assert.equal(sent[1].w, 0);
});

test('pending reservation never renews motion after a safety latch or physical release', () => {
  for (const cause of ['latch', 'release']) {
    const { view, gate, sent } = fixture(); view.scanPending = true; view.status.mode = 'scan';
    if (cause === 'latch') gate.invalidate('camera_stale'); else view.input.held = false;
    view.pump(); assert.equal(sent[0].deadman, false, cause);
  }
});


test('resampled stick override during reservation cancels before forwarding nonzero teleop or scan motion', () => {
  for (const mode of ['teleop', 'scan']) for (const axis of ['v', 'w']) {
    const { view, gate, sent } = fixture();
    view.scanPending = true; view.status.mode = mode;
    view.input[axis] = .4;
    view.pump();
    assert.equal(sent.length, 1);
    assert.equal(sent[0].deadman, false);
    assert.equal(sent[0].v, 0); assert.equal(sent[0].w, 0);
    assert.equal(gate.needsRelease, true);
    assert.equal(view.scanPendingCancelled, true);
    assert.match(view.scanMessage, /stick override/);
    clock += 25; view.pump();
    assert.equal(sent[1].deadman, false, 'centering again while held cannot renew the reservation');
  }
});

test('late successful POST cannot rearm a reservation cancelled by newly sampled stick input', async () => {
  const { view, gate, sent } = fixture();
  let deliver;
  fetchImpl = () => new Promise(resolve => { deliver = resolve; });
  const pending = view.startScan();
  assert.equal(view.scanPending, true);
  view.status.mode = 'scan'; view.input.w = -.45; view.pump();
  assert.equal(sent.at(-1).deadman, false); assert.equal(gate.needsRelease, true);
  const cancelledMessage = view.scanMessage;
  deliver({ ok: true, json: async () => ({ simulated: true, approximate: true }) });
  await pending;
  assert.equal(view.scanPending, false); assert.equal(view.scanning, false);
  assert.equal(view.scanMessage, cancelledMessage);
});

test('completion observed before POST delivery polls before classifying a held stick or release latch as abort', () => {
  const { view, gate, sent } = fixture();
  view.scanPending = true; view.scanCompleting = true; view.scanPollAt = -200;
  view.input.v = .35; gate.invalidate('release_required');
  let polls = 0; view.pollScan = () => { polls++; };
  view.pump();
  assert.equal(polls, 1);
  assert.equal(view.scanPendingCancelled, false);
  assert.equal(sent[0].deadman, false); assert.equal(sent[0].v, 0);
  assert.equal(view.scanMessage, undefined, 'no premature cancelled label replaces completion polling');
});

test('pending completion poll accepts the authoritative result without waiting for the POST response', async () => {
  const { view } = fixture();
  view.scanPending = true; view.scanCompleting = true;
  let selected;
  view.setView = next => { selected = next; };
  fetchImpl = async () => ({ ok: true, json: async () => ({ state: 'complete', running: false,
    points: 123, scan_ms: 400, approximate: true }) });
  await view.pollScan();
  assert.equal(selected, '3d'); assert.equal(view.scanning, false);
  assert.equal(view.scanPendingCancelled, true, 'late POST must not overwrite the completed result');
  assert.match(view.scanMessage, /123 points/);
});
