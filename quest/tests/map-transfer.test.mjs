import assert from 'node:assert/strict';
import test from 'node:test';
import { chooseMapTransfer } from '../src/map-transfer.js';

const state = (changes = {}) => ({
  needed: true, busy: false, now: 15000, lastStarted: 10000,
  pointsOnly: false, view: '3d', triangles: 200,
  pointsTransport: 'http', pointRevision: 12, meshRevision: 9,
  appliedPoints: 11, appliedMesh: 8, ...changes,
});

test('hybrid 3D downloads the mesh replacement once without a second point snapshot', () => {
  assert.equal(chooseMapTransfer(state()), 'mesh');
  assert.equal(chooseMapTransfer(state({ appliedMesh: 9 })), null);
  // A newer point revision cannot turn an unchanged preferred mesh into two pulls.
  assert.equal(chooseMapTransfer(state({ appliedMesh: 9, pointRevision: 100 })), null);
  // The mesh remains useful even when points are already current or use WebSocket.
  assert.equal(chooseMapTransfer(state({ appliedPoints: 12, pointsTransport: 'ws' })), 'mesh');
});

test('point views use only HTTP point replacements and their own revision', () => {
  for (const mode of [{ pointsOnly: true }, { view: '2d' }, { triangles: 0 }]) {
    assert.equal(chooseMapTransfer(state(mode)), 'points');
    assert.equal(chooseMapTransfer(state({ ...mode, appliedPoints: 12 })), null);
    assert.equal(chooseMapTransfer(state({ ...mode, pointsTransport: 'ws' })), null);
  }
});

test('five-second admission is shared across mesh/points when the view changes', () => {
  assert.equal(chooseMapTransfer(state({ now: 14999.999 })), null);
  assert.equal(chooseMapTransfer(state({ now: 15000 })), 'mesh');
  const started = 15000;
  assert.equal(chooseMapTransfer(state({ pointsOnly: true, lastStarted: started, now: 19999 })), null);
  assert.equal(chooseMapTransfer(state({ pointsOnly: true, lastStarted: started, now: 20000 })), 'points');
  assert.equal(chooseMapTransfer(state({ lastStarted: 20000, now: 24999 })), null);
  assert.equal(chooseMapTransfer(state({ lastStarted: 20000, now: 25000 })), 'mesh');
});

test('in-flight work and absent demand prevent admission in every view', () => {
  for (const mode of [{}, { pointsOnly: true }, { view: '2d' }, { triangles: 0 }]) {
    assert.equal(chooseMapTransfer(state({ ...mode, busy: true, now: 100000 })), null);
    assert.equal(chooseMapTransfer(state({ ...mode, needed: false, now: 100000 })), null);
  }
});

test('first admission and opaque revision transitions do not depend on frame counters', () => {
  assert.equal(chooseMapTransfer(state({ lastStarted: -Infinity, now: 0 })), 'mesh');
  assert.equal(chooseMapTransfer(state({ meshRevision: 'epoch2:1', appliedMesh: 'epoch1:99' })), 'mesh');
  assert.equal(chooseMapTransfer(state({ pointsOnly: true, pointRevision: 'epoch2:1', appliedPoints: 'epoch1:99' })), 'points');
  assert.equal(chooseMapTransfer(state({ meshRevision: 'same', appliedMesh: 'same' })), null);
});

test('a capped mesh alternates with HTTP points instead of standing in for them', () => {
  const capped = changes => state({ capped: true, ...changes });
  assert.equal(chooseMapTransfer(capped()), 'mesh');
  assert.equal(chooseMapTransfer(capped({ lastKind: 'mesh' })), 'points');
  assert.equal(chooseMapTransfer(capped({ lastKind: 'points' })), 'mesh');
  // Whichever is stale goes next; nothing when both are current.
  assert.equal(chooseMapTransfer(capped({ appliedMesh: 9 })), 'points');
  assert.equal(chooseMapTransfer(capped({ lastKind: 'mesh', appliedPoints: 12 })), 'mesh');
  assert.equal(chooseMapTransfer(capped({ appliedMesh: 9, appliedPoints: 12 })), null);
  assert.equal(chooseMapTransfer(capped({ lastKind: 'mesh', pointsTransport: 'ws' })), 'mesh');
  // Still one transfer per five seconds.
  assert.equal(chooseMapTransfer(capped({ lastKind: 'mesh', now: 14999 })), null);
});
