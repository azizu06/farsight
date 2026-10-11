//! Optional final stage (default off): refine the compiled surfels into a
//! photo-real Gaussian splat by training Brush on the posed capture, seeded
//! from our compiled surfels (`export::brush::write_seed`). Brush is spawned
//! right after the seed lands so the texture bake / export overlap training.
//!
//! Presets (mirroring tools/splat/run.sh + train_job.ps1; swap for W7's
//! `refine_presets` when it lands): `fast60` ≈ 2.5k steps for the 1-minute
//! budget, `max` = the full 30k-step quality run.

use crate::core::{Capture, Mesh, PointCloud};
use crate::export::brush;
use crate::Stats;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Instant;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct RefineOptions {
    /// Run the refine stage after the seed export.
    pub enabled: bool,
    /// Brush arg preset name: fast60 | max.
    pub preset: String,
    /// Path to the brush_app binary (else $BRUSH, else the tools/splat cache).
    pub brush: Option<PathBuf>,
    /// Cap on seed surfels (~200-300k recommended for fast60).
    pub seed_max: usize,
    /// Base opacity for seed surfels.
    pub opacity: f32,
    /// Flat-disk thin axis ratio.
    pub thin: f32,
    /// Compiler output dir (the brush/ workdir lives inside it).
    pub dir: PathBuf,
    /// Source nerfstudio capture dir for image symlinks.
    pub capture_dir: Option<PathBuf>,
    /// Share of seed_max drawn from mesh-surface sampling (rest from cloud).
    pub mesh_share: f32,
    /// Total steps override (0 = preset value).
    pub steps: u32,
}

impl Default for RefineOptions {
    fn default() -> Self {
        RefineOptions {
            enabled: false,
            preset: "fast60".into(),
            brush: None,
            seed_max: 250_000,
            opacity: 0.8,
            thin: 0.15,
            dir: PathBuf::new(),
            capture_dir: None,
            mesh_share: 0.5,
            steps: 0,
        }
    }
}

/// Brush CLI args per preset (mirrors tools/postprocess/train_job.ps1).
pub fn preset_args(preset: &str, steps_override: u32) -> Vec<String> {
    let (steps, every, eval_every) = match preset {
        "max" => (30_000, 5_000, 30_000),
        _ => (2_500, 500, 2_500),
    };
    let steps = if steps_override > 0 { steps_override } else { steps };
    [
        "--total-steps".into(),
        steps.to_string(),
        "--export-every".into(),
        every.to_string(),
        "--export-path".into(),
        "EXPORTS".into(),
        "--max-splats".into(),
        "300000".into(),
        "--eval-split-every".into(),
        "8".into(),
        "--eval-every".into(),
        eval_every.to_string(),
        "--eval-save-to-disk".into(),
    ]
    .into_iter()
    .collect()
}

fn locate_brush(explicit: Option<&Path>) -> Option<PathBuf> {
    if let Some(p) = explicit {
        return Some(p.to_path_buf());
    }
    if let Ok(p) = std::env::var("BRUSH") {
        return Some(PathBuf::from(p));
    }
    // same pinned cache layout as tools/splat/run.sh
    let cache = std::env::var("XDG_CACHE_HOME").map(PathBuf::from).unwrap_or_else(|_| {
        PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".cache")
    });
    let cand = cache.join("farsight-splat/brush-v0.3.0/brush-app-aarch64-apple-darwin/brush_app");
    if cand.exists() {
        return Some(cand);
    }
    // last resort: PATH lookup
    std::env::var("PATH")
        .ok()?
        .split(':')
        .map(|d| PathBuf::from(d).join("brush_app"))
        .find(|p| p.exists())
}

/// A spawned refine job; `finish` collects it. `child` is None when the
/// seed was written but no brush_app binary was found.
pub struct RefineHandle {
    pub child: Option<Child>,
    pub started: Instant,
    pub workdir: PathBuf,
    pub exports: PathBuf,
    pub images: PathBuf,
    pub seed_stats: Stats,
    pub steps: u32,
}

/// Build the seed and spawn Brush. Mesh-surface samples (flat disks on the
/// baked/owned surfels) + decimated densified cloud fill the seed budget.
pub fn start(cloud: &PointCloud, mesh: &Mesh, capture: Option<&Capture>, opts: &RefineOptions) -> Option<RefineHandle> {
    if !opts.enabled {
        return None;
    }
    let cap = capture?;
    let out = (|| -> Result<(PathBuf, Stats)> {
        // seed = mesh surface surfels + decimated densified cloud
        let mesh_target = (opts.seed_max as f32 * opts.mesh_share) as usize;
        let cloud_target = opts.seed_max.saturating_sub(mesh_target);
        let mut seed = brush::sample_surfels(mesh, 0.01, mesh_target);
        let voxel = crate::export::splat::median_radius(cloud, 0.01);
        let dc = crate::export::splat::decimate(cloud, cloud_target, voxel);
        seed.pos.extend(dc.pos);
        seed.col.extend(dc.col);
        seed.nrm.extend(dc.nrm);
        seed.weight.extend(dc.weight);
        seed.radius.extend(dc.radius);
        seed.flags.extend(dc.flags);
        let stats = brush::write_seed(&opts.dir, &seed, cap, opts.capture_dir.as_deref(), opts.opacity, opts.thin, 0.01)?;
        Ok((opts.dir.join("brush"), stats))
    })();
    let (workdir, seed_stats) = match out {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[refine] seed failed: {e:#}");
            return None;
        }
    };
    let exports = workdir.join("exports");
    let images = workdir.join("images");
    let steps = if opts.steps > 0 { opts.steps } else if opts.preset == "max" { 30_000 } else { 2_500 };
    let Some(bin) = locate_brush(opts.brush.as_deref()) else {
        eprintln!("[refine] brush_app not found (set --brush or $BRUSH); seed left at {}", workdir.display());
        return Some(RefineHandle { child: None, started: Instant::now(), workdir, exports, images, seed_stats, steps });
    };
    let _ = std::fs::create_dir_all(&exports);
    let logf = std::fs::File::create(workdir.join("brush.log")).ok()?;
    let args: Vec<String> = preset_args(&opts.preset, opts.steps)
        .into_iter()
        .map(|a| if a == "EXPORTS" { exports.display().to_string() } else { a })
        .collect();
    let child = Command::new(&bin)
        .arg(&workdir)
        .args(&args)
        .stdout(Stdio::from(logf.try_clone().ok()?))
        .stderr(Stdio::from(logf))
        .spawn();
    let t0 = Instant::now();
    match child {
        Ok(child) => {
            eprintln!("[refine] brush spawned ({} steps) on {}", steps, workdir.display());
            Some(RefineHandle { child: Some(child), started: t0, workdir, exports, images, seed_stats, steps })
        }
        Err(e) => {
            eprintln!("[refine] spawn failed: {e}");
            None
        }
    }
}

/// PSNR (score_heldout.py equivalent): for each render PNG in `eval_dir`,
/// compare with `gt_dir/<stem>.jpg|png`, resizing the render to GT size.
pub fn psnr_dir(gt_dir: &Path, eval_dir: &Path) -> serde_json::Value {
    let mut ps: Vec<f64> = Vec::new();
    let mut rd = std::fs::read_dir(eval_dir).ok().map(|d| d.collect::<Result<Vec<_>, _>>());
    let mut names: Vec<PathBuf> = match &mut rd {
        Some(Ok(v)) => v.drain(..).map(|e| e.path()).filter(|p| p.extension().map(|e| e == "png").unwrap_or(false)).collect(),
        _ => Vec::new(),
    };
    names.sort();
    for p in names {
        let stem = p.file_stem().unwrap_or_default().to_string_lossy().to_string();
        let gt = ["jpg", "jpeg", "png"].iter().map(|e| gt_dir.join(format!("{stem}.{e}"))).find(|g| g.exists());
        let Some(gt) = gt else { continue };
        let (Ok(g), Ok(r)) = (image::open(&gt), image::open(&p)) else { continue };
        let g = g.to_rgb8();
        let r = if r.width() == g.width() && r.height() == g.height() {
            r.to_rgb8()
        } else {
            image::imageops::resize(&r.to_rgb8(), g.width(), g.height(), image::imageops::FilterType::Triangle)
        };
        let mut mse = 0f64;
        for (a, b) in g.pixels().zip(r.pixels()) {
            for k in 0..3 {
                let d = a[k] as f64 - b[k] as f64;
                mse += d * d;
            }
        }
        mse /= (g.width() * g.height() * 3) as f64;
        if mse > 0.0 {
            ps.push(10.0 * (255.0f64 * 255.0 / mse).log10());
        }
    }
    if ps.is_empty() {
        return serde_json::json!({ "views": 0 });
    }
    let mut s = ps.clone();
    s.sort_by(|a, b| a.total_cmp(b));
    serde_json::json!({
        "views": ps.len(),
        "mean": ps.iter().sum::<f64>() / ps.len() as f64,
        "median": s[s.len() / 2],
        "min": s[0],
        "max": s[s.len() - 1],
    })
}

/// Wait for Brush, promote the newest export to `refined.ply` + `refined.splat`,
/// score held-out eval renders, and return report stats. Never hard-fails.
pub fn finish(h: &mut Option<RefineHandle>, out: &Path) -> Stats {
    let Some(h) = h.take() else {
        return serde_json::json!({ "enabled": false });
    };
    let mut st = serde_json::json!({ "enabled": true, "seed": h.seed_stats });
    let Some(child) = h.child else {
        st["ran"] = serde_json::json!(false);
        st["note"] = serde_json::json!("brush_app not found; seed written under out/brush");
        return st;
    };
    st["ran"] = serde_json::json!(true);
    let status = child.wait_with_output();
    let train_s = h.started.elapsed().as_secs_f64();
    st["train_s"] = serde_json::json!(train_s);
    match status {
        Ok(o) => {
            st["exit_ok"] = serde_json::json!(o.status.success());
            if !o.status.success() {
                st["note"] = serde_json::json!("brush exited non-zero; see brush/brush.log");
            }
        }
        Err(e) => {
            st["note"] = serde_json::json!(format!("brush wait failed: {e}"));
            return st;
        }
    }
    // newest export ply -> refined.ply + refined.splat
    let mut latest: Option<(u32, PathBuf)> = None;
    if let Ok(rd) = std::fs::read_dir(&h.exports) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().map(|x| x == "ply").unwrap_or(false) {
                let step = p.file_stem().unwrap_or_default().to_string_lossy().rsplit('_').next().and_then(|s| s.parse::<u32>().ok()).unwrap_or(0);
                if latest.as_ref().map(|(s, _)| step >= *s).unwrap_or(true) {
                    latest = Some((step, p));
                }
            }
        }
    }
    if let Some((step, p)) = latest {
        st["steps_exported"] = serde_json::json!(step);
        st["steps_per_s"] = serde_json::json!(step as f64 / train_s.max(1e-3));
        let dst = out.join("refined.ply");
        if std::fs::copy(&p, &dst).is_ok() {
            st["refined_ply"] = serde_json::json!(dst.display().to_string());
        }
        if let Ok(rc) = brush::read_splat_ply(&p) {
            let bytes = crate::export::splat::compact(&rc, 1.0, 1.0, 0.15, 0.01);
            let dst = out.join("refined.splat");
            if std::fs::write(&dst, bytes).is_ok() {
                st["refined_splat"] = serde_json::json!(dst.display().to_string());
            }
        }
    } else {
        st["note"] = serde_json::json!("no export ply produced; see brush/brush.log");
    }
    // held-out PSNR from eval renders (eval_<step>/ inside exports dir)
    if let Ok(rd) = std::fs::read_dir(&h.exports) {
        let mut best = serde_json::json!({ "views": 0 });
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() && p.file_name().unwrap_or_default().to_string_lossy().starts_with("eval") {
                let v = psnr_dir(&h.images, &p);
                if v["views"].as_u64().unwrap_or(0) > best["views"].as_u64().unwrap_or(0)
                    || v["mean"].as_f64().unwrap_or(0.0) > best["mean"].as_f64().unwrap_or(0.0)
                {
                    best = v;
                }
            }
        }
        st["heldout_psnr"] = best;
    }
    eprintln!("[refine] done: {st}");
    st
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preset_args_shape() {
        let a = preset_args("fast60", 0);
        assert!(a.windows(2).any(|w| w == ["--total-steps", "2500"]));
        assert!(a.iter().any(|x| x == "--eval-split-every"));
        let m = preset_args("max", 1234);
        assert!(m.windows(2).any(|w| w == ["--total-steps", "1234"]));
    }

    #[test]
    fn psnr_identical_images() {
        let dir = std::env::temp_dir().join("fc_psnr_test");
        let (gt, ev) = (dir.join("gt"), dir.join("ev"));
        std::fs::create_dir_all(&gt).unwrap();
        std::fs::create_dir_all(&ev).unwrap();
        let img = image::RgbImage::from_fn(8, 8, |x, y| image::Rgb([(x * 8) as u8, (y * 8) as u8, 100]));
        img.save(gt.join("a.jpg")).unwrap();
        img.save(ev.join("a.png")).unwrap();
        let r = psnr_dir(&gt, &ev);
        assert!(r["views"].as_u64() == Some(1));
        assert!(r["mean"].as_f64().unwrap() > 40.0, "identical images -> high psnr, got {r}");
    }
}
