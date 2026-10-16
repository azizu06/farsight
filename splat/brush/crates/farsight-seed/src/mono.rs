//! Monocular depth completion (Neural Engine / ANE lane).
//!
//! Depth Anything V2 Small (ONNX) per view -> robust scale/shift fit against
//! the measured LiDAR cloud -> metric DepthMap with per-pixel confidence.
//! Ports farsight-compiler `learned/mod.rs` onto SeedView/glam types.
//!
//! Builds:
//!   - default: no ONNX runtime — `complete()` returns empty (Mac run docs it)
//!   - `--features onnx`: links `ort` (CPU EP on Linux for correctness)
//!   - `--features coreml` (macOS): registers CoreML EP (ComputeUnits::All,
//!     MLProgram, static shapes) so inference runs on the Neural Engine
//!     on a worker thread concurrent with GPU training.

use crate::views::SeedView;
use glam::Vec3;
use rayon::prelude::*;
use std::path::Path;
use std::sync::atomic::{AtomicU32, Ordering};

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct MonoConfig {
    /// Depth Anything V2 input edge (square).
    pub model_input: u32,
    /// Min measured-sample pairs before trusting a fit.
    pub min_pairs: usize,
    /// Huber IRLS iterations for the scale/shift fit.
    pub huber_iters: usize,
    /// Confidence window radius (px) for local measured support.
    pub conf_radius: u32,
    /// Drop a view when normalised fit RMSE exceeds this (inverse-depth units
    /// relative to 1/z variance — > this means the model disagrees with LiDAR).
    pub max_fit_rmse: f32,
    /// Max views to run the model on (0 = all).
    pub max_frames: usize,
    /// Candidate pixel stride when back-projecting fill points.
    pub px_step: u32,
    /// Min per-pixel confidence to emit a fill point.
    pub conf_min: f32,
    /// Only synthesise where the measured cloud has NO coverage within
    /// `hole_px` px (keeps mono points out of measured areas).
    pub hole_px: u32,
}

impl Default for MonoConfig {
    fn default() -> Self {
        MonoConfig {
            model_input: 518,
            min_pairs: 200,
            huber_iters: 4,
            conf_radius: 12,
            max_fit_rmse: 0.5,
            max_frames: 0,
            px_step: 8,
            conf_min: 0.25,
            hole_px: 2,
        }
    }
}

/// Metric dense depth for one view: row-major `width*height`, metres,
/// plus confidence 0..1. `scale`/`shift` map model output rel→1/z.
#[derive(Clone, Debug)]
pub struct DepthMap {
    pub view_index: usize,
    pub width: u32,
    pub height: u32,
    pub depth: Vec<f32>,
    pub confidence: Vec<f32>,
    pub scale: f32,
    pub shift: f32,
    pub pairs: usize,
    pub fit_rmse: f32,
}

/// Z-buffer of measured cloud depth in one view (nearest wins) + hit counts.
pub struct ViewZ {
    pub w: usize,
    pub h: usize,
    z: Vec<AtomicU32>,
    cnt: Vec<AtomicU32>,
}

impl ViewZ {
    #[inline]
    pub fn depth_at(&self, x: usize, y: usize) -> Option<f32> {
        let b = self.z[y * self.w + x].load(Ordering::Relaxed);
        if b == u32::MAX {
            None
        } else {
            Some(f32::from_bits(b))
        }
    }
    #[inline]
    pub fn count_at(&self, x: usize, y: usize) -> u32 {
        self.cnt[y * self.w + x].load(Ordering::Relaxed)
    }
}

/// Project the measured cloud into a view (nearest depth + count per px).
pub fn project_view(cam: &crate::views::SeedCamera, pts: &[Vec3]) -> ViewZ {
    let (w, h) = (cam.width as usize, cam.height as usize);
    let v = ViewZ {
        w,
        h,
        z: (0..w * h).map(|_| AtomicU32::new(u32::MAX)).collect(),
        cnt: (0..w * h).map(|_| AtomicU32::new(0)).collect(),
    };
    pts.par_chunks(8192).for_each(|chunk| {
        for &p in chunk {
            if let Some((u, vv, d)) = cam.project(p) {
                let (x, y) = (u.floor() as i32, vv.floor() as i32);
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

/// IRLS fit of inverse depth `y = a*rel + b` (Huber, MAD scale).
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
    let (mut a, mut b) = {
        let (x, y) = lsq(&w);
        (x as f32, y as f32)
    };
    for _ in 0..iters {
        let mut res: Vec<f32> = pairs
            .iter()
            .map(|(x, y)| (a * x + b - y).abs())
            .collect();
        res.sort_by(|p, q| p.partial_cmp(q).unwrap_or(std::cmp::Ordering::Equal));
        let med = res.get(res.len() / 2).copied().unwrap_or(0.0);
        let sigma = (1.4826 * med).max(1e-5);
        let c = 1.345 * sigma;
        for ((x, y), wi) in pairs.iter().zip(w.iter_mut()) {
            let r = (a * x + b - y).abs();
            *wi = if r <= c { 1.0 } else { (c / r) as f64 };
        }
        let (na, nb) = lsq(&w);
        a = na as f32;
        b = nb as f32;
    }
    let rmse = {
        let mut s = 0.0f64;
        for (x, y) in pairs {
            let r = (a * x + b - y) as f64;
            s += r * r;
        }
        (s / pairs.len() as f64).sqrt() as f32
    };
    (a, b, rmse)
}

/// Integral image of hit counts -> local measured density around each px.
fn local_density(vz: &ViewZ, r: u32) -> Vec<f32> {
    let (w, h) = (vz.w, vz.h);
    let mut ii = vec![0f64; (w + 1) * (h + 1)];
    for y in 0..h {
        let mut row = 0f64;
        for x in 0..w {
            row += vz.count_at(x, y) as f64;
            ii[(y + 1) * (w + 1) + x + 1] = ii[y * (w + 1) + x + 1] + row;
        }
    }
    let r = r as i64;
    let mut out = vec![0f32; w * h];
    out.par_chunks_mut(w).enumerate().for_each(|(y, row)| {
        let (y0, y1) = (
            (y as i64 - r).max(0) as usize,
            ((y as i64 + r + 1).min(h as i64)) as usize,
        );
        for (x, cell) in row.iter_mut().enumerate() {
            let (x0, x1) = (
                (x as i64 - r).max(0) as usize,
                ((x as i64 + r + 1).min(w as i64)) as usize,
            );
            let s = ii[y1 * (w + 1) + x1] - ii[y0 * (w + 1) + x1] - ii[y1 * (w + 1) + x0]
                + ii[y0 * (w + 1) + x0];
            *cell = s as f32 / ((x1 - x0) * (y1 - y0)) as f32;
        }
    });
    out
}

/// Convert a view's relative model output into a metric DepthMap by fitting
/// `1/z = a*rel + b` on projected measured samples. Returns None when the
/// fit is weak (few pairs or RMSE over `max_fit_rmse`).
pub fn finish_view(
    rel: &[f32],
    cam: &crate::views::SeedCamera,
    view_idx: usize,
    vz: &ViewZ,
    cfg: &MonoConfig,
) -> Option<DepthMap> {
    let (w, h) = (vz.w, vz.h);
    let pairs: Vec<(f32, f32)> = (0..h)
        .into_par_iter()
        .flat_map_iter(|y| {
            (0..w).filter_map(move |x| {
                let z = vz.depth_at(x, y)?;
                if z <= 1e-4 {
                    return None;
                }
                Some((rel[y * w + x], 1.0 / z))
            })
        })
        .collect();
    if pairs.len() < cfg.min_pairs {
        return None;
    }
    let (a, b, rmse) = fit_affine_irls(&pairs, cfg.huber_iters);
    let ymean = pairs.iter().map(|p| p.1 as f64).sum::<f64>() / pairs.len() as f64;
    let yvar =
        pairs.iter().map(|p| (p.1 as f64 - ymean) * (p.1 as f64 - ymean)).sum::<f64>() / pairs.len() as f64;
    let q_fit = if yvar <= 1e-9 {
        0.5
    } else {
        (1.0 - rmse as f64 / yvar.sqrt()).clamp(0.0, 1.0) as f32
    };
    if (1.0 - q_fit) > cfg.max_fit_rmse {
        return None; // model disagrees with LiDAR on this view
    }
    let density = local_density(vz, cfg.conf_radius);
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
                crow[x] = if d > 0.0 {
                    q_fit * (density[i] / 0.04).min(1.0)
                } else {
                    0.0
                };
            }
        });
    Some(DepthMap {
        view_index: view_idx,
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

/// World position of pixel (u,v) at axial `depth` (along camera -Z).
pub fn backproject(cam: &crate::views::SeedCamera, u: f32, v: f32, depth: f32) -> Vec3 {
    let r = cam.ray(u, v);
    let m = &cam.c2w;
    let fwd = Vec3::new(-m[0][2], -m[1][2], -m[2][2]); // camera -Z in world
    let t = depth / r.dot(fwd).max(1e-6);
    cam.position() + r * t
}

/// Back-project confident, uncovered pixels of `maps` into fill candidates.
/// A pixel is "uncovered" when the measured ViewZ is empty within `hole_px`.
/// Candidates are voxel-deduplicated against `existing` (spacing from the
/// cloud's median spacing).
pub fn fill_from_maps(
    maps: &[DepthMap],
    views: &[SeedView],
    vz_per_view: &[Option<ViewZ>],
    existing: &[Vec3],
    spacing: f32,
    cfg: &MonoConfig,
    max_out: usize,
) -> Vec<(Vec3, f32, usize)> {
    // dedup on the existing cloud's voxel grid
    let vox = (spacing * 1.5).max(0.01);
    let mut seen: rustc_hash::FxHashSet<(i32, i32, i32)> = rustc_hash::FxHashSet::default();
    for p in existing {
        seen.insert((
            (p.x / vox).floor() as i32,
            (p.y / vox).floor() as i32,
            (p.z / vox).floor() as i32,
        ));
    }
    let mut out = Vec::new();
    let step = cfg.px_step.max(1) as usize;
    for m in maps {
        let Some(cam) = views.get(m.view_index).map(|v| &v.camera) else {
            continue;
        };
        let Some(vz) = vz_per_view.get(m.view_index).and_then(|o| o.as_ref()) else {
            continue;
        };
        let hp = cfg.hole_px as i32;
        for y in (0..m.height as usize).step_by(step) {
            for x in (0..m.width as usize).step_by(step) {
                if out.len() >= max_out {
                    return out;
                }
                let i = y * m.width as usize + x;
                let d = m.depth[i];
                if d <= 0.0 || m.confidence[i] < cfg.conf_min {
                    continue;
                }
                // uncovered test: no measured dot within hole_px window
                let mut covered = false;
                'w: for dy in -hp..=hp {
                    for dx in -hp..=hp {
                        let (xx, yy) = (x as i32 + dx, y as i32 + dy);
                        if xx >= 0 && yy >= 0 && xx < vz.w as i32 && yy < vz.h as i32 {
                            if vz.count_at(xx as usize, yy as usize) > 0 {
                                covered = true;
                                break 'w;
                            }
                        }
                    }
                }
                if covered {
                    continue;
                }
                let p = backproject(cam, x as f32 + 0.5, y as f32 + 0.5, d);
                let key = (
                    (p.x / vox).floor() as i32,
                    (p.y / vox).floor() as i32,
                    (p.z / vox).floor() as i32,
                );
                if seen.insert(key) {
                    out.push((p, m.confidence[i], m.view_index));
                }
            }
        }
    }
    out
}

// ---------------- ONNX inference (feature-gated) ----------------

/// Run mono depth over `views` (needs decoded `view.image`), returning
/// metric DepthMaps for views that pass the fit gate. Empty vec when built
/// without `onnx` or no model path.
pub fn complete(views: &[SeedView], pts: &[Vec3], model: &str, cfg: &MonoConfig) -> Vec<DepthMap> {
    if model.is_empty() || !Path::new(model).exists() {
        return Vec::new();
    }
    #[cfg(feature = "onnx")]
    {
        match onnx::run(views, pts, model, cfg) {
            Ok(maps) => return maps,
            Err(e) => {
                eprintln!("farsight-seed mono: onnx failed ({e}); skipping");
                return Vec::new();
            }
        }
    }
    #[cfg(not(feature = "onnx"))]
    {
        let _ = (views, pts, cfg);
        eprintln!("farsight-seed mono: built without `onnx` feature; skipping depth completion ({model})");
        Vec::new()
    }
}

#[cfg(feature = "onnx")]
mod onnx {
    use super::*;
    use anyhow::{bail, Context, Result};
    use image::imageops::FilterType;
    use ort::session::Session;
    use ort::value::Tensor;
    use std::path::PathBuf;

    pub fn run(views: &[SeedView], pts: &[Vec3], model: &str, cfg: &MonoConfig) -> Result<Vec<DepthMap>> {
        #[allow(unused_mut)]
        let mut builder = Session::builder().map_err(|e| anyhow::anyhow!("ort builder: {e}"))?;
        #[cfg(feature = "coreml")]
        let mut builder = {
            use ort::ep::coreml::{ComputeUnits, ModelFormat};
            let mut builder = builder;
            // ANE: whole graph resident, MLProgram + ALL units + static shapes.
            builder.with_execution_providers([
                ort::ep::CoreML::default()
                    .with_model_format(ModelFormat::MLProgram)
                    .with_compute_units(ComputeUnits::All)
                    .with_static_input_shapes(true)
                    .build(),
            ]).map_err(|e| anyhow::anyhow!("ort coreml ep: {e}"))?
        };
        let mut session = builder
            .commit_from_file(PathBuf::from(model))
            .map_err(|e| anyhow::anyhow!("ort session: {e}"))?;
        let n = if cfg.max_frames > 0 {
            cfg.max_frames.min(views.len())
        } else {
            views.len()
        };
        let mut out = Vec::new();
        for i in 0..n {
            let Some(view) = views.get(i) else { continue };
            let Some(img) = &view.image else { continue };
            let t = std::time::Instant::now();
            let (rel, rw, rh) = infer(&mut session, img, cfg).with_context(|| format!("view {i}"))?;
            let rel_full = resize_rel(&rel, rw, rh, view.camera.width as usize, view.camera.height as usize);
            let vz = project_view(&view.camera, pts);
            let ms = t.elapsed().as_secs_f64() * 1e3;
            if let Some(dm) = finish_view(&rel_full, &view.camera, i, &vz, cfg) {
                eprintln!(
                    "mono: view {i}: {ms:.0}ms pairs={} rmse={:.3} a={:.4} b={:.4}",
                    dm.pairs, dm.fit_rmse, dm.scale, dm.shift
                );
                out.push(dm);
            } else {
                eprintln!("mono: view {i}: {ms:.0}ms fit rejected (pairs or rmse)");
            }
        }
        // ort with load-dynamic can segfault in Session::drop after the
        // provider lib is unloaded — leak it (process is exiting anyway).
        std::mem::forget(session);
        Ok(out)
    }

    fn infer(session: &mut Session, img: &image::RgbImage, cfg: &MonoConfig) -> Result<(Vec<f32>, usize, usize)> {
        let (iw, ih) = (cfg.model_input.max(32) as usize, cfg.model_input.max(32) as usize);
        let resized = image::imageops::resize(img, iw as u32, ih as u32, FilterType::Triangle);
        const MEAN: [f32; 3] = [0.485, 0.456, 0.406];
        const STD: [f32; 3] = [0.229, 0.224, 0.225];
        let mut data = vec![0f32; 3 * ih * iw];
        for y in 0..ih {
            for x in 0..iw {
                let p = resized.get_pixel(x as u32, y as u32);
                for c in 0..3 {
                    data[c * ih * iw + y * iw + x] = (p[c] as f32 / 255.0 - MEAN[c]) / STD[c];
                }
            }
        }
        let input = Tensor::from_array((vec![1i64, 3, ih as i64, iw as i64], data))
            .map_err(|e| anyhow::anyhow!("ort tensor: {e}"))?;
        let outputs = session
            .run(ort::inputs![input])
            .map_err(|e| anyhow::anyhow!("ort run: {e}"))?;
        let (_name, out) = outputs.iter().next().context("no model output")?;
        let (shape, vals) = out
            .try_extract_tensor::<f32>()
            .map_err(|e| anyhow::anyhow!("ort extract: {e}"))?;
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

    #[test]
    fn irls_ignores_outliers() {
        let mut pairs: Vec<(f32, f32)> = (0..500)
            .map(|i| {
                let x = i as f32 / 500.0;
                (x, 2.0 * x + 0.1)
            })
            .collect();
        for i in 0..50 {
            pairs.push((i as f32 / 50.0, 100.0));
        }
        let (a, b, _r) = fit_affine_irls(&pairs, 4);
        assert!((a - 2.0).abs() < 0.05 && (b - 0.1).abs() < 0.05, "a={a} b={b}");
    }
}
