//! Held-out scoring — port of `score_heldout.py`: per-view PSNR between
//! Brush's `eval_<iter>/` renders and the real photos (resized to match),
//! reported mean/median/min/max.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Serialize;

#[derive(Clone, Debug, Default, Serialize)]
pub struct ViewScore {
    pub view: String,
    pub psnr: f64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ScoreReport {
    pub views: usize,
    pub psnr_mean: f64,
    pub psnr_median: f64,
    pub psnr_min: f64,
    pub psnr_max: f64,
    pub per_view: Vec<ViewScore>,
    pub ssim_mean: Option<f64>,
}

/// PSNR of one rendered image against its ground-truth photo, using
/// score_heldout.py's formula: 10*log10(255^2 / mse(u8 diff)), render resized
/// to the GT size when shapes differ.
fn psnr_pair(gt: &image::RgbImage, render: &image::RgbImage) -> f64 {
    let render = if render.dimensions() != gt.dimensions() {
        image::imageops::resize(
            render,
            gt.width(),
            gt.height(),
            image::imageops::FilterType::Triangle,
        )
    } else {
        render.clone()
    };
    let mut mse = 0.0f64;
    for (a, b) in gt.pixels().zip(render.pixels()) {
        for c in 0..3 {
            let d = a[c] as f64 - b[c] as f64;
            mse += d * d;
        }
    }
    mse /= (gt.width() * gt.height() * 3) as f64;
    if mse <= 0.0 {
        return f64::INFINITY;
    }
    10.0 * (255.0f64.powi(2) / mse).log10()
}

/// Score every render in `eval_dir` against the matching photo under
/// `images_root`: `eval_dir/images/frame_x.png` -> `images_root/images/frame_x.*`.
/// Returns per-view PSNR (sorted by filename) — same pairing as the python.
pub fn score_eval_dir(images_root: &Path, eval_dir: &Path) -> Result<ScoreReport> {
    let mut per_view = Vec::new();
    let renders: Vec<PathBuf> = walk_pngs(eval_dir)?;
    for r in renders {
        let rel = r.strip_prefix(eval_dir).unwrap_or(&r);
        let stem = rel.with_extension("");
        let gt_path = find_image(images_root, &stem);
        let Some(gt_path) = gt_path else { continue };
        let gt = image::ImageReader::open(&gt_path)
            .ok()
            .and_then(|x| x.decode().ok())
            .map(|d| d.to_rgb8());
        let render = image::ImageReader::open(&r)
            .ok()
            .and_then(|x| x.decode().ok())
            .map(|d| d.to_rgb8());
        let (Some(gt), Some(render)) = (gt, render) else {
            continue;
        };
        let p = psnr_pair(&gt, &render);
        per_view.push(ViewScore {
            view: rel.display().to_string(),
            psnr: p,
        });
    }
    Ok(report_of(per_view))
}

/// Score renders held in memory: `pairs` = (name, gt rgb, render rgb).
pub fn score_pairs(pairs: Vec<(String, image::RgbImage, image::RgbImage)>) -> ScoreReport {
    let per_view = pairs
        .into_iter()
        .map(|(view, gt, r)| ViewScore {
            view,
            psnr: psnr_pair(&gt, &r),
        })
        .collect();
    report_of(per_view)
}

/// Public alias of [`report_of`] for callers that built ViewScores elsewhere.
pub fn report_of_public(per_view: Vec<ViewScore>) -> ScoreReport {
    report_of(per_view)
}

fn report_of(mut per_view: Vec<ViewScore>) -> ScoreReport {
    per_view.sort_by(|a, b| a.view.cmp(&b.view));
    let mut psnrs: Vec<f64> = per_view.iter().map(|v| v.psnr).collect();
    psnrs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = psnrs.len();
    let rep = if n == 0 {
        ScoreReport::default()
    } else {
        ScoreReport {
            views: n,
            psnr_mean: psnrs.iter().sum::<f64>() / n as f64,
            psnr_median: psnrs[n / 2],
            psnr_min: psnrs[0],
            psnr_max: psnrs[n - 1],
            per_view,
            ssim_mean: None,
        }
    };
    rep
}

fn walk_pngs(dir: &Path) -> Result<Vec<PathBuf>> {
    let mut out = vec![];
    if !dir.is_dir() {
        return Ok(out);
    }
    for e in std::fs::read_dir(dir)? {
        let p = e?.path();
        if p.is_dir() {
            out.extend(walk_pngs(&p)?);
        } else if p.extension().is_some_and(|x| x == "png") {
            out.push(p);
        }
    }
    out.sort();
    Ok(out)
}

fn find_image(root: &Path, stem: &Path) -> Option<PathBuf> {
    let dir = root.join(stem.parent()?);
    let name = stem.file_name()?.to_string_lossy();
    for e in std::fs::read_dir(dir).ok()? {
        let p = e.ok()?.path();
        if p.file_stem().is_some_and(|s| s == name.as_ref()) {
            return Some(p);
        }
    }
    None
}

/// Resolve `<out>/eval_<iter>`: the exact dir when `iter` given, else the
/// highest-numbered eval_* directory under `out`.
pub fn resolve_eval_dir(out: &Path, iter: Option<u32>) -> Option<PathBuf> {
    if let Some(i) = iter {
        let p = out.join(format!("eval_{i}"));
        return p.is_dir().then_some(p);
    }
    let mut best: Option<(u32, PathBuf)> = None;
    for e in std::fs::read_dir(out).ok()? {
        let p = e.ok()?.path();
        let Some(name) = p.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if let Some(num) = name.strip_prefix("eval_").and_then(|s| s.parse::<u32>().ok())
            && p.is_dir()
            && best.as_ref().is_none_or(|(b, _)| num > *b)
        {
            best = Some((num, p));
        }
    }
    best.map(|(_, p)| p)
}
