import test from "node:test";
import assert from "node:assert/strict";
import { decodeDenseChunk } from "../src/dense-protocol.js";

function packet(count = 1) {
  const header = new TextEncoder().encode(JSON.stringify({ format: "xyz_mm_rgb8_op", epoch: "test", points: count }));
  const buffer = new ArrayBuffer(4 + header.length + count * 16), view = new DataView(buffer);
  view.setUint32(0, header.length, true); new Uint8Array(buffer, 4, header.length).set(header);
  for (let i = 0; i < count; i++) {
    const at = 4 + header.length + i * 16;
    view.setInt32(at, -1234, true); view.setInt32(at + 4, 2000, true); view.setInt32(at + 8, -3000, true);
    view.setUint8(at + 12, 255); view.setUint8(at + 13, 128); view.setUint8(at + 15, 1);
  }
  return buffer;
}

test("dense measured millimetres and RGB survive decoding including negative coordinates", () => {
  const decoded = decodeDenseChunk(packet());
  assert.equal(decoded.count, 1);
  assert.ok(Math.abs(decoded.positions[0] + 1.234) < 1e-6);
  assert.deepEqual([...decoded.positions.slice(1)], [2, -3]);
  assert.ok(decoded.colors[1] > .21 && decoded.colors[1] < .22);
  assert.equal(decoded.colors[0], 1);
});

test("dense view decoder rejects truncation, oversize and deletion records", () => {
  assert.throws(() => decodeDenseChunk(packet().slice(0, -1)));
  assert.throws(() => decodeDenseChunk(packet(8193)));
  const body = packet(); new DataView(body).setUint8(body.byteLength - 1, 0);
  assert.throws(() => decodeDenseChunk(body));
});
