//! Export: Brush `.ply` via `brush_serde`, and the antimatter15 `.splat`
//! (32 B/splat) conversion ported from `ply2splat.py` — computed straight off
//! the `Splats` tensors so it also works at a mid-run deadline.

use std::path::Path;

use anyhow::{Context, Result};
use brush_render::gaussian_splats::Splats;
use brush_render::sh::sh_coeffs_for_degree;

/// Write `<out>/scene.ply` exactly like Brush's checkpoint export
/// (binary little-endian gaussian ply, same comments block).
pub async fn export_ply(splats: Splats, out: &Path, up_axis: Option<glam::Vec3>) -> Result<usize> {
    let bytes = brush_serde::splat_to_ply(splats, up_axis)
        .await
        .context("splat_to_ply failed")?;
    let n = bytes.len();
    tokio::fs::write(out.join("scene.ply"), bytes).await?;
    Ok(n)
}

/// antimatter15 `.splat`: 32 B records [pos f32x3][scale f32x3][rgba u8x4][rot u8x4],
/// sorted biggest+most-opaque first (port of tools/postprocess/ply2splat.py).
pub async fn export_splat(splats: Splats, out: &Path) -> Result<usize> {
    // Fold the 3D-filter floor into scales/opacities like the ply export does.
    let splats = splats.bake_min_scale();
    let n = splats.num_splats() as usize;
    let sh_coeffs_num = sh_coeffs_for_degree(splats.sh_degree()) as usize;

    let transforms: Vec<f32> = splats
        .transforms
        .val()
        .into_data_async()
        .await
        .context("transforms readback")?
        .try_into_vec()
        .map_err(|_| anyhow::anyhow!("transforms dtype"))?;
    let raw_opac: Vec<f32> = splats
        .raw_opacities
        .val()
        .into_data_async()
        .await
        .context("opacity readback")?
        .try_into_vec()
        .map_err(|_| anyhow::anyhow!("opacity dtype"))?;
    let sh: Vec<f32> = splats
        .sh_coeffs
        .val()
        .into_data_async()
        .await
        .context("sh readback")?
        .try_into_vec()
        .map_err(|_| anyhow::anyhow!("sh dtype"))?;

    struct Row {
        pos: [f32; 3],
        scale: [f32; 3],
        rgba: [u8; 4],
        rot: [u8; 4],
        key: f32,
    }

    let mut rows: Vec<Row> = (0..n)
        .map(|i| {
            let t = i * 10;
            let (r0, r1, r2, r3) = (
                transforms[t + 3],
                transforms[t + 4],
                transforms[t + 5],
                transforms[t + 6],
            );
            let rn = (r0 * r0 + r1 * r1 + r2 * r2 + r3 * r3).sqrt().max(1e-12);
            let (q0, q1, q2, q3) = (r0 / rn, r1 / rn, r2 / rn, r3 / rn);
            let scale = [
                transforms[t + 7].exp(),
                transforms[t + 8].exp(),
                transforms[t + 9].exp(),
            ];
            let op = 1.0 / (1.0 + (-raw_opac[i]).exp());
            let base = i * sh_coeffs_num * 3;
            let dc = [
                sh[base],
                sh[base + sh_coeffs_num],
                sh[base + 2 * sh_coeffs_num],
            ];
            let rgb = [
                ((0.5 + 0.282_094_79 * dc[0]).clamp(0.0, 1.0) * 255.0) as u8,
                ((0.5 + 0.282_094_79 * dc[1]).clamp(0.0, 1.0) * 255.0) as u8,
                ((0.5 + 0.282_094_79 * dc[2]).clamp(0.0, 1.0) * 255.0) as u8,
            ];
            let rot = [
                (q0 * 128.0 + 128.0).clamp(0.0, 255.0) as u8,
                (q1 * 128.0 + 128.0).clamp(0.0, 255.0) as u8,
                (q2 * 128.0 + 128.0).clamp(0.0, 255.0) as u8,
                (q3 * 128.0 + 128.0).clamp(0.0, 255.0) as u8,
            ];
            Row {
                pos: [transforms[t], transforms[t + 1], transforms[t + 2]],
                scale,
                rgba: [rgb[0], rgb[1], rgb[2], (op * 255.0) as u8],
                rot,
                key: scale[0] * scale[1] * scale[2] * op,
            }
        })
        .collect();
    rows.sort_by(|a, b| b.key.partial_cmp(&a.key).unwrap_or(std::cmp::Ordering::Equal));

    let mut bytes = Vec::with_capacity(n * 32);
    for r in &rows {
        for v in r.pos {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
        for v in r.scale {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
        bytes.extend_from_slice(&r.rgba);
        bytes.extend_from_slice(&r.rot);
    }
    tokio::fs::write(out.join("scene.splat"), bytes).await?;
    Ok(n)
}

/// `farsight-splat ply2splat in.ply out.splat` — same 32 B/splat conversion as
/// [`export_splat`] but straight off a gaussian `.ply` file (port of
/// ply2splat.py without going through GPU tensors).
pub fn ply_to_splat(src: &Path, dst: &Path) -> Result<usize> {
    let raw = std::fs::read(src).with_context(|| format!("reading {}", src.display()))?;
    let end = raw
        .windows(b"end_header\n".len())
        .position(|w| w == b"end_header\n")
        .map(|i| i + b"end_header\n".len())
        .context("ply: no end_header")?;
    let header = String::from_utf8_lossy(&raw[..end]);
    let props: Vec<String> = header
        .lines()
        .filter(|l| l.starts_with("property"))
        .map(|l| l.split_whitespace().last().unwrap_or("").to_string())
        .collect();
    let n: usize = header
        .lines()
        .find(|l| l.starts_with("element vertex"))
        .and_then(|l| l.split_whitespace().last())
        .and_then(|v| v.parse().ok())
        .context("ply: no vertex count")?;
    let idx = |name: &str| props.iter().position(|p| p == name);
    let need = ["x", "y", "z", "scale_0", "scale_1", "scale_2", "opacity", "f_dc_0", "f_dc_1", "f_dc_2", "rot_0", "rot_1", "rot_2", "rot_3"];
    let col: Vec<usize> = need
        .iter()
        .map(|k| idx(k).with_context(|| format!("ply: missing property {k}")))
        .collect::<Result<_>>()?;
    let stride = props.len();
    let body = &raw[end..];
    if body.len() < n * stride * 4 {
        anyhow::bail!("ply body too short: {} < {}", body.len(), n * stride * 4);
    }
    let f = |i: usize, j: usize| -> f32 {
        let o = (i * stride + col[j]) * 4;
        f32::from_le_bytes(body[o..o + 4].try_into().unwrap())
    };
    struct Row {
        pos: [f32; 3],
        scale: [f32; 3],
        rgba: [u8; 4],
        rot: [u8; 4],
        key: f32,
    }
    let mut rows = Vec::with_capacity(n);
    for i in 0..n {
        let (r0, r1, r2, r3) = (f(i, 10), f(i, 11), f(i, 12), f(i, 13));
        let rn = (r0 * r0 + r1 * r1 + r2 * r2 + r3 * r3).sqrt().max(1e-12);
        let q = [r0 / rn, r1 / rn, r2 / rn, r3 / rn];
        let scale = [f(i, 3).exp(), f(i, 4).exp(), f(i, 5).exp()];
        let op = 1.0 / (1.0 + (-f(i, 6)).exp());
        let rgb = [
            ((0.5 + 0.282_094_79 * f(i, 7)).clamp(0.0, 1.0) * 255.0) as u8,
            ((0.5 + 0.282_094_79 * f(i, 8)).clamp(0.0, 1.0) * 255.0) as u8,
            ((0.5 + 0.282_094_79 * f(i, 9)).clamp(0.0, 1.0) * 255.0) as u8,
        ];
        rows.push(Row {
            pos: [f(i, 0), f(i, 1), f(i, 2)],
            scale,
            rgba: [rgb[0], rgb[1], rgb[2], (op * 255.0) as u8],
            rot: q.map(|v| (v * 128.0 + 128.0).clamp(0.0, 255.0) as u8),
            key: scale[0] * scale[1] * scale[2] * op,
        });
    }
    rows.sort_by(|a, b| b.key.partial_cmp(&a.key).unwrap_or(std::cmp::Ordering::Equal));
    let mut bytes = Vec::with_capacity(n * 32);
    for r in &rows {
        for v in r.pos {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
        for v in r.scale {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
        bytes.extend_from_slice(&r.rgba);
        bytes.extend_from_slice(&r.rot);
    }
    std::fs::write(dst, bytes)?;
    Ok(n)
}
