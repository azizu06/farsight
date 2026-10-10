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
    pose: bool,
}

impl OnnxDetector {
    pub fn new(model: &str, pose: bool) -> Result<Self, String> {
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
        Ok(Self { session, labels, pose })
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
        if self.pose {
            decode_pose(data, &shape, w0, h0)
        } else {
            decode(data, &shape, w0, h0, self.labels.as_deref())
        }
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

/// YOLO11-pose decode: anchors x 56 (4 box + conf + 17*3 kpts).
pub fn decode_pose(data: &[f32], shape: &[i64], width: usize, height: usize) -> Result<Vec<Value>, String> {
    let dims: Vec<i64> = shape.iter().copied().collect();
    let (cols, rows) = match dims.len() {
        3 => (dims[1] as usize, dims[2] as usize),
        2 => (dims[0] as usize, dims[1] as usize),
        _ => return Err("Expected YOLO-pose tensor [1,56,anchors]".into()),
    };
    if cols != 56 {
        return Err("Expected YOLO-pose tensor with 56 channels".into());
    }
    let at = |r: usize, c: usize| data.get(c * rows + r).copied().unwrap_or(f32::NAN);
    let scale = (SIDE as f32 / width as f32).min(SIDE as f32 / height as f32);
    let pad_x = ((SIDE - (width as f32 * scale).round() as usize) / 2) as f32;
    let pad_y = ((SIDE - (height as f32 * scale).round() as usize) / 2) as f32;
    let mut cand: Vec<(usize, f32)> = (0..rows)
        .map(|r| (r, at(r, 4)))
        .filter(|&(r, s)| s >= CONFIDENCE && (0..56).all(|c| at(r, c).is_finite()))
        .collect();
    cand.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut keep: Vec<usize> = Vec::new();
    let boxes_of = |r: usize| -> [f32; 4] {
        let (cx, cy, w, h) = ((at(r, 0) - pad_x) / scale, (at(r, 1) - pad_y) / scale, at(r, 2) / scale, at(r, 3) / scale);
        [
            (cx - w / 2.0).clamp(0.0, width as f32),
            (cy - h / 2.0).clamp(0.0, height as f32),
            (cx + w / 2.0).clamp(0.0, width as f32) - (cx - w / 2.0).clamp(0.0, width as f32),
            (cy + h / 2.0).clamp(0.0, height as f32) - (cy - h / 2.0).clamp(0.0, height as f32),
        ]
    };
    for (r, _) in &cand {
        let b = boxes_of(*r);
        if keep.iter().all(|&k| iou(&b, &boxes_of(k)) < NMS_IOU) {
            keep.push(*r);
        }
    }
    let mut out = Vec::new();
    for r in keep {
        let b = boxes_of(r);
        if b[2] <= 0.0 || b[3] <= 0.0 {
            continue;
        }
        let mut kpts = [[0f32; 3]; 17];
        for k in 0..17 {
            kpts[k] = [(at(r, 5 + k * 3) - pad_x) / scale, (at(r, 6 + k * 3) - pad_y) / scale, at(r, 7 + k * 3)];
        }
        let down = is_down(&kpts, &b);
        out.push(json!({
            "cls": "person", "class_id": 0,
            "conf": (at(r, 4) * 1000.0).round() / 1000.0,
            "x": b[0] / width as f32, "y": b[1] / height as f32,
            "w": b[2] / width as f32, "h": b[3] / height as f32,
            "down": down,
            "kpts": kpts.iter().map(|k| json!([(k[0] / width as f32 * 10000.0).round() / 10000.0,
                (k[1] / height as f32 * 10000.0).round() / 10000.0, (k[2] * 1000.0).round() / 1000.0])).collect::<Vec<_>>(),
        }));
    }
    Ok(out)
}

const KPT_CONF: f32 = 0.3;

fn midpoint(kpts: &[[f32; 3]; 17], a: usize, b: usize) -> Option<[f32; 2]> {
    let pts: Vec<[f32; 2]> = [a, b].into_iter().filter(|&i| kpts[i][2] >= KPT_CONF).map(|i| [kpts[i][0], kpts[i][1]]).collect();
    if pts.is_empty() {
        None
    } else {
        Some([pts.iter().map(|p| p[0]).sum::<f32>() / pts.len() as f32, pts.iter().map(|p| p[1]).sum::<f32>() / pts.len() as f32])
    }
}

/// 'Person down' rule from COCO-17 keypoints (jetson/pose/keypoints.py).
pub fn is_down(kpts: &[[f32; 3]; 17], box_: &[f32; 4]) -> bool {
    let shoulders = midpoint(kpts, 5, 6);
    let lower = midpoint(kpts, 11, 12).or_else(|| midpoint(kpts, 15, 16));
    let (Some(s), Some(l)) = (shoulders, lower) else {
        return box_[2] > 1.2 * box_[3];
    };
    (l[0] - s[0]).abs().atan2((l[1] - s[1]).abs()).to_degrees() > 60.0
}

/// UltraFace RFB-320: direct 320x240 resize, normalized xyxy out.
pub fn decode_face(scores: &[f32], sshape: &[i64], boxes: &[f32], bshape: &[i64], confidence: f32) -> Result<Vec<Value>, String> {
    if sshape.len() != 3 || bshape.len() != 3 || sshape[0] != 1 || sshape[2] != 2
        || !(bshape[0] == 1 && bshape[1] == sshape[1] && bshape[2] == 4)
    {
        return Err("Expected UltraFace scores [1,N,2] and boxes [1,N,4]".into());
    }
    let n = sshape[1] as usize;
    let mut cand: Vec<(usize, f32, [f32; 4])> = (0..n)
        .filter_map(|i| {
            let s = scores.get(i * 2 + 1).copied()?;
            let b = [boxes.get(i * 4)?, boxes.get(i * 4 + 1)?, boxes.get(i * 4 + 2)?, boxes.get(i * 4 + 3)?];
            let b = [b[0], b[1], b[2], b[3]];
            if s.is_finite() && b.iter().all(|v| v.is_finite()) && s >= confidence {
                Some((i, s, [b[0].clamp(0.0, 1.0), b[1].clamp(0.0, 1.0), b[2].clamp(0.0, 1.0), b[3].clamp(0.0, 1.0)]))
            } else {
                None
            }
        })
        .map(|(i, s, b)| (i, s, [b[0], b[1], (b[2] - b[0]).max(0.0), (b[3] - b[1]).max(0.0)]))
        .filter(|&(_, _, b)| b[2] > 0.0 && b[3] > 0.0)
        .collect();
    cand.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let mut out = Vec::new();
    for (_, s, b) in &cand {
        if out.iter().all(|o: &Value| {
            iou(&[o["x"].as_f64().unwrap_or(0.0) as f32, o["y"].as_f64().unwrap_or(0.0) as f32,
                  o["w"].as_f64().unwrap_or(0.0) as f32, o["h"].as_f64().unwrap_or(0.0) as f32], b) < 0.3
        }) {
            out.push(json!({"cls": "face", "conf": (s * 1000.0).round() / 1000.0,
                "x": b[0], "y": b[1], "w": b[2], "h": b[3]}));
        }
    }
    Ok(out)
}

pub struct FaceDetector {
    session: ort::session::Session,
}

impl FaceDetector {
    pub fn new(model: &str) -> Result<Self, String> {
        if !std::path::Path::new(model).is_file() {
            return Err(format!("Face model not found: {model}"));
        }
        let session = ort::session::Session::builder()
            .map_err(|e| e.to_string())?
            .commit_from_file(model)
            .map_err(|e| e.to_string())?;
        Ok(Self { session })
    }

    /// blobFromImage(frame, 1/128, (320,240), mean=(127,127,127), swapRB=True).
    pub fn detect(&mut self, jpeg: &[u8]) -> Result<Vec<Value>, String> {
        let image = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg)
            .map_err(|_| "Invalid detector camera image".to_string())?
            .to_rgb8();
        let resized = image::imageops::resize(&image, 320, 240, FilterType::Triangle);
        let mut blob = vec![0f32; 3 * 240 * 320];
        for y in 0..240usize {
            for x in 0..320usize {
                let px = resized.get_pixel(x as u32, y as u32);
                for c in 0..3 {
                    blob[c * 240 * 320 + y * 320 + x] = (px[c] as f32 - 127.0) / 128.0;
                }
            }
        }
        let input = ([1usize, 3, 240, 320], blob);
        let input_name = self.session.inputs()[0].name().to_string();
        let mut scores: Option<(&[i64], &[f32])> = None;
        let mut boxes: Option<(&[i64], &[f32])> = None;
        let outputs = self
            .session
            .run(ort::inputs![input_name => ort::value::Tensor::from_array(input).map_err(|e| e.to_string())?])
            .map_err(|e| e.to_string())?;
        for name in outputs.keys() {
            if let Ok((shape, data)) = outputs.get(name).unwrap().try_extract_tensor::<f32>() {
                match shape.last().copied().unwrap_or(0) {
                    2 => scores = Some((shape, data)),
                    4 => boxes = Some((shape, data)),
                    _ => {}
                }
            }
        }
        let (Some((ss, sd)), Some((bs, bd))) = (scores, boxes) else {
            return Err("UltraFace outputs missing [N,2]/[N,4] tensors".into());
        };
        decode_face(sd, ss, bd, bs, 0.7)
    }
}
