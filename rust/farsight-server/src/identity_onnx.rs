//! ONNX YuNet + SFace backends for the identity engine (feature `detector`).
//! Port of cv2.FaceDetectorYN (opencv/modules/objdetect/src/face_detect.cpp)
//! and cv2.FaceRecognizerSF (face_recognize.cpp): pad-to-32 BGR input,
//! prior-grid decode, score = sqrt(cls*obj), int-rect NMS, Kabsch similarity
//! alignment onto the canonical 112x112 crop, RGB 0-255 recognizer blob.
//! Requires an ONNX Runtime shared lib at runtime (ort `load-dynamic`).

#![cfg(feature = "detector")]

use std::collections::HashMap;

use crate::identity::{FaceDetect, FaceRecognize, FACE_ROW};

const DIVISOR: u32 = 32;
const STRIDES: [u32; 3] = [8, 16, 32];
const SCORE_THRESHOLD: f32 = 0.9;
const NMS_THRESHOLD: f32 = 0.3;
const TOP_K: usize = 5000;

fn session(model: &str) -> Result<ort::session::Session, String> {
    if !std::path::Path::new(model).is_file() {
        return Err(format!("Identity model not found: {model}"));
    }
    // Match the detector lane's measured CPU budget (detector.rs).
    ort::session::Session::builder()
        .map_err(|e| e.to_string())?
        .with_intra_threads(2)
        .map_err(|e| e.to_string())?
        .commit_from_file(model)
        .map_err(|e| e.to_string())
}

fn run(session: &mut ort::session::Session, input: ([usize; 4], Vec<f32>)) -> Result<HashMap<String, Vec<f32>>, String> {
    let input_name = session.inputs()[0].name().to_string();
    let outputs = session
        .run(ort::inputs![input_name => ort::value::Tensor::from_array(input).map_err(|e| e.to_string())?])
        .map_err(|e| e.to_string())?;
    let mut result = HashMap::new();
    for name in outputs.keys() {
        let (_, data) = outputs
            .get(name)
            .unwrap()
            .try_extract_tensor::<f32>()
            .map_err(|e| e.to_string())?;
        result.insert(name.to_string(), data.to_vec());
    }
    Ok(result)
}

/// NCHW blob: values 0-255, channel order selectable (BGR for YuNet's
/// blobFromImage default, RGB for SFace's swapRB=True).
fn blob(image: &image::RgbImage, width: u32, height: u32, bgr: bool) -> Vec<f32> {
    let mut out = vec![0f32; 3 * (height * width) as usize];
    let plane = (height * width) as usize;
    for y in 0..height {
        for x in 0..width {
            let px = image.get_pixel(x, y);
            for c in 0..3usize {
                let ch = if bgr { 2 - c } else { c };
                out[c * plane + (y * width + x) as usize] = px[ch] as f32;
            }
        }
    }
    out
}

// ---------- YuNet (cv2.FaceDetectorYN) ----------

pub struct YuNet {
    session: ort::session::Session,
    input_w: u32,
    input_h: u32,
}

impl YuNet {
    pub fn new(model: &str) -> Result<Self, String> {
        Ok(Self { session: session(model)?, input_w: 640, input_h: 480 })
    }
}

impl FaceDetect for YuNet {
    fn set_input_size(&mut self, width: u32, height: u32) {
        self.input_w = width;
        self.input_h = height;
    }

    fn detect(&mut self, image: &image::RgbImage) -> Result<Vec<[f32; FACE_ROW]>, String> {
        let pad_w = (self.input_w - 1) / DIVISOR * DIVISOR + DIVISOR;
        let pad_h = (self.input_h - 1) / DIVISOR * DIVISOR + DIVISOR;
        // copyMakeBorder(bottom,right, 0) then blobFromImage (BGR, no scale).
        let input = ([1usize, 3, pad_h as usize, pad_w as usize], blob(image, pad_w, pad_h, true));
        let outputs = run(&mut self.session, input)?;
        let mut faces: Vec<[f32; FACE_ROW]> = Vec::new();
        for stride in STRIDES.iter() {
            let cols = pad_w / stride;
            let rows = pad_h / stride;
            let get = |kind: &str| -> Result<&Vec<f32>, String> {
                outputs
                    .get(&format!("{kind}_{stride}"))
                    .ok_or_else(|| format!("Missing YuNet output {kind}_{stride}"))
            };
            let cls = get("cls")?;
            let obj = get("obj")?;
            let bbox = get("bbox")?;
            let kps = get("kps")?;
            for r in 0..rows {
                for c in 0..cols {
                    let idx = (r * cols + c) as usize;
                    let cls_score = cls.get(idx).copied().unwrap_or(0.0).clamp(0.0, 1.0);
                    let obj_score = obj.get(idx).copied().unwrap_or(0.0).clamp(0.0, 1.0);
                    let score = (cls_score * obj_score).sqrt();
                    if score < SCORE_THRESHOLD {
                        continue;
                    }
                    let b = |k: usize| bbox.get(idx * 4 + k).copied().unwrap_or(f32::NAN);
                    let cx = (c as f32 + b(0)) * *stride as f32;
                    let cy = (r as f32 + b(1)) * *stride as f32;
                    let w = b(2).exp() * *stride as f32;
                    let h = b(3).exp() * *stride as f32;
                    let mut face = [0f32; FACE_ROW];
                    face[0] = cx - w / 2.0;
                    face[1] = cy - h / 2.0;
                    face[2] = w;
                    face[3] = h;
                    for n in 0..5 {
                        face[4 + 2 * n] =
                            (kps.get(idx * 10 + 2 * n).copied().unwrap_or(f32::NAN) + c as f32)
                                * *stride as f32;
                        face[4 + 2 * n + 1] =
                            (kps.get(idx * 10 + 2 * n + 1).copied().unwrap_or(f32::NAN) + r as f32)
                                * *stride as f32;
                    }
                    face[14] = score;
                    faces.push(face);
                }
            }
        }
        if faces.len() > 1 {
            Ok(nms(faces))
        } else {
            Ok(faces)
        }
    }
}

/// dnn::NMSBoxes on Rect2i (floats truncated), score-threshold greedy,
/// IoU > nms_threshold suppresses.
fn nms(faces: Vec<[f32; FACE_ROW]>) -> Vec<[f32; FACE_ROW]> {
    let mut order: Vec<usize> = (0..faces.len()).collect();
    order.sort_by(|&a, &b| {
        faces[b][14]
            .partial_cmp(&faces[a][14])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let rect = |f: &[f32; FACE_ROW]| -> [i32; 4] {
        [f[0] as i32, f[1] as i32, f[2] as i32, f[3] as i32]
    };
    let iou = |a: [i32; 4], b: [i32; 4]| -> f64 {
        let x1 = a[0].max(b[0]);
        let y1 = a[1].max(b[1]);
        let x2 = (a[0] + a[2]).min(b[0] + b[2]);
        let y2 = (a[1] + a[3]).min(b[1] + b[3]);
        let inter = ((x2 - x1).max(0) * (y2 - y1).max(0)) as f64;
        let area_a = (a[2] * a[3]) as f64;
        let area_b = (b[2] * b[3]) as f64;
        inter / (area_a + area_b - inter).max(1e-9)
    };
    let mut suppressed = vec![false; faces.len()];
    let mut keep = Vec::new();
    for &i in &order {
        if suppressed[i] {
            continue;
        }
        keep.push(i);
        if keep.len() >= TOP_K {
            break;
        }
        for &j in &order {
            if j != i && !suppressed[j] && iou(rect(&faces[i]), rect(&faces[j])) > NMS_THRESHOLD as f64 {
                suppressed[j] = true;
            }
        }
    }
    keep.into_iter().map(|i| faces[i]).collect()
}

// ---------- SFace (cv2.FaceRecognizerSF) ----------

pub struct SFace {
    session: ort::session::Session,
}

impl SFace {
    pub fn new(model: &str) -> Result<Self, String> {
        Ok(Self { session: session(model)? })
    }
}

/// Canonical 112x112 arcface destination landmarks (face_recognize.cpp).
const DST: [[f64; 2]; 5] = [
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
];
const DST_MEAN: [f64; 2] = [56.0262, 71.9008];

/// getSimilarityTransformMatrix: least-squares similarity via a 2x2 SVD.
fn similarity_transform(src: [[f64; 2]; 5]) -> [[f64; 3]; 2] {
    let src_mean = [
        src.iter().map(|p| p[0]).sum::<f64>() / 5.0,
        src.iter().map(|p| p[1]).sum::<f64>() / 5.0,
    ];
    let mut a = [[0.0f64; 2]; 2]; // A[i][j] = mean(dst_i_demean * src_j_demean)
    for p in 0..5 {
        let s = [src[p][0] - src_mean[0], src[p][1] - src_mean[1]];
        let d = [DST[p][0] - DST_MEAN[0], DST[p][1] - DST_MEAN[1]];
        a[0][0] += d[0] * s[0];
        a[0][1] += d[0] * s[1];
        a[1][0] += d[1] * s[0];
        a[1][1] += d[1] * s[1];
    }
    for e in a.iter_mut().flat_map(|r| r.iter_mut()) {
        *e /= 5.0;
    }
    let det_a = a[0][0] * a[1][1] - a[0][1] * a[1][0];
    let mut d = [1.0f64, 1.0];
    if det_a < 0.0 {
        d[1] = -1.0;
    }
    // 2x2 SVD: eig of A^T A gives Vt rows; U = A V s^-1.
    let (u, s, vt) = svd2x2(&a);
    let smax = s[0].max(s[1]);
    let tol = smax * 2.0 * f32::MIN_POSITIVE as f64;
    let rank = (s[0] > tol) as usize + (s[1] > tol) as usize;
    let det_u = u[0][0] * u[1][1] - u[0][1] * u[1][0];
    let det_vt = vt[0][0] * vt[1][1] - vt[0][1] * vt[1][0];
    let mut t = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]];
    let m = if rank == 1 {
        if det_u * det_vt > 0.0 {
            udvt_forced(&u, &vt, 1.0, 1.0) // T = U*Vt
        } else {
            udvt_forced(&u, &vt, 1.0, -1.0) // T = U*diag(1,-1)*Vt
        }
    } else {
        udvt_forced(&u, &vt, d[0], d[1]) // T = U*D*Vt
    };
    t[0][0] = m[0][0];
    t[0][1] = m[0][1];
    t[1][0] = m[1][0];
    t[1][1] = m[1][1];
    let var1 = src.iter().map(|p| (p[0] - src_mean[0]).powi(2)).sum::<f64>() / 5.0;
    let var2 = src.iter().map(|p| (p[1] - src_mean[1]).powi(2)).sum::<f64>() / 5.0;
    let scale = (s[0] * d[0] + s[1] * d[1]) / (var1 + var2);
    let ts = [
        t[0][0] * src_mean[0] + t[0][1] * src_mean[1],
        t[1][0] * src_mean[0] + t[1][1] * src_mean[1],
    ];
    t[0][2] = DST_MEAN[0] - scale * ts[0];
    t[1][2] = DST_MEAN[1] - scale * ts[1];
    t[0][0] *= scale;
    t[0][1] *= scale;
    t[1][0] *= scale;
    t[1][1] *= scale;
    [[t[0][0], t[0][1], t[0][2]], [t[1][0], t[1][1], t[1][2]]]
}

fn udvt_forced(u: &[[f64; 2]; 2], vt: &[[f64; 2]; 2], d0: f64, d1: f64) -> [[f64; 2]; 2] {
    let mut m = [[0.0f64; 2]; 2];
    for i in 0..2 {
        for j in 0..2 {
            m[i][j] = d0 * u[i][0] * vt[0][j] + d1 * u[i][1] * vt[1][j];
        }
    }
    m
}

/// Exact 2x2 SVD: returns (U, s descending, Vt) with A = U diag(s) Vt.
fn svd2x2(a: &[[f64; 2]; 2]) -> ([[f64; 2]; 2], [f64; 2], [[f64; 2]; 2]) {
    // Eigen of A^T A (symmetric [[p,q],[q,r]]).
    let p = a[0][0] * a[0][0] + a[1][0] * a[1][0];
    let q = a[0][0] * a[0][1] + a[1][0] * a[1][1];
    let r = a[0][1] * a[0][1] + a[1][1] * a[1][1];
    let mean = (p + r) / 2.0;
    let dev = (((p - r) / 2.0).powi(2) + q * q).sqrt();
    let (l1, l2) = ((mean + dev).max(0.0), (mean - dev).max(0.0));
    let s = [l1.sqrt(), l2.sqrt()];
    // Eigenvector for l1.
    let v1 = if q.abs() > 1e-30 || p != r {
        let mut v = [q, l1 - p];
        if v[0].abs() < 1e-30 && v[1].abs() < 1e-30 {
            v = [l1 - r, q];
        }
        let n = (v[0] * v[0] + v[1] * v[1]).sqrt();
        if n < 1e-30 {
            [1.0, 0.0]
        } else {
            [v[0] / n, v[1] / n]
        }
    } else if p >= r {
        [1.0, 0.0]
    } else {
        [0.0, 1.0]
    };
    let v2 = [-v1[1], v1[0]];
    let vt = [[v1[0], v1[1]], [v2[0], v2[1]]]; // rows are v_i^T
    let mut u = [[0.0f64; 2]; 2];
    for (k, (sv, vv)) in s.iter().zip([v1, v2].iter()).enumerate() {
        let (ux, uy) = if *sv > 1e-12 {
            (
                (a[0][0] * vv[0] + a[0][1] * vv[1]) / sv,
                (a[1][0] * vv[0] + a[1][1] * vv[1]) / sv,
            )
        } else if k == 0 {
            (1.0, 0.0)
        } else {
            (-u[1][0], u[0][0]) // orthogonal to u1
        };
        u[0][k] = ux;
        u[1][k] = uy;
    }
    // Orthonormalize U columns defensively (zero/duplicate singular values).
    let n1 = (u[0][0].powi(2) + u[1][0].powi(2)).sqrt();
    if n1 > 1e-12 {
        u[0][0] /= n1;
        u[1][0] /= n1;
    }
    // u2 must be perpendicular to u1.
    let proj = u[0][0] * u[0][1] + u[1][0] * u[1][1];
    u[0][1] -= proj * u[0][0];
    u[1][1] -= proj * u[1][0];
    let n2 = (u[0][1].powi(2) + u[1][1].powi(2)).sqrt();
    if n2 > 1e-12 {
        u[0][1] /= n2;
        u[1][1] /= n2;
    } else {
        u[0][1] = -u[1][0];
        u[1][1] = u[0][0];
    }
    (u, s, vt)
}

/// warpAffine(INTER_LINEAR, BORDER_CONSTANT 0) onto a 112x112 crop; OpenCV
/// samples via the inverse transform.
fn warp_affine(image: &image::RgbImage, m: [[f64; 3]; 2], width: u32, height: u32) -> image::RgbImage {
    let det = m[0][0] * m[1][1] - m[0][1] * m[1][0];
    let mut out = image::RgbImage::new(width, height);
    if det.abs() < 1e-30 {
        return out;
    }
    let i00 = m[1][1] / det;
    let i01 = -m[0][1] / det;
    let i10 = -m[1][0] / det;
    let i11 = m[0][0] / det;
    let t0 = -(i00 * m[0][2] + i01 * m[1][2]);
    let t1 = -(i10 * m[0][2] + i11 * m[1][2]);
    let (iw, ih) = (image.width() as f64, image.height() as f64);
    for y in 0..height {
        for x in 0..width {
            let sx = i00 * x as f64 + i01 * y as f64 + t0;
            let sy = i10 * x as f64 + i11 * y as f64 + t1;
            out.put_pixel(x, y, bilinear(image, sx, sy, iw, ih));
        }
    }
    out
}

fn bilinear(image: &image::RgbImage, sx: f64, sy: f64, iw: f64, ih: f64) -> image::Rgb<u8> {
    let x0 = sx.floor() as i64;
    let y0 = sy.floor() as i64;
    let fx = sx - x0 as f64;
    let fy = sy - y0 as f64;
    let at = |x: i64, y: i64| -> [f64; 3] {
        if x < 0 || y < 0 || x as f64 >= iw || y as f64 >= ih {
            [0.0, 0.0, 0.0]
        } else {
            let p = image.get_pixel(x as u32, y as u32);
            [p[0] as f64, p[1] as f64, p[2] as f64]
        }
    };
    let mut px = image::Rgb([0u8; 3]);
    for c in 0..3 {
        let top = at(x0, y0)[c] * (1.0 - fx) + at(x0 + 1, y0)[c] * fx;
        let bot = at(x0, y0 + 1)[c] * (1.0 - fx) + at(x0 + 1, y0 + 1)[c] * fx;
        px[c] = (top * (1.0 - fy) + bot * fy).round().clamp(0.0, 255.0) as u8;
    }
    px
}

impl FaceRecognize for SFace {
    fn align_crop(&mut self, image: &image::RgbImage, row: &[f32; FACE_ROW]) -> Result<image::RgbImage, String> {
        let mut src = [[0f64; 2]; 5];
        for n in 0..5 {
            src[n][0] = row[4 + 2 * n] as f64;
            src[n][1] = row[4 + 2 * n + 1] as f64;
        }
        let m = similarity_transform(src);
        Ok(warp_affine(image, m, 112, 112))
    }

    /// blobFromImage(aligned, 1, 112x112, (0,0,0), swapRB=True) -> 128 floats.
    fn feature(&mut self, aligned: &image::RgbImage) -> Result<Vec<f32>, String> {
        let input = ([1usize, 3, 112, 112], blob(aligned, 112, 112, false));
        let outputs = run(&mut self.session, input)?;
        let data = outputs
            .values()
            .next()
            .ok_or("SFace produced no outputs")?;
        if data.len() != 128 {
            return Err("SFace feature must contain 128 values".into());
        }
        Ok(data.clone())
    }
}
