//! Optional read-only same-frame RGB-D export for the desktop
//! reconstruction lab (jetson/mapping/raw_capture.py port).
//!
//! Mounted only when `FARSIGHT_RAW_CAPTURE=1`; the module alone is inactive.
//! The observer retains ONE accepted frame by clone — no encoding or fusion
//! on the ingest path — and `GET /phone/frame.bin` serves it in the
//! documented wire layout: u32le JSON-header length + header + native JPEG +
//! depth f4 + confidence u8.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use serde_json::{json, Value};

use crate::phone::RgbdFrame;
use crate::state::{now_monotonic, MapState};

/// One retained accepted frame, slimmed to what the wire body needs.
struct Held {
    header: Value,
    jpeg: Bytes,
    depth: Arc<Vec<f32>>,
    confidence: Arc<Vec<u8>>,
    generation: u64,
}

/// What `snapshot` yields on a hit: the body plus the three wire headers.
pub struct Snapshot {
    pub body: Option<Bytes>,
    pub key: String,
    pub age_ms: f64,
}

/// Failure mapped to the same HTTPException statuses as Python.
#[derive(Debug)]
pub enum SnapError {
    Waiting,        // 503 'Waiting for accepted RGB-D'
    Unavailable,    // 503 'RGB-D capture scope unavailable'
    Stale,          // 503 'RGB-D capture stale'
    ScopeChanged,   // 503 'RGB-D scope changed during export'
    TooLarge,       // 413 'RGB-D capture exceeds 8 MiB'
    BadReceipt,     // 500 — adjusted_receipt raised upstream
}

impl SnapError {
    pub fn status(self) -> (u16, &'static str) {
        match self {
            SnapError::Waiting => (503, "Waiting for accepted RGB-D"),
            SnapError::Unavailable => (503, "RGB-D capture scope unavailable"),
            SnapError::Stale => (503, "RGB-D capture stale"),
            SnapError::ScopeChanged => (503, "RGB-D scope changed during export"),
            SnapError::TooLarge => (413, "RGB-D capture exceeds 8 MiB"),
            SnapError::BadReceipt => (500, "RGB-D receipt invalid"),
        }
    }
}

/// Key-keyed body plus its negotiated variants (compress.py EncodedCache:
/// a new capture key drops the old entry wholesale).
struct CachedBody {
    key: String,
    raw: Bytes,
    variants: HashMap<&'static str, Bytes>,
}

pub struct RawCapture {
    state: Arc<MapState>,
    latest: Mutex<Option<Held>>,
    cached: Mutex<Option<CachedBody>>,
}

impl RawCapture {
    pub fn new(state: Arc<MapState>) -> Arc<Self> {
        Arc::new(Self {
            state,
            latest: Mutex::new(None),
            cached: Mutex::new(None),
        })
    }

    /// Called after accepted-frame validation via frame_observers. Retains
    /// one immutable observation; the clone is the only copy on this path.
    pub fn observe(&self, frame: &RgbdFrame) {
        let s = self.state.inner.lock().unwrap();
        if frame.header["_mapping_socket_generation"].as_u64() != Some(s.socket_generation)
            || !s.connected
        {
            return;
        }
        let generation = s.generation;
        drop(s);
        *self.latest.lock().unwrap() = Some(Held {
            header: frame.header.clone(),
            jpeg: Bytes::copy_from_slice(&frame.jpeg),
            depth: Arc::new(frame.depth.clone()),
            confidence: Arc::new(frame.confidence.clone()),
            generation,
        });
    }

    pub fn snapshot(&self, after: Option<&str>) -> Result<Snapshot, SnapError> {
        let (header, generation, jpeg, depth, confidence) = {
            let held = self.latest.lock().unwrap();
            let h = held.as_ref().ok_or(SnapError::Waiting)?;
            (
                h.header.clone(),
                h.generation,
                h.jpeg.clone(),
                h.depth.clone(),
                h.confidence.clone(),
            )
        };
        let session_id = header["session_id"].as_str().unwrap_or("").to_string();
        let map_epoch = header["map_epoch"].as_u64().unwrap_or(0);
        let frame_id = header["frame_id"].as_i64().unwrap_or(0);
        let scope = {
            let s = self.state.inner.lock().unwrap();
            s.generation == generation
                && s.connected
                && s.identity.as_ref() == Some(&(session_id.clone(), map_epoch))
                && header["_mapping_socket_generation"].as_u64() == Some(s.socket_generation)
                && !s.paused
                && header["tracking"].as_str() == Some("normal")
                && s.tracking == "normal"
        };
        if !scope {
            return Err(SnapError::Unavailable);
        }
        let received = header["_received_monotonic"].as_f64().unwrap_or(0.0);
        let receipt =
            crate::camera::adjusted_receipt(&header, received).map_err(|_| SnapError::BadReceipt)?;
        let age = now_monotonic() - receipt;
        if !(0.0..=2.0).contains(&age) {
            return Err(SnapError::Stale);
        }
        let key = format!("{session_id}:{map_epoch}:{generation}:{frame_id}");
        let age_ms = (age * 1000.0 * 100.0).round() / 100.0;
        if after == Some(key.as_str()) {
            return Ok(Snapshot {
                body: None,
                key,
                age_ms,
            });
        }
        if let Some(cached) = self.cached.lock().unwrap().as_ref() {
            if cached.key == key {
                return Ok(Snapshot {
                    body: Some(cached.raw.clone()),
                    key,
                    age_ms,
                });
            }
        }
        // Encode once per capture key: non-underscore header fields plus
        // map_generation, capture_source and image.jpeg_len; then native
        // JPEG + little-endian f4 depth + u8 confidence verbatim.
        let mut out_header = serde_json::Map::new();
        if let Some(obj) = header.as_object() {
            for (k, v) in obj.iter() {
                if !k.starts_with('_') {
                    out_header.insert(k.clone(), v.clone());
                }
            }
        }
        out_header.insert("map_generation".into(), json!(generation));
        out_header.insert(
            "capture_source".into(),
            json!(if self.state.demo { "SIMULATED" } else { "IPHONE LIDAR" }),
        );
        let mut image = out_header
            .get("image")
            .cloned()
            .unwrap_or_else(|| json!({}));
        image["jpeg_len"] = json!(jpeg.len());
        out_header.insert("image".into(), image);
        let encoded = serde_json::to_vec(&Value::Object(out_header)).unwrap_or_default();
        let mut body = Vec::with_capacity(4 + encoded.len() + jpeg.len() + depth.len() * 4 + confidence.len());
        body.extend_from_slice(&(encoded.len() as u32).to_le_bytes());
        body.extend_from_slice(&encoded);
        body.extend_from_slice(&jpeg);
        for d in depth.iter() {
            body.extend_from_slice(&d.to_le_bytes());
        }
        body.extend_from_slice(&confidence);
        if body.len() > 8 * 1024 * 1024 {
            return Err(SnapError::TooLarge);
        }
        let body = Bytes::from(body);
        // Post-encode scope re-check (Python's second state.lock pass):
        // a reset/disconnect mid-export must not publish into a new scope.
        {
            let s = self.state.inner.lock().unwrap();
            if s.generation != generation
                || !s.connected
                || header["_mapping_socket_generation"].as_u64() != Some(s.socket_generation)
            {
                return Err(SnapError::ScopeChanged);
            }
        }
        *self.cached.lock().unwrap() = Some(CachedBody {
            key: key.clone(),
            raw: body.clone(),
            variants: HashMap::new(),
        });
        Ok(Snapshot {
            body: Some(body),
            key,
            age_ms,
        })
    }

    /// compress.py EncodedCache.get('raw', ...) parity: br then gzip by
    /// q-value (wildcard counts), encoded once per capture key, 1 KiB floor.
    pub fn encoded_body(&self, key: &str, raw: Bytes, accept: &str) -> (Bytes, &'static str) {
        let mut cached = self.cached.lock().unwrap();
        if cached.as_ref().map(|c| c.key != key).unwrap_or(true) {
            *cached = Some(CachedBody {
                key: key.to_string(),
                raw: raw.clone(),
                variants: HashMap::new(),
            });
        }
        let entry = cached.as_mut().unwrap();
        let coding = negotiate(accept);
        if coding.is_empty() || entry.raw.len() < 1024 {
            return (entry.raw.clone(), "");
        }
        let body = entry
            .variants
            .entry(coding)
            .or_insert_with(|| encode(coding, &entry.raw));
        (body.clone(), coding)
    }
}

/// compress.negotiate: iterates ("br","gzip") in order; an explicit offer or
/// a positive wildcard wins, q=0 refuses. Only this subset — the raw route
/// never negotiates zstd (unlike the map packet paths).
fn negotiate(accept: &str) -> &'static str {
    let mut offered: HashMap<String, f64> = HashMap::new();
    for part in accept.split(',') {
        let mut seg = part.trim().splitn(2, ';');
        let name = seg.next().unwrap_or("").trim().to_ascii_lowercase();
        let params = seg.next().unwrap_or("").trim().replace(' ', "");
        let mut q = 1.0;
        if params.starts_with("q=") {
            q = params[2..].parse::<f64>().unwrap_or(0.0);
        }
        if !name.is_empty() {
            offered.insert(name, q);
        }
    }
    let wildcard = offered.get("*").copied().unwrap_or(0.0);
    for coding in ["br", "gzip"] {
        if offered.get(coding).copied().unwrap_or(wildcard) > 0.0 {
            return coding;
        }
    }
    ""
}

fn encode(coding: &str, body: &[u8]) -> Bytes {
    let out = match coding {
        "br" => crate::state::brotli_compress(body),
        _ => crate::state::gzip_compress(body),
    };
    Bytes::from(out)
}
