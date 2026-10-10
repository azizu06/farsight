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
    if op == Op::Replace && (vcount == 0 || icount == 0 || !icount.is_multiple_of(3)) {
        return Err("replace needs a non-empty triangle mesh".into());
    }
    if op != Op::Replace && (vcount != 0 || icount != 0) {
        return Err("non-replace op must carry an empty payload".into());
    }
    let payload = packet[off..].to_vec();
    let payload = match coding {
        RecordCoding::Raw => payload,
        RecordCoding::Zlib => {
            use std::io::Read;
            let mut out = Vec::new();
            flate2::read::ZlibDecoder::new(&payload[..])
                .read_to_end(&mut out)
                .map_err(|e| format!("deflate payload invalid: {e}"))?;
            out
        }
        RecordCoding::Brotli => {
            use std::io::Read;
            let mut out = Vec::new();
            brotli::Decompressor::new(&payload[..], 4096)
                .read_to_end(&mut out)
                .map_err(|e| format!("brotli payload invalid: {e}"))?;
            out
        }
    };
    let want = vcount * 12 + icount * 4;
    if payload.len() != want {
        return Err(format!("payload length {} != declared {want}", payload.len()));
    }
    let mesh = if op == Op::Replace {
        let mut positions = Vec::with_capacity(vcount);
        let mut colors = Vec::with_capacity(vcount);
        let mut indices = Vec::with_capacity(icount / 3);
        for b in payload[..vcount * 12].chunks_exact(12) {
            positions.push([
                f32::from_le_bytes(b[0..4].try_into().unwrap()),
                f32::from_le_bytes(b[4..8].try_into().unwrap()),
                f32::from_le_bytes(b[8..12].try_into().unwrap()),
            ]);
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
    revision: u64,
    session: [u8; 16],
    epoch: u64,
    last_seq: u64,
    /// Buffered ops between Reset and SnapshotEnd — published atomically.
    pending: Option<IndexMap<[u8; 16], ChunkMesh, FxBuildHasher>>,
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
            revision: 0,
            session: [0; 16],
            epoch: 0,
            last_seq: 0,
            pending: None,
            pub_lane: LaneStats::default(),
        }
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn triangles(&self) -> usize {
        self.count
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

    /// Apply with ownership of the mesh payload (avoids cloning chunk data).
    pub fn apply_owned(&mut self, u: Update) -> bool {
        if u.session != self.session || u.epoch != self.epoch {
            self.session = u.session;
            self.epoch = u.epoch;
            self.last_seq = 0;
            self.chunks.clear();
            self.count = 0;
            self.pending = Some(IndexMap::with_hasher(FxBuildHasher));
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
                self.pending = Some(IndexMap::with_hasher(FxBuildHasher));
                self.revision += 1;
            }
            Op::SnapshotEnd => {
                if let Some(pending) = self.pending.take() {
                    for (id, mesh) in pending {
                        self.count += mesh.indices.len();
                        self.chunks.insert(id, mesh);
                    }
                    while self.count > self.budget {
                        if let Some((_, evicted)) = self.chunks.shift_remove_index(0) {
                            self.count -= evicted.indices.len();
                        } else {
                            break;
                        }
                    }
                    self.revision += 1;
                }
            }
            Op::Replace => {
                let Some(mesh) = u.mesh else {
                    self.pub_lane.dropped_malformed += 1;
                    return false;
                };
                if let Some(pending) = &mut self.pending {
                    pending.insert(u.anchor, mesh);
                } else {
                    self.upsert(u.anchor, mesh);
                }
            }
            Op::Remove => {
                if let Some(pending) = &mut self.pending {
                    pending.shift_remove(&u.anchor);
                } else {
                    self.remove(u.anchor);
                }
            }
        }
        self.pub_lane.applied += 1;
        true
    }

    fn upsert(&mut self, anchor_id: [u8; 16], mesh: ChunkMesh) -> bool {
        if mesh.indices.is_empty() || mesh.indices.len() > self.budget {
            return false;
        }
        if let Some(old) = self.chunks.shift_remove(&anchor_id) {
            self.count -= old.indices.len();
        }
        self.count += mesh.indices.len();
        self.chunks.insert(anchor_id, mesh);
        while self.count > self.budget {
            if let Some((_, evicted)) = self.chunks.shift_remove_index(0) {
                self.count -= evicted.indices.len();
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
        "session": hex_uuid(&geo.session()),
        "epoch": geo.epoch(),
        "anchors": geo.anchors(),
        "triangles": geo.triangles(),
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

/// Quest keeps the existing same-origin shape: the assembled mesh answers
/// under both /geometry/* and the /map/* aliases so a relay or the headset
/// can consume this service drop-in next to the Python stack.
pub fn router(shared: Arc<Shared>) -> Router {
    Router::new()
        .route("/geometry/mesh.bin", get(mesh_bin))
        .route("/geometry/status", get(status))
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
