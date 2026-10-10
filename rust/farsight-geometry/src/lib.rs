//! Mac-side geometry assembly for the phone-constructs/Mac-assembles split.
//! Pulls incremental measured ARKit mesh-anchor updates from the rover phone,
//! combines keyed chunks, and serves the assembled geometry as the exact
//! `mesh.bin` wire format farsight-server serves — Quest decodes it unchanged.
//!
//! Record codec v1 (issue #24 contract): length-framed binary updates with
//! optional zlib DEFLATE payload; see `decode` for the byte layout.

use bytes::Bytes;
use indexmap::IndexMap;
use rustc_hash::FxBuildHasher;

/// One measured mesh chunk keyed by its producer anchor id. Same layout as
/// farsight-server's `phone::Mesh`.
#[derive(Clone)]
pub struct ChunkMesh {
    pub positions: Vec<[f32; 3]>,
    pub colors: Vec<[u8; 3]>,
    /// One entry per triangle: 3 vertex indices into this chunk's positions.
    pub indices: Vec<[u32; 3]>,
}

// --- v1 wire record ---------------------------------------------------------

pub const PROTOCOL_VERSION: u8 = 1;
/// Per-record byte cap on the geometry lane (board-agreed bound).
pub const MAX_UPDATE_BYTES: usize = 256 * 1024;
pub const MAX_VERTICES: usize = 64 * 1024;
pub const MAX_INDICES: usize = 64 * 1024;
/// Quest decodeMesh ceiling: 600k vertices. Board-agreed published bound.
pub const MAX_PUBLISHED_VERTICES: usize = 600 * 1024;
/// Quest decodeMesh ceiling: 480k scalar indices = 160k faces. The default
/// lane budget stays under it (100k faces) for headroom.
pub const MAX_PUBLISHED_TRIANGLES: usize = 160 * 1024;
/// Bound on anchors buffered between Reset and SnapshotEnd.
pub const MAX_PENDING_ANCHORS: usize = 4096;
/// Bounded delta journal for /geometry/chunks — one entry per published op
/// (or budget eviction tombstone); consumers replay from seq.
pub const JOURNAL_CAP: usize = 8192;
/// Identity 4x4 — delta records carry it because positions are already
/// world-baked at ingest (bake-once contract, never re-apply).
const IDENTITY: [f32; 16] = [
    1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0,
];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Op {
    Replace,
    Remove,
    Reset,
    SnapshotEnd,
}

/// One decoded lane update. `session`/`epoch` identify the capture session;
/// `seq` is the producer's monotonically increasing sequence.
pub struct Update {
    pub session: [u8; 16],
    pub epoch: u64,
    pub seq: u64,
    pub op: Op,
    pub anchor: [u8; 16],
    /// Column-major ARKit anchor transform (row-major storage here mirrors
    /// the decoder layout only; assembler applies it at ingest).
    pub transform: [f32; 16],
    pub mesh: Option<ChunkMesh>,
}

/// Record payload compression. Flag bits 0-1 are an enum, not bit flags:
/// 0=raw, 1=zlib-deflate, 2=iOS-native Brotli; 3 and any other flag bit
/// are rejected (board-agreed after Codex adopted this header).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum RecordCoding {
    Raw,
    Zlib,
    Brotli,
}

/// Decode one v1 record. Layout (little-endian):
///   u8 version | u8 op | u16 flags (bits0-1: payload coding enum)
///   u64 seq | u64 epoch | 16B session | 16B anchor
///   f32 transform[16] | u32 vertex_count | u32 index_count
///   payload: vertices f32 xyz | indices u32 (compressed per flags)
/// Padding is not allowed — trailing bytes make the record malformed.
pub fn decode(packet: &[u8]) -> Result<Update, String> {
    const HEAD: usize = 1 + 1 + 2 + 8 + 8 + 16 + 16 + 16 * 4 + 4 + 4;
    if packet.is_empty() || packet.len() > MAX_UPDATE_BYTES {
        return Err("update exceeds the 256 KiB lane bound or is empty".into());
    }
    if packet.len() < HEAD {
        return Err("update header truncated".into());
    }
    let mut off = 0usize;
    let mut take = |n: usize| -> &[u8] { let s = &packet[off..off + n]; off += n; s };
    let version = take(1)[0];
    if version != PROTOCOL_VERSION {
        return Err(format!("unsupported geometry protocol version {version}"));
    }
    let op = match take(1)[0] {
        0 => Op::Replace,
        1 => Op::Remove,
        2 => Op::Reset,
        3 => Op::SnapshotEnd,
        other => return Err(format!("unknown geometry op {other}")),
    };
    let flags = u16::from_le_bytes(take(2).try_into().unwrap());
    let coding = match flags & 3 {
        0 => RecordCoding::Raw,
        1 => RecordCoding::Zlib,
        2 => RecordCoding::Brotli,
        _ => return Err(format!("unknown geometry payload coding in flags {flags:#x}")),
    };
    if flags & !3 != 0 {
        return Err(format!("unknown geometry flags {flags:#x}"));
    }
    let seq = u64::from_le_bytes(take(8).try_into().unwrap());
    let epoch = u64::from_le_bytes(take(8).try_into().unwrap());
    let session: [u8; 16] = take(16).try_into().unwrap();
    let anchor: [u8; 16] = take(16).try_into().unwrap();
    let mut transform = [0f32; 16];
    for (i, b) in take(64).chunks_exact(4).enumerate() {
        transform[i] = f32::from_le_bytes(b.try_into().unwrap());
    }
    let vcount = u32::from_le_bytes(take(4).try_into().unwrap()) as usize;
    let icount = u32::from_le_bytes(take(4).try_into().unwrap()) as usize;
    if vcount > MAX_VERTICES || icount > MAX_INDICES {
        return Err("update exceeds per-chunk vertex/index bounds".into());
    }
    if !transform.iter().all(|v| v.is_finite()) {
        return Err("non-finite anchor transform".into());
    }
    if op == Op::Replace && (vcount == 0 || icount == 0 || !icount.is_multiple_of(3)) {
        return Err("replace needs a non-empty triangle mesh".into());
    }
    if op != Op::Replace && (vcount != 0 || icount != 0) {
        return Err("non-replace op must carry an empty payload".into());
    }
    // Decompress bounded at declared size + 1 — a malformed compressed body
    // can never allocate beyond the declared geometry (Codex receiver fix).
    let want = vcount * 12 + icount * 4;
    let payload = packet[off..].to_vec();
    let payload = match coding {
        RecordCoding::Raw => payload,
        RecordCoding::Zlib => {
            use std::io::Read;
            let mut out = Vec::with_capacity(want);
            flate2::read::ZlibDecoder::new(&payload[..])
                .take((want + 1) as u64)
                .read_to_end(&mut out)
                .map_err(|e| format!("deflate payload invalid: {e}"))?;
            out
        }
        RecordCoding::Brotli => {
            use std::io::Read;
            let mut out = Vec::with_capacity(want);
            brotli::Decompressor::new(&payload[..], 4096)
                .take((want + 1) as u64)
                .read_to_end(&mut out)
                .map_err(|e| format!("brotli payload invalid: {e}"))?;
            out
        }
    };
    if payload.len() != want {
        return Err(format!("payload length {} != declared {want}", payload.len()));
    }
    let mesh = if op == Op::Replace {
        let mut positions = Vec::with_capacity(vcount);
        let mut colors = Vec::with_capacity(vcount);
        let mut indices = Vec::with_capacity(icount / 3);
        for b in payload[..vcount * 12].chunks_exact(12) {
            let v = [
                f32::from_le_bytes(b[0..4].try_into().unwrap()),
                f32::from_le_bytes(b[4..8].try_into().unwrap()),
                f32::from_le_bytes(b[8..12].try_into().unwrap()),
            ];
            if !v.iter().all(|x| x.is_finite()) {
                return Err("non-finite vertex".into());
            }
            positions.push(v);
            // v1 carries no color: neutral grey until a calibrated field lands.
            colors.push([160, 160, 160]);
        }
        let idx = &payload[vcount * 12..];
        for t in idx.chunks_exact(12) {
            let a = u32::from_le_bytes(t[0..4].try_into().unwrap());
            let b = u32::from_le_bytes(t[4..8].try_into().unwrap());
            let c = u32::from_le_bytes(t[8..12].try_into().unwrap());
            if a as usize >= vcount || b as usize >= vcount || c as usize >= vcount {
                return Err("triangle index out of range".into());
            }
            indices.push([a, b, c]);
        }
        Some(ChunkMesh { positions, colors, indices })
    } else {
        None
    };
    Ok(Update { session, epoch, seq, op, anchor, transform, mesh })
}

/// Encode one v1 record (test + forwarder use; the phone is the real producer).
pub fn encode(update: &Update, coding: RecordCoding) -> Vec<u8> {
    let (vcount, icount) = update
        .mesh
        .as_ref()
        .map(|m| (m.positions.len(), m.indices.len() * 3))
        .unwrap_or((0, 0));
    let mut payload = Vec::with_capacity(vcount * 12 + icount * 4);
    if let Some(m) = &update.mesh {
        for p in &m.positions {
            for v in p {
                payload.extend_from_slice(&v.to_le_bytes());
            }
        }
        for t in &m.indices {
            for i in t {
                payload.extend_from_slice(&i.to_le_bytes());
            }
        }
    }
    let payload = match coding {
        RecordCoding::Raw => payload,
        RecordCoding::Zlib => {
            use std::io::Write;
            let mut e = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::new(1));
            e.write_all(&payload).unwrap();
            e.finish().unwrap()
        }
        RecordCoding::Brotli => {
            // Apple COMPRESSION_BROTLI encodes at fixed level 2; the decoder
            // accepts any level, so match the phone's emitted profile.
            let mut out = Vec::new();
            {
                let mut w = brotli::CompressorWriter::new(&mut out, 4096, 2, 22);
                use std::io::Write;
                w.write_all(&payload).unwrap();
            }
            out
        }
    };
    let mut out = Vec::with_capacity(124 + payload.len());
    out.push(PROTOCOL_VERSION);
    out.push(match update.op {
        Op::Replace => 0,
        Op::Remove => 1,
        Op::Reset => 2,
        Op::SnapshotEnd => 3,
    });
    out.extend_from_slice(&(match coding { RecordCoding::Raw => 0u16, RecordCoding::Zlib => 1, RecordCoding::Brotli => 2 }).to_le_bytes());
    out.extend_from_slice(&update.seq.to_le_bytes());
    out.extend_from_slice(&update.epoch.to_le_bytes());
    out.extend_from_slice(&update.session);
    out.extend_from_slice(&update.anchor);
    for v in &update.transform {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.extend_from_slice(&(vcount as u32).to_le_bytes());
    out.extend_from_slice(&(icount as u32).to_le_bytes());
    out.extend_from_slice(&payload);
    out
}

// --- combiner ---------------------------------------------------------------

/// Bounded map of the newest chunk per anchor, evicting the least recently
/// written anchor once the triangle budget is exceeded. Also enforces the
/// lane semantics: atomic session/epoch reset, monotonic seq, and no publish
/// between Reset and SnapshotEnd.
pub struct AnchorGeometry {
    budget: usize,
    chunks: IndexMap<[u8; 16], ChunkMesh, FxBuildHasher>,
    count: usize,
    verts: usize,
    revision: u64,
    /// True only after an accepted SnapshotEnd publishes a staged snapshot;
    /// false on reset/reconnect staging so consumers never mistake a partial
    /// or cleared map for a complete one (tools/qualify_geometry.py gate).
    snapshot_complete: bool,
    session: [u8; 16],
    epoch: u64,
    last_seq: u64,
    /// Buffered ops between Reset and SnapshotEnd — published atomically.
    /// Bounded so a hostile/buggy snapshot can't grow unboundedly. Each
    /// staged chunk keeps its producer seq for the delta journal.
    pending: Option<IndexMap<[u8; 16], (u64, ChunkMesh), FxBuildHasher>>,
    pending_tri: usize,
    pending_vert: usize,
    /// Bounded record of published ops for /geometry/chunks delta replay:
    /// (producer seq, anchor, op). Cleared on session/epoch change.
    journal: std::collections::VecDeque<(u64, [u8; 16], Op)>,
    /// Revision seen by consumers (bumps only on publishable changes).
    pub_lane: LaneStats,
}

#[derive(Clone, Copy, Default)]
pub struct LaneStats {
    pub applied: u64,
    pub dropped_stale: u64,
    pub dropped_malformed: u64,
    pub reconnects: u64,
}

impl AnchorGeometry {
    pub fn new(triangle_budget: usize) -> Self {
        Self {
            budget: triangle_budget,
            chunks: IndexMap::with_hasher(FxBuildHasher),
            count: 0,
            verts: 0,
            revision: 0,
            snapshot_complete: false,
            session: [0; 16],
            epoch: 0,
            last_seq: 0,
            pending: None,
            pending_tri: 0,
            pending_vert: 0,
            journal: std::collections::VecDeque::new(),
            pub_lane: LaneStats::default(),
        }
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn snapshot_complete(&self) -> bool {
        self.snapshot_complete
    }

    pub fn triangles(&self) -> usize {
        self.count
    }

    pub fn vertices(&self) -> usize {
        self.verts
    }

    pub fn anchors(&self) -> usize {
        self.chunks.len()
    }

    pub fn session(&self) -> [u8; 16] {
        self.session
    }

    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    pub fn last_seq(&self) -> u64 {
        self.last_seq
    }

    pub fn lane(&self) -> LaneStats {
        self.pub_lane
    }

    pub fn lane_mut(&mut self) -> &mut LaneStats {
        &mut self.pub_lane
    }

    fn jpush(&mut self, seq: u64, anchor: [u8; 16], op: Op) {
        self.journal.push_back((seq, anchor, op));
        while self.journal.len() > JOURNAL_CAP {
            self.journal.pop_front();
        }
    }

    /// Published ops journal (oldest first) for the /geometry/chunks delta.
    pub fn journal(&self) -> &std::collections::VecDeque<(u64, [u8; 16], Op)> {
        &self.journal
    }

    /// Published chunks in insertion order for bootstrap delta responses.
    pub fn chunks(&self) -> &IndexMap<[u8; 16], ChunkMesh, FxBuildHasher> {
        &self.chunks
    }

    /// Apply with ownership of the mesh payload (avoids cloning chunk data).
    pub fn apply_owned(&mut self, mut u: Update) -> bool {
        // ARKit anchor vertices are local. Transform each replacement once,
        // before any session/sequence/cache mutation, into sensor-world metres.
        if let Some(mesh) = &mut u.mesh {
            let m = u.transform;
            for point in &mut mesh.positions {
                let [x, y, z] = point.map(f64::from);
                let world = [
                    f64::from(m[0]) * x + f64::from(m[4]) * y + f64::from(m[8]) * z + f64::from(m[12]),
                    f64::from(m[1]) * x + f64::from(m[5]) * y + f64::from(m[9]) * z + f64::from(m[13]),
                    f64::from(m[2]) * x + f64::from(m[6]) * y + f64::from(m[10]) * z + f64::from(m[14]),
                ].map(|v| v as f32);
                if !world.iter().all(|v| v.is_finite()) {
                    self.pub_lane.dropped_malformed += 1;
                    return false;
                }
                *point = world;
            }
        }
        if u.session != self.session || u.epoch != self.epoch {
            self.session = u.session;
            self.epoch = u.epoch;
            self.last_seq = 0;
            self.chunks.clear();
            self.count = 0;
            self.verts = 0;
            self.pending = Some(IndexMap::with_hasher(FxBuildHasher));
            self.pending_tri = 0;
            self.pending_vert = 0;
            self.journal.clear();
            self.snapshot_complete = false;
            self.pub_lane.reconnects += 1;
            self.revision += 1;
        }
        if u.seq <= self.last_seq {
            self.pub_lane.dropped_stale += 1;
            return false;
        }
        self.last_seq = u.seq;
        match u.op {
            Op::Reset => {
                self.chunks.clear();
                self.count = 0;
                self.verts = 0;
                self.pending = Some(IndexMap::with_hasher(FxBuildHasher));
                self.pending_tri = 0;
                self.pending_vert = 0;
                self.snapshot_complete = false;
                self.revision += 1;
                self.jpush(u.seq, u.anchor, Op::Reset);
            }
            Op::SnapshotEnd => {
                if let Some(pending) = self.pending.take() {
                    self.pending_tri = 0;
                    self.pending_vert = 0;
                    for (id, (seq, mesh)) in pending {
                        self.count += mesh.indices.len();
                        self.verts += mesh.positions.len();
                        self.chunks.insert(id, mesh);
                        self.jpush(seq, id, Op::Replace);
                    }
                    while self.count > self.budget || self.verts > MAX_PUBLISHED_VERTICES {
                        if let Some((id, evicted)) = self.chunks.shift_remove_index(0) {
                            self.count -= evicted.indices.len();
                            self.verts -= evicted.positions.len();
                            self.jpush(self.last_seq, id, Op::Remove);
                        } else {
                            break;
                        }
                    }
                    self.snapshot_complete = true;
                    self.revision += 1;
                }
            }
            Op::Replace => {
                let Some(mesh) = u.mesh else {
                    self.pub_lane.dropped_malformed += 1;
                    return false;
                };
                if let Some(pending) = &mut self.pending {
                    let tri = mesh.indices.len();
                    let vert = mesh.positions.len();

                    // Pending snapshot is bounded before insert — anchors,
                    // triangles, vertices each capped so a bad producer can't
                    // grow the buffer unboundedly.
                    if pending.len() >= MAX_PENDING_ANCHORS
                        || self.pending_tri + tri > self.budget
                        || self.pending_vert + vert > MAX_PUBLISHED_VERTICES
                    {
                        self.pub_lane.dropped_malformed += 1;
                        return false;
                    }
                    if let Some(old) = pending.get(&u.anchor) {
                        self.pending_tri -= old.1.indices.len();
                        self.pending_vert -= old.1.positions.len();
                    }
                    self.pending_tri += tri;
                    self.pending_vert += vert;
                    pending.insert(u.anchor, (u.seq, mesh));
                } else {
                    if self.upsert(u.anchor, mesh) {
                        self.jpush(u.seq, u.anchor, Op::Replace);
                    }
                }
            }
            Op::Remove => {
                if let Some(pending) = &mut self.pending {
                    pending.shift_remove(&u.anchor);
                } else if self.remove(u.anchor) {
                    self.jpush(u.seq, u.anchor, Op::Remove);
                }
            }
        }
        self.pub_lane.applied += 1;
        true
    }

    fn upsert(&mut self, anchor_id: [u8; 16], mesh: ChunkMesh) -> bool {
        if mesh.indices.is_empty()
            || mesh.indices.len() > self.budget
            || mesh.positions.len() > MAX_PUBLISHED_VERTICES
        {
            return false;
        }
        if let Some(old) = self.chunks.shift_remove(&anchor_id) {
            self.count -= old.indices.len();
            self.verts -= old.positions.len();
        }
        self.count += mesh.indices.len();
        self.verts += mesh.positions.len();
        self.chunks.insert(anchor_id, mesh);
        while self.count > self.budget || self.verts > MAX_PUBLISHED_VERTICES {
            if let Some((id, evicted)) = self.chunks.shift_remove_index(0) {
                self.count -= evicted.indices.len();
                self.verts -= evicted.positions.len();
                self.jpush(self.last_seq, id, Op::Remove);
            } else {
                break;
            }
        }
        self.revision += 1;
        true
    }

    fn remove(&mut self, anchor_id: [u8; 16]) -> bool {
        if let Some(old) = self.chunks.shift_remove(&anchor_id) {
            self.count -= old.indices.len();
            self.verts -= old.positions.len();
            self.revision += 1;
            true
        } else {
            false
        }
    }

    /// Exact `mesh.bin` wire format served by farsight-server: u32 vertex
    /// count, u32 scalar index count, 16-byte vertex records (xyz f32 + rgb
    /// + pad), then u32 indices with per-chunk offsets applied.
    pub fn encode(&self) -> Vec<u8> {
        let index_scalars = self.count * 3;
        let mut vertex_count: u32 = 0;
        for mesh in self.chunks.values() {
            vertex_count += mesh.positions.len() as u32;
        }
        let mut out = Vec::with_capacity(8 + vertex_count as usize * 16 + index_scalars * 4);
        out.extend_from_slice(&vertex_count.to_le_bytes());
        out.extend_from_slice(&(index_scalars as u32).to_le_bytes());
        for mesh in self.chunks.values() {
            for (p, c) in mesh.positions.iter().zip(mesh.colors.iter()) {
                for v in p {
                    out.extend_from_slice(&v.to_le_bytes());
                }
                out.extend_from_slice(c);
                out.push(0);
            }
        }
        let mut offset = 0u32;
        for mesh in self.chunks.values() {
            for tri in &mesh.indices {
                for &i in tri {
                    out.extend_from_slice(&(i + offset).to_le_bytes());
                }
            }
            offset += mesh.positions.len() as u32;
        }
        out
    }
}

// --- serve-side variant cache ------------------------------------------------

/// One encoded resource plus lazily-compressed variants, keyed by revision —
/// mirrors farsight-server's per-version VariantCache.
pub struct VariantCache {
    raw: Bytes,
    zstd: Option<Bytes>,
    br: Option<Bytes>,
    gz: Option<Bytes>,
}

impl VariantCache {
    pub fn new(raw: Vec<u8>) -> Self {
        Self { raw: raw.into(), zstd: None, br: None, gz: None }
    }

    /// (body, content-encoding). Bodies under 1 KiB serve raw — compression
    /// overhead isn't worth it there.
    pub fn variant(&mut self, coding: Coding) -> (Bytes, &'static str) {
        if coding == Coding::Identity || self.raw.len() < 1024 {
            return (self.raw.clone(), "");
        }
        let slot = match coding {
            Coding::Zstd => &mut self.zstd,
            Coding::Br => &mut self.br,
            Coding::Gzip => &mut self.gz,
            Coding::Identity => unreachable!(),
        };
        if slot.is_none() {
            *slot = Some(Bytes::from(match coding {
                Coding::Zstd => zstd::bulk::compress(&self.raw, 9).unwrap_or_default(),
                Coding::Br => {
                    use std::io::Read;
                    let mut out = Vec::new();
                    let mut r = brotli::CompressorReader::new(&self.raw[..], 1 << 20, 4, 22);
                    let _ = r.read_to_end(&mut out);
                    out
                }
                Coding::Gzip => {
                    use std::io::Write;
                    let mut e = flate2::write::GzEncoder::new(
                        Vec::new(),
                        flate2::Compression::new(5),
                    );
                    let _ = e.write_all(&self.raw);
                    e.finish().unwrap_or_default()
                }
                Coding::Identity => unreachable!(),
            }));
        }
        (slot.clone().unwrap(), coding.name())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Coding {
    Identity,
    Zstd,
    Br,
    Gzip,
}

impl Coding {
    pub fn name(self) -> &'static str {
        match self {
            Coding::Zstd => "zstd",
            Coding::Br => "br",
            Coding::Gzip => "gzip",
            Coding::Identity => "",
        }
    }
}

/// zstd > br > gzip; a q=0 refusal drops that coding.
pub fn negotiate_encoding(accept: &str) -> Coding {
    let mut zstd = false;
    let mut br = false;
    let mut gz = false;
    for part in accept.split(',') {
        let mut seg = part.trim().splitn(2, ';');
        let name = seg.next().unwrap_or("").trim().to_ascii_lowercase();
        let refused = seg.next().map(|p| p.replace(' ', "").contains("q=0")).unwrap_or(false);
        match name.as_str() {
            "zstd" if !refused => zstd = true,
            "br" if !refused => br = true,
            "gzip" if !refused => gz = true,
            _ => {}
        }
    }
    if zstd {
        Coding::Zstd
    } else if br {
        Coding::Br
    } else if gz {
        Coding::Gzip
    } else {
        Coding::Identity
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mesh(tris: usize) -> ChunkMesh {
        let mut positions = Vec::new();
        let mut colors = Vec::new();
        let mut indices = Vec::new();
        for _ in 0..tris {
            let base = positions.len() as u32;
            positions.push([0.0; 3]);
            positions.push([1.0; 3]);
            positions.push([0.0, 1.0, 0.0]);
            colors.extend_from_slice(&[[9, 9, 9]; 3]);
            indices.push([base, base + 1, base + 2]);
        }
        ChunkMesh { positions, colors, indices }
    }

    fn upd(seq: u64, op: Op, m: Option<ChunkMesh>) -> Update {
        Update {
            session: [7; 16],
            epoch: 3,
            seq,
            op,
            anchor: [1; 16],
            transform: [0.0; 16],
            mesh: m,
        }
    }

    #[test]
    fn record_roundtrip_plain_and_deflate() {
        for coding in [RecordCoding::Raw, RecordCoding::Zlib, RecordCoding::Brotli] {
            let u = upd(9, Op::Replace, Some(mesh(2)));
            let bytes = encode(&u, coding);
            let d = decode(&bytes).unwrap();
            assert_eq!((d.seq, d.epoch, d.op, d.anchor), (9, 3, Op::Replace, [1; 16]));
            let m = d.mesh.unwrap();
            assert_eq!((m.positions.len(), m.indices.len()), (6, 2));
        }
    }

    #[test]
    fn decode_rejects_garbage_and_oversized() {
        assert!(decode(&[]).is_err());
        assert!(decode(&[0u8; 8]).is_err());
        let mut bad = encode(&upd(1, Op::Replace, Some(mesh(1))), RecordCoding::Raw);
        bad[0] = 99; // version
        assert!(decode(&bad).is_err());
        let mut bad = encode(&upd(1, Op::Replace, Some(mesh(1))), RecordCoding::Raw);
        bad[1] = 9; // op
        assert!(decode(&bad).is_err());
        assert!(decode(&vec![0u8; MAX_UPDATE_BYTES + 1]).is_err());
        // remove must not carry a payload
        assert!(decode(&encode(&upd(1, Op::Remove, Some(mesh(1))), RecordCoding::Raw)).is_err());
        // flags 1+2 together (enum value 3) and any higher bits reject
        let mut bad = encode(&upd(1, Op::Replace, Some(mesh(1))), RecordCoding::Raw);
        bad[2] = 3;
        assert!(decode(&bad).is_err());
        bad[2] = 4;
        assert!(decode(&bad).is_err());
        // compressed body that inflates past declared length rejects
        let mut oversized = encode(&upd(1, Op::Replace, Some(mesh(1))), RecordCoding::Zlib);
        // lie: declare more vertices than the payload actually carries, but
        // keep the compressed body — decode must not allocate unboundedly.
        // vcount field offset: 1+1+2+8+8+16+16+64 = 116
        let honest = mesh(1).positions.len() as u32 * 12 + mesh(1).indices.len() as u32 * 4;
        let _ = honest;
        oversized[116..120].copy_from_slice(&(1u32).to_le_bytes());
        assert!(decode(&oversized).is_err());
        // non-finite transform / vertex reject
        let mut nan = encode(&upd(1, Op::Replace, Some(mesh(1))), RecordCoding::Raw);
        nan[84] = 0xFF; nan[85] = 0xFF; nan[86] = 0xFF; nan[87] = 0x7F; // NaN into first transform f32
        assert!(decode(&nan).is_err());
    }

    #[test]
    fn pending_snapshot_is_bounded() {
        let at = |seq: u64, op: Op, m: Option<ChunkMesh>, anchor: [u8; 16]| {
            let mut u = upd(seq, op, m);
            u.anchor = anchor;
            u
        };
        // A snapshot that never ends must not grow unboundedly.
        let mut geo = AnchorGeometry::new(2);
        geo.apply_owned(at(1, Op::Reset, None, [7; 16]));
        // budget 2 faces: the third pending replace drops as malformed.
        assert!(geo.apply_owned(at(2, Op::Replace, Some(mesh(1)), [8; 16])));
        assert!(geo.apply_owned(at(3, Op::Replace, Some(mesh(1)), [9; 16])));
        assert!(!geo.apply_owned(at(4, Op::Replace, Some(mesh(1)), [10; 16])));
        // lane is still usable — snapshot_end publishes the bounded snapshot.
        assert!(geo.apply_owned(at(5, Op::SnapshotEnd, None, [11; 16])));
        assert_eq!(geo.triangles(), 2);
    }

    #[test]
    fn session_change_resets_and_snapshot_publishes_atomically() {
        let mut g = AnchorGeometry::new(100);
        g.apply_owned(upd(1, Op::SnapshotEnd, None));
        g.apply_owned(upd(2, Op::Replace, Some(mesh(2))));
        assert_eq!(g.triangles(), 2);
        // New session: atomic clear + pending until snapshot-end.
        let mut reset = upd(2, Op::Reset, None);
        reset.session = [9; 16];
        g.apply_owned(reset);
        assert_eq!((g.triangles(), g.anchors()), (0, 0));
        let mut a = upd(3, Op::Replace, Some(mesh(3)));
        a.session = [9; 16];
        g.apply_owned(a);
        assert_eq!(g.triangles(), 0); // buffered, not published
        let mut end = upd(4, Op::SnapshotEnd, None);
        end.session = [9; 16];
        g.apply_owned(end);
        assert_eq!(g.triangles(), 3); // whole snapshot publishes at once
        assert_eq!(g.lane().reconnects, 2);
    }

    #[test]
    fn anchor_rotation_and_translation_are_applied_once_before_export() {
        let mut g = AnchorGeometry::new(100);
        g.apply_owned(upd(1, Op::SnapshotEnd, None));
        let mut update = upd(2, Op::Replace, Some(mesh(1)));
        // Rotate +90 degrees around Z, then translate (1, 2, 3).
        update.transform = [0., 1., 0., 0., -1., 0., 0., 0., 0., 0., 1., 0., 1., 2., 3., 1.];
        assert!(g.apply_owned(update));
        let bytes = g.encode();
        let point = |index: usize| {
            let offset = 8 + index * 16;
            [0, 4, 8].map(|axis| f32::from_le_bytes(bytes[offset + axis..offset + axis + 4].try_into().unwrap()))
        };
        assert_eq!(point(0), [1., 2., 3.]);
        assert_eq!(point(1), [0., 3., 4.]);
        assert_eq!(point(2), [0., 2., 3.]);
    }

    #[test]
    fn stale_seq_drops_and_remove_works() {
        let mut g = AnchorGeometry::new(100);
        g.apply_owned(upd(1, Op::SnapshotEnd, None)); // publish first snapshot
        g.apply_owned(upd(5, Op::Replace, Some(mesh(2))));
        assert!(!g.apply_owned(upd(5, Op::Replace, Some(mesh(1)))));
        assert!(!g.apply_owned(upd(4, Op::Remove, None)));
        assert_eq!(g.lane().dropped_stale, 2);
        assert!(g.apply_owned(upd(6, Op::Remove, None)));
        assert_eq!(g.triangles(), 0);
    }

    #[test]
    fn encode_is_mesh_bin_wire_format() {
        let mut g = AnchorGeometry::new(100);
        g.apply_owned(upd(1, Op::SnapshotEnd, None));
        g.apply_owned(upd(2, Op::Replace, Some(mesh(2))));
        let buf = g.encode();
        assert_eq!(u32::from_le_bytes(buf[0..4].try_into().unwrap()), 6);
        assert_eq!(u32::from_le_bytes(buf[4..8].try_into().unwrap()), 6);
        assert_eq!(buf.len(), 8 + 6 * 16 + 6 * 4);
    }

    #[test]
    fn snapshot_complete_lifecycle() {
        // tools/qualify_geometry.py gate: unknown/staging must read false;
        // only an accepted SnapshotEnd may report complete.
        let mut g = AnchorGeometry::new(100);
        assert!(!g.snapshot_complete());
        // Staged replace without publish still incomplete.
        g.apply_owned(upd(1, Op::Replace, Some(mesh(1))));
        assert!(!g.snapshot_complete());
        // Accepted SnapshotEnd publishes → complete.
        g.apply_owned(upd(2, Op::SnapshotEnd, None));
        assert!(g.snapshot_complete());
        // A new snapshot staging clears published → incomplete again.
        g.apply_owned(upd(3, Op::Reset, None));
        assert!(!g.snapshot_complete());
        // Reconnect staging (new session) also reports incomplete.
        let mut u = upd(4, Op::Replace, Some(mesh(1)));
        u.session = [9; 16];
        g.apply_owned(u);
        assert!(!g.snapshot_complete());
    }

    #[test]
    fn encode_applies_anchor_transform() {
        // Regression (Codex e2e catch): vertices are anchor-local; mesh.bin
        // must bake the record's column-major transform or every anchor
        // piles at its local origin.
        let mut g = AnchorGeometry::new(1 << 20);
        let mut u = upd(1, Op::Replace, Some(mesh(1)));
        // Column-major translation (1,2,3).
        u.transform[12] = 1.0;
        u.transform[13] = 2.0;
        u.transform[14] = 3.0;
        assert!(g.apply_owned(u));
        // First update of a session lands in the pending snapshot; publish it.
        assert!(g.apply_owned(upd(2, Op::SnapshotEnd, None)));
        let bin = g.encode();
        let vx = f32::from_le_bytes(bin[8..12].try_into().unwrap());
        let vy = f32::from_le_bytes(bin[12..16].try_into().unwrap());
        let vz = f32::from_le_bytes(bin[16..20].try_into().unwrap());
        let local = mesh(1).positions[0];
        assert!((vx - (local[0] + 1.0)).abs() < 1e-6);
        assert!((vy - (local[1] + 2.0)).abs() < 1e-6);
        assert!((vz - (local[2] + 3.0)).abs() < 1e-6);
    }

    #[test]
    fn encode_applies_anchor_rotation() {
        let mut g = AnchorGeometry::new(1 << 20);
        let mut u = upd(1, Op::Replace, Some(mesh(1)));
        // 90-degree rotation about Z (column-major): x' = -y, y' = x, z' = z.
        u.transform[0] = 0.0;
        u.transform[4] = -1.0;
        u.transform[1] = 1.0;
        u.transform[5] = 0.0;
        assert!(g.apply_owned(u));
        assert!(g.apply_owned(upd(2, Op::SnapshotEnd, None)));
        let bin = g.encode();
        let vx = f32::from_le_bytes(bin[8..12].try_into().unwrap());
        let vy = f32::from_le_bytes(bin[12..16].try_into().unwrap());
        let vz = f32::from_le_bytes(bin[16..20].try_into().unwrap());
        let local = mesh(1).positions[0];
        assert!((vx - (-local[1])).abs() < 1e-6);
        assert!((vy - local[0]).abs() < 1e-6);
        assert!((vz - local[2]).abs() < 1e-6);
    }
}


// --- service plumbing ---------------------------------------------------------

use std::sync::{Arc, Mutex};

/// Shared service state: assembled geometry + revision-keyed packet cache.
pub struct Shared {
    pub geo: Mutex<AnchorGeometry>,
    packet: Mutex<Option<(u64, VariantCache)>>,
    connected: Mutex<Option<String>>,
}

impl Shared {
    pub fn new(triangle_budget: usize) -> Arc<Self> {
        Arc::new(Self {
            geo: Mutex::new(AnchorGeometry::new(triangle_budget)),
            packet: Mutex::new(None),
            connected: Mutex::new(None),
        })
    }
}


use axum::body::Body;
use axum::extract::State;
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use futures_util::StreamExt;
use serde_json::{json, Value};

fn mesh_response(s: &Shared, accept: &str, if_none_match: &str) -> Response {
    let coding = negotiate_encoding(accept);
    let (revision, body, enc) = {
        let geo = s.geo.lock().unwrap();
        let revision = geo.revision();
        let mut cache = s.packet.lock().unwrap();
        if cache.as_ref().map(|(r, _)| *r != revision).unwrap_or(true) {
            *cache = Some((revision, VariantCache::new(geo.encode())));
        }
        let entry = &mut cache.as_mut().unwrap().1;
        let (body, enc) = entry.variant(coding);
        (revision, body, enc)
    };
    let tag = format!("\"{revision}-{}\"", if enc.is_empty() { "i" } else { enc });
    for candidate in if_none_match.split(',') {
        if candidate.trim() == tag || candidate.trim() == "*" {
            return (StatusCode::NOT_MODIFIED, [(header::ETAG, tag)]).into_response();
        }
    }
    let mut resp = Response::new(Body::from(body));
    let h = resp.headers_mut();
    h.insert(
        header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("application/octet-stream"),
    );
    h.insert(header::ETAG, tag.parse().unwrap());
    h.insert(header::CACHE_CONTROL, "no-cache".parse().unwrap());
    h.insert(header::VARY, "Accept-Encoding".parse().unwrap());
    if !enc.is_empty() {
        h.insert(header::CONTENT_ENCODING, enc.parse().unwrap());
    }
    resp
}

async fn mesh_bin(State(s): State<Arc<Shared>>, headers: HeaderMap) -> Response {
    let accept = headers
        .get(header::ACCEPT_ENCODING)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let inm = headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    mesh_response(&s, accept, inm)
}

async fn status(State(s): State<Arc<Shared>>) -> Json<Value> {
    let geo = s.geo.lock().unwrap();
    let lane = geo.lane();
    let connected = s.connected.lock().unwrap().clone();
    Json(json!({
        "revision": geo.revision(),
        "snapshot_complete": geo.snapshot_complete(),
        "session": hex_uuid(&geo.session()),
        "epoch": geo.epoch(),
        "anchors": geo.anchors(),
        "triangles": geo.triangles(),
        // Dense-reconstruction audit (6102570158): actual retained counts,
        // not configured caps.
        "retained": {
            "anchors": geo.anchors(),
            "vertices": geo.vertices(),
            "triangles": geo.triangles(),
            "points": geo.vertices(),
            "bytes": geo.vertices() * 16 + geo.triangles() * 12,
        },
        "last_seq": geo.last_seq(),
        "lane": {
            "upstream": connected,
            "applied": lane.applied,
            "dropped_stale": lane.dropped_stale,
            "dropped_malformed": lane.dropped_malformed,
            "reconnects": lane.reconnects,
        },
    }))
}

fn hex_uuid(b: &[u8; 16]) -> String {
    if b.iter().all(|&x| x == 0) {
        return String::new();
    }
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]
    )
}

#[derive(serde::Deserialize)]
pub struct DeltaQuery {
    after: Option<u64>,
    limit: Option<u64>,
}

/// Mac-specific dense/chunked path (6102570158): incremental deltas of the
/// published retained cloud as concatenated v1 wire records — the same
/// format the phone lane produces, coding=0, transform=identity because
/// positions are already world-baked at ingest. A Mac accumulator replays
/// `after=<seq>` pages and retains millions of points without ever
/// re-pulling a whole snapshot; the bounded Quest mesh.bin is untouched.
async fn chunks_delta(
    State(s): State<Arc<Shared>>,
    axum::extract::Query(q): axum::extract::Query<DeltaQuery>,
) -> Response {
    let after = q.after.unwrap_or(0);
    let limit = q.limit.unwrap_or(64).min(512) as usize;
    let geo = s.geo.lock().unwrap();
    let session = geo.session();
    let epoch = geo.epoch();
    let oldest_journaled = geo.journal().front().map(|e| e.0);
    // Bootstrap when the consumer cannot prove continuity: fresh cursor, or
    // its seq fell behind the bounded journal. Synthetic Reset then every
    // retained chunk, then journal tail — all in seq order.
    let bootstrap = after == 0
        || match oldest_journaled {
            Some(o) => after < o,
            None => geo.anchors() > 0,
        };
    let truncated = bootstrap && after > 0;
    let mut entries: Vec<(u64, Op, [u8; 16], Option<ChunkMesh>)> = Vec::new();
    if bootstrap {
        entries.push((after, Op::Reset, [0; 16], None));
        for (id, mesh) in geo.chunks() {
            // chunk seq: its last published write, recoverable from journal
            // (fallback last_seq for anything that predates journaling).
            let seq = geo
                .journal()
                .iter()
                .filter(|e| e.1 == *id && e.2 == Op::Replace)
                .map(|e| e.0)
                .max()
                .unwrap_or_else(|| geo.last_seq());
            entries.push((seq, Op::Replace, *id, Some(mesh.clone())));
        }
        for &(seq, id, op) in geo.journal().iter() {
            if seq > after && op != Op::Replace {
                entries.push((seq, op, id, None));
            }
        }
        entries.sort_by_key(|e| e.0);
    } else {
        for &(seq, id, op) in geo.journal().iter() {
            if seq <= after {
                continue;
            }
            let entry = match op {
                Op::Replace => match geo.chunks().get(&id) {
                    Some(mesh) => (seq, op, id, Some(mesh.clone())),
                    // Evicted between the journal entry and now: degrade to a
                    // tombstone so consumers drop the ghost anchor.
                    None => (seq, Op::Remove, id, None),
                },
                _ => (seq, op, id, None),
            };
            entries.push(entry);
        }
    }
    entries.truncate(limit);
    let next_seq = entries.last().map(|e| e.0).unwrap_or(after);
    let remaining = geo.journal().iter().filter(|e| e.0 > next_seq).count() as u64;
    let mut body = Vec::new();
    for (seq, op, id, mesh) in &entries {
        let update = Update {
            session,
            epoch,
            seq: *seq,
            op: *op,
            anchor: *id,
            transform: IDENTITY,
            mesh: mesh.clone(),
        };
        body.extend_from_slice(&encode(&update, RecordCoding::Raw));
    }
    (
        [
            (header::CONTENT_TYPE, "application/octet-stream".to_string()),
            (header::CACHE_CONTROL, "no-store".to_string()),
            (header::HeaderName::from_static("x-geometry-next-seq"), next_seq.to_string()),
            (header::HeaderName::from_static("x-geometry-remaining"), remaining.to_string()),
            (header::HeaderName::from_static("x-geometry-truncated"), if truncated { "1".into() } else { "0".into() }),
        ],
        body,
    )
        .into_response()
}

/// Quest keeps the existing same-origin shape: the assembled mesh answers
/// under both /geometry/* and the /map/* aliases so a relay or the headset
/// can consume this service drop-in next to the Python stack.
pub fn router(shared: Arc<Shared>) -> Router {
    Router::new()
        .route("/geometry/mesh.bin", get(mesh_bin))
        .route("/geometry/status", get(status))
        .route("/geometry/chunks", get(chunks_delta))
        .route("/map/mesh.bin", get(mesh_bin))
        .route("/map/status", get(status))
        .with_state(shared)
}

/// Pull loop: tries each upstream in order, reconnects with backoff. Binary
/// WS messages are v1 records; seq/session ordering is enforced inside
/// apply_owned, and lane stats count rejects.
pub async fn pull_loop(s: Arc<Shared>, upstreams: Vec<String>, retry_min: f64, retry_max: f64) {
    if upstreams.is_empty() {
        return;
    }
    let mut backoff = retry_min;
    loop {
        for url in &upstreams {
            let connect = tokio_tungstenite::connect_async(url).await;
            let Ok((mut ws, _)) = connect else {
                tracing::warn!("geometry lane {url} connect failed");
                continue;
            };
            *s.connected.lock().unwrap() = Some(url.clone());
            backoff = retry_min;
            tracing::info!("geometry lane connected: {url}");
            while let Some(msg) = ws.next().await {
                let Ok(msg) = msg else { break };
                let tokio_tungstenite::tungstenite::Message::Binary(data) = msg else {
                    continue;
                };
                match decode(&data) {
                    Ok(u) => {
                        s.geo.lock().unwrap().apply_owned(u);
                    }
                    Err(e) => {
                        s.geo.lock().unwrap().lane_mut().dropped_malformed += 1;
                        tracing::warn!("geometry record rejected: {e}");
                    }
                }
            }
            *s.connected.lock().unwrap() = None;
            tracing::warn!("geometry lane {url} dropped; retrying");
        }
        tokio::time::sleep(std::time::Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 2.0).min(retry_max);
    }
}
