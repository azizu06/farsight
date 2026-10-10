//! ONNX YOLO detection (feature `detector`) — port of the cv2.dnn path in
//! jetson/perception.py: 640-letterbox, 1/255 RGB blob, xywh+scores decode,
//! per-class NMS (conf .35, IoU .45). Requires an ONNX Runtime shared lib at
//! runtime (ort `load-dynamic`).

#![cfg(feature = "detector")]

use std::path::Path;

use image::imageops::FilterType;
use serde_json::{json, Value};

const SIDE: usize = 640;
const CONFIDENCE: f32 = 0.35;
const NMS_IOU: f32 = 0.45;

pub struct OnnxDetector {
    session: ort::session::Session,
    labels: Option<Vec<String>>,
}

impl OnnxDetector {
    pub fn new(model: &str) -> Result<Self, String> {
        let session = ort::session::Session::builder()
            .map_err(|e| e.to_string())?
            .commit_from_file(model)
            .map_err(|e| e.to_string())?;
        // Optional labels file: <model>.labels.json, bounded string list.
        let label_path = Path::new(model).with_extension("labels.json");
        let labels = if label_path.exists() {
            match std::fs::read_to_string(&label_path)
                .ok()
                .and_then(|t| serde_json::from_str::<Vec<String>>(&t).ok())
            {
                Some(list) if list.len() <= 256 && list.iter().all(|x| x.len() <= 100) => Some(list),
                _ => return Err("Detector labels must be a bounded string list".into()),
            }
        } else {
            None
        };
        Ok(Self { session, labels })
    }

    /// JPEG bytes -> normalized detection boxes.
    pub fn detect(&mut self, jpeg: &[u8]) -> Result<Vec<Value>, String> {
        let image = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg)
            .map_err(|_| "Invalid detector camera image".to_string())?
            .to_rgb8();
        let (w0, h0) = (image.width() as usize, image.height() as usize);
        let scale = (SIDE as f32 / w0 as f32).min(SIDE as f32 / h0 as f32);
        let (rw, rh) = (((w0 as f32 * scale).round() as usize).max(1), ((h0 as f32 * scale).round() as usize).max(1));
        let resized = image::imageops::resize(&image, rw as u32, rh as u32, FilterType::Triangle);
        // blobFromImage(canvas, 1/255, 640x640, swapRB=True): RGB channels, /255.
        let mut blob = vec![114.0f32 / 255.0; 3 * SIDE * SIDE];
        let pad_x = (SIDE - rw) / 2;
        let pad_y = (SIDE - rh) / 2;
        for y in 0..rh {
            for x in 0..rw {
                let px = resized.get_pixel(x as u32, y as u32);
                for c in 0..3 {
                    blob[c * SIDE * SIDE + (y + pad_y) * SIDE + (x + pad_x)] = px[c] as f32 / 255.0;
                }
            }
        }
        let input = ([1usize, 3, SIDE, SIDE], blob);
        let input_name = self.session.inputs()[0].name().to_string();
        let out_name = self.session.outputs()[0].name().to_string();
        let outputs = self
            .session
            .run(ort::inputs![input_name => ort::value::Tensor::from_array(input).map_err(|e| e.to_string())?])
            .map_err(|e| e.to_string())?;
        let output = outputs.get(&out_name).ok_or("Missing detector output")?;
        let (shape, data) = output.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
        decode(data, &shape, w0, h0, self.labels.as_deref())
    }
}

fn decode(data: &[f32], shape: &[i64], width: usize, height: usize, labels: Option<&[String]>) -> Result<Vec<Value>, String> {
    // rows: [1, C, N] or [C, N] or [N, C]; normalize to N rows of C columns.
    let dims: Vec<i64> = shape.iter().copied().collect();
    let (mut n_cols, mut n_rows) = match dims.len() {
        3 => (dims[1] as usize, dims[2] as usize),
        2 => (dims[0] as usize, dims[1] as usize),
        _ => return Err("Expected YOLO detection tensor [1, classes+4, candidates]".into()),
    };
    let mut transposed = false;
    if n_cols < n_rows {
        std::mem::swap(&mut n_cols, &mut n_rows);
        transposed = true;
    }
    if n_cols < 5 {
        return Err("Unsupported YOLO output; export a detection model without NMS".into());
    }
    let at = |row: usize, col: usize| -> f32 {
        let idx = if transposed { col * n_rows + row } else { row * n_cols + col };
        data.get(idx).copied().unwrap_or(f32::NAN)
    };
    let scale = (SIDE as f32 / width as f32).min(SIDE as f32 / height as f32);
    let pad_x = ((SIDE - (width as f32 * scale).round() as usize) / 2) as f32;
    let pad_y = ((SIDE - (height as f32 * scale).round() as usize) / 2) as f32;
    let mut boxes: Vec<[f32; 4]> = Vec::new();
    let mut scores: Vec<f32> = Vec::new();
    let mut classes: Vec<usize> = Vec::new();
    for r in 0..n_rows {
        // best class column
        let mut best = (f32::NEG_INFINITY, 0usize);
        for c in 4..n_cols {
            let v = at(r, c);
            if v > best.0 {
                best = (v, c - 4);
            }
        }
        if !(0..n_cols).all(|c| at(r, c).is_finite()) || best.0 < CONFIDENCE {
            continue;
        }
        let (cx, cy, w, h) = (at(r, 0), at(r, 1), at(r, 2), at(r, 3));
        let x1 = ((cx - w / 2.0 - pad_x) / scale).clamp(0.0, width as f32);
        let y1 = ((cy - h / 2.0 - pad_y) / scale).clamp(0.0, height as f32);
        let x2 = ((cx + w / 2.0 - pad_x) / scale).clamp(0.0, width as f32);
        let y2 = ((cy + h / 2.0 - pad_y) / scale).clamp(0.0, height as f32);
        if x2 <= x1 || y2 <= y1 {
            continue;
        }
        boxes.push([x1, y1, x2 - x1, y2 - y1]);
        scores.push(best.0);
        classes.push(best.1);
    }
    // Per-class NMS (greedy, score-descending).
    let mut result = Vec::new();
    let mut uniq: Vec<usize> = classes.clone();
    uniq.sort_unstable();
    uniq.dedup();
    for cls in uniq {
        let mut idx: Vec<usize> = (0..boxes.len()).filter(|&i| classes[i] == cls).collect();
        idx.sort_by(|&a, &b| scores[b].partial_cmp(&scores[a]).unwrap_or(std::cmp::Ordering::Equal));
        let mut keep: Vec<usize> = Vec::new();
        for &i in &idx {
            if keep.iter().all(|&k| iou(&boxes[i], &boxes[k]) < NMS_IOU) {
                keep.push(i);
            }
        }
        for i in keep {
            let name = labels
                .and_then(|l| l.get(cls).cloned())
                .unwrap_or_else(|| if cls == 0 { "person".into() } else { format!("class_{cls}") });
            let [x, y, w, h] = boxes[i];
            result.push(json!({
                "cls": name,
                "class_id": cls,
                "conf": (scores[i] * 1000.0).round() / 1000.0,
                "x": x / width as f32,
                "y": y / height as f32,
                "w": w / width as f32,
                "h": h / height as f32,
            }));
        }
    }
    Ok(result)
}

fn iou(a: &[f32; 4], b: &[f32; 4]) -> f32 {
    let x1 = a[0].max(b[0]);
    let y1 = a[1].max(b[1]);
    let x2 = (a[0] + a[2]).min(b[0] + b[2]);
    let y2 = (a[1] + a[3]).min(b[1] + b[3]);
    let inter = (x2 - x1).max(0.0) * (y2 - y1).max(0.0);
    inter / (a[2] * a[3] + b[2] * b[3] - inter).max(1e-9)
}
