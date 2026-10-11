import assert from 'node:assert/strict';
import { test } from 'node:test';
import { sceneAnchor } from '../mac_render/scene-anchor.mjs';
test('empty geometry has no invented scene; coarse geometry supplies the anchor', () => {
  assert.equal(sceneAnchor([], undefined, 0), null);
  assert.deepEqual(sceneAnchor([], new Float32Array([-2, -1, -4, 2, 3, 0]), 2),
    { floor_y: -1, center: [0, 1, -2], span: 4 });
});
test('dense chunks supersede the bounded coarse snapshot and unused buffer capacity', () => {
  const chunks = [{ positions: new Float32Array([-4, -2, 0, 4, 2, 6, 100, 100, 100]), count: 2 },
    { positions: new Float32Array([0, -2, -6]), count: 1 }];
  assert.deepEqual(sceneAnchor(chunks, new Float32Array([100, 100, 100]), 1),
    { floor_y: -2, center: [0, 0, 0], span: 12 });
});
test('sampling spans multiple chunks and visits at most the budget at large capacity', () => {
  let reads = 0;
  const positions = new Proxy([], { get(_target, key) { reads++; return Number(key) % 3 === 1 ? -1 : Number(key) / 3; } });
  const result = sceneAnchor([{ positions, count: 1_800_000 }, { positions, count: 200_000 }], [], 0);
  assert.equal(result.floor_y, -1); assert.ok(reads <= 4096 * 7);
  assert.ok(result.span > 1_700_000);
});
