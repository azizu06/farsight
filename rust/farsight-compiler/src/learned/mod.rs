//! Learned priors (W1): monocular depth completion via an ONNX model.
//!
//! The default build is pure Rust and needs no ML runtime. Building with
//! `--features onnx` links ONNX Runtime (CPU EP); `--features coreml`
//! additionally registers the CoreML EP (Apple Neural Engine + GPU) on macOS.
//! The model path comes from `FARSIGHT_DEPTH_MODEL` / `FARSIGHT_LEARNED` JSON;
//! when it is absent `complete_depth*` returns an empty vec — callers skip.

use std::path::Path;
use std::sync::atomic::{AtomicU32, Ordering};

use rayon::prelude::*;

use crate::core::{Camera, Capture, PointCloud};

/// Options for the learned stage. Defaults are tuned for Depth Anything V2
/// Small (518x518 input). Merge a JSON patch via `FARSIGHT_LEARNED` or set the
/// model path directly with `FARSIGHT_DEPTH_MODEL`.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct LearnedOptions {
    /// Path to the ONNX depth model. Empty = learned stage disabled.
    pub depth_model: String,
    /// Expected model input size (width, height). 0 = read from model.
    pub model_input_w: u32,
    pub model_input_h: u32,
    /// Minimum measured (projected cloud) samples required before trusting a fit.
    pub min_pairs: usize,
    /// Huber IRLS iterations for the scale/shift fit.
    pub huber_iters: usize,
    /// Confidence window radius in output pixels.
    pub conf_radius: u32,
    /// Max frames to run the model on (0 = all).
    pub max_frames: usize,
}

impl Default for LearnedOptions {
    fn default() -> Self {
        Self {
            depth_model: String::new(),
            model_input_w: 518,
            model_input_h: 518,
            min_pairs: 200,
            huber_iters: 4,
            conf_radius: 12,
            max_frames: 0,
        }
    }
}

/// Options from `FARSIGHT_DEPTH_MODEL` + `FARSIGHT_LEARNED` JSON patch.
pub fn options() -> LearnedOptions {
    let mut o = LearnedOptions::default();
    if let Ok(m) = std::env::var("FARSIGHT_DEPTH_MODEL") {
        o.depth_model = m;
    }
    if let Ok(patch) = std::env::var("FARSIGHT_LEARNED") {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&patch) {
            let _ = serde_json::from_value::<LearnedOptions>(merge(
                serde_json::to_value(&o).unwrap_or_default(),
                v,
            ))
            .map(|p| o = p);
        }
    }
    o
}

fn merge(mut base: serde_json::Value, patch: serde_json::Value) -> serde_json::Value {
    if let (Some(b), Some(p)) = (base.as_object_mut(), patch.as_object()) {
        for (k, v) in p {
            b.insert(k.clone(), v.clone());
        }
    }
    base
}

/// Metric dense depth for one capture view.
/// `depth`/`confidence` are row-major `width*height`; depth in metres
/// (0 = invalid), confidence 0..1.
#[derive(Clone, Debug)]
pub struct DepthMap {
    /// Index into `Capture::cameras`/`images`.
    pub camera: usize,
    pub width: u32,
    pub height: u32,
    pub depth: Vec<f32>,
    pub confidence: Vec<f32>,
    /// Robust fit: inverse depth ~= `a * rel + b`, plus pair count and
    /// normalised residual (for W3/telemetry).
    pub scale: f32,
    pub shift: f32,
    pub pairs: usize,
    pub fit_rmse: f32,
}

/// Public entry: complete metric depth for every capture view.
/// Returns an empty vec when no model is configured or no frames are usable.
pub fn complete_depth(capture: &Capture, cloud: &PointCloud) -> Vec<DepthMap> {
    complete_depth_opts(capture, cloud, &options())
}

/// Entry with explicit options.
pub fn complete_depth_opts(capture: &Capture, cloud: &PointCloud, opts: &LearnedOptions) -> Vec<DepthMap> {
    if opts.depth_model.is_empty() || !Path::new(&opts.depth_model).exists() {
        return Vec::new();
    }
    #[cfg(feature = "onnx")]
    {
        match onnx::run(capture, cloud, opts) {
            Ok(maps) => return maps,
            Err(e) => {
                eprintln!("learned: onnx inference failed ({e}); skipping depth completion");
                return Vec::new();
            }
        }
    }
    #[cfg(not(feature = "onnx"))]
    {
        eprintln!("learned: built without `onnx` feature; skipping depth completion (model {})", opts.depth_model);
        let _ = (capture, cloud);
        Vec::new()
    }
}

// ---------------- measured-depth projection ----------------

/// Z-buffer of measured cloud depth in one view (nearest wins) + hit counts.
pub struct ViewZ {
    w: usize,
    h: usize,
    z: Vec<AtomicU32>,   // f32 bits of nearest measured depth; u32::MAX = empty
    cnt: Vec<AtomicU32>, // measured samples per pixel
}

/// Project the measured cloud into a view: nearest depth + counts per pixel.
pub fn project_view(cam: &Camera, cloud: &PointCloud) -> ViewZ {
    let w = cam.width as usize;
    let h = cam.height as usize;
    let v = ViewZ {
        w,
        h,
        z: (0..w * h).map(|_| AtomicU32::new(u32::MAX)).collect(),
        cnt: (0..w * h).map(|_| AtomicU32::new(0)).collect(),
    };
    cloud.pos.par_chunks(4096).for_each(|chunk| {
        for p in chunk {
            if let Some((u, vv, d)) = cam.project(*p) {
                let x = u.floor() as i32;
                let y = vv.floor() as i32;
                if x < 0 || y < 0 || x >= w as i32 || y >= h as i32 {
                    continue;
                }
                let i = y as usize * w + x as usize;
                v.z[i].fetch_min(d.to_bits(), Ordering::Relaxed);
                v.cnt[i].fetch_add(1, Ordering::Relaxed);
            }
        }
    });
    v
}

// ---------------- robust scale/shift fit ----------------

/// Fit inverse depth `y = a*rel + b` by iteratively reweighted least squares
/// (Huber weights, MAD scale). Returns (a, b, rmse in inverse-depth units).
fn fit_affine_irls(pairs: &[(f32, f32)], iters: usize) -> (f32, f32, f32) {
    let lsq = |w: &[f64]| -> (f64, f64) {
        let (mut sw, mut sx, mut sxx, mut sxy, mut sy) = (0.0, 0.0, 0.0, 0.0, 0.0);
        for ((x, y), wi) in pairs.iter().zip(w) {
            let w = *wi;
            let (x, y) = (*x as f64, *y as f64);
            sw += w;
            sx += w * x;
            sxx += w * x * x;
            sxy += w * x * y;
            sy += w * y;
        }
        let det = sw * sxx - sx * sx;
        if det.abs() < 1e-12 {
            return (0.0, if sw > 0.0 { sy / sw } else { 0.0 });
        }
        ((sw * sxy - sx * sy) / det, (sxx * sy - sx * sxy) / det)
    };
    let mut w = vec![1.0f64; pairs.len()];
    let (mut a, mut b) = lsq(&w);
    for _ in 0..iters {
        let mut res: Vec<f32> = pairs.iter().map(|(x, y)| (a as f32 * x + b as f32 - y).abs()).collect();
        res.sort_by(|p, q| p.partial_cmp(q).unwrap_or(std::cmp::Ordering::Equal));
        let med = res.get(res.len() / 2).copied().unwrap_or(0.0);
        let sigma = (1.4826 * med).max(1e-5);
        let c = 1.345 * sigma;
        for ((x, y), wi) in pairs.iter().zip(w.iter_mut()) {
            let r = (a as f32 * x + b as f32 - y).abs();
            *wi = if r <= c { 1.0 } else { (c / r) as f64 };
        }
        let (na, nb) = lsq(&w);
        a = na;
        b = nb;
    }
    let rmse = {
        let mut s = 0.0f64;
        for (x, y) in pairs {
            let r = (a as f32 * x + b as f32 - y) as f64;
            s += r * r;
        }
        (s / pairs.len() as f64).sqrt() as f32
    };
    (a as f32, b as f32, rmse)
}

/// Integral image of per-pixel hit counts -> local measured density in a
/// window of radius `r` around each pixel.
fn local_density(vz: &ViewZ, r: u32) -> Vec<f32> {
    let (w, h) = (vz.w, vz.h);
    let mut ii = vec![0f64; (w + 1) * (h + 1)];
    for y in 0..h {
        let mut row = 0f64;
        for x in 0..w {
            row += vz.cnt[y * w + x].load(Ordering::Relaxed) as f64;
            ii[(y + 1) * (w + 1) + x + 1] = ii[y * (w + 1) + x + 1] + row;
        }
    }
    let r = r as i64;
    let mut out = vec![0f32; w * h];
    out.par_chunks_mut(w).enumerate().for_each(|(y, row)| {
        let (y0, y1) = ((y as i64 - r).max(0) as usize, ((y as i64 + r + 1).min(h as i64)) as usize);
        for (x, cell) in row.iter_mut().enumerate() {
            let (x0, x1) = ((x as i64 - r).max(0) as usize, ((x as i64 + r + 1).min(w as i64)) as usize);
            let s = ii[y1 * (w + 1) + x1] - ii[y0 * (w + 1) + x1] - ii[y1 * (w + 1) + x0] + ii[y0 * (w + 1) + x0];
            *cell = s as f32 / ((x1 - x0) * (y1 - y0)) as f32;
        }
    });
    out
}

/// Convert the model's relative output for one view into a metric `DepthMap`
/// by projecting the measured cloud into the view and fitting inverse depth
/// `1/z = a*rel + b`. `rel` is at camera resolution.
pub fn finish_view(rel: &[f32], cam: &Camera, camera_idx: usize, vz: &ViewZ, opts: &LearnedOptions) -> Option<DepthMap> {
    let (w, h) = (vz.w, vz.h);
    // gather fit pairs
    let pairs: Vec<(f32, f32)> = (0..h)
        .into_par_iter()
        .flat_map_iter(|y| {
            (0..w).filter_map(move |x| {
                let i = y * w + x;
                let zb = vz.z[i].load(Ordering::Relaxed);
                if zb == u32::MAX {
                    return None;
                }
                let z = f32::from_bits(zb);
                if z <= 1e-4 {
                    return None;
                }
                Some((rel[i], 1.0 / z))
            })
        })
        .collect();
    if pairs.len() < opts.min_pairs {
        return None;
    }
    let (a, b, rmse) = fit_affine_irls(&pairs, opts.huber_iters);
    // fit quality: rmse relative to inverse-depth spread
    let ymean = pairs.iter().map(|p| p.1 as f64).sum::<f64>() / pairs.len() as f64;
    let yvar = pairs.iter().map(|p| (p.1 as f64 - ymean) * (p.1 as f64 - ymean)).sum::<f64>() / pairs.len() as f64;
    let q_fit = if yvar <= 1e-9 { 0.5 } else { (1.0 - rmse as f64 / yvar.sqrt()).clamp(0.0, 1.0) as f32 };
    let density = local_density(vz, opts.conf_radius);

    let mut depth = vec![0f32; w * h];
    let mut confidence = vec![0f32; w * h];
    depth
        .par_chunks_mut(w)
        .zip(confidence.par_chunks_mut(w))
        .enumerate()
        .for_each(|(y, (drow, crow))| {
            for x in 0..w {
                let i = y * w + x;
                let inv = a * rel[i] + b;
                let d = if inv > 1e-4 { 1.0 / inv } else { 0.0 };
                drow[x] = d;
                // confidence: fit quality * local measured support (saturates ~4% coverage)
                crow[x] = if d > 0.0 { q_fit * (density[i] / 0.04).min(1.0) } else { 0.0 };
            }
        });
    Some(DepthMap {
        camera: camera_idx,
        width: cam.width,
        height: cam.height,
        depth,
        confidence,
        scale: a,
        shift: b,
        pairs: pairs.len(),
        fit_rmse: rmse,
    })
}

// ---------------- ONNX inference (feature-gated) ----------------

#[cfg(feature = "onnx")]
mod onnx {
    use super::*;
    use crate::core::Image;
    use anyhow::{bail, Context, Result};
    use image::imageops::FilterType;
    use ort::session::Session;
    use ort::value::Tensor;
    use std::path::PathBuf;

    /// Run the model per frame, fit to the cloud, return depth maps.
    pub fn run(capture: &Capture, cloud: &PointCloud, opts: &LearnedOptions) -> Result<Vec<DepthMap>> {
        #[allow(unused_mut)]
        let mut builder = Session::builder()?;
        #[cfg(feature = "coreml")]
        {
            use ort::execution_providers::coreml::CoreMLExecutionProvider;
            builder.with_execution_providers([CoreMLExecutionProvider::default().build()])?;
        }
        let mut session = builder.commit_from_file(PathBuf::from(&opts.depth_model))?;

        let n = if opts.max_frames > 0 { opts.max_frames.min(capture.cameras.len()) } else { capture.cameras.len() };
        let mut out = Vec::new();
        for i in 0..n {
            let (Some(cam), Some(img)) = (capture.cameras.get(i), capture.images.get(i)) else { continue };
            let (rel, rw, rh) = infer(&mut session, img, opts).with_context(|| format!("frame {i}"))?;
            // rescale relative map to camera resolution
            let rel_full = resize_rel(&rel, rw, rh, cam.width as usize, cam.height as usize);
            let vz = project_view(cam, cloud);
            if let Some(dm) = finish_view(&rel_full, cam, i, &vz, opts) {
                out.push(dm);
            } else {
                eprintln!("learned: frame {i}: insufficient measured pairs, skipped");
            }
        }
        Ok(out)
    }

    /// Preprocess image -> model input tensor, run, return relative depth at
    /// model resolution.
    fn infer(session: &mut Session, img: &Image, opts: &LearnedOptions) -> Result<(Vec<f32>, usize, usize)> {
        let (iw, ih) = (opts.model_input_w.max(32) as usize, opts.model_input_h.max(32) as usize);
        let rgb = image::RgbImage::from_raw(img.width, img.height, img.rgb.clone())
            .context("image buffer")?;
        let resized = image::imageops::resize(&rgb, iw as u32, ih as u32, FilterType::Triangle);
        // NCHW, normalise to [-1,1] (DAV2 onnx exports use mean/std 0.5)
        let mut data = vec![0f32; 3 * ih * iw];
        for y in 0..ih {
            for x in 0..iw {
                let p = resized.get_pixel(x as u32, y as u32);
                for c in 0..3 {
                    data[c * ih * iw + y * iw + x] = (p[c] as f32 / 255.0 - 0.5) / 0.5;
                }
            }
        }
        let input = Tensor::from_array((vec![1i64, 3, ih as i64, iw as i64], data))?;
        let outputs = session.run(ort::inputs![input])?;
        let (_name, out) = outputs.iter().next().context("no model output")?;
        let (shape, vals) = out.try_extract_tensor::<f32>()?;
        let dims: Vec<i64> = shape.iter().cloned().collect();
        let (h, w) = match dims.len() {
            4 => (dims[2] as usize, dims[3] as usize),
            3 => (dims[1] as usize, dims[2] as usize),
            2 => (dims[0] as usize, dims[1] as usize),
            _ => bail!("unexpected depth output shape {dims:?}"),
        };
        let vals: Vec<f32> = vals.to_vec();
        if vals.len() != h * w {
            bail!("depth output len {} != {h}x{w}", vals.len());
        }
        Ok((vals, w, h))
    }

    /// Bilinear rescale of a model-resolution rel map to camera resolution.
    fn resize_rel(rel: &[f32], rw: usize, rh: usize, w: usize, h: usize) -> Vec<f32> {
        if rw == w && rh == h {
            return rel.to_vec();
        }
        let mut out = vec![0f32; w * h];
        let sx = rw as f32 / w as f32;
        let sy = rh as f32 / h as f32;
        out.par_chunks_mut(w).enumerate().for_each(|(y, row)| {
            let fy = (y as f32 + 0.5) * sy - 0.5;
            let y0 = fy.floor().clamp(0.0, (rh - 1) as f32) as usize;
            let y1 = (y0 + 1).min(rh - 1);
            let wy = fy - y0 as f32;
            for x in 0..w {
                let fx = (x as f32 + 0.5) * sx - 0.5;
                let x0 = fx.floor().clamp(0.0, (rw - 1) as f32) as usize;
                let x1 = (x0 + 1).min(rw - 1);
                let wx = fx - x0 as f32;
                let a = rel[y0 * rw + x0] * (1.0 - wx) + rel[y0 * rw + x1] * wx;
                let b = rel[y1 * rw + x0] * (1.0 - wx) + rel[y1 * rw + x1] * wx;
                row[x] = a * (1.0 - wy) + b * wy;
            }
        });
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::V3;

    fn wall_cloud(n: usize, z: f32, half: f32) -> PointCloud {
        let mut pos = Vec::with_capacity(n * n);
        for i in 0..n {
            for j in 0..n {
                let x = -half + 2.0 * half * i as f32 / (n - 1) as f32;
                let y = -half + 2.0 * half * j as f32 / (n - 1) as f32;
                pos.push([x, y, -z] as V3);
            }
        }
        let n_pts = pos.len();
        PointCloud { pos, col: vec![[128; 3]; n_pts], ..Default::default() }
    }

    #[test]
    fn fit_recovers_metric_depth() {
        let w = 64usize;
        let cam = Camera::look_at([0.0, 0.0, 0.0], [0.0, 0.0, -2.0], w as u32, w as u32, 90.0);
        let cloud = wall_cloud(80, 2.0, 0.8);
        let vz = project_view(&cam, &cloud);
        // rel = inverse depth (DAV2-style), noisy affine of truth
        let rel: Vec<f32> = (0..w * w)
            .map(|i| {
                let zb = vz.z[i].load(Ordering::Relaxed);
                if zb == u32::MAX {
                    0.5
                } else {
                    1.0 / f32::from_bits(zb)
                }
            })
            .collect();
        let opts = LearnedOptions { min_pairs: 32, ..Default::default() };
        let dm = finish_view(&rel, &cam, 0, &vz, &opts).expect("fit should succeed");
        let centre = dm.depth[33 * w + 32];
        assert!((centre - 2.0).abs() < 0.05, "centre depth {centre}");
        assert!(dm.confidence[33 * w + 32] > 0.1);
        assert!(dm.pairs > 32);
    }

    #[test]
    fn irls_ignores_outliers() {
        let mut pairs: Vec<(f32, f32)> = (0..500).map(|i| {
            let x = i as f32 / 500.0;
            (x, 2.0 * x + 0.1)
        }).collect();
        for i in 0..50 {
            pairs.push((i as f32 / 50.0, 100.0));
        }
        let (a, b, _rmse) = fit_affine_irls(&pairs, 4);
        assert!((a - 2.0).abs() < 0.05 && (b - 0.1).abs() < 0.05, "a={a} b={b}");
    }

    #[test]
    fn skips_without_model() {
        let cap = Capture::default();
        let cloud = PointCloud::default();
        assert!(complete_depth_opts(&cap, &cloud, &LearnedOptions::default()).is_empty());
    }
}
