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

import * as reference from "./fixtures/protocol-reference.js";

function randomPacket(count, offset, seed) {
  let s = seed;
  const rnd = () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 2 ** 32);
  const buffer = new ArrayBuffer(offset + count * 16);
  const view = new DataView(buffer);
  if (offset >= 4) view.setUint32(0, count, true);
  for (let i = 0; i < count; i++) {
    const start = offset + i * 16;
    for (let a = 0; a < 3; a++) view.setFloat32(start + a * 4, (rnd() - 0.5) * 2000, true);
    for (let a = 0; a < 3; a++) view.setUint8(start + 12 + a, Math.floor(rnd() * 256));
  }
  return buffer;
}

function sameBits(a, b) {
  assert.deepEqual(new Uint8Array(a.buffer), new Uint8Array(b.buffer));
}

test("decodePoints matches the reference bit-for-bit on randomized packets", () => {
  for (const [count, seed] of [[0, 1], [1, 2], [1000, 3], [50000, 4]]) {
    const buffer = randomPacket(count, 4, seed);
    const got = decodePoints(buffer);
    const want = reference.decodePoints(buffer);
    assert.equal(got.count, want.count);
    sameBits(got.positions, want.positions);
    sameBits(got.colors, want.colors);
  }
});

test("every byte value linearizes identically to the reference", () => {
  const buffer = new ArrayBuffer(4 + 256 * 16);
  const view = new DataView(buffer);
  view.setUint32(0, 256, true);
  for (let i = 0; i < 256; i++) for (let a = 0; a < 3; a++) view.setUint8(4 + i * 16 + 12 + a, (i + a * 85) & 255);
  sameBits(decodePoints(buffer).colors, reference.decodePoints(buffer).colors);
});

test("unaligned offset takes the DataView path with identical output", () => {
  const buffer = randomPacket(500, 5, 9);
  const got = decodePoints(buffer, 5, 500);
  const want = reference.decodePoints(buffer, 5, 500);
  sameBits(got.positions, want.positions);
  sameBits(got.colors, want.colors);
});

test("rejects nonfinite coordinates on any axis, bad counts, truncation", () => {
  for (const bad of [NaN, Infinity, -Infinity]) {
    for (let axis = 0; axis < 3; axis++) {
      const buffer = randomPacket(100, 4, 5);
      new DataView(buffer).setFloat32(4 + 77 * 16 + axis * 4, bad, true);
      assert.throws(() => decodePoints(buffer), /Non-finite/);
      assert.throws(() => reference.decodePoints(buffer), /Non-finite/);
    }
  }
  const big = new ArrayBuffer(8);
  new DataView(big).setUint32(0, 600001, true);
  assert.throws(() => decodePoints(big), /Invalid point count/);
  const short = randomPacket(10, 4, 6);
  assert.throws(() => decodePoints(short.slice(0, short.byteLength - 1)), /Invalid point count/);
  assert.throws(() => decodePoints(new ArrayBuffer(3)), /Truncated/);
});

test("decodeMesh matches the reference and still validates", () => {
  const verts = 200;
  const tris = 150;
  const end = 8 + verts * 16;
  const buffer = new ArrayBuffer(end + tris * 3 * 4);
  const pts = randomPacket(verts, 8, 11);
  new Uint8Array(buffer).set(new Uint8Array(pts));
  const view = new DataView(buffer);
  view.setUint32(0, verts, true);
  view.setUint32(4, tris * 3, true);
  for (let i = 0; i < tris * 3; i++) view.setUint32(end + i * 4, (i * 7) % verts, true);
  const got = decodeMesh(buffer);
  const want = reference.decodeMesh(buffer);
  sameBits(got.positions, want.positions);
  sameBits(got.colors, want.colors);
  assert.deepEqual([...got.indices], [...want.indices]);
  assert.throws(() => decodeMesh(buffer.slice(0, buffer.byteLength - 4)), /Invalid surface packet/);
  view.setUint32(end, verts, true);
  assert.throws(() => decodeMesh(buffer), /out of bounds/);
});
