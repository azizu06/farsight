import assert from 'node:assert/strict';
import test from 'node:test';
import { chooseMapTransfer } from '../src/map-transfer.js';

const state = (changes = {}) => ({
  needed: true, busy: false, now: 15000, lastStarted: 10000,
  pointsOnly: false, view: '3d', triangles: 200,
  pointsTransport: 'http', pointRevision: 12, meshRevision: 9,
  appliedPoints: 11, appliedMesh: 8,
  pointsDelta: false, lastDelta: -Infinity, ...changes,
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

test('delta lane merges voxel cells between authority pulls', () => {
  // 1 s inside the 2 s authority window, 1 s since the last delta -> merge.
  const delta = changes => state({ pointsDelta: true, lastStarted: 14000, lastDelta: 14000, ...changes });
  assert.equal(chooseMapTransfer(delta()), 'pointsDelta');
  // Deltas need a real watermark and a newer server revision.
  assert.equal(chooseMapTransfer(delta({ appliedPoints: 12 })), null);
  assert.equal(chooseMapTransfer(delta({ appliedPoints: 12, pointRevision: 12 })), null);
  // appliedPoints -1 means the next transfer is always a full pull, so the
  // keyed slot map can be rebuilt before the fast lane runs.
  assert.equal(chooseMapTransfer(delta({ appliedPoints: -1 })), 'mesh');
  assert.equal(chooseMapTransfer(delta({ appliedPoints: -1, triangles: 0 })), 'points');
  // Pacing: requests stay under ~4 Hz even while revisions stream in.
  assert.equal(chooseMapTransfer(delta({ lastDelta: 14900 })), null);
  assert.equal(chooseMapTransfer(delta({ lastDelta: 14700 })), 'pointsDelta');
  // No flag -> the fallback path never picks the fast lane.
  assert.equal(chooseMapTransfer(state({ lastStarted: 14000, lastDelta: 14000 })), null);
});

test('delta servers run two-second authority pulls that alternate mesh/points', () => {
  const d = changes => state({ pointsDelta: true, lastStarted: 12000, ...changes });
  // 3 s > 2 s authority window -> a full pull preempts the delta lane.
  assert.equal(chooseMapTransfer(d()), 'mesh');
  assert.equal(chooseMapTransfer(d({ lastKind: 'mesh' })), 'points');
  assert.equal(chooseMapTransfer(d({ lastKind: 'points' })), 'mesh');
  // Revalidation ignores revision equality: the ETag answers 304s for ~100 B
  // and the pulls are what age server-side evictions out of the merged cloud.
  assert.equal(chooseMapTransfer(d({ appliedMesh: 9, appliedPoints: 12 })), 'mesh');
  assert.equal(chooseMapTransfer(d({ appliedMesh: 9, appliedPoints: 12, lastKind: 'mesh' })), 'points');
  // Boundary: due at exactly 2 s, inside the window the delta runs instead.
  assert.equal(chooseMapTransfer(d({ lastStarted: 13000 })), 'mesh');
  assert.equal(chooseMapTransfer(d({ lastStarted: 13001 })), 'pointsDelta');
  // The alternation is shared with the capped case.
  assert.equal(chooseMapTransfer(d({ capped: true })), 'mesh');
  assert.equal(chooseMapTransfer(d({ capped: true, lastKind: 'mesh' })), 'points');
});

test('delta-capable points-only views revalidate points and merge in between', () => {
  const d = changes => state({ pointsDelta: true, pointsOnly: true, ...changes });
  assert.equal(chooseMapTransfer(d({ appliedPoints: -1 })), 'points');
  assert.equal(chooseMapTransfer(d({ lastStarted: 13000 })), 'points');
  assert.equal(chooseMapTransfer(d({ lastStarted: 13001 })), 'pointsDelta');
  assert.equal(chooseMapTransfer(d({ lastStarted: 13001, appliedPoints: 12 })), null);
});
