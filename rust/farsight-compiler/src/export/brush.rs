//! Brush (Gaussian-splat trainer) seed export: `<out>/brush/` holding
//! `init.ply` (INRIA-layout flat surfels from the compiled result),
//! `transforms.json` (nerfstudio frames with `ply_file_path`), and `images/`
//! (symlinks to the source capture, or JPEGs when images only exist in RAM).

use crate::core::*;
use crate::export::splat;
use crate::Stats;
use anyhow::{Context, Result};
use rustc_hash::FxHashMap;
use std::path::Path;

/// Sample the mesh surface at ~`spacing` metres, returning a point cloud of
/// flat surfels (position, interpolated colour, interpolated vertex normal —
/// already plane-exact on planar charts, surfel radius = spacing*0.55).
/// Deterministic stratified (golden-ratio) barycentric sampling.
pub fn sample_surfels(mesh: &Mesh, spacing: f32, target: usize) -> PointCloud {
    let mut out = PointCloud::default();
    if mesh.triangles() == 0 || target == 0 {
        return out;
    }
    // total area -> spacing scaled so ~target samples land
    let mut areas = Vec::with_capacity(mesh.triangles());
    let mut total = 0.0f64;
    for t in mesh.idx.chunks_exact(3) {
        let (a, b, c) = (mesh.pos[t[0] as usize], mesh.pos[t[1] as usize], mesh.pos[t[2] as usize]);
        let ar = 0.5 * length(cross(sub(b, a), sub(c, a))) as f64;
        areas.push(ar);
        total += ar;
    }
    let s = (total / target as f64).sqrt().max(spacing as f64);
    for (fi, t) in mesh.idx.chunks_exact(3).enumerate() {
        let (ia, ib, ic) = (t[0] as usize, t[1] as usize, t[2] as usize);
        let (a, b, c) = (mesh.pos[ia], mesh.pos[ib], mesh.pos[ic]);
        let n = ((areas[fi] / (s * s)).ceil() as usize).clamp(1, 64);
        for j in 0..n {
            // golden-ratio low-discrepancy triangle points
            let mut u = (j as f64 + 0.5) / n as f64;
            let mut v = ((j as f64) * 0.618_033_988_75).fract();
            if u + v > 1.0 {
                u = 1.0 - u;
                v = 1.0 - v;
            }
            let w = (1.0 - u - v) as f32;
            let (u, v) = (u as f32, v as f32);
            out.pos.push(add(add(scale(a, w), scale(b, u)), scale(c, v)));
            if mesh.col.len() == mesh.pos.len() {
                let mut rgb = [0f32; 3];
                for k in 0..3 {
                    rgb[k] = mesh.col[ia][k] as f32 * w + mesh.col[ib][k] as f32 * u + mesh.col[ic][k] as f32 * v;
                }
                out.col.push([rgb[0] as u8, rgb[1] as u8, rgb[2] as u8]);
            }
            if mesh.nrm.len() == mesh.pos.len() {
                out.nrm.push(normalize(add(add(scale(mesh.nrm[ia], w), scale(mesh.nrm[ib], u)), scale(mesh.nrm[ic], v))));
            }
            out.radius.push((s as f32) * 0.55);
            out.weight.push(0.8);
            out.flags.push(0);
        }
    }
    out
}

/// transforms.json in the shape `io::load_capture` / Brush consume:
/// per-frame intrinsics + c2w matrix, frames[].file_path relative to dir.
fn transforms(capture: &Capture, rel_paths: &[String]) -> serde_json::Value {
    let frames: Vec<serde_json::Value> = capture
        .cameras
        .iter()
        .enumerate()
        .map(|(i, cam)| {
            serde_json::json!({
                "file_path": rel_paths[i], "fl_x": cam.fx, "fl_y": cam.fy,
                "cx": cam.cx, "cy": cam.cy, "w": cam.width, "h": cam.height,
                "transform_matrix": cam.c2w, "frame_id": i,
            })
        })
        .collect();
    serde_json::json!({ "camera_model": "OPENCV", "k1": 0.0, "k2": 0.0, "p1": 0.0, "p2": 0.0, "ply_file_path": "init.ply", "frames": frames })
}

/// Write `<out>/brush/` — init.ply + transforms.json + images — ready for
/// `brush_app <dir>`. `capture_dir`, when given, is the nerfstudio dir the
/// capture was loaded from: frames are symlinked so big JPEGs aren't copied.
pub fn write_seed(
    out: &Path,
    seed_cloud: &PointCloud,
    capture: &Capture,
    capture_dir: Option<&Path>,
    opacity0: f32,
    thin: f32,
    default_radius: f32,
) -> Result<Stats> {
    let dir = out.join("brush");
    std::fs::create_dir_all(&dir)?;

    // init.ply — full INRIA layout (Brush reads gaussian props for seeding)
    let ply = splat::splats(seed_cloud, opacity0, 1.0, thin, default_radius, 3);
    std::fs::write(dir.join("init.ply"), &ply)?;

    // images + transforms
    let mut rel: Vec<String> = Vec::with_capacity(capture.cameras.len());
    let copied = capture_dir.and_then(|cd| std::fs::read_to_string(cd.join("transforms.json")).ok());
    if let (Some(cd), Some(text)) = (capture_dir, copied) {
        // preserve the original doc (distortion, masks...) and symlink frames
        let mut doc: serde_json::Value = serde_json::from_str(&text).context("transforms.json parse")?;
        if let Some(frames) = doc.get("frames").and_then(|f| f.as_array()) {
            for f in frames {
                if let Some(fp) = f.get("file_path").and_then(|v| v.as_str()) {
                    let rel_path = fp.to_string();
                    let src = cd.join(fp);
                    let dst = dir.join(fp);
                    if let Some(parent) = dst.parent() {
                        std::fs::create_dir_all(parent)?;
                    }
                    let _ = std::fs::remove_file(&dst);
                    #[cfg(unix)]
                    std::os::unix::fs::symlink(&src, &dst).or_else(|_| std::fs::copy(&src, &dst).map(|_| ()))?;
                    #[cfg(not(unix))]
                    std::fs::copy(&src, &dst)?;
                    rel.push(rel_path);
                }
            }
        }
        doc["ply_file_path"] = serde_json::json!("init.ply");
        std::fs::write(dir.join("transforms.json"), serde_json::to_vec_pretty(&doc)?)?;
    } else {
        std::fs::create_dir_all(dir.join("images"))?;
        for (i, img) in capture.images.iter().enumerate() {
            let r = format!("images/{i:06}.jpg");
            image::RgbImage::from_raw(img.width, img.height, img.rgb.clone())
                .context("bad image")?
                .save_with_format(dir.join(&r), image::ImageFormat::Jpeg)?;
            rel.push(r);
        }
        std::fs::write(dir.join("transforms.json"), serde_json::to_vec_pretty(&transforms(capture, &rel))?)?;
    }

    Ok(serde_json::json!({ "dir": dir.display().to_string(), "seed_splats": seed_cloud.len(), "frames": rel.len() }))
}

/// Read an INRIA-format splat PLY (as Brush exports) back into a PointCloud:
/// positions, sRGB colours from f_dc, opacity as weight, log scales -> radius.
pub fn read_splat_ply(path: &Path) -> Result<PointCloud> {
    let bytes = std::fs::read(path)?;
    let end = bytes
        .windows(b"end_header\n".len())
        .position(|w| w == b"end_header\n")
        .context("no end_header")?
        + b"end_header\n".len();
    let head = std::str::from_utf8(&bytes[..end])?;
    let n: usize = head
        .lines()
        .find_map(|l| l.strip_prefix("element vertex ").and_then(|v| v.split_whitespace().next().and_then(|s| s.parse().ok())))
        .context("no vertex count")?;
    let props: Vec<String> = head
        .lines()
        .filter_map(|l| l.strip_prefix("property").map(|r| r.split_whitespace().last().unwrap_or("").to_string()))
        .collect();
    let idx: FxHashMap<&str, usize> = props.iter().enumerate().map(|(i, p)| (p.as_str(), i)).collect();
    let np = props.len();
    let mut out = PointCloud::default();
    for i in 0..n {
        let base = end + i * np * 4;
        if base + np * 4 > bytes.len() {
            break;
        }
        let f = |name: &str, def: f32| -> f32 {
            idx.get(name)
                .map(|&j| f32::from_le_bytes(bytes[base + 4 * j..base + 4 * j + 4].try_into().unwrap()))
                .unwrap_or(def)
        };
        out.pos.push([f("x", 0.0), f("y", 0.0), f("z", 0.0)]);
        let rgb = |k: usize| -> u8 { ((f(&format!("f_dc_{k}"), 0.0) * 0.282_094_79 + 0.5) * 255.0).clamp(0.0, 255.0) as u8 };
        out.col.push([rgb(0), rgb(1), rgb(2)]);
        out.nrm.push([0.0, 0.0, 1.0]);
        out.weight.push(1.0 / (1.0 + (-f("opacity", 0.0)).exp()));
        out.radius.push(f("scale_0", -4.0).exp().max(1e-4));
        out.flags.push(0);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_covers_mesh() {
        let mesh = Mesh {
            pos: vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
            col: vec![[9, 9, 9]; 4],
            nrm: vec![[0.0, 0.0, 1.0]; 4],
            idx: vec![0, 1, 2, 0, 2, 3],
            ..Default::default()
        };
        let s = sample_surfels(&mesh, 0.1, 50);
        assert!(!s.pos.is_empty() && s.pos.len() <= 4 * 64);
        assert!(s.pos.iter().all(|p| p[2].abs() < 1e-6));
        assert!(s.nrm.iter().all(|n| (n[2] - 1.0).abs() < 1e-4));
    }

    #[test]
    fn transforms_has_ply_pointer() {
        let cap = Capture {
            cameras: vec![Camera { width: 8, height: 8, fx: 4.0, fy: 4.0, cx: 4.0, cy: 4.0, c2w: [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]] }],
            images: vec![Image { width: 8, height: 8, rgb: vec![7; 8 * 8 * 3] }],
        };
        let t = transforms(&cap, &["images/0.jpg".to_string()]);
        assert_eq!(t["ply_file_path"], "init.ply");
        assert_eq!(t["frames"][0]["transform_matrix"][0][3], 0.0);
    }
}
