import test from 'node:test';
import assert from 'node:assert/strict';
import { ReplayBadge } from '../src/replay-badge.js';

test('health flag labels replay in the headset and stays labelled on network failure', async () => {
  const badge = new ReplayBadge();
  assert.equal(badge.label('USB CAMERA'), '');
  await badge.refresh(async () => ({ ok: true, json: async () => ({ replay: true }) }));
  assert.equal(badge.label('USB CAMERA'), 'REPLAY · ');
  await badge.refresh(async () => { throw new Error('offline'); });
  assert.equal(badge.label('USB CAMERA'), 'REPLAY · ');
  await badge.refresh(async () => ({ ok: true, json: async () => ({ replay: false }) }));
  assert.equal(badge.label('USB CAMERA'), '');
  assert.equal(badge.label('REPLAY: SIMULATED'), 'REPLAY · ');
});
