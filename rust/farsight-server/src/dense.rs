//! Measured visual detail, independent of the navigation voxel/mesh budgets.
//! Port of jetson/mapping/dense.py.
//!
//! Wire: u32 JSON length, UTF-8 metadata, then 16-byte records (<iiiBBBB):
//! millimetre xyz, RGB, operation (1 upsert, 0 delete). No synthesized samples.
//! All samples expire: an unsegmented moving body must never become permanent.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use indexmap::IndexMap;
use serde_json::Value;

/// `<iiiBBBB` — i32 xyz millimetres, u8 rgb, u8 op (1 upsert, 0 delete).
pub const RECORD_SIZE: usize = 16;
pub const MAX_RECORDS: usize = 8192;
pub const CAPACITY: usize = 600_000;
pub const TTL_S: f64 = 30.0;
pub const JOURNAL_SIZE: usize = 200_000;
/// Snapshot pages are kept for slow consumers this long (dense.py literal).
const SNAPSHOT_TTL_S: f64 = 30.0;
/// Identical upserts renew the wire at most every 5 s (dense.py coalescing).
const REFRESH_S: f64 = 5.0;

pub type Key = (i32, i32, i32);

/// One wire record. `op`: 1 upsert, 0 delete.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Record {
    pub x: i32,
    pub y: i32,
    pub z: i32,
    pub r: u8,
    pub g: u8,
    pub b: u8,
    pub op: u8,
}

impl Record {
    pub fn new(x: i32, y: i32, z: i32, r: u8, g: u8, b: u8, op: u8) -> Self {
        Self { x, y, z, r, g, b, op }
    }

    pub fn encode(&self) -> [u8; RECORD_SIZE] {
        let mut out = [0u8; RECORD_SIZE];
        out[0..4].copy_from_slice(&self.x.to_le_bytes());
        out[4..8].copy_from_slice(&self.y.to_le_bytes());
        out[8..12].copy_from_slice(&self.z.to_le_bytes());
        out[12] = self.r;
        out[13] = self.g;
        out[14] = self.b;
        out[15] = self.op;
        out
    }

    pub fn decode(b: &[u8]) -> Self {
        Self {
            x: i32::from_le_bytes(b[0..4].try_into().unwrap()),
            y: i32::from_le_bytes(b[4..8].try_into().unwrap()),
            z: i32::from_le_bytes(b[8..12].try_into().unwrap()),
            r: b[12],
            g: b[13],
            b: b[14],
            op: b[15],
        }
    }

    /// Fine cell key: floor(mm / 10) per axis, including negatives.
    pub fn key(&self) -> Key {
        (self.x.div_euclid(10), self.y.div_euclid(10), self.z.div_euclid(10))
    }
}

/// u32 JSON length, UTF-8 metadata, then the 16-byte records.
pub fn packet(metadata: &str, records: &[[u8; RECORD_SIZE]]) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 + metadata.len() + records.len() * RECORD_SIZE);
    out.extend_from_slice(&(metadata.len() as u32).to_le_bytes());
    out.extend_from_slice(metadata.as_bytes());
    for record in records {
        out.extend_from_slice(record);
    }
    out
}

/// Port of dense.unpack: header, format, budget and value validation.
pub fn unpack(body: &[u8]) -> Result<(Value, Vec<Record>), String> {
    if body.len() < 4 {
        return Err("Truncated dense header".into());
    }
    let size = u32::from_le_bytes(body[0..4].try_into().unwrap()) as usize;
    if size == 0 || size > 65536 || size + 4 > body.len() {
        return Err("Invalid dense header".into());
    }
    let metadata: Value =
        serde_json::from_slice(&body[4..4 + size]).map_err(|e| format!("Invalid dense header: {e}"))?;
    let data = &body[4 + size..];
    if metadata.get("format").and_then(Value::as_str) != Some("xyz_mm_rgb8_op")
        || data.len() % RECORD_SIZE != 0
    {
        return Err("Invalid dense records".into());
    }
    if data.len() / RECORD_SIZE > MAX_RECORDS {
        return Err("Dense chunk exceeds record budget".into());
    }
    let records: Vec<Record> = data.chunks_exact(RECORD_SIZE).map(Record::decode).collect();
    if records
        .iter()
        .any(|r| r.op > 1 || r.x.abs() >= 10_000_000 || r.y.abs() >= 10_000_000 || r.z.abs() >= 10_000_000)
    {
        return Err("Invalid dense operation or coordinate".into());
    }
    Ok((metadata, records))
}

struct Cell {
    rev: u64,
    record: [u8; RECORD_SIZE],
    /// Clock at the last journaled upsert; identical refreshes inside 5 s
    /// keep the old stamp (dense.py: cells run "at most 5 s behind").
    refreshed: f64,
}

struct Inner {
    /// Insertion-ordered by last wire refresh — non-decreasing revision.
    cells: IndexMap<Key, Cell>,
    /// Contiguous (revision, record) tail; oldest drops off the bound.
    journal: VecDeque<(u64, [u8; RECORD_SIZE])>,
    revision: u64,
    epoch: String,
    /// [session_id, map_epoch] of the capture, or null.
    source: Value,
    /// anchor revision -> immutable (rev, record) page space for bootstrap.
    snapshots: IndexMap<u64, Vec<(u64, [u8; RECORD_SIZE])>>,
    snapshot_times: HashMap<u64, f64>,
    /// Cumulative observed upserts this epoch; `cells.len()` is the bounded
    /// retained window — historical observations are not retained points.
    observed: u64,
}

fn new_epoch() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// Fine cells + bounded change journal. Slow clients bootstrap in bounded
/// pages. A revision belongs only to its epoch; snapshot pages use the
/// active cell revision order, so concurrent observations move forward and
/// cannot be silently skipped.
pub struct DenseMap {
    inner: Mutex<Inner>,
    capacity: usize,
    ttl: f64,
    journal_size: usize,
    /// Retain points beyond the publisher's cache budget: capacity eviction
    /// drops the cell without a deletion record (dense.py cache_only).
    cache_only: bool,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
}

impl DenseMap {
    pub fn new(cache_only: bool) -> Self {
        Self::with_options(
            CAPACITY,
            TTL_S,
            JOURNAL_SIZE,
            cache_only,
            Arc::new(crate::state::now_monotonic),
        )
    }

    /// Full option surface, mirroring DenseMap(capacity, ttl, journal_size,
    /// clock, cache_only) — used by tests for a frozen clock and small budgets.
    pub fn with_options(
        capacity: usize,
        ttl: f64,
        journal_size: usize,
        cache_only: bool,
        clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    ) -> Self {
        Self {
            inner: Mutex::new(Inner {
                cells: IndexMap::new(),
                journal: VecDeque::new(),
                revision: 0,
                epoch: new_epoch(),
                source: Value::Null,
                snapshots: IndexMap::new(),
                snapshot_times: HashMap::new(),
                observed: 0,
            }),
            capacity,
            ttl,
            journal_size,
            cache_only,
            clock,
        }
    }

    pub fn epoch(&self) -> String {
        self.inner.lock().unwrap().epoch.clone()
    }

    pub fn revision(&self) -> u64 {
        self.inner.lock().unwrap().revision
    }

    /// Retained unique cells in the bounded window.
    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().cells.len()
    }

    /// Cumulative upserts observed this epoch (not the retained window).
    pub fn observed(&self) -> u64 {
        self.inner.lock().unwrap().observed
    }

    pub fn set_source(&self, source: Value) {
        self.inner.lock().unwrap().source = source;
    }

    pub fn clear(&self, source: Value) {
        let mut s = self.inner.lock().unwrap();
        s.cells.clear();
        s.journal.clear();
        s.revision = 0;
        s.epoch = new_epoch();
        s.source = source;
        s.snapshots.clear();
        s.snapshot_times.clear();
        s.observed = 0;
    }

    fn change(s: &mut Inner, journal_size: usize, record: [u8; RECORD_SIZE]) -> u64 {
        s.revision += 1;
        if s.journal.len() >= journal_size {
            s.journal.pop_front();
        }
        s.journal.push_back((s.revision, record));
        s.revision
    }

    fn delete(s: &mut Inner, journal_size: usize, key: Key) {
        s.cells.shift_remove(&key);
        let tombstone = Record::new(key.0 * 10, key.1 * 10, key.2 * 10, 0, 0, 0, 0).encode();
        Self::change(s, journal_size, tombstone);
    }

    fn expire(&self, s: &mut Inner) {
        let now = (self.clock)();
        let stale: Vec<u64> = s
            .snapshots
            .keys()
            .filter(|a| now - s.snapshot_times[a] > SNAPSHOT_TTL_S)
            .copied()
            .collect();
        for anchor in stale {
            s.snapshots.shift_remove(&anchor);
            s.snapshot_times.remove(&anchor);
        }
        let cutoff = now - self.ttl;
        // Cells are ordered by last wire refresh, at most 5 s behind receipt.
        while let Some((key, cell)) = s.cells.first() {
            if cell.refreshed > cutoff {
                break;
            }
            let key = *key;
            Self::delete(s, self.journal_size, key);
        }
    }

    /// Retain a real measured sample per cell, not an interpolated surface.
    /// Port of dense.integrate: |m| < 10000 gate, mm quantization, and
    /// np.unique(axis=0, return_index) first-sample dedupe in sorted-key order.
    pub fn integrate(&self, points: &[[f32; 3]], colors: &[[u8; 3]]) {
        let mut first: HashMap<Key, usize> = HashMap::new();
        for (i, (p, _)) in points.iter().zip(colors.iter()).enumerate() {
            if !p.iter().all(|v| v.is_finite() && v.abs() < 10000.0) {
                continue;
            }
            let mm = [
                (p[0] * 1000.0).floor() as i32,
                (p[1] * 1000.0).floor() as i32,
                (p[2] * 1000.0).floor() as i32,
            ];
            first.entry((mm[0].div_euclid(10), mm[1].div_euclid(10), mm[2].div_euclid(10)))
                .or_insert(i);
        }
        let mut kept: Vec<(Key, usize)> = first.into_iter().collect();
        // np.unique(axis=0) emits rows in sorted unique-key order.
        kept.sort_unstable_by_key(|(k, _)| *k);
        let records: Vec<Record> = kept
            .into_iter()
            .map(|(_, i)| {
                let p = points[i];
                Record::new(
                    (p[0] * 1000.0).floor() as i32,
                    (p[1] * 1000.0).floor() as i32,
                    (p[2] * 1000.0).floor() as i32,
                    colors[i][0],
                    colors[i][1],
                    colors[i][2],
                    1,
                )
            })
            .collect();
        if !records.is_empty() {
            let _ = self.apply(&records);
        }
    }

    /// Port of dense.apply: tombstones, op validation, 5 s coalescing,
    /// OrderedDict refresh order and bounded capacity eviction.
    pub fn apply(&self, records: &[Record]) -> Result<(), String> {
        let mut s = self.inner.lock().unwrap();
        self.expire(&mut s);
        let now = (self.clock)();
        for record in records {
            let key = record.key();
            if record.op == 0 {
                if s.cells.contains_key(&key) {
                    Self::delete(&mut s, self.journal_size, key);
                }
                continue;
            }
            if record.op != 1 {
                return Err("Unknown dense operation".into());
            }
            s.observed += 1;
            let encoded = record.encode();
            // Coalesce noisy refreshes; still renew remote TTL every 5 s.
            if let Some(old) = s.cells.get(&key) {
                if old.record == encoded && now - old.refreshed < REFRESH_S {
                    continue;
                }
            }
            let rev = Self::change(&mut s, self.journal_size, encoded);
            s.cells.shift_remove(&key);
            s.cells.insert(key, Cell { rev, record: encoded, refreshed: now });
        }
        while s.cells.len() > self.capacity {
            let key = *s.cells.keys().next().unwrap();
            if self.cache_only {
                // Retains points beyond Jetson's cache budget — no deletion.
                s.cells.shift_remove(&key);
            } else {
                Self::delete(&mut s, self.journal_size, key);
            }
        }
        Ok(())
    }

    /// Bounded delta/snapshot page. Port of dense.delta: epoch/journal-gap
    /// resets, anchored snapshot paging and the contiguous journal replay.
    pub fn delta(
        &self,
        epoch: Option<&str>,
        after: i64,
        snapshot: bool,
        limit: i64,
        anchor: Option<i64>,
    ) -> Result<Vec<u8>, String> {
        let mut s = self.inner.lock().unwrap();
        self.expire(&mut s);
        let limit = (limit.max(1) as usize).min(MAX_RECORDS);
        let mut after = after.max(0) as u64;
        let mut snapshot = snapshot;
        let mut anchor = anchor;
        let mut reset = epoch != Some(s.epoch.as_str()) || after > s.revision;
        if !snapshot && !s.journal.is_empty() && after + 1 < s.journal[0].0 {
            reset = true;
        }
        if snapshot
            && anchor
                .map(|a| u64::try_from(a).map(|k| !s.snapshots.contains_key(&k)).unwrap_or(true))
                .unwrap_or(false)
        {
            reset = true;
        }
        if reset {
            after = 0;
            snapshot = true;
            anchor = Some(s.revision as i64);
            // Immutable cell records let pages seek by revision rather than
            // rescanning every cell for each bounded page.
            let entries: Vec<(u64, [u8; RECORD_SIZE])> =
                s.cells.values().map(|c| (c.rev, c.record)).collect();
            let at = (self.clock)();
            let revision = s.revision;
            s.snapshots.insert(revision, entries);
            s.snapshot_times.insert(revision, at);
            while s.snapshots.len() > 2 {
                let stale = *s.snapshots.keys().next().unwrap();
                s.snapshots.shift_remove(&stale);
                s.snapshot_times.remove(&stale);
            }
        }
        if snapshot && anchor.is_none() {
            return Err("Snapshot continuation requires its journal anchor".into());
        }
        let rows: Vec<(u64, [u8; RECORD_SIZE])> = if snapshot {
            let key = anchor.unwrap() as u64;
            let Some(entries) = s.snapshots.get(&key) else {
                return Err("Snapshot continuation requires its journal anchor".into());
            };
            // entries are in cell order — non-decreasing revision.
            let start = entries.partition_point(|(rev, _)| *rev <= after);
            entries[start..(start + limit + 1).min(entries.len())].to_vec()
        } else {
            let start = if s.journal.is_empty() {
                0
            } else {
                (after as i64 - s.journal[0].0 as i64 + 1).max(0) as usize
            };
            s.journal
                .iter()
                .skip(start)
                .take(limit + 1)
                .copied()
                .collect()
        };
        let more = rows.len() > limit;
        let rows = &rows[..rows.len().min(limit)];
        let cursor = if more {
            rows.last().map(|(rev, _)| *rev as i64).unwrap_or_else(|| anchor.unwrap_or(0))
        } else if snapshot {
            anchor.unwrap_or(0)
        } else {
            s.revision as i64
        };
        let anchor_json = anchor.map(|a| a.to_string()).unwrap_or_else(|| "null".into());
        let ttl_json = if self.ttl.fract() == 0.0 {
            format!("{}", self.ttl as i64)
        } else {
            format!("{}", self.ttl)
        };
        // Key order and compact separators match Python's metadata dict.
        let metadata = format!(
            "{{\"format\":\"xyz_mm_rgb8_op\",\"epoch\":{},\"source\":{},\"cursor\":{},\"reset\":{},\"snapshot\":{},\"more\":{},\"anchor\":{},\"points\":{},\"voxel_m\":0.01,\"ttl_s\":{}}}",
            serde_json::to_string(&s.epoch).unwrap(),
            serde_json::to_string(&s.source).unwrap(),
            cursor,
            reset,
            snapshot && more,
            more,
            anchor_json,
            s.cells.len(),
            ttl_json,
        );
        let records: Vec<[u8; RECORD_SIZE]> = rows.iter().map(|(_, r)| *r).collect();
        Ok(packet(&metadata, &records))
    }
}
