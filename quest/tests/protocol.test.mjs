import test from "node:test";
import assert from "node:assert/strict";
import { decodePoints, decodeMesh } from "../src/protocol.js";

test("little-endian positions, RGB linearization, and bounds", () => {
  const buffer = new ArrayBuffer(20);
  const view = new DataView(buffer);
  view.setUint32(0, 1, true);
  [-1.5, 2, -4].forEach((n, i) => view.setFloat32(4 + i * 4, n, true));
  [255, 0, 128].forEach((n, i) => view.setUint8(16 + i, n));
  const decoded = decodePoints(buffer);
  assert.deepEqual([...decoded.positions], [-1.5, 2, -4]);
  assert.equal(decoded.colors[0], 1);
  assert.equal(decoded.colors[1], 0);
  assert.ok(Math.abs(decoded.colors[2] - 0.21586) < 0.0001);
  assert.throws(() => decodePoints(buffer.slice(0, 19)));
  view.setFloat32(4, NaN, true);
  assert.throws(() => decodePoints(buffer));
});

test("empty mesh and malicious surface indices", () => {
  assert.equal(decodeMesh(new ArrayBuffer(8)).indices.length, 0);
  const buffer = new ArrayBuffer(8 + 16 + 12);
  const view = new DataView(buffer);
  view.setUint32(0, 1, true);
  view.setUint32(4, 3, true);
  view.setUint32(24, 1, true);
  assert.throws(() => decodeMesh(buffer), /out of bounds/);
});
