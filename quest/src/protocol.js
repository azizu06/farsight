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

/** Voxel identity of one map record. The server guarantees a cell's mean
 * always stays inside its voxel, so floor(coord / voxel_m) recovers the key
 * a /map/points.delta record belongs to. */
export function voxelKey(x, y, z, voxelM) {
  return `${Math.floor(x / voxelM)},${Math.floor(y / voxelM)},${Math.floor(z / voxelM)}`;
}

/**
 * Persistent merge target for one point cloud: a single growing backing
 * store keyed by voxel so additive deltas overwrite their cell's slot in
 * place and unseen cells append. Full snapshots stay authoritative — they
 * rebuild the slot map and bounds exactly (server-side evictions included).
 * Geometry-agnostic: callers hand sync() a THREE-style BufferGeometry plus an
 * attribute factory so this module never imports three.
 */
export class KeyedCloudMerger {
  constructor() {
    this.clear();
  }
  clear() {
    this.slots = new Map();
    this.used = 0;
    this.capacity = 0;
    this.positions = new Float32Array(0);
    this.colors = new Float32Array(0);
    // [minX, minY, minZ, maxX, maxY, maxZ] or null when empty. Additive merges
    // can only grow bounds, so they stay exact without a rescan.
    this.bounds = null;
    this._dirtyFrom = Infinity;
    this._dirtyTo = -1;
  }
  _grow(records) {
    if (records <= this.capacity) return;
    this.capacity = Math.max(records + (records >> 1), this.capacity + (this.capacity >> 1), 1024);
    const positions = new Float32Array(this.capacity * 3);
    const colors = new Float32Array(this.capacity * 3);
    positions.set(this.positions.subarray(0, this.used * 3));
    colors.set(this.colors.subarray(0, this.used * 3));
    this.positions = positions;
    this.colors = colors;
  }
  _extendBounds(x, y, z) {
    const b = (this.bounds ??= [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity]);
    if (x < b[0]) b[0] = x;
    if (x > b[3]) b[3] = x;
    if (y < b[1]) b[1] = y;
    if (y > b[4]) b[4] = y;
    if (z < b[2]) b[2] = z;
    if (z > b[5]) b[5] = z;
  }
  _touched(slot) {
    const element = slot * 3;
    if (element < this._dirtyFrom) this._dirtyFrom = element;
    if (element + 3 > this._dirtyTo) this._dirtyTo = element + 3;
  }
  /** Authoritative replacement; rebuilds slots and bounds. Returns the count. */
  applyFull(data, voxelM = 0) {
    const count = data.count;
    this._grow(count);
    this.positions.set(data.positions.subarray(0, count * 3));
    this.colors.set(data.colors.subarray(0, count * 3));
    this.used = count;
    this.slots.clear();
    this.bounds = null;
    const keyed = voxelM > 0;
    for (let i = 0; i < count; i++) {
      const o = i * 3;
      const x = data.positions[o], y = data.positions[o + 1], z = data.positions[o + 2];
      this._extendBounds(x, y, z);
      if (keyed) this.slots.set(voxelKey(x, y, z, voxelM), i);
    }
    this._dirtyFrom = 0;
    this._dirtyTo = count * 3;
    return count;
  }
  /** Additive merge: overwrite the keyed slot or append a new one.
   * Returns how many records were applied. */
  applyDelta(data, voxelM) {
    if (!(voxelM > 0)) return 0;
    const positions = data.positions, colors = data.colors;
    for (let i = 0; i < data.count; i++) {
      const o = i * 3;
      const x = positions[o], y = positions[o + 1], z = positions[o + 2];
      const key = voxelKey(x, y, z, voxelM);
      let slot = this.slots.get(key);
      if (slot === undefined) {
        this._grow(this.used + 1);
        slot = this.used++;
        this.slots.set(key, slot);
      }
      const d = slot * 3;
      this.positions[d] = x;
      this.positions[d + 1] = y;
      this.positions[d + 2] = z;
      this.colors[d] = colors[o];
      this.colors[d + 1] = colors[o + 1];
      this.colors[d + 2] = colors[o + 2];
      this._extendBounds(x, y, z);
      this._touched(slot);
    }
    return data.count;
  }
  /** xyz view of the live prefix, for stats like the walk-mode floor. */
  usedPositions() {
    return this.positions.subarray(0, this.used * 3);
  }
  /** Push the changed span into the geometry's position/color attributes.
   * Attributes are recreated only when the backing store grew; otherwise the
   * touched element range is marked for a partial GPU upload. drawRange is
   * set to the used prefix. */
  sync(geometry, makeAttribute) {
    const dirty = this._dirtyTo >= this._dirtyFrom;
    for (const [name, array] of [["position", this.positions], ["color", this.colors]]) {
      let attribute = geometry.getAttribute(name);
      if (attribute?.array !== array) {
        attribute = makeAttribute(array, 3);
        if (attribute.clearUpdateRanges) {
          attribute.clearUpdateRanges();
          attribute.addUpdateRange(0, this.used * 3);
        }
        geometry.setAttribute(name, attribute);
        attribute.needsUpdate = true;
      } else if (dirty) {
        attribute.clearUpdateRanges();
        attribute.addUpdateRange(this._dirtyFrom, this._dirtyTo - this._dirtyFrom);
        attribute.needsUpdate = true;
      }
    }
    geometry.setDrawRange(0, this.used);
    this._dirtyFrom = Infinity;
    this._dirtyTo = -1;
  }
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
  // Triangle soup: each vertex carries one sampled color, so coincident
  // corners across triangles leave hard seams (the blocky patches seen in
  // live review). Blend same-position vertex colors at decode time — the
  // wire format is unchanged.
  const { positions, colors } = data;
  const acc = new Map();
  const keyOf = k =>
    (Math.round(positions[k] * 512) + 8192) * 268435456 +
    (Math.round(positions[k + 1] * 512) + 8192) * 16384 +
    Math.round(positions[k + 2] * 512) + 8192;
  for (let i = 0; i < vertices; i++) {
    const k = i * 3, key = keyOf(k), e = acc.get(key);
    if (e) { e[0] += colors[k]; e[1] += colors[k + 1]; e[2] += colors[k + 2]; e[3]++; }
    else acc.set(key, [colors[k], colors[k + 1], colors[k + 2], 1]);
  }
  for (let i = 0; i < vertices; i++) {
    const k = i * 3, e = acc.get(keyOf(k));
    colors[k] = e[0] / e[3];
    colors[k + 1] = e[1] / e[3];
    colors[k + 2] = e[2] / e[3];
  }
  const indices = new Uint32Array(count);
  // `end` is a multiple of 4, so a typed view is aligned on little-endian hosts.
  const raw = LITTLE_ENDIAN ? new Uint32Array(buffer, end, count) : null;
  for (let i = 0; i < count; i++) {
    indices[i] = raw ? raw[i] : view.getUint32(end + i * 4, true);
    if (indices[i] >= vertices) throw new Error("Surface index out of bounds");
  }
  return { ...data, indices };
}
