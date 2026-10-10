// All map snapshots are authoritative replacements, not append-only chunks.
/** @param {ArrayBuffer} buffer @param {number} offset @param {number | undefined} count */
export function decodePoints(buffer, offset = 4, count = undefined) {
  const view = new DataView(buffer);
  if (buffer.byteLength < offset) throw new Error("Truncated map packet");
  count ??= view.getUint32(0, true);
  if (count > 600000 || offset + count * 16 > buffer.byteLength)
    throw new Error("Invalid point count");
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const start = offset + i * 16;
    for (let axis = 0; axis < 3; axis++) {
      const value = view.getFloat32(start + axis * 4, true);
      if (!Number.isFinite(value)) throw new Error("Non-finite map coordinate");
      positions[i * 3 + axis] = value;
      const srgb = view.getUint8(start + 12 + axis) / 255;
      colors[i * 3 + axis] =
        srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
    }
  }
  return { positions, colors, count };
}

export function decodeMesh(buffer) {
  if (buffer.byteLength < 8) throw new Error("Truncated surface packet");
  const view = new DataView(buffer);
  const vertices = view.getUint32(0, true);
  const count = view.getUint32(4, true);
  const end = 8 + vertices * 16;
  if (count % 3 || count > 480000 || end + count * 4 !== buffer.byteLength)
    throw new Error("Invalid surface packet");
  const data = decodePoints(buffer, 8, vertices);
  const indices = new Uint32Array(count);
  for (let i = 0; i < count; i++) {
    indices[i] = view.getUint32(end + i * 4, true);
    if (indices[i] >= vertices) throw new Error("Surface index out of bounds");
  }
  return { ...data, indices };
}
