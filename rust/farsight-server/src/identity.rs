//! Opt-in, local face identity: aligned embeddings and a bounded enrollment
//! gallery. Port of jetson/identity_engine.py + jetson/identity_service.py +
//! jetson/identity_api.py. A similarity is a cosine score, not a probability;
//! unknown and ambiguous faces are deliberately left unnamed. This module
//! never authorizes rover movement.

use std::io::Write;
use std::path::{Path as FsPath, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use axum::body::Body;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Request, State};
use axum::http::{header, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use indexmap::IndexMap;
use serde_json::{json, Value};

use crate::camera::CameraService;
use crate::state::now_monotonic;

const MAX_IMAGE_BYTES: usize = 4 * 1024 * 1024;
const MAX_IMAGE_SIDE: u32 = 4096;
const MAX_IMAGE_PIXELS: u64 = 12_000_000;
/// Python MAX_BODY for POST /phone/identity/people.
const MAX_BODY: usize = 6 * 1024 * 1024;
/// Base64 characters of the largest thumbnail the wire contract carries
/// (identity_api.py: len(thumbnail) <= 10924).
const MAX_THUMBNAIL_B64: usize = 10924;

/// FaceDetectorYN admission: 15-float rows [x,y,w,h, kps*10, score].
pub const FACE_ROW: usize = 15;

/// One admitted face plus its template — the service matches `embedding`
/// against the gallery and publishes the remaining public fields.
#[derive(Clone)]
pub struct DetectedFace {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
    pub conf: f32,
    pub embedding: Vec<f32>,
    pub thumbnail: Option<String>,
}

/// cv2.FaceDetectorYN parity: takes the (already downscaled) image and yields
/// raw detector rows in pixel coordinates, NMS'd by the backend.
pub trait FaceDetect: Send {
    fn set_input_size(&mut self, width: u32, height: u32);
    fn detect(&mut self, image: &image::RgbImage) -> Result<Vec<[f32; FACE_ROW]>, String>;
}

/// cv2.FaceRecognizerSF parity: similarity-aligns a detected face onto the
/// canonical 112x112 crop and extracts its template.
pub trait FaceRecognize: Send {
    fn align_crop(&mut self, image: &image::RgbImage, row: &[f32; FACE_ROW]) -> Result<image::RgbImage, String>;
    fn feature(&mut self, aligned: &image::RgbImage) -> Result<Vec<f32>, String>;
}

/// The inference surface the service drives (identity_engine.IdentityEngine).
pub trait IdentityInfer: Send {
    fn infer(&mut self, jpeg: &[u8]) -> Result<Vec<DetectedFace>, String>;
    fn enroll_embedding(&mut self, image: &[u8]) -> Result<Vec<f32>, String>;
}

// ---------- input validation (identity_engine.py) ----------

/// `_embedding`: exactly 128 finite values with a usable norm; returned
/// L2-normalized. Also accepts the [[128]] shape like np.asarray ndim 1|2.
pub fn normalize_embedding(value: &[f32]) -> Result<Vec<f32>, String> {
    if value.len() != 128 {
        return Err("Face embedding must contain 128 values".into());
    }
    let norm = value.iter().map(|v| (*v as f64) * (*v as f64)).sum::<f64>().sqrt() as f32;
    if !value.iter().all(|v| v.is_finite()) || !norm.is_finite() || norm < 1e-8 {
        return Err("Face embedding must be finite and nonzero".into());
    }
    Ok(value.iter().map(|v| v / norm).collect())
}

/// Unicode category C* approximation (unicodedata.category(c).startswith('C')):
/// Cc via char::is_control plus the Cf format ranges.
fn is_disallowed_name_char(c: char) -> bool {
    if c.is_control() {
        return true;
    }
    matches!(c as u32,
        // Cf format characters (the ranges unicodedata assigns to category C*).
        0x00AD | 0x0600..=0x0605 | 0x061C | 0x06DD | 0x070F | 0x08E2
        | 0x180E | 0x200B..=0x200F | 0x202A..=0x202E | 0x2060..=0x2064
        | 0x2066..=0x206F | 0xFEFF | 0xFFF9..=0xFFFB
        | 0x1BCA0..=0x1BCA3 | 0x1D173..=0x1D17A | 0xE0001 | 0xE0020..=0xE007F
        // Co private use.
        | 0xE000..=0xF8FF | 0xF0000..=0xFFFFD | 0x100000..=0x10FFFD)
}

/// `_name`: trimmed, 1-48 chars, no control characters.
pub fn valid_name(value: &str) -> Result<String, String> {
    let result = value.trim();
    if result.is_empty() || result.chars().count() > 48 || result.chars().any(is_disallowed_name_char) {
        return Err("Name must contain 1–48 characters without control characters".into());
    }
    Ok(result.to_string())
}

/// `_image_dimensions`: bounded header sniffing before any decoder runs.
pub fn image_dimensions(data: &[u8]) -> Result<(u32, u32), String> {
    if data.is_empty() || data.len() > MAX_IMAGE_BYTES {
        return Err("Image must be a JPEG or PNG of at most 4 MiB".into());
    }
    let (mut width, mut height) = (0u32, 0u32);
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        if data.len() < 33 || &data[8..16] != b"\0\0\0\x0DIHDR" {
            return Err("Invalid PNG header".into());
        }
        width = u32::from_be_bytes(data[16..20].try_into().unwrap());
        height = u32::from_be_bytes(data[20..24].try_into().unwrap());
    } else if data.starts_with(b"\xff\xd8") {
        let mut cursor = 2usize;
        while cursor < data.len() {
            if data[cursor] != 0xff {
                return Err("Invalid JPEG header".into());
            }
            while cursor < data.len() && data[cursor] == 0xff {
                cursor += 1;
            }
            if cursor >= data.len() {
                break;
            }
            let marker = data[cursor];
            cursor += 1;
            if marker == 0xd9 || marker == 0xda {
                break;
            }
            if marker == 0x00 || marker == 0xd8 || (0xd0..=0xd7).contains(&marker) {
                return Err("Invalid JPEG marker".into());
            }
            if cursor + 2 > data.len() {
                return Err("Truncated JPEG header".into());
            }
            let length = u16::from_be_bytes(data[cursor..cursor + 2].try_into().unwrap()) as usize;
            if length < 2 || cursor + length > data.len() {
                return Err("Truncated JPEG header".into());
            }
            if matches!(marker, 0xc0 | 0xc1 | 0xc2 | 0xc3 | 0xc5 | 0xc6 | 0xc7 | 0xc9 | 0xca | 0xcb | 0xcd | 0xce | 0xcf) {
                if length < 8 || width != 0 || height != 0 {
                    return Err("Invalid JPEG dimensions".into());
                }
                height = u16::from_be_bytes(data[cursor + 3..cursor + 5].try_into().unwrap()) as u32;
                width = u16::from_be_bytes(data[cursor + 5..cursor + 7].try_into().unwrap()) as u32;
            }
            cursor += length;
        }
    } else {
        return Err("Image must be JPEG or PNG".into());
    }
    if width < 64
        || height < 64
        || width > MAX_IMAGE_SIDE
        || height > MAX_IMAGE_SIDE
        || width as u64 * height as u64 > MAX_IMAGE_PIXELS
    {
        return Err("Image dimensions are outside supported limits".into());
    }
    Ok((width, height))
}

// ---------- engine (identity_engine.IdentityEngine) ----------

struct EngineParts {
    detector: Box<dyn FaceDetect>,
    recognizer: Box<dyn FaceRecognize>,
}

/// CPU YuNet detection + SFace alignment/recognition, serialized per engine.
pub struct IdentityEngine {
    inner: Mutex<EngineParts>,
}

impl IdentityEngine {
    pub fn new(detector: Box<dyn FaceDetect>, recognizer: Box<dyn FaceRecognize>) -> Self {
        Self { inner: Mutex::new(EngineParts { detector, recognizer }) }
    }

    #[cfg(feature = "detector")]
    pub fn from_paths(detector_path: &str, recognizer_path: &str) -> Result<Self, String> {
        let detector = crate::identity_onnx::YuNet::new(detector_path)?;
        let recognizer = crate::identity_onnx::SFace::new(recognizer_path)?;
        Ok(Self::new(Box::new(detector), Box::new(recognizer)))
    }

    #[cfg(not(feature = "detector"))]
    pub fn from_paths(_detector_path: &str, _recognizer_path: &str) -> Result<Self, String> {
        Err("Cannot load YuNet/SFace models: this build lacks the detector feature".into())
    }

    /// `_faces`: admission, alignment, embeddings and optional thumbnails.
    pub fn faces(&self, data: &[u8], enrollment: bool) -> Result<Vec<DetectedFace>, String> {
        let dimensions = image_dimensions(data)?;
        // cv2.imdecode returning None lands here too — same message either way.
        let image = image::load_from_memory(data)
            .map_err(|_| "Cannot decode face image or dimensions differ from its header".to_string())?
            .to_rgb8();
        if (image.width(), image.height()) != dimensions {
            return Err("Cannot decode face image or dimensions differ from its header".into());
        }
        let (original_width, original_height) = (dimensions.0 as f64, dimensions.1 as f64);
        let maximum_side = if enrollment { 1024.0 } else { 640.0 };
        let scale = (maximum_side / original_width.max(original_height)).min(1.0);
        let image = if scale < 1.0 {
            let w = ((original_width * scale).round() as u32).max(1);
            let h = ((original_height * scale).round() as u32).max(1);
            resize_area(&image, w, h)
        } else {
            image
        };
        let (width, height) = (image.width(), image.height());
        let mut parts = self.inner.lock().unwrap();
        // OpenCV's detector input size and recognizer are mutable; enrollment
        // and live inference share one lock here, like the Python.
        parts.detector.set_input_size(width, height);
        let rows = parts.detector.detect(&image)?;
        if enrollment && rows.len() != 1 {
            return Err("Enrollment requires exactly one visible face".into());
        }
        let mut result: Vec<DetectedFace> = Vec::new();
        let mut recognition_attempts = 0usize;
        for row in &rows {
            if !row.iter().all(|v| v.is_finite()) {
                continue;
            }
            let (x, y, box_width, box_height, score) = (row[0], row[1], row[2], row[3], row[14]);
            if score < 0.9
                || score > 1.0
                || x < 0.0
                || y < 0.0
                || box_width < 36.0
                || box_height < 36.0
                || x + box_width > width as f32
                || y + box_height > height as f32
                || box_width as f64 / width as f64 * original_width < 60.0
                || box_height as f64 / height as f64 * original_height < 60.0
            {
                continue;
            }
            let mut landmark_ok = true;
            for n in 0..5 {
                let lx = row[4 + 2 * n];
                let ly = row[4 + 2 * n + 1];
                if lx < 0.0 || lx >= width as f32 || ly < 0.0 || ly >= height as f32 {
                    landmark_ok = false;
                }
            }
            if !landmark_ok {
                continue;
            }
            if recognition_attempts >= 8 {
                break;
            }
            recognition_attempts += 1;
            let aligned = parts
                .recognizer
                .align_crop(&image, row)
                .map_err(|_| "Face model inference failed".to_string())?;
            let feature = parts
                .recognizer
                .feature(&aligned)
                .map_err(|_| "Face model inference failed".to_string())?;
            let embedding = match normalize_embedding(&feature) {
                Ok(e) => e,
                Err(_) => continue,
            };
            let mut identified = DetectedFace {
                x: x / width as f32,
                y: y / height as f32,
                w: box_width / width as f32,
                h: box_height / height as f32,
                conf: score,
                embedding,
                thumbnail: None,
            };
            // A card shows the actual face used by this inference rather than
            // attaching an old name box to a newer video frame. Enrollment
            // never generates images for the gallery.
            if !enrollment && result.len() < 2 && aligned.width() == 112 && aligned.height() == 112 {
                if let Some(jpeg) = encode_jpeg(&aligned, 65) {
                    if !jpeg.is_empty() && jpeg.len() <= 8192 {
                        identified.thumbnail =
                            Some(base64::engine::general_purpose::STANDARD.encode(jpeg));
                    }
                }
            }
            result.push(identified);
        }
        if enrollment && result.len() != 1 {
            return Err("Use one clear, front-facing face of at least 60 pixels".into());
        }
        Ok(result)
    }
}

/// cv2.resize INTER_AREA for downscale: each destination pixel averages the
/// source rectangle it covers, weighted by per-pixel overlap.
fn resize_area(src: &image::RgbImage, dst_w: u32, dst_h: u32) -> image::RgbImage {
    let (sw, sh) = (src.width() as f64, src.height() as f64);
    let inv_x = sw / dst_w as f64;
    let inv_y = sh / dst_h as f64;
    // Precompute per-axis coverage weights: (src_index, weight) per dst index.
    let weights = |dst_n: u32, src_n: u32, inv: f64| -> Vec<Vec<(u32, f64)>> {
        (0..dst_n)
            .map(|d| {
                let a = d as f64 * inv;
                let b = ((d + 1) as f64 * inv).min(src_n as f64);
                let mut spans = Vec::new();
                let mut i = a.floor() as u32;
                while (i as f64) < b && i < src_n {
                    let lo = a.max(i as f64);
                    let hi = b.min((i + 1) as f64);
                    if hi > lo {
                        spans.push((i, hi - lo));
                    }
                    i += 1;
                }
                spans
            })
            .collect()
    };
    let wx = weights(dst_w, src.width(), inv_x);
    let wy = weights(dst_h, src.height(), inv_y);
    let mut out = image::RgbImage::new(dst_w, dst_h);
    for dy in 0..dst_h {
        for dx in 0..dst_w {
            let mut acc = [0f64; 3];
            let mut total = 0f64;
            for &(sy, wy_) in &wy[dy as usize] {
                for &(sx, wx_) in &wx[dx as usize] {
                    let w = wy_ * wx_;
                    let px = src.get_pixel(sx, sy);
                    for c in 0..3 {
                        acc[c] += px[c] as f64 * w;
                    }
                    total += w;
                }
            }
            let mut px = image::Rgb([0u8; 3]);
            if total > 0.0 {
                for c in 0..3 {
                    px[c] = (acc[c] / total).round().clamp(0.0, 255.0) as u8;
                }
            }
            out.put_pixel(dx, dy, px);
        }
    }
    out
}

fn encode_jpeg(image: &image::RgbImage, quality: u8) -> Option<Vec<u8>> {
    let mut buf = Vec::new();
    let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, quality);
    image::ImageEncoder::write_image(
        enc,
        image.as_raw(),
        image.width(),
        image.height(),
        image::ExtendedColorType::Rgb8,
    )
    .ok()?;
    Some(buf)
}

impl IdentityInfer for IdentityEngine {
    fn infer(&mut self, jpeg: &[u8]) -> Result<Vec<DetectedFace>, String> {
        self.faces(jpeg, false)
    }

    fn enroll_embedding(&mut self, image: &[u8]) -> Result<Vec<f32>, String> {
        Ok(self.faces(image, true)?.remove(0).embedding)
    }
}

// ---------- gallery (identity_engine.Gallery) ----------

#[derive(Clone)]
struct Person {
    id: String,
    name: String,
    embeddings: Vec<Vec<f32>>,
}

struct GalleryState {
    people: IndexMap<String, Person>,
    generation: u64,
}

/// Small, optional persistent collection of self-enrolled face embeddings.
pub struct Gallery {
    path: Option<PathBuf>,
    max_people: usize,
    max_samples: usize,
    threshold: f32,
    margin: f32,
    /// Serializes writers through persistence; committed state stays readable
    /// while a slow disk flush is pending in the inference worker.
    mutation: Mutex<()>,
    state: RwLock<GalleryState>,
}

fn public_person(person: &Person) -> Value {
    json!({"id": person.id, "name": person.name, "samples": person.embeddings.len()})
}

impl Gallery {
    /// Gallery(path=None, max_people=20, max_samples=5, threshold=.5, margin=.08)
    pub fn new(path: Option<PathBuf>) -> Result<Self, String> {
        Self::with_limits(path, 20, 5, 0.5, 0.08)
    }

    pub fn with_limits(
        path: Option<PathBuf>,
        max_people: usize,
        max_samples: usize,
        threshold: f32,
        margin: f32,
    ) -> Result<Self, String> {
        if !(1..=100).contains(&max_people)
            || !(1..=20).contains(&max_samples)
            || !threshold.is_finite()
            || !(0.0..=1.0).contains(&threshold)
            || !margin.is_finite()
            || !(0.0..=1.0).contains(&margin)
        {
            return Err("Invalid gallery limits".into());
        }
        let gallery = Self {
            path,
            max_people,
            max_samples,
            threshold,
            margin,
            mutation: Mutex::new(()),
            state: RwLock::new(GalleryState { people: IndexMap::new(), generation: 0 }),
        };
        if gallery.path.as_ref().map(|p| p.exists()).unwrap_or(false) {
            gallery.load()?;
        }
        Ok(gallery)
    }

    pub fn generation(&self) -> u64 {
        self.state.read().unwrap().generation
    }

    pub fn people(&self) -> Vec<Value> {
        self.state.read().unwrap().people.values().map(public_person).collect()
    }

    pub fn enroll(&self, name: &str, embedding: &[f32], person_id: Option<&str>) -> Result<Value, String> {
        let name = valid_name(name)?;
        let embedding = normalize_embedding(embedding)?;
        let _mutation = self.mutation.lock().unwrap();
        let (updated, public) = {
            let state = self.state.read().unwrap();
            let mut updated = state.people.clone();
            let (person, id) = match person_id {
                None => {
                    if updated.len() >= self.max_people {
                        return Err("Enrollment gallery is full".into());
                    }
                    let id = uuid::Uuid::new_v4().to_string();
                    (
                        Person { id: id.clone(), name: name.clone(), embeddings: Vec::new() },
                        id,
                    )
                }
                Some(id) => {
                    let Some(existing) = updated.get(id) else {
                        return Err("Person does not exist".into());
                    };
                    let mut person = existing.clone();
                    if person
                        .embeddings
                        .iter()
                        .map(|sample| dot(&embedding, sample))
                        .fold(f32::NEG_INFINITY, f32::max)
                        < self.threshold
                    {
                        return Err("New sample does not match this person's enrolled face".into());
                    }
                    person.name = name.clone();
                    (person, id.to_string())
                }
            };
            let mut person = person;
            if person.embeddings.len() >= self.max_samples {
                return Err("This person already has the maximum number of samples".into());
            }
            person.embeddings.push(embedding.clone());
            updated.insert(id, person.clone());
            (updated, public_person(&person))
        };
        self.persist(&updated)?;
        let mut state = self.state.write().unwrap();
        state.people = updated;
        state.generation += 1;
        Ok(public)
    }

    pub fn remove(&self, person_id: &str) -> Result<bool, String> {
        let _mutation = self.mutation.lock().unwrap();
        let updated = {
            let state = self.state.read().unwrap();
            if !state.people.contains_key(person_id) {
                return Ok(false);
            }
            let mut updated = state.people.clone();
            updated.shift_remove(person_id);
            updated
        };
        self.persist(&updated)?;
        let mut state = self.state.write().unwrap();
        state.people = updated;
        state.generation += 1;
        Ok(true)
    }

    /// Highest-cosine match with an ambiguity margin; a name is only returned
    /// for a clear winner — never a probability.
    pub fn match_person(&self, embedding: &[f32]) -> Option<Value> {
        let query = normalize_embedding(embedding).ok()?;
        let state = self.state.read().unwrap();
        let mut ranked: Vec<(f32, &Person)> = state
            .people
            .values()
            .map(|person| {
                (
                    person
                        .embeddings
                        .iter()
                        .map(|sample| dot(&query, sample))
                        .fold(f32::NEG_INFINITY, f32::max),
                    person,
                )
            })
            .collect();
        ranked.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        if ranked.is_empty() || ranked[0].0 < self.threshold {
            return None;
        }
        let (score, person) = ranked[0];
        if ranked.len() > 1 && score - ranked[1].0 < self.margin {
            return None;
        }
        Some(json!({
            "person_id": person.id,
            "name": person.name,
            "similarity": score.clamp(-1.0, 1.0),
        }))
    }

    fn load(&self) -> Result<(), String> {
        (|| -> Result<(), String> {
            let raw = {
                let path = self.path.as_ref().unwrap();
                let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
                let mut buf = Vec::new();
                use std::io::Read;
                file.take(1024 * 1024 + 1).read_to_end(&mut buf).map_err(|e| e.to_string())?;
                buf
            };
            if raw.len() > 1024 * 1024 {
                return Err("Enrollment gallery is too large".into());
            }
            let data: Value = serde_json::from_slice(&raw).map_err(|e| e.to_string())?;
            if !data.is_object() || data["version"].as_i64() != Some(1) || !data["people"].is_array() {
                return Err("Invalid enrollment gallery format".into());
            }
            let people = data["people"].as_array().unwrap();
            if people.len() > self.max_people {
                return Err("Enrollment gallery exceeds person limit".into());
            }
            let mut result: IndexMap<String, Person> = IndexMap::new();
            for item in people {
                let person_id = item["id"].as_str().ok_or("Invalid enrollment person ID")?;
                let canonical = uuid::Uuid::parse_str(person_id)
                    .map(|u| u.to_string() == person_id)
                    .unwrap_or(false);
                if !canonical || result.contains_key(person_id) {
                    return Err("Invalid enrollment person ID".into());
                }
                let samples = item["embeddings"]
                    .as_array()
                    .ok_or("Invalid enrollment sample count")?;
                if samples.is_empty() || samples.len() > self.max_samples {
                    return Err("Invalid enrollment sample count".into());
                }
                let mut embeddings = Vec::with_capacity(samples.len());
                for sample in samples {
                    embeddings.push(normalize_embedding(&json_embedding(sample)?)?);
                }
                result.insert(
                    person_id.to_string(),
                    Person {
                        id: person_id.to_string(),
                        name: valid_name(item["name"].as_str().ok_or("Enter a name")?)?,
                        embeddings,
                    },
                );
            }
            self.state.write().unwrap().people = result;
            Ok(())
        })()
        .map_err(|_| "Cannot load enrollment gallery; recognition remains disabled".to_string())
    }

    fn persist(&self, people: &IndexMap<String, Person>) -> Result<(), String> {
        let Some(path) = &self.path else { return Ok(()) };
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(FsPath::new("."));
        std::fs::create_dir_all(parent).map_err(|_| "Cannot save enrollment gallery".to_string())?;
        let payload = json!({
            "version": 1,
            "people": people.values().map(|p| json!({
                "id": p.id, "name": p.name,
                "embeddings": p.embeddings,
            })).collect::<Vec<_>>(),
        });
        let file_name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("gallery.json");
        let temporary = parent.join(format!(".identity-{}-{file_name}", uuid::Uuid::new_v4()));
        let write_result = (|| -> Result<(), String> {
            let mut output = std::fs::File::create(&temporary).map_err(|e| e.to_string())?;
            output
                .write_all(payload.to_string().as_bytes())
                .and_then(|_| output.flush())
                .and_then(|_| output.sync_all())
                .map_err(|e| e.to_string())?;
            drop(output);
            std::fs::rename(&temporary, path).map_err(|e| e.to_string())?;
            Ok(())
        })();
        if write_result.is_err() {
            let _ = std::fs::remove_file(&temporary);
        }
        write_result.map_err(|_| "Cannot save enrollment gallery".to_string())
    }
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b.iter()).map(|(x, y)| x * y).sum()
}

/// Gallery embeddings on disk accept [128] or [[128]] like np.asarray.
fn json_embedding(sample: &Value) -> Result<Vec<f32>, String> {
    // np.asarray + reshape(128): accept [128], [[128]] or a 128-cell 2-D shape.
    fn leaves(v: &Value, depth: usize, out: &mut Vec<f32>) -> bool {
        match v {
            Value::Array(arr) if depth < 2 => {
                arr.iter().all(|x| leaves(x, depth + 1, out))
            }
            Value::Array(_) => false,
            _ => {
                out.push(v.as_f64().map(|f| f as f32).unwrap_or(f32::NAN));
                true
            }
        }
    }
    let mut flat = Vec::new();
    if !sample.is_array() || !leaves(sample, 0, &mut flat) {
        return Err("Invalid face embedding".into());
    }
    Ok(flat)
}

// ---------- service (identity_service.IdentityService) ----------

const MAX_AGE_MS: i64 = 1000;

pub struct IdentityService {
    pub camera: Arc<CameraService>,
    pub detector_path: Option<String>,
    pub recognizer_path: Option<String>,
    pub gallery_path: Option<String>,
    pub fps: f64,
    pub engine: Arc<Mutex<Option<Box<dyn IdentityInfer>>>>,
    pub gallery: Arc<RwLock<Gallery>>,
    pub error: Mutex<Option<String>>,
    /// One inference at a time (the Python asyncio.Lock + single-worker
    /// executor). Held by the run loop and by enrollment alike.
    pub gate: Arc<tokio::sync::Mutex<()>>,
    /// One bounded priority waiter; all other uploads fail busy.
    pub enrolling: AtomicBool,
    pub packet: Mutex<Option<Value>>,
    pub binding: Mutex<Option<Arc<crate::capture_permits::CapturePermits>>>,
    previous: Mutex<Option<(String, i64, u64, usize)>>,
    closed: AtomicBool,
    task: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl IdentityService {
    /// IdentityService(camera, yunet, sface, gallery, fps=3); fps clamps 1..=5.
    pub fn new(
        camera: Arc<CameraService>,
        detector_path: Option<String>,
        recognizer_path: Option<String>,
        gallery_path: Option<String>,
        fps: f64,
    ) -> Self {
        Self {
            camera,
            detector_path,
            recognizer_path,
            gallery_path,
            fps: fps.clamp(1.0, 5.0),
            engine: Arc::new(Mutex::new(None)),
            gallery: Arc::new(RwLock::new(Gallery::new(None).expect("default gallery"))),
            error: Mutex::new(None),
            gate: Arc::new(tokio::sync::Mutex::new(())),
            enrolling: AtomicBool::new(false),
            packet: Mutex::new(None),
            binding: Mutex::new(None),
            previous: Mutex::new(None),
            closed: AtomicBool::new(false),
            task: Mutex::new(None),
        }
    }

    fn setup(&self) -> Result<(), String> {
        if let Some(path) = &self.gallery_path {
            *self.gallery.write().unwrap() = Gallery::new(Some(PathBuf::from(path)))?;
        }
        let engine = IdentityEngine::from_paths(
            self.detector_path.as_deref().unwrap_or(""),
            self.recognizer_path.as_deref().unwrap_or(""),
        )?;
        *self.engine.lock().unwrap() = Some(Box::new(engine));
        Ok(())
    }

    /// Mirrors start(): no models configured stays silently disabled; a bad
    /// model leaves the rest of the server up with `error` set.
    pub fn start(self: &Arc<Self>) {
        if self.closed.load(Ordering::Relaxed) || self.task.lock().unwrap().is_some() {
            return;
        }
        if self.engine.lock().unwrap().is_none()
            && self.detector_path.is_none()
            && self.recognizer_path.is_none()
        {
            return;
        }
        if self.engine.lock().unwrap().is_none() {
            let result = if !(self.detector_path.is_some() && self.recognizer_path.is_some()) {
                Err("Configure both YuNet and SFace models".to_string())
            } else {
                self.setup()
            };
            if let Err(e) = result {
                *self.error.lock().unwrap() = Some(e.chars().take(240).collect());
                return;
            }
        }
        let service = Arc::clone(self);
        let handle = std::thread::spawn(move || service.run());
        *self.task.lock().unwrap() = Some(handle);
    }

    fn run(self: &Arc<Self>) {
        while !self.closed.load(Ordering::Relaxed) {
            let started = now_monotonic();
            self.process_once();
            let wait = (1.0 / self.fps - (now_monotonic() - started)).max(0.01);
            std::thread::sleep(Duration::from_secs_f64(wait));
        }
    }

    /// engine is None only before setup; a held lock means inference is
    /// running, which still counts as ready (Python `self.engine is not
    /// None` never waits on inference).
    fn engine_present(&self) -> bool {
        match self.engine.try_lock() {
            Ok(guard) => guard.is_some(),
            Err(_) => true,
        }
    }

    pub fn status(&self) -> Value {
        let engine_ready = self.engine_present();
        json!({
            "enabled": engine_ready || self.detector_path.is_some() || self.recognizer_path.is_some(),
            "ready": engine_ready && !self.closed.load(Ordering::Relaxed),
            "error": self.error.lock().unwrap().clone(),
            "people": self.gallery.read().unwrap().people().len(),
            "fps": self.fps,
            "gallery_generation": self.gallery.read().unwrap().generation(),
        })
    }

    pub fn people(&self) -> Vec<Value> {
        self.gallery.read().unwrap().people()
    }

    /// Enrollment: one bounded waiter, then straight into the single worker
    /// slot — a continuous camera cannot starve it, extra uploads fail busy.
    pub async fn enroll(
        self: &Arc<Self>,
        name: String,
        image: Vec<u8>,
        person_id: Option<String>,
    ) -> Result<Value, String> {
        if self.closed.load(Ordering::Relaxed) || !self.engine_present() {
            return Err("Face recognition is not ready".into());
        }
        if self.enrolling.swap(true, Ordering::SeqCst) {
            return Err("Face recognition is busy; retry in a moment".into());
        }
        let guard = match tokio::time::timeout(
            Duration::from_secs(1),
            Arc::clone(&self.gate).lock_owned(),
        )
        .await
        {
            Ok(g) => g,
            Err(_) => {
                self.enrolling.store(false, Ordering::SeqCst);
                return Err("Face recognition is busy; retry in a moment".into());
            }
        };
        if self.closed.load(Ordering::Relaxed) {
            drop(guard);
            self.enrolling.store(false, Ordering::SeqCst);
            return Err("Face recognition is not ready".into());
        }
        // Keep the single-worker gate held even if the awaiting request
        // cancels: the guard and the busy flag live inside the blocking task,
        // so a dropped future never frees the inference slot early.
        let (tx, rx) = tokio::sync::oneshot::channel();
        let service = Arc::clone(self);
        tokio::task::spawn_blocking(move || {
            let _gate = guard;
            let result = (|| -> Result<Value, String> {
                let embedding = {
                    let mut engine = service.engine.lock().unwrap();
                    engine
                        .as_mut()
                        .ok_or("Face recognition is not ready")?
                        .enroll_embedding(&image)?
                };
                let record = service
                    .gallery
                    .read()
                    .unwrap()
                    .enroll(&name, &embedding, person_id.as_deref())?;
                Ok(record)
            })();
            if result.is_ok() {
                *service.packet.lock().unwrap() = None;
            }
            service.enrolling.store(false, Ordering::SeqCst);
            let _ = tx.send(result);
        });
        match rx.await {
            Ok(result) => result,
            Err(_) => Err("Face recognition worker failed".into()),
        }
    }

    pub fn remove(&self, person_id: &str) -> Result<bool, String> {
        let removed = self.gallery.read().unwrap().remove(person_id)?;
        if removed {
            *self.packet.lock().unwrap() = None;
        }
        Ok(removed)
    }

    /// Mirrors process_once: skip when busy/enrolling/closed or the camera
    /// frame is stale or already processed; a removed enrollment cannot
    /// reappear from an in-flight result (generation re-check).
    pub fn process_once(&self) {
        if self.closed.load(Ordering::Relaxed)
            || !self.engine_present()
            || self.enrolling.load(Ordering::SeqCst)
        {
            return;
        }
        let _gate = match Arc::clone(&self.gate).try_lock_owned() {
            Ok(g) => g,
            Err(_) => return,
        };
        let Some(frame) = self.camera.snapshot() else { return };
        if frame.age_ms() >= MAX_AGE_MS {
            return;
        }
        let identity = (
            frame.source.clone(),
            frame.frame_id,
            frame.received.to_bits(),
            frame.capture_binding.as_ref().map(|b| Arc::as_ptr(b) as usize).unwrap_or(0),
        );
        {
            let mut previous = self.previous.lock().unwrap();
            if previous.as_ref() == Some(&identity) {
                return;
            }
            *previous = Some(identity);
        }
        let generation = self.gallery.read().unwrap().generation();
        let started = now_monotonic();
        let (mut faces, error) = {
            let mut engine = self.engine.lock().unwrap();
            match engine.as_mut().unwrap().infer(&frame.jpeg) {
                Ok(detected) => {
                    let mut faces = Vec::new();
                    for box_ in detected.iter().take(8) {
                        let mut face = json!({
                            "x": box_.x, "y": box_.y, "w": box_.w, "h": box_.h,
                            "conf": box_.conf,
                        });
                        match self.gallery.read().unwrap().match_person(&box_.embedding) {
                            Some(m) => {
                                face["name"] = m["name"].clone();
                                face["person_id"] = m["person_id"].clone();
                                face["similarity"] = m["similarity"].clone();
                            }
                            None => {
                                face["name"] = json!("Unknown");
                                face["person_id"] = Value::Null;
                                face["similarity"] = Value::Null;
                            }
                        }
                        if faces.len() < 2 {
                            if let Some(thumb) = &box_.thumbnail {
                                if thumb.len() <= MAX_THUMBNAIL_B64 {
                                    face["thumbnail"] = json!(thumb);
                                }
                            }
                        }
                        faces.push(face);
                    }
                    (faces, None)
                }
                Err(e) => (Vec::new(), Some(e.chars().take(240).collect::<String>())),
            }
        };
        // A removed enrollment cannot reappear from an in-flight result.
        if generation != self.gallery.read().unwrap().generation() {
            faces = Vec::new();
        }
        *self.error.lock().unwrap() = error;
        *self.binding.lock().unwrap() = frame.capture_binding.clone();
        *self.packet.lock().unwrap() = Some(json!({
            "type": "identity",
            "source": frame.source,
            "frame": frame.frame_id,
            "received": frame.received,
            "faces": faces,
            "gallery_generation": generation,
            "infer_ms": (((now_monotonic() - started) * 1000.0) * 10.0).round() / 10.0,
        }));
    }

    /// Mirrors snapshot(): the packet always carries its original `received`;
    /// freshness and age are computed per read and never renewed by sends.
    pub fn snapshot(&self) -> Value {
        let stored = self.packet.lock().unwrap().clone().unwrap_or_else(|| {
            json!({
                "type": "identity", "source": Value::Null, "frame": Value::Null,
                "received": Value::Null, "faces": [],
                "gallery_generation": self.gallery.read().unwrap().generation(),
                "infer_ms": Value::Null,
            })
        });
        let mut packet = stored;
        let frame = self.camera.snapshot();
        let received = packet["received"].as_f64();
        let age = received.map(|r| ((now_monotonic() - r) * 1000.0).round().max(0.0) as i64);
        let binding = self.binding.lock().unwrap().clone();
        let closed = self.closed.load(Ordering::Relaxed);
        let fresh = !closed
            && frame.is_some()
            && age.map(|a| a < MAX_AGE_MS).unwrap_or(false)
            && frame.as_ref().map(|f| {
                f.age_ms() < MAX_AGE_MS
                    && f.source == packet["source"].as_str().unwrap_or("")
                    && packet["frame"].as_i64().map(|pf| f.frame_id >= pf).unwrap_or(false)
                    && packet["received"].as_f64().map(|pr| f.received >= pr).unwrap_or(false)
                    && same_binding(&f.capture_binding, &binding)
                    && (!f.capture_verified
                        || binding.as_ref().map(|b| b.is_active()).unwrap_or(false))
            }).unwrap_or(false)
            && packet["gallery_generation"].as_u64() == Some(self.gallery.read().unwrap().generation());
        packet["age_ms"] = json!(age);
        packet["fresh"] = json!(fresh);
        packet["error"] = json!(self.error.lock().unwrap().clone());
        if !fresh {
            packet["faces"] = json!([]);
        }
        packet
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Some(task) = self.task.lock().unwrap().take() {
            let _ = task.join();
        }
        *self.packet.lock().unwrap() = None;
    }
}

fn same_binding(
    a: &Option<Arc<crate::capture_permits::CapturePermits>>,
    b: &Option<Arc<crate::capture_permits::CapturePermits>>,
) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(x), Some(y)) => Arc::ptr_eq(x, y),
        _ => false,
    }
}

// ---------- API (identity_api.identity_router) ----------

fn detail(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"detail": message}))).into_response()
}

async fn enroll_page() -> Response {
    let mut response = Html(include_str!("../../../jetson/identity_enroll.html")).into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response
}

async fn identity_status(State(s): State<Arc<IdentityService>>) -> Json<Value> {
    Json(s.status())
}

async fn identity_people(State(s): State<Arc<IdentityService>>) -> Json<Value> {
    let mut payload = s.status();
    payload["people"] = json!(s.people());
    Json(payload)
}

async fn identity_enroll(State(s): State<Arc<IdentityService>>, request: Request) -> Response {
    // Streaming limit applies before parsing JSON or allocating decoded images.
    let body = request.into_body();
    let raw_body = match axum::body::to_bytes(body, MAX_BODY).await {
        Ok(b) => b,
        Err(_) => return detail(StatusCode::PAYLOAD_TOO_LARGE, "Enrollment upload is too large"),
    };
    let parse = || -> Result<(String, String, Option<String>, Vec<u8>), String> {
        let payload: Value = serde_json::from_slice(&raw_body)
            .map_err(|e| e.to_string())?;
        if !payload.is_object() || payload["consent"] != json!(true) {
            return Err("Confirm that this is your own face and you agree to enrollment".into());
        }
        let name = payload["name"].as_str();
        let image = payload["image"].as_str();
        let person_id = payload.get("person_id");
        let (Some(name), Some(image)) = (name, image) else {
            return Err("Provide a name and a camera photo".into());
        };
        let person_id = match person_id {
            None | Some(Value::Null) => None,
            Some(v) => Some(
                v.as_str()
                    .ok_or("Invalid enrollment ID".to_string())?
                    .to_string(),
            ),
        };
        let raw = base64::engine::general_purpose::STANDARD
            .decode(image)
            .map_err(|e| e.to_string())?;
        if raw.is_empty() || raw.len() > MAX_IMAGE_BYTES {
            return Err("Photo must be between 1 byte and 4 MiB".into());
        }
        Ok((name.to_string(), image.to_string(), person_id, raw))
    };
    let (name, _image, person_id, raw) = match parse() {
        Ok(v) => v,
        Err(e) => return detail(StatusCode::UNPROCESSABLE_ENTITY, &e),
    };
    let status = s.status();
    if !status["ready"].as_bool().unwrap_or(false) {
        return detail(
            StatusCode::SERVICE_UNAVAILABLE,
            status["error"]
                .as_str()
                .unwrap_or("Face recognition models are not configured"),
        );
    }
    match s.enroll(name, raw, person_id).await {
        Ok(record) => Json(record).into_response(),
        Err(e) => detail(
            if e.contains("busy") { StatusCode::TOO_MANY_REQUESTS } else { StatusCode::UNPROCESSABLE_ENTITY },
            &e,
        ),
    }
}

async fn identity_remove(
    State(s): State<Arc<IdentityService>>,
    Path(person_id): Path<String>,
) -> Response {
    let service = s.clone();
    let removed = match tokio::task::spawn_blocking(move || service.remove(&person_id)).await {
        Ok(r) => r,
        Err(_) => Err("Face recognition worker failed".into()),
    };
    match removed {
        Err(e) => detail(StatusCode::SERVICE_UNAVAILABLE, &e),
        Ok(false) => detail(StatusCode::NOT_FOUND, "Enrollment not found"),
        Ok(true) => Json(json!({"removed": true})).into_response(),
    }
}

async fn ws_identity(ws: WebSocketUpgrade, State(s): State<Arc<IdentityService>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_identity(socket, s))
}

/// identity_api.identities: 5 Hz checks, sends on change or a 2 s heartbeat.
/// Repeated heartbeats never refresh the observation age (snapshot() does
/// that math per call).
async fn handle_ws_identity(socket: WebSocket, s: Arc<IdentityService>) {
    let (mut tx, mut rx) = socket.split();
    let mut previous: Option<Value> = None;
    let mut last_sent = 0.0f64;
    let mut interval = tokio::time::interval(Duration::from_millis(200));
    loop {
        tokio::select! {
            _ = interval.tick() => {
                let packet = s.snapshot();
                let key = json!([
                    packet["source"], packet["frame"], packet["received"],
                    packet["gallery_generation"], packet["fresh"], packet["error"],
                ]);
                let now = now_monotonic();
                if previous.as_ref() != Some(&key) || now - last_sent >= 2.0 {
                    let sent = tokio::time::timeout(
                        Duration::from_millis(500),
                        tx.send(Message::Text(packet.to_string().into())),
                    ).await;
                    if sent.map(|r| r.is_err()).unwrap_or(true) {
                        return;
                    }
                    previous = Some(key);
                    last_sent = now;
                }
            }
            msg = rx.next() => {
                match msg {
                    Some(Ok(Message::Close(_))) | None => return,
                    Some(Err(_)) => return,
                    Some(_) => {}
                }
            }
        }
    }
}

/// The identity router; merged into the app and always mounted (like the
/// Python include_router — a disabled service still answers these routes).
pub fn router<S>(service: Arc<IdentityService>) -> Router<S>
where
    S: Clone + Send + Sync + 'static,
{
    Router::new()
        .route("/phone/identity/enroll", get(enroll_page))
        .route("/phone/identity/status", get(identity_status))
        .route("/phone/identity/people", get(identity_people))
        .route("/phone/identity/people", post(identity_enroll))
        .route("/phone/identity/people/{person_id}", delete(identity_remove))
        .route("/ws/identity", get(ws_identity))
        .with_state(service)
}

#[allow(dead_code)]
fn _unused(_: Body) {}
