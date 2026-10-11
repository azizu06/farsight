//! nerfstudio capture dir (`transforms.json` + images) -> `Capture`.
//! Layout per `docs/postrun-capture.md` and `synth::write_capture`: per-frame
//! `file_path`, `transform_matrix` (row-major camera-to-world, OpenGL axes),
//! `fl_x/fl_y/cx/cy/w/h` per frame or at document level. JPEG/PNG decoded in
//! parallel; `IoOptions::capture_downscale` shrinks images and intrinsics.
use crate::core::{Camera, Capture, Image};
use anyhow::{bail, Context, Result};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};

/// Frame culling for capture loading — a port of `tools/postprocess/cull.py`
/// (dechante-3d-pipeline) plus pose-coverage sampling. Drops frames that hurt
/// reconstruction: non-normal tracking, parked near-duplicates, blurry frames,
/// and any excess over a pose-coverage cap. On by default.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct CaptureFilter {
    /// Master switch (false = load every decodable frame).
    pub enabled: bool,
    /// Parked grouping distance / angle vs the group's first frame.
    pub parked_m: f32,
    pub parked_deg: f32,
    /// Fraction of parked-group winners dropped by lowest sharpness.
    pub blur_frac: f32,
    /// Grey image size (max dimension, px) the sharpness score is computed on.
    pub sharp_px: u32,
    /// Pose-coverage cap: farthest-point sampling on camera position plus
    /// view direction (0 = keep all).
    pub max_views: usize,
    /// Optional raw frame range cut [first, last] inclusive (find_drives.py
    /// `--cut`), applied before every other cull.
    pub drive: Option<[usize; 2]>,
    /// Forward-direction weight (metres per unit vector) in coverage sampling.
    pub dir_weight: f32,
}

impl Default for CaptureFilter {
    fn default() -> Self {
        CaptureFilter {
            enabled: true,
            parked_m: 0.005,
            parked_deg: 0.5,
            blur_frac: 0.10,
            sharp_px: 256,
            max_views: 160,
            drive: None,
            dir_weight: 0.5,
        }
    }
}

/// Rotation angle (deg) between two c2w rotation parts.
fn rot_angle(a: &[[f32; 4]; 4], b: &[[f32; 4]; 4]) -> f32 {
    let mut tr = 0f32;
    for i in 0..3 {
        for j in 0..3 {
            tr += a[j][i] * b[j][i];
        }
    }
    (((tr - 1.0) * 0.5).clamp(-1.0, 1.0)).acos().to_degrees()
}

/// Laplacian variance on a downscaled grey image — frame sharpness.
fn sharpness(img: &Image, px: u32) -> f32 {
    let (w, h) = (img.width as usize, img.height as usize);
    if w < 3 || h < 3 {
        return 0.0;
    }
    let s = (px as f64 / w.max(h) as f64).min(1.0);
    let (sw, sh) = (((w as f64 * s) as usize).max(3), ((h as f64 * s) as usize).max(3));
    let mut g = vec![0f32; sw * sh];
    for y in 0..sh {
        let sy = ((y as f64 + 0.5) / sh as f64 * h as f64) as usize;
        let sy = sy.min(h - 1);
        for x in 0..sw {
            let sx = ((x as f64 + 0.5) / sw as f64 * w as f64) as usize;
            let sx = sx.min(w - 1);
            let i = (sy * w + sx) * 3;
            g[y * sw + x] = 0.299 * img.rgb[i] as f32 + 0.587 * img.rgb[i + 1] as f32 + 0.114 * img.rgb[i + 2] as f32;
        }
    }
    // 4-neighbour Laplacian, variance over interior.
    let (mut n, mut sum, mut sq) = (0u64, 0f64, 0f64);
    for y in 1..sh - 1 {
        for x in 1..sw - 1 {
            let c = g[y * sw + x] * 4.0 - g[y * sw + x - 1] - g[y * sw + x + 1] - g[(y - 1) * sw + x] - g[(y + 1) * sw + x];
            sum += c as f64;
            sq += (c * c) as f64;
            n += 1;
        }
    }
    if n == 0 {
        return 0.0;
    }
    let mean = sum / n as f64;
    (sq / n as f64 - mean * mean).max(0.0) as f32
}

/// Farthest-point sampling over camera position + `dir_w`-weighted forward.
/// Returns indices in original order (sorted) of a ≤max subset.
fn coverage_subset(cams: &[Camera], max: usize, dir_w: f32) -> Vec<usize> {
    if max == 0 || cams.len() <= max {
        return (0..cams.len()).collect();
    }
    let feat: Vec<[f32; 6]> = cams
        .iter()
        .map(|c| {
            let p = c.position();
            let m = &c.c2w;
            [p[0], p[1], p[2], -m[0][2] * dir_w, -m[1][2] * dir_w, -m[2][2] * dir_w]
        })
        .collect();
    let n = feat.len();
    let mut d2 = vec![f32::INFINITY; n];
    let mut pick = vec![0usize];
    for _ in 0..max {
        let &last = pick.last().unwrap();
        for i in 0..n {
            let mut s = 0f32;
            for k in 0..6 {
                let d = feat[i][k] - feat[last][k];
                s += d * d;
            }
            if s < d2[i] {
                d2[i] = s;
            }
        }
        let (mut best, mut bd) = (usize::MAX, 0f32);
        for i in 0..n {
            if d2[i] > bd && d2[i] > 0.0 {
                bd = d2[i];
                best = i;
            }
        }
        if best == usize::MAX {
            break;
        }
        pick.push(best);
    }
    pick.sort_unstable();
    pick
}

fn f64_at(v: &Value, key: &str) -> Option<f64> {
    v.get(key).and_then(Value::as_f64)
}

fn c2w_of(v: &Value) -> Result<[[f32; 4]; 4]> {
    let tm = v.get("transform_matrix").ok_or_else(|| anyhow::anyhow!("frame lacks transform_matrix"))?;
    if let Some(rows) = tm.as_array() {
        if rows.len() == 4 && rows.iter().all(|r| r.as_array().map(|r| r.len() == 4).unwrap_or(false)) {
            let mut m = [[0f32; 4]; 4];
            for (i, r) in rows.iter().enumerate() {
                for (j, c) in r.as_array().unwrap().iter().enumerate() {
                    m[i][j] = c.as_f64().ok_or_else(|| anyhow::anyhow!("transform_matrix non-numeric"))? as f32;
                }
            }
            return Ok(m);
        }
        if rows.len() == 16 {
            // Flat row-major 4x4.
            let mut m = [[0f32; 4]; 4];
            for (i, c) in rows.iter().enumerate() {
                m[i / 4][i % 4] = c.as_f64().ok_or_else(|| anyhow::anyhow!("transform_matrix non-numeric"))? as f32;
            }
            return Ok(m);
        }
    }
    bail!("transform_matrix must be a 4x4 nested or flat-16 array")
}

fn resolve_frame(dir: &Path, file_path: &str) -> Option<PathBuf> {
    let rel = file_path.trim_start_matches("./");
    let direct = dir.join(rel);
    if direct.is_file() {
        return Some(direct);
    }
    let base = Path::new(rel).file_name()?;
    [dir.join("images").join(base), dir.join(base)].into_iter().find(|c| c.is_file())
}

/// Load a nerfstudio capture directory.
pub fn load(dir: &Path, opts: &super::IoOptions) -> Result<Capture> {
    load_filtered(dir, opts).map(|(cap, _)| cap)
}

/// `load` plus filter statistics (dropped counts per reason).
pub fn load_filtered(dir: &Path, opts: &super::IoOptions) -> Result<(Capture, crate::Stats)> {
    // Accept either the run dir or a direct path to transforms.json.
    let (dir, json_path) = if dir.is_file() {
        (dir.parent().unwrap_or_else(|| Path::new(".")).to_path_buf(), dir.to_path_buf())
    } else {
        (dir.to_path_buf(), dir.join("transforms.json"))
    };
    let doc: Value = serde_json::from_slice(&std::fs::read(&json_path).with_context(|| format!("reading {}", json_path.display()))?)
        .context("transforms.json is not valid JSON")?;
    let frames = doc.get("frames").and_then(Value::as_array).ok_or_else(|| anyhow::anyhow!("transforms.json lacks frames[]"))?;
    let n_frames = frames.len();
    let filt = &opts.capture_filter;
    let mut why = serde_json::json!({"drive":0,"tracking":0,"unusable":0,"decode":0,"parked":0,"blur":0,"coverage":0});

    // --drive A B: raw frame range cut before everything else.
    let frames: &[Value] = if filt.enabled {
        if let Some([a, b]) = filt.drive {
            let (a, b) = (a.min(n_frames.saturating_sub(1)), b.min(n_frames.saturating_sub(1)));
            if a <= b {
                why["drive"] = (n_frames - (b - a + 1)).into();
                &frames[a..=b]
            } else {
                &frames[..]
            }
        } else {
            &frames[..]
        }
    } else {
        &frames[..]
    };

    // Intrinsics may live at doc level (nerfstudio shared camera) or per frame.
    let g = |k: &str| f64_at(&doc, k);
    struct Row {
        cam: Camera,
        path: PathBuf,
        decl_w: f64,
        decl_h: f64,
    }
    let mut rows = Vec::with_capacity(frames.len());
    for f in frames {
        // tracking must be "normal" (ARKit tracking state)
        if filt.enabled && f.get("tracking").and_then(Value::as_str).map(|t| t != "normal").unwrap_or(false) {
            why["tracking"] = (why["tracking"].as_u64().unwrap_or(0) + 1).into();
            continue;
        }
        let fp = f.get("file_path").and_then(Value::as_str);
        let (Some(fp), Ok(c2w)) = (fp, c2w_of(f)) else {
            why["unusable"] = (why["unusable"].as_u64().unwrap_or(0) + 1).into();
            continue;
        };
        let (Some(path),) = (resolve_frame(&dir, fp),) else {
            why["unusable"] = (why["unusable"].as_u64().unwrap_or(0) + 1).into();
            continue;
        };
        let pick = |k: &str| f64_at(f, k).or_else(|| g(k));
        let (fx, fy, cx, cy, w, h) = (pick("fl_x"), pick("fl_y"), pick("cx"), pick("cy"), pick("w"), pick("h"));
        let (Some(fx), Some(fy), Some(cx), Some(cy), Some(w), Some(h)) = (fx, fy, cx, cy, w, h) else {
            why["unusable"] = (why["unusable"].as_u64().unwrap_or(0) + 1).into();
            continue;
        };
        rows.push(Row {
            cam: Camera {
                width: w as u32,
                height: h as u32,
                fx: fx as f32,
                fy: fy as f32,
                cx: cx as f32,
                cy: cy as f32,
                c2w,
            },
            path,
            decl_w: w,
            decl_h: h,
        });
    }
    if rows.is_empty() {
        bail!("no usable frames in {}", json_path.display());
    }
    if why["unusable"].as_u64().unwrap_or(0) > 0 {
        eprintln!("[io] capture: skipped {} unusable/missing frames", why["unusable"]);
    }

    let d = opts.capture_downscale.max(1);
    let decoded: Vec<(Camera, Option<(Image, f32)>)> = rows
        .into_par_iter()
        .map(|row| {
            let img = match std::fs::read(&row.path)
                .ok()
                .and_then(|b| image::load_from_memory(&b).ok())
                .map(|i| i.into_rgb8())
            {
                Some(i) => i,
                None => return (row.cam, None),
            };
            let mut cam = row.cam;
            // Intrinsics describe the declared file; rescale to decoded size.
            let (dw, dh) = (img.width() as f64, img.height() as f64);
            let (sx, sy) = (dw / row.decl_w, dh / row.decl_h);
            cam.fx *= sx as f32;
            cam.fy *= sy as f32;
            cam.cx *= sx as f32;
            cam.cy *= sy as f32;
            cam.width = img.width();
            cam.height = img.height();
            let img = if d > 1 {
                let (nw, nh) = ((img.width() / d).max(1), (img.height() / d).max(1));
                let (rx, ry) = (nw as f64 / img.width() as f64, nh as f64 / img.height() as f64);
                cam.fx *= rx as f32;
                cam.fy *= ry as f32;
                cam.cx = ((cam.cx as f64 + 0.5) * rx - 0.5) as f32;
                cam.cy = ((cam.cy as f64 + 0.5) * ry - 0.5) as f32;
                cam.width = nw;
                cam.height = nh;
                image::imageops::resize(&img, nw, nh, image::imageops::FilterType::Triangle)
            } else {
                img
            };
            let img = Image { width: img.width(), height: img.height(), rgb: img.into_raw() };
            let sharp = if filt.enabled { sharpness(&img, filt.sharp_px) } else { 0.0 };
            (cam, Some((img, sharp)))
        })
        .collect();

    let mut cams = Vec::with_capacity(decoded.len());
    let mut imgs = Vec::with_capacity(decoded.len());
    let mut sharps = Vec::with_capacity(decoded.len());
    for (cam, img) in decoded {
        match img {
            Some((img, s)) => {
                cams.push(cam);
                imgs.push(img);
                sharps.push(s);
            }
            None => why["decode"] = (why["decode"].as_u64().unwrap_or(0) + 1).into(),
        }
    }
    if why["decode"].as_u64().unwrap_or(0) > 0 {
        eprintln!("[io] capture: {} frames failed to decode", why["decode"]);
    }
    if imgs.is_empty() {
        bail!("no decodable images in {}", dir.display());
    }

    let mut keep: Vec<usize> = (0..imgs.len()).collect();
    if filt.enabled {
        // Parked stretches: frames within parked_m / parked_deg of the group's
        // first frame collapse to the sharpest member.
        let mut groups: Vec<Vec<usize>> = Vec::new();
        let mut start: Option<[[f32; 4]; 4]> = None;
        for i in keep.iter().copied() {
            let m = &cams[i].c2w;
            if let Some(s) = start {
                let dx = m[0][3] - s[0][3];
                let dy = m[1][3] - s[1][3];
                let dz = m[2][3] - s[2][3];
                if (dx * dx + dy * dy + dz * dz).sqrt() < filt.parked_m && rot_angle(&s, m) < filt.parked_deg {
                    groups.last_mut().unwrap().push(i);
                    continue;
                }
            }
            groups.push(vec![i]);
            start = Some(*m);
        }
        why["parked"] = groups.iter().map(|g| g.len() as u64 - 1).sum::<u64>().into();
        let mut winners: Vec<usize> = groups
            .iter()
            .map(|g| *g.iter().max_by(|a, b| sharps[**a].partial_cmp(&sharps[**b]).unwrap_or(std::cmp::Ordering::Equal)).unwrap())
            .collect();

        // Blur cut: drop the bottom blur_frac of survivors by sharpness.
        if filt.blur_frac > 0.0 && winners.len() > 2 {
            let mut s: Vec<f32> = winners.iter().map(|i| sharps[*i]).collect();
            s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let cut = s[(s.len() as f64 * filt.blur_frac as f64) as usize];
            let before = winners.len();
            winners.retain(|i| sharps[*i] >= cut);
            why["blur"] = ((before - winners.len()) as u64).into();
        }

        // Pose coverage cap: farthest-point sampling.
        if filt.max_views > 0 && winners.len() > filt.max_views {
            let sub_cams: Vec<Camera> = winners.iter().map(|i| cams[*i].clone()).collect();
            let sel = coverage_subset(&sub_cams, filt.max_views, filt.dir_weight);
            why["coverage"] = ((winners.len() - sel.len()) as u64).into();
            winners = sel.iter().map(|j| winners[*j]).collect();
            winners.sort_unstable();
        }
        keep = winners;
    }

    let mut cap = Capture::default();
    for i in keep {
        cap.cameras.push(cams[i].clone());
        cap.images.push(std::mem::take(&mut imgs[i]));
    }
    if filt.enabled {
        eprintln!(
            "[io] capture filter: kept {}/{} (drive {} tracking {} unusable {} decode {} parked {} blur {} coverage {})",
            cap.images.len(), n_frames,
            why["drive"], why["tracking"], why["unusable"], why["decode"], why["parked"], why["blur"], why["coverage"]
        );
    }
    let st = serde_json::json!({"frames": n_frames, "kept": cap.images.len(), "dropped": why});
    Ok((cap, st))
}

impl Capture {
    /// Write this capture as a nerfstudio dataset (`transforms.json` +
    /// `images/img_XXXX.png`, per-frame intrinsics, row-major OpenGL c2w) —
    /// e.g. a filtered subset for the Brush refine stage (W6 dataset).
    pub fn write_nerfstudio(&self, dir: &Path) -> Result<()> {
        let img_dir = dir.join("images");
        std::fs::create_dir_all(&img_dir).with_context(|| format!("creating {}", img_dir.display()))?;
        let mut frames = Vec::with_capacity(self.cameras.len());
        for (i, (cam, img)) in self.cameras.iter().zip(self.images.iter()).enumerate() {
            let name = format!("img_{i:04}.png");
            let rgb = image::RgbImage::from_raw(img.width, img.height, img.rgb.clone())
                .ok_or_else(|| anyhow::anyhow!("image {i} buffer"))?;
            rgb.save(img_dir.join(&name)).with_context(|| format!("writing {name}"))?;
            frames.push(serde_json::json!({
                "file_path": format!("./images/{name}"),
                "w": cam.width, "h": cam.height,
                "fl_x": cam.fx, "fl_y": cam.fy, "cx": cam.cx, "cy": cam.cy,
                "camera_model": "OPENCV",
                "transform_matrix": cam.c2w.iter().map(|r| r.to_vec()).collect::<Vec<_>>(),
            }));
        }
        let doc = serde_json::json!({"camera_model": "OPENCV", "frames": frames});
        std::fs::write(dir.join("transforms.json"), serde_json::to_vec_pretty(&doc)?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn writes_cap(dir: &Path, poses: &[[f32; 3]], tracking: &[&str]) {
        let mut cap = Capture::default();
        for (i, p) in poses.iter().enumerate() {
            let mut cam = Camera::look_at(*p, [0.0, 0.0, 0.0], 64, 48, 60.0);
            // vary content so sharpness differs per frame
            let mut img = Image::new(64, 48);
            for (k, px) in img.rgb.iter_mut().enumerate() {
                *px = ((k + i * 7) % 251) as u8;
            }
            cam.c2w[0][3] = p[0];
            cap.cameras.push(cam);
            cap.images.push(img);
        }
        // write as nerfstudio and patch tracking flags
        cap.write_nerfstudio(dir).unwrap();
        let p = dir.join("transforms.json");
        let mut doc: Value = serde_json::from_slice(&std::fs::read(&p).unwrap()).unwrap();
        for (f, t) in doc["frames"].as_array_mut().unwrap().iter_mut().zip(tracking.iter()) {
            f["tracking"] = (*t).into();
        }
        std::fs::write(&p, serde_json::to_vec(&doc).unwrap()).unwrap();
    }

    #[test]
    fn filter_culls_tracking_parked_blur_coverage() {
        let dir = std::env::temp_dir().join("fs_capfilter_test");
        let _ = std::fs::remove_dir_all(&dir);
        // poses: A, parked triple, B, C, D (distinct poses)
        let poses = [
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.001, 0.0, 0.0], [1.002, 0.0, 0.0],
            [2.0, 0.0, 0.0], [3.0, 0.0, 0.0], [4.0, 0.0, 0.0],
        ];
        let tracking = ["normal"; 7].map(|s| s);
        let mut t = tracking.to_vec();
        t[5] = "limited"; // gets culled
        writes_cap(&dir, &poses, &t);
        let mut opts = crate::io::IoOptions::default();
        opts.capture_filter.blur_frac = 0.0;
        opts.capture_filter.max_views = 4;
        let (cap, st) = load_filtered(&dir, &opts).unwrap();
        // 1 tracking + 2 parked (3->1) + coverage cut
        assert_eq!(st["dropped"]["tracking"], 1);
        assert_eq!(st["dropped"]["parked"], 2);
        assert_eq!(cap.cameras.len(), 4, "kept {}", cap.cameras.len());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn write_nerfstudio_roundtrip() {
        let a = std::env::temp_dir().join("fs_capw_a");
        let b = std::env::temp_dir().join("fs_capw_b");
        let _ = std::fs::remove_dir_all(&a);
        let _ = std::fs::remove_dir_all(&b);
        let poses = [[0.0, 0.0, 0.0], [1.0, 0.5, 0.2]];
        writes_cap(&a, &poses, &["normal", "normal"]);
        let mut opts = crate::io::IoOptions::default();
        opts.capture_filter.enabled = false;
        let cap = load(&a, &opts).unwrap();
        assert_eq!(cap.cameras.len(), 2);
        cap.write_nerfstudio(&b).unwrap();
        let cap2 = load(&b, &opts).unwrap();
        assert_eq!(cap2.cameras.len(), 2);
        assert!((cap2.cameras[0].fx - cap.cameras[0].fx).abs() < 1e-3);
        let _ = std::fs::remove_dir_all(&a);
        let _ = std::fs::remove_dir_all(&b);
    }
}
