//! nerfstudio capture dir (`transforms.json` + images) -> `Capture`.
//! Layout per `docs/postrun-capture.md` and `synth::write_capture`: per-frame
//! `file_path`, `transform_matrix` (row-major camera-to-world, OpenGL axes),
//! `fl_x/fl_y/cx/cy/w/h` per frame or at document level. JPEG/PNG decoded in
//! parallel; `IoOptions::capture_downscale` shrinks images and intrinsics.
use crate::core::{Camera, Capture, Image};
use anyhow::{bail, Context, Result};
use rayon::prelude::*;
use serde_json::Value;
use std::path::{Path, PathBuf};

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
    for cand in [dir.join("images").join(base), dir.join(base)] {
        if cand.is_file() {
            return Some(cand);
        }
    }
    None
}

/// Load a nerfstudio capture directory.
pub fn load(dir: &Path, opts: &super::IoOptions) -> Result<Capture> {
    // Accept either the run dir or a direct path to transforms.json.
    let (dir, json_path) = if dir.is_file() {
        (dir.parent().unwrap_or_else(|| Path::new(".")).to_path_buf(), dir.to_path_buf())
    } else {
        (dir.to_path_buf(), dir.join("transforms.json"))
    };
    let doc: Value = serde_json::from_slice(&std::fs::read(&json_path).with_context(|| format!("reading {}", json_path.display()))?)
        .context("transforms.json is not valid JSON")?;
    let frames = doc.get("frames").and_then(Value::as_array).ok_or_else(|| anyhow::anyhow!("transforms.json lacks frames[]"))?;

    // Intrinsics may live at doc level (nerfstudio shared camera) or per frame.
    let g = |k: &str| f64_at(&doc, k);
    struct Row {
        cam: Camera,
        path: PathBuf,
        decl_w: f64,
        decl_h: f64,
    }
    let mut rows = Vec::with_capacity(frames.len());
    let mut skipped = 0usize;
    for f in frames {
        let fp = f.get("file_path").and_then(Value::as_str);
        let (Some(fp), Ok(c2w)) = (fp, c2w_of(f)) else {
            skipped += 1;
            continue;
        };
        let (Some(path),) = (resolve_frame(&dir, fp),) else {
            skipped += 1;
            continue;
        };
        let pick = |k: &str| f64_at(f, k).or_else(|| g(k));
        let (fx, fy, cx, cy, w, h) = (pick("fl_x"), pick("fl_y"), pick("cx"), pick("cy"), pick("w"), pick("h"));
        let (Some(fx), Some(fy), Some(cx), Some(cy), Some(w), Some(h)) = (fx, fy, cx, cy, w, h) else {
            skipped += 1;
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
    if skipped > 0 {
        eprintln!("[io] capture: skipped {skipped} unusable/missing frames");
    }

    let d = opts.capture_downscale.max(1);
    let decoded: Vec<(Camera, Option<Image>)> = rows
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
            (cam, Some(Image { width: img.width(), height: img.height(), rgb: img.into_raw() }))
        })
        .collect();

    let mut cap = Capture::default();
    let mut dropped = 0usize;
    for (cam, img) in decoded {
        match img {
            Some(img) => {
                cap.cameras.push(cam);
                cap.images.push(img);
            }
            None => dropped += 1,
        }
    }
    if dropped > 0 {
        eprintln!("[io] capture: {dropped} frames failed to decode");
    }
    if cap.images.is_empty() {
        bail!("no decodable images in {}", dir.display());
    }
    Ok(cap)
}
