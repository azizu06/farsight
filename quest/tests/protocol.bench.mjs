// Usage: node tests/protocol.bench.mjs  (standalone; not run by `npm test`)
import { decodePoints, decodeMesh } from "../src/protocol.js";
import * as reference from "./fixtures/protocol-reference.js";

function packet(count) {
  const buffer = new ArrayBuffer(4 + count * 16);
  const view = new DataView(buffer);
  view.setUint32(0, count, true);
  for (let i = 0; i < count; i++) {
    const s = 4 + i * 16;
    for (let a = 0; a < 3; a++) view.setFloat32(s + a * 4, Math.sin(i * (a + 1)) * 10, true);
    for (let a = 0; a < 3; a++) view.setUint8(s + 12 + a, (i * 31 + a * 97) & 255);
  }
  return buffer;
}
function mesh(vertices, tris) {
  const end = 8 + vertices * 16;
  const buffer = new ArrayBuffer(end + tris * 12);
  new Uint8Array(buffer).set(new Uint8Array(packet(vertices)).subarray(4), 8);
  const view = new DataView(buffer);
  view.setUint32(0, vertices, true);
  view.setUint32(4, tris * 3, true);
  for (let i = 0; i < tris * 3; i++) view.setUint32(end + i * 4, i % vertices, true);
  return buffer;
}
function median(fn, runs = 31) {
  for (let i = 0; i < 5; i++) fn();
  const t = [];
  for (let i = 0; i < runs; i++) {
    const s = performance.now();
    fn();
    t.push(performance.now() - s);
  }
  return t.sort((a, b) => a - b)[runs >> 1];
}
console.log(`node ${process.version} ${process.platform}/${process.arch}`);
const cases = [
  ...[50000, 80000, 150000, 600000].map((n) => [`points ${n}`, packet(n), "p"]),
  ["mesh 80k verts / 120k tris", mesh(80000, 120000), "m"],
];
for (const [name, buf, kind] of cases) {
  const a = kind === "p" ? reference.decodePoints : reference.decodeMesh;
  const b = kind === "p" ? decodePoints : decodeMesh;
  const before = median(() => a(buf));
  const after = median(() => b(buf));
  console.log(`${name}: reference ${before.toFixed(2)} ms, current ${after.toFixed(2)} ms (${(before / after).toFixed(2)}x)`);
}
