// Turn the rover's dots into a surface with the team's builder (tools/mac_render, PR #201).
import { readFileSync, writeFileSync } from "node:fs";
import { buildMeasuredSurface } from "../mac_render/measured-surface.worker.mjs";
function load(file, offset, stride) {
  const b = readFileSync(file), n = (b.length - offset) / stride | 0;
  const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) { const o = offset + i * stride;
    pos.set([b.readFloatLE(o), b.readFloatLE(o + 4), b.readFloatLE(o + 8)], i * 3);
    col.set([b[o + 12] / 255, b[o + 13] / 255, b[o + 14] / 255], i * 3); }
  return [pos, col, n];
}
function pack(r) { // u32 nv, u32 ni, f32 pos*3, u8 rgb*3 (padded), u32 idx
  const m = r.mesh, nv = m.vertices, ni = m.indices.length;
  const buf = Buffer.alloc(8 + nv * 16 + ni * 4); buf.writeUInt32LE(nv, 0); buf.writeUInt32LE(ni, 4);
  for (let i = 0; i < nv; i++) { const o = 8 + i * 16;
    buf.writeFloatLE(m.positions[i * 3], o); buf.writeFloatLE(m.positions[i * 3 + 1], o + 4); buf.writeFloatLE(m.positions[i * 3 + 2], o + 8);
    for (let k = 0; k < 3; k++) buf[o + 12 + k] = Math.round(Math.min(1, Math.max(0, m.colors[i * 3 + k])) * 255); }
  Buffer.from(m.indices.buffer).copy(buf, 8 + nv * 16);
  return buf;
}
const runs = [["dense.bin", 0, {}, "surf_dense"], ["points.bin", 4, { cell: 0.08, minPoints: 2, minNeighbors: 3 }, "surf_map8"],
              ["points.bin", 4, { cell: 0.12, minPoints: 3, minNeighbors: 4 }, "surf_map12"]];
for (const [file, off, opt, name] of runs) {
  const [p, c, n] = load(file, off, 16);
  const r = buildMeasuredSurface(p, c, n, opt);
  writeFileSync(name + ".bin", pack(r));
  console.log(name, JSON.stringify(r.stats));
}
