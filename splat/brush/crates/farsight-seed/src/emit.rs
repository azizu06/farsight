//! Output writers: INRIA-layout splat PLY (what Brush's `ply_file_path`
//! loader reads back into SplatData) and the sparse inverse-depth export
//! for the T2/T3 depth-supervision lane.

use crate::colour::SparseDepthView;
use crate::core::{logit, quat_from_z};
use glam::Vec3;
use std::io::Write;
use std::path::Path;

const SH_C0: f32 = 0.282_094_791_773_878_14;

/// One seed splat before packing.
#[derive(Clone, Copy, Debug)]
pub struct SeedSplat {
    pub pos: Vec3,
    /// Unit columns [t0, t1, normal] — the rotation matrix whose +Z is the
    /// surface normal. Serialized as quaternion (w,x,y,z).
    pub rot_cols: [[f32; 3]; 3],
    /// Linear scales (metres, pre-log): [s_t0, s_t1, s_thin].
    pub scales: [f32; 3],
    /// sRGB colour 0..255.
    pub rgb: [u8; 3],
    /// Post-sigmoid opacity 0..1.
    pub opacity: f32,
}

/// INRIA-layout binary PLY (SH0: 17 floats/vertex, no f_rest).
/// rot_* order (w,x,y,z); scale_* are log scales; opacity is logit(p);
/// f_dc_* are SH DC coefficients ((c/255 - 0.5) / SH_C0).
pub fn splats_ply(splats: &[SeedSplat]) -> Vec<u8> {
    let n = splats.len();
    let mut header = format!("ply\nformat binary_little_endian 1.0\nelement vertex {n}\n");
    for name in ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2", "opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"] {
        header.push_str(&format!("property float {name}\n"));
    }
    header.push_str("end_header\n");
    let mut out = header.into_bytes();
    out.reserve(n * 17 * 4);
    let mut buf = Vec::with_capacity(n * 17 * 4);
    for s in splats {
        let w = |v: f32, buf: &mut Vec<u8>| buf.extend_from_slice(&v.to_le_bytes());
        for v in [s.pos.x, s.pos.y, s.pos.z] {
            w(v, &mut buf);
        }
        // nx ny nz — INRIA writes zeros; readers ignore them anyway.
        for v in [0.0f32, 0.0, 0.0] {
            w(v, &mut buf);
        }
        for k in 0..3 {
            w((s.rgb[k] as f32 / 255.0 - 0.5) / SH_C0, &mut buf);
        }
        w(logit(s.opacity.clamp(0.02, 0.98)), &mut buf);
        w(s.scales[0].max(1e-5).ln(), &mut buf);
        w(s.scales[1].max(1e-5).ln(), &mut buf);
        w(s.scales[2].max(1e-5).ln(), &mut buf);
        // quat (w,x,y,z) of the rotation matrix [t0 t1 n]
        let q = quat_from_mat3(s.rot_cols);
        for v in q {
            w(v, &mut buf);
        }
    }
    out.extend_from_slice(&buf);
    out
}

/// Quaternion (w,x,y,z) from a rotation matrix with columns [t0 t1 n].
fn quat_from_mat3(c: [[f32; 3]; 3]) -> [f32; 4] {
    // Shepperd's method on the trace; falls back to quat_from_z when degenerate.
    let (m00, m01, m02) = (c[0][0], c[0][1], c[0][2]);
    let (m10, m11, m12) = (c[1][0], c[1][1], c[1][2]);
    let (m20, m21, m22) = (c[2][0], c[2][1], c[2][2]);
    let tr = m00 + m11 + m22;
    let (w, x, y, z);
    if tr > 0.0 {
        let s = (tr + 1.0).sqrt() * 2.0;
        w = 0.25 * s;
        x = (m21 - m12) / s;
        y = (m02 - m20) / s;
        z = (m10 - m01) / s;
    } else if m00 > m11 && m00 > m22 {
        let s = (1.0 + m00 - m11 - m22).sqrt() * 2.0;
        w = (m21 - m12) / s;
        x = 0.25 * s;
        y = (m01 + m10) / s;
        z = (m02 + m20) / s;
    } else if m11 > m22 {
        let s = (1.0 + m11 - m00 - m22).sqrt() * 2.0;
        w = (m02 - m20) / s;
        x = (m01 + m10) / s;
        y = 0.25 * s;
        z = (m12 + m21) / s;
    } else {
        let s = (1.0 + m22 - m00 - m11).sqrt() * 2.0;
        w = (m10 - m01) / s;
        x = (m02 + m20) / s;
        y = (m12 + m21) / s;
        z = 0.25 * s;
    }
    let l = (w * w + x * x + y * y + z * z).sqrt().max(1e-8);
    [w / l, x / l, y / l, z / l]
}

/// Simpler rotation path used when only a normal is known.
pub fn quat_from_normal(n: Vec3) -> [f32; 4] {
    quat_from_z(n)
}

/// Write `<dir>/view_NNNNNN.bin` per view + `<dir>/index.json`.
///
/// Format (farsight.sparse_depth.v1): per file `u32 count` (LE) then `count`
/// records of `(f32 u, f32 v, f32 inv_depth)` — pixel coords in the view's
/// native image space. index.json maps each file to its transforms.json
/// frame index so T2/T3 can pair them with training views.
pub fn write_sparse_depth(dir: &Path, views: &[SparseDepthView]) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let mut index = Vec::with_capacity(views.len());
    for v in views {
        let name = format!("view_{:06}.bin", v.frame_index);
        let mut f = std::fs::File::create(dir.join(&name))?;
        f.write_all(&(v.samples.len() as u32).to_le_bytes())?;
        for s in &v.samples {
            for x in [s.u, s.v, s.inv_d] {
                f.write_all(&x.to_le_bytes())?;
            }
        }
        index.push(serde_json::json!({
            "view": v.frame_index,
            "file": name,
            "count": v.samples.len(),
        }));
    }
    let doc = serde_json::json!({
        "format": "farsight.sparse_depth.v1",
        "record": ["u:f32", "v:f32", "inv_depth:f32"],
        "coords": "pixel (u right, v down, top-left origin), inv_depth = 1/metres",
        "views": index,
    });
    std::fs::write(dir.join("index.json"), serde_json::to_vec_pretty(&doc)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ply_layout_17_floats() {
        let s = SeedSplat {
            pos: Vec3::new(1.0, 2.0, 3.0),
            rot_cols: [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            scales: [0.01, 0.01, 0.002],
            rgb: [128, 128, 128],
            opacity: 0.6,
        };
        let b = splats_ply(&[s]);
        let txt = String::from_utf8_lossy(&b);
        assert!(txt.contains("element vertex 1"));
        let off = txt.find("end_header\n").unwrap() + "end_header\n".len();
        assert_eq!(b.len() - off, 17 * 4);
        let x = f32::from_le_bytes(b[off..off + 4].try_into().unwrap());
        assert_eq!(x, 1.0);
        // rot_0 (w) of identity rotation = 1
        let rot0 = f32::from_le_bytes(b[off + 13 * 4..off + 14 * 4].try_into().unwrap());
        assert!((rot0 - 1.0).abs() < 1e-5, "{rot0}");
    }
}
