// Fine measured points have their own bounded decoder; legacy map caps stay intact.
export const DENSE_CHUNK_POINTS = 8192;
export const DENSE_VIEW_CHUNKS = 32;
const linear = new Float32Array(256);
for (let i = 0; i < 256; i++) {
  const c = i / 255;
  linear[i] = c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
}

/** @param {ArrayBuffer} buffer */
export function decodeDenseChunk(buffer) {
  if (buffer.byteLength < 4) throw new Error("Truncated dense chunk");
  const view = new DataView(buffer), headerSize = view.getUint32(0, true);
  if (headerSize < 1 || headerSize > 65536 || headerSize + 4 > buffer.byteLength)
    throw new Error("Invalid dense header");
  const metadata = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, headerSize)));
  const offset = headerSize + 4, count = (buffer.byteLength - offset) / 16;
  if (metadata.format !== "xyz_mm_rgb8_op" || !Number.isInteger(count) || count > DENSE_CHUNK_POINTS
      || metadata.points !== count || typeof metadata.epoch !== "string")
    throw new Error("Invalid dense point count or format");
  const positions = new Float32Array(count * 3), colors = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const at = offset + i * 16;
    if (view.getUint8(at + 15) !== 1) throw new Error("View chunks contain only measured upserts");
    for (let axis = 0; axis < 3; axis++) {
      const value = view.getInt32(at + axis * 4, true) / 1000;
      if (Math.abs(value) >= 10000) throw new Error("Dense coordinate exceeds map bounds");
      positions[i * 3 + axis] = value;
      colors[i * 3 + axis] = linear[view.getUint8(at + 12 + axis)];
    }
  }
  return { metadata, positions, colors, count };
}
