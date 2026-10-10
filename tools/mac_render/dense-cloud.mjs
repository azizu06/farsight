/** Full measured dense cloud for the Mac renderer. No LOD/point synthesis/server.
 * Integrate: receiver.update(now); gpu.flush(); gpu.beginFrame(); render(); gpu.endFrame().
 * Material/renderer are supplied by the owner. All requests are read-only GETs.
 */
export const DENSE_PAGE_RECORDS = 8192;
export const DENSE_CHUNK_CELLS = 8192;
export const DENSE_MAX_BODY = 4 + 65536 + DENSE_PAGE_RECORDS * 16;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const safeInteger = value => Number.isSafeInteger(value) && value >= 0;
const linear = new Float32Array(256);
for (let i = 0; i < 256; i++) {
  const value = i / 255;
  linear[i] = value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
}
const cellKey = (x, y, z) => `${Math.floor(x / 10)}_${Math.floor(y / 10)}_${Math.floor(z / 10)}`;
function sourceIdentity(source, selected) {
  if (source === null) return { key: 'waiting', label: 'WAITING' };
  let session, epoch, kind;
  if (Array.isArray(source) && source.length === 2 && selected === 'rgbd') {
    [session, epoch] = source; kind = 'rgbd';
  } else if (source && typeof source === 'object' && !Array.isArray(source) &&
      source.kind === 'arkit_mesh_neutral' && selected === 'geometry') {
    ({ session, epoch } = source); kind = source.kind;
  } else throw new Error('Dense source kind does not match selected lane');
  if (typeof session !== 'string' || !UUID.test(session) || !safeInteger(epoch)) throw new Error('Invalid dense capture identity');
  return { key: `${kind}:${session.toLowerCase()}:${epoch}`,
    label: kind === 'rgbd' ? 'SOURCE RGB-D SAMPLES' : 'ARKIT MESH · NEUTRAL RGB' };
}
export function decodeDenseDelta(buffer, selected = 'rgbd') {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 4 || buffer.byteLength > DENSE_MAX_BODY)
    throw new Error('Dense page body exceeds bounds');
  const view = new DataView(buffer), headerSize = view.getUint32(0, true);
  if (!headerSize || headerSize > 65536 || headerSize + 4 > buffer.byteLength) throw new Error('Invalid dense header');
  const metadata = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buffer, 4, headerSize)));
  const offset = 4 + headerSize, count = (buffer.byteLength - offset) / 16;
  if (!metadata || metadata.format !== 'xyz_mm_rgb8_op' || typeof metadata.epoch !== 'string' ||
      !/^[0-9a-f]{32}$/i.test(metadata.epoch) || !Number.isInteger(count) || count > DENSE_PAGE_RECORDS ||
      !safeInteger(metadata.cursor) || !safeInteger(metadata.points) || typeof metadata.reset !== 'boolean' ||
      typeof metadata.snapshot !== 'boolean' || typeof metadata.more !== 'boolean' ||
      !(metadata.anchor === null || safeInteger(metadata.anchor)) || metadata.voxel_m !== .01 ||
      typeof metadata.ttl_s !== 'number' || !Number.isFinite(metadata.ttl_s) || metadata.ttl_s < 1 || metadata.ttl_s > 86400 ||
      (metadata.snapshot && (!metadata.more || metadata.anchor === null || metadata.cursor >= metadata.anchor)) ||
      (metadata.reset && metadata.anchor === null) || (count > 0 && metadata.cursor === 0) || (metadata.more && !count))
    throw new Error('Invalid dense cursor metadata');
  metadata.epoch = metadata.epoch.toLowerCase();
  const identity = sourceIdentity(metadata.source, selected);
  if (identity.key === 'waiting' && (count || metadata.points)) throw new Error('Unidentified dense samples');
  // Decode/validate the entire page before it can mutate retained state.
  const rows = new Int32Array(count * 7);
  for (let i = 0; i < count; i++) {
    const at = offset + i * 16, row = i * 7;
    for (let axis = 0; axis < 3; axis++) {
      const value = view.getInt32(at + axis * 4, true);
      if (Math.abs(value) >= 10_000_000) throw new Error('Dense coordinate exceeds bounds');
      rows[row + axis] = value;
    }
    for (let column = 3; column < 7; column++) rows[row + column] = view.getUint8(at + 9 + column);
    if (rows[row + 6] !== 0 && rows[row + 6] !== 1) throw new Error('Invalid dense operation');
  }
  return { metadata, rows, count, identity };
}

export class DenseCloudState {
  constructor({ capacity = 2_000_000, clock = () => performance.now() } = {}) {
    if (!Number.isInteger(capacity) || capacity < 1 || capacity > 5_000_000) throw new Error('Dense capacity must be 1..5000000');
    this.capacity = capacity; this.clock = clock;
    this.cells = new Map(); this.keys = []; this.chunks = []; this.dirty = new Set();
    this.resetVersion = 0; this.receivedUpserts = this.receivedDeletes = this.localEvictions = 0;
    this.ttlMs = 30000; this.lastGood = null;
  }
  get size() { return this.cells.size; }
  clear() {
    this.cells.clear(); this.keys.length = 0; this.chunks.length = 0; this.dirty.clear();
    this.resetVersion++; this.lastGood = null;
  }
  _chunk(slot) {
    const id = Math.floor(slot / DENSE_CHUNK_CELLS);
    let chunk = this.chunks[id];
    if (!chunk) this.chunks[id] = chunk = {
      positions: new Float32Array(DENSE_CHUNK_CELLS * 3), colors: new Float32Array(DENSE_CHUNK_CELLS * 3),
      dirty: new Uint8Array(DENSE_CHUNK_CELLS), count: 0,
    };
    return [id, chunk, slot % DENSE_CHUNK_CELLS];
  }
  _touch(slot) { const [id, chunk, local] = this._chunk(slot); chunk.dirty[local] = 1; this.dirty.add(id); }
  _remove(key) {
    const slot = this.cells.get(key);
    if (slot === undefined) return false;
    const last = this.keys.length - 1, lastKey = this.keys[last];
    if (slot !== last) {
      const [, from, fromLocal] = this._chunk(last), [, to, toLocal] = this._chunk(slot);
      for (const name of ['positions', 'colors']) to[name].set(from[name].subarray(fromLocal * 3, fromLocal * 3 + 3), toLocal * 3);
      this.keys[slot] = lastKey; this.cells.set(lastKey, slot); this._touch(slot);
    }
    this.keys.pop(); this.cells.delete(key);
    const [lastId, lastChunk] = this._chunk(last);
    lastChunk.count = last % DENSE_CHUNK_CELLS;
    if (!lastChunk.count) { this.chunks.pop(); this.dirty.delete(lastId); }
    else this.dirty.add(lastId); // draw range shrinks, deleted cells are never drawn
    return true;
  }
  apply(rows, now = this.clock()) {
    if (!(rows instanceof Int32Array) || rows.length % 7 || rows.length / 7 > DENSE_PAGE_RECORDS)
      throw new Error('Invalid decoded dense page');
    for (let row = 0; row < rows.length; row += 7) {
      const key = cellKey(rows[row], rows[row + 1], rows[row + 2]);
      if (!rows[row + 6]) { this.receivedDeletes++; this._remove(key); continue; }
      this.receivedUpserts++;
      let slot = this.cells.get(key);
      if (slot === undefined) {
        if (this.size >= this.capacity) { this._remove(this.cells.keys().next().value); this.localEvictions++; }
        slot = this.keys.length; this.keys.push(key);
      }
      this.cells.delete(key); this.cells.set(key, slot); // bounded LRU, not observations-as-points
      const [, chunk, local] = this._chunk(slot);
      for (let axis = 0; axis < 3; axis++) {
        chunk.positions[local * 3 + axis] = rows[row + axis] / 1000;
        chunk.colors[local * 3 + axis] = linear[rows[row + 3 + axis]];
      }
      chunk.count = Math.max(chunk.count, local + 1); this._touch(slot);
    }
    this.lastGood = now;
  }
  expire(now = this.clock()) {
    // Source authority delivers per-cell TTL tombstones. If the link stops
    // delivering altogether, retain nothing beyond that source's advertised TTL.
    if (this.lastGood !== null && now - this.lastGood >= this.ttlMs) this.clear();
  }
  metrics() {
    return { received_upserts: this.receivedUpserts, received_deletes: this.receivedDeletes,
      retained_cells: this.size, local_evictions: this.localEvictions, pending_gpu_chunks: this.dirty.size };
  }
}

export class DenseCloudReceiver {
  constructor({ state = new DenseCloudState(), endpoint = '/dense/delta.bin', source = 'rgbd',
      fetcher = globalThis.fetch, timeoutMs = 5000, clock = () => performance.now() } = {}) {
    if (!['rgbd', 'geometry'].includes(source) || !Number.isFinite(timeoutMs) || timeoutMs < 100 || timeoutMs > 30000)
      throw new Error('Invalid dense source or deadline');
    this.state = state; this.endpoint = endpoint; this.source = source; this.fetcher = fetcher;
    this.timeoutMs = timeoutMs; this.clock = clock; this.cursor = { after: 0 };
    this.epoch = this.identity = null; this.sourceLabel = 'WAITING'; this.sourcePoints = 0;
    this.busy = this.stopped = false; this.generation = 0; this.request = null;
    this.lastPull = -Infinity; this.more = false; this.error = null; this.bytes = this.pages = this.resets = 0;
  }
  reset() {
    this.generation++; this.request?.abort(); this.state.clear(); this.cursor = { after: 0 };
    this.epoch = this.identity = null; this.sourceLabel = 'WAITING'; this.sourcePoints = 0; this.more = false;
  }
  setSource(source) {
    if (!['rgbd', 'geometry'].includes(source)) throw new Error('Unknown dense lane');
    if (source !== this.source) { this.reset(); this.source = source; }
  }
  update(now = this.clock(), enabled = true) {
    this.state.expire(now);
    if (!enabled) { this.request?.abort(); return; }
    if (!this.stopped && !this.busy && now - this.lastPull >= (this.more ? 0 : 100)) void this.pollOnce();
  }
  accept(page) {
    const { metadata: meta, rows, count, identity } = page;
    const changed = this.epoch !== null && (meta.epoch !== this.epoch || identity.key !== this.identity);
    if ((this.epoch === null || changed) && !meta.reset) {
      this.reset(); this.error = 'Dense identity changed without reset'; throw new Error(this.error);
    }
    if (!meta.reset) {
      if (meta.cursor < this.cursor.after || (meta.more && meta.cursor <= this.cursor.after)) throw new Error('Dense cursor did not advance');
      if (this.cursor.snapshot === 'true' && meta.anchor !== this.cursor.anchor) throw new Error('Dense snapshot anchor changed');
      if (meta.snapshot && this.cursor.snapshot !== 'true') throw new Error('Unexpected dense snapshot continuation');
    }
    if (meta.reset) { this.state.clear(); this.resets++; }
    this.epoch = meta.epoch.toLowerCase(); this.identity = identity.key; this.sourceLabel = identity.label;
    this.sourcePoints = meta.points; this.state.ttlMs = meta.ttl_s * 1000;
    this.state.apply(rows, this.clock());
    this.cursor = { epoch: this.epoch, after: meta.cursor, snapshot: meta.snapshot ? 'true' : 'false' };
    if (meta.snapshot) this.cursor.anchor = meta.anchor;
    this.more = meta.more; this.pages++;
    return count;
  }
  async pollOnce() {
    if (this.busy || this.stopped) return false;
    this.busy = true; this.lastPull = this.clock();
    const version = this.generation, abort = new AbortController(); this.request = abort;
    const timer = setTimeout(() => abort.abort(), this.timeoutMs);
    let reader;
    try {
      const url = new URL(this.endpoint, globalThis.location?.origin || 'http://localhost');
      for (const [key, value] of Object.entries({ ...this.cursor, source: this.source, limit: DENSE_PAGE_RECORDS })) url.searchParams.set(key, String(value));
      const response = await this.fetcher.call(globalThis, url, { cache: 'no-store', signal: abort.signal, redirect: 'error' });
      if (!response.ok) throw new Error(`Dense HTTP ${response.status}`);
      reader = response.body.getReader(); const chunks = []; let length = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (abort.signal.aborted || version !== this.generation) return false;
        if (done) break;
        length += value.byteLength;
        if (length > DENSE_MAX_BODY) throw new Error('Dense response exceeds page limit');
        chunks.push(value);
      }
      const body = new Uint8Array(length); let at = 0;
      for (const part of chunks) { body.set(part, at); at += part.byteLength; }
      const page = decodeDenseDelta(body.buffer, this.source);
      if (abort.signal.aborted || version !== this.generation) return false;
      this.accept(page); this.bytes += length; this.error = null; return true;
    } catch (error) {
      if (!abort.signal.aborted && version === this.generation) this.error = String(error).slice(0, 160);
      return false;
    } finally {
      if (reader) await reader.cancel().catch(() => {});
      clearTimeout(timer); this.busy = false;
      if (this.request === abort) this.request = null;
    }
  }
  metrics() {
    return { ...this.state.metrics(), received_payload_bytes: this.bytes, received_pages: this.pages,
      source_reported_cells: this.sourcePoints, source_label: this.sourceLabel, selected_source: this.source,
      bootstrap_incomplete: this.cursor.snapshot === 'true', cursor: this.cursor.after,
      resets: this.resets, request_active: this.busy, error: this.error };
  }
  dispose() { this.stopped = true; this.reset(); }
}

function ranges(bits, count) {
  const result = [];
  for (let start = 0; start < count;) {
    if (!bits[start]) { start++; continue; }
    let end = start + 1; while (end < count && bits[end]) end++;
    result.push({ start: start * 3, count: (end - start) * 3 }); start = end;
  }
  return result;
}
export class DenseGpuChunks {
  constructor({ state, THREE, material, parent = null }) {
    if (!state || !THREE || !material) throw new Error('Dense GPU helper needs state, THREE and owner material');
    this.state = state; this.THREE = THREE; this.material = material; this.group = new THREE.Group();
    this.group.name = 'Full source dense cloud'; parent?.add(this.group);
    this.slots = new Map(); this.version = state.resetVersion; this.uploadedWrites = this.drawn = this.frameDrawn = 0;
  }
  _remove(id) {
    const slot = this.slots.get(id); if (!slot) return;
    this.group.remove(slot.points); slot.points.geometry.dispose(); this.slots.delete(id);
  }
  flush() {
    const { state, THREE } = this;
    if (this.version !== state.resetVersion) { for (const id of this.slots.keys()) this._remove(id); this.version = state.resetVersion; }
    for (const id of this.slots.keys()) if (id >= state.chunks.length) this._remove(id);
    for (const id of state.dirty) {
      const chunk = state.chunks[id]; if (!chunk) continue;
      let slot = this.slots.get(id);
      if (!slot) {
        const geometry = new THREE.BufferGeometry();
        for (const name of ['position', 'color']) geometry.setAttribute(name,
          new THREE.BufferAttribute(name === 'position' ? chunk.positions : chunk.colors, 3).setUsage(THREE.DynamicDrawUsage));
        const points = new THREE.Points(geometry, this.material);
        slot = { points, chunk, pending: new Uint8Array(DENSE_CHUNK_CELLS), uploaded: 0 };
        geometry.getAttribute('position').onUpload(() => {
          this.uploadedWrites += slot.pending.subarray(0, slot.chunk.count).reduce((sum, value) => sum + value, 0);
          slot.pending.fill(0); slot.uploaded = slot.chunk.count;
        });
        points.onAfterRender = () => { this.frameDrawn += points.geometry.drawRange.count; };
        this.group.add(points); this.slots.set(id, slot);
      }
      for (let i = 0; i < DENSE_CHUNK_CELLS; i++) if (chunk.dirty[i]) slot.pending[i] = 1;
      const updates = ranges(slot.pending, chunk.count);
      for (const name of ['position', 'color']) {
        const attribute = slot.points.geometry.getAttribute(name);
        attribute.clearUpdateRanges();
        for (const range of updates) attribute.addUpdateRange(range.start, range.count);
        if (updates.length) attribute.needsUpdate = true;
      }
      slot.points.geometry.setDrawRange(0, chunk.count);
      slot.points.geometry.boundingBox ??= new THREE.Box3();
      slot.points.geometry.boundingBox.setFromArray(chunk.positions.subarray(0, chunk.count * 3));
      slot.points.geometry.boundingSphere ??= new THREE.Sphere();
      slot.points.geometry.boundingBox.getBoundingSphere(slot.points.geometry.boundingSphere);
      slot.uploaded = Math.min(slot.uploaded, chunk.count);
      chunk.dirty.fill(0);
    }
    state.dirty.clear();
  }
  beginFrame() { this.frameDrawn = 0; }
  endFrame() { this.drawn = this.frameDrawn; }
  metrics() {
    return { gpu_uploaded_cells: [...this.slots.values()].reduce((n, slot) => n + slot.uploaded, 0),
      gpu_uploaded_cell_writes: this.uploadedWrites, gpu_drawn_cells: this.drawn, gpu_chunks: this.slots.size };
  }
  dispose() { for (const id of this.slots.keys()) this._remove(id); this.group.parent?.remove(this.group); }
}
