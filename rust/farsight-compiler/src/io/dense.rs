//! Dense measured-cloud packets: `u32 LE json_len` + UTF-8 metadata + 16-byte
//! `<iiiBBBB` records (i32 x,y,z millimetres, u8 r,g,b, u8 op). Served by
//! `/map/dense.bin` (Jetson delta), `/dense/delta.bin` (Mac retained pages)
//! and `/dense/chunk.bin` (Mac view chunks). ops: 1 upsert, 0 tombstone.
//! Cell key is `floor(mm / 10)` per axis — rust/farsight-server/src/dense.rs.
use crate::core::PointCloud;
use anyhow::{bail, Context, Result};
use rustc_hash::FxHashMap;
use serde_json::Value;

pub const RECORD_SIZE: usize = 16;
pub const MAX_HEADER: usize = 65536;

/// One wire record. `op`: 1 upsert, 0 delete (tombstone).
#[derive(Clone, Copy, Debug)]
pub struct Record {
    pub x_mm: i32,
    pub y_mm: i32,
    pub z_mm: i32,
    pub rgb: [u8; 3],
    pub op: u8,
}

impl Record {
    #[inline]
    pub fn decode(b: &[u8]) -> Self {
        Record {
            x_mm: i32::from_le_bytes(b[0..4].try_into().unwrap()),
            y_mm: i32::from_le_bytes(b[4..8].try_into().unwrap()),
            z_mm: i32::from_le_bytes(b[8..12].try_into().unwrap()),
            rgb: [b[12], b[13], b[14]],
            op: b[15],
        }
    }
    /// Fine cell key: floor(mm / 10) per axis, including negatives.
    #[inline]
    pub fn key(&self) -> (i32, i32, i32) {
        (self.x_mm.div_euclid(10), self.y_mm.div_euclid(10), self.z_mm.div_euclid(10))
    }
}

/// Split a dense packet into (metadata JSON, record bytes). Validates the
/// `xyz_mm_rgb8_op` format tag and record alignment.
pub fn unpack(body: &[u8]) -> Result<(Value, &[u8])> {
    if body.len() < 4 {
        bail!("truncated dense header");
    }
    let size = u32::from_le_bytes(body[0..4].try_into().unwrap()) as usize;
    if size == 0 || size > MAX_HEADER || size + 4 > body.len() {
        bail!("invalid dense header");
    }
    let meta: Value = serde_json::from_slice(&body[4..4 + size]).context("dense metadata JSON")?;
    let data = &body[4 + size..];
    if meta.get("format").and_then(Value::as_str) != Some("xyz_mm_rgb8_op") || !data.len().is_multiple_of(RECORD_SIZE) {
        bail!("invalid dense records");
    }
    Ok((meta, data))
}

/// Cheap shape check without parsing the JSON payload.
pub fn looks_like(body: &[u8]) -> bool {
    if body.len() < 8 {
        return false;
    }
    let size = u32::from_le_bytes(body[0..4].try_into().unwrap()) as usize;
    size > 0 && size <= MAX_HEADER && size + 4 <= body.len() && body[4] == b'{'
}

/// Records of one packet as a decoded slice iterator.
pub fn records(data: &[u8]) -> impl Iterator<Item = Record> + '_ {
    data.chunks_exact(RECORD_SIZE).map(Record::decode)
}

/// In-memory keyed cloud accumulator for paged delta replay: upserts keyed by
/// 10 mm cell (last wins), tombstones remove. Positions are kept in integer
/// millimetres so cell keys stay exact; converted to metres on emit.
#[derive(Default)]
pub struct Accumulator {
    cells: FxHashMap<(i32, i32, i32), usize>,
    pos_mm: Vec<[i32; 3]>,
    col: Vec<[u8; 3]>,
}

impl Accumulator {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn clear(&mut self) {
        self.cells.clear();
        self.pos_mm.clear();
        self.col.clear();
    }
    pub fn len(&self) -> usize {
        self.cells.len()
    }
    pub fn is_empty(&self) -> bool {
        self.cells.is_empty()
    }
    /// Apply one raw record block (a packet's record section).
    pub fn apply(&mut self, data: &[u8]) {
        for rec in records(data) {
            self.apply_record(&rec);
        }
    }
    pub fn apply_record(&mut self, rec: &Record) {
        let key = rec.key();
        if rec.op == 0 {
            if let Some(slot) = self.cells.remove(&key) {
                // Swap-remove keeps a dense Vec; fix the moved entry's slot.
                self.pos_mm.swap_remove(slot);
                self.col.swap_remove(slot);
                if slot < self.pos_mm.len() {
                    let moved = self.pos_mm[slot];
                    self.cells.insert(
                        (moved[0].div_euclid(10), moved[1].div_euclid(10), moved[2].div_euclid(10)),
                        slot,
                    );
                }
            }
            return;
        }
        if rec.op != 1 {
            return;
        }
        match self.cells.get(&key) {
            Some(&slot) => {
                self.pos_mm[slot] = [rec.x_mm, rec.y_mm, rec.z_mm];
                self.col[slot] = rec.rgb;
            }
            None => {
                self.cells.insert(key, self.pos_mm.len());
                self.pos_mm.push([rec.x_mm, rec.y_mm, rec.z_mm]);
                self.col.push(rec.rgb);
            }
        }
    }
    /// Emit the accumulated cloud: integer mm -> metres.
    pub fn into_cloud(self) -> PointCloud {
        let pos = self
            .pos_mm
            .into_iter()
            .map(|p| [p[0] as f32 * 1e-3, p[1] as f32 * 1e-3, p[2] as f32 * 1e-3])
            .collect();
        PointCloud { pos, col: self.col, ..Default::default() }
    }
}

/// Parse a single dense packet into a cloud (last-wins keyed apply).
pub fn read_cloud(body: &[u8]) -> Result<PointCloud> {
    let (_, data) = unpack(body)?;
    let mut acc = Accumulator::new();
    acc.apply(data);
    Ok(acc.into_cloud())
}

/// If `body` is a dense packet, decode it; otherwise None.
pub fn try_read_cloud(body: &[u8]) -> Result<Option<PointCloud>> {
    if !looks_like(body) {
        return Ok(None);
    }
    match unpack(body) {
        Ok((_, data)) => {
            let mut acc = Accumulator::new();
            acc.apply(data);
            Ok(Some(acc.into_cloud()))
        }
        Err(_) => Ok(None),
    }
}
