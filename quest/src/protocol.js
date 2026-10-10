// All map snapshots are authoritative replacements, not append-only chunks.
// sRGB byte -> linear Float32, same formula as before; Float32Array storage
// rounds exactly like the per-point store did.
const SRGB_TO_LINEAR = new Float32Array(256);
for (let i = 0; i < 256; i++) {
  const srgb = i / 255;
  SRGB_TO_LINEAR[i] =
    srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
}
const LITTLE_ENDIAN = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;

/** @param {ArrayBuffer} buffer @param {number} offset @param {number | undefined} count */
export function decodePoints(buffer, offset = 4, count = undefined) {
  const view = new DataView(buffer);
  if (buffer.byteLength < offset) throw new Error("Truncated map packet");
  count ??= view.getUint32(0, true);
  if (count > 600000 || offset + count * 16 > buffer.byteLength)
    throw new Error("Invalid point count");
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  // Typed views need 4-byte alignment and a little-endian host; the wire
  // format is little-endian float32, so otherwise fall back to DataView.
  if (LITTLE_ENDIAN && offset % 4 === 0) {
    const floats = new Float32Array(buffer, offset, count * 4);
    const bytes = new Uint8Array(buffer, offset, count * 16);
    for (let i = 0, f = 0, b = 12, o = 0; i < count; i++, f += 4, b += 16, o += 3) {
      const x = floats[f];
      const y = floats[f + 1];
      const z = floats[f + 2];
      // Sum of three float32 values cannot overflow a double, so it is
      // finite exactly when all three are.
      if (!Number.isFinite(x + y + z)) throw new Error("Non-finite map coordinate");
      positions[o] = x;
      positions[o + 1] = y;
      positions[o + 2] = z;
      colors[o] = SRGB_TO_LINEAR[bytes[b]];
      colors[o + 1] = SRGB_TO_LINEAR[bytes[b + 1]];
      colors[o + 2] = SRGB_TO_LINEAR[bytes[b + 2]];
    }
    return { positions, colors, count };
  }
  for (let i = 0; i < count; i++) {
    const start = offset + i * 16;
    for (let axis = 0; axis < 3; axis++) {
      const value = view.getFloat32(start + axis * 4, true);
      if (!Number.isFinite(value)) throw new Error("Non-finite map coordinate");
      positions[i * 3 + axis] = value;
      colors[i * 3 + axis] = SRGB_TO_LINEAR[view.getUint8(start + 12 + axis)];
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
  // `end` is a multiple of 4, so a typed view is aligned on little-endian hosts.
  const raw = LITTLE_ENDIAN ? new Uint32Array(buffer, end, count) : null;
  for (let i = 0; i < count; i++) {
    indices[i] = raw ? raw[i] : view.getUint32(end + i * 4, true);
    if (indices[i] >= vertices) throw new Error("Surface index out of bounds");
  }
  return { ...data, indices };
}
