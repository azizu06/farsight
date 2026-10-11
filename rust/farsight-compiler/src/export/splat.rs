//! 3D Gaussian splat export in the INRIA 3DGS PLY layout: every densified
//! point becomes a flat surfel oriented by its normal, scale from surfel
//! radius, SH DC colour from sRGB, opacity from confidence. Loads in standard
//! splat viewers and works as a Brush training `init.ply`.

use crate::core::*;
use rayon::prelude::*;

const SH_C0: f32 = 0.282_094_791_773_878_14;
const N_REST: usize = 45; // f_rest_0..44 (harmonics 1-15 x rgb), zeros

/// Quaternion (w, x, y, z) rotating +Z onto unit vector `n`.
#[inline]
fn quat_from_z(n: V3) -> [f32; 4] {
    let z = n[2].clamp(-1.0, 1.0);
    if z < -0.999_99 {
        return [0.0, 1.0, 0.0, 0.0]; // 180 deg about X
    }
    let c = cross([0.0, 0.0, 1.0], n);
    let w = 1.0 + z;
    let l = (w * w + c[0] * c[0] + c[1] * c[1] + c[2] * c[2]).sqrt();
    [w / l, c[0] / l, c[1] / l, c[2] / l]
}

#[inline]
fn logit(p: f32) -> f32 {
    let p = p.clamp(1e-4, 1.0 - 1e-4);
    (p / (1.0 - p)).ln()
}

/// INRIA-layout binary PLY of the cloud as Gaussian surfels.
/// `opacity0` scales confidence; `scale_mul` inflates/deflates surfels;
/// `thin` is the normal-axis scale ratio (flat splats hug the surface).
pub fn splats(cloud: &PointCloud, opacity0: f32, scale_mul: f32, thin: f32, default_radius: f32) -> Vec<u8> {
    let n = cloud.len();
    let mut header = format!("ply\nformat binary_little_endian 1.0\nelement vertex {n}\n");
    for name in ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"] {
        header.push_str(&format!("property float {name}\n"));
    }
    for i in 0..N_REST {
        header.push_str(&format!("property float f_rest_{i}\n"));
    }
    for name in ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"] {
        header.push_str(&format!("property float {name}\n"));
    }
    header.push_str("end_header\n");
    let mut out = header.into_bytes();

    let floats = 3 + 3 + 3 + N_REST + 1 + 3 + 4;
    out.reserve(n * floats * 4);
    let has_n = cloud.has_normals();
    let has_r = cloud.radius.len() == n;
    let rows: Vec<Vec<u8>> = (0..n)
        .into_par_iter()
        .map(|i| {
            let mut row = Vec::with_capacity(floats * 4);
            let mut w = |v: f32| row.extend_from_slice(&v.to_le_bytes());
            for v in cloud.pos[i] {
                w(v);
            }
            let nrm = if has_n { cloud.nrm[i] } else { [0.0, 1.0, 0.0] };
            for _ in 0..3 {
                w(0.0); // nx ny nz — INRIA writes zeros
            }
            let c = cloud.col.get(i).copied().unwrap_or([128, 128, 128]);
            for k in 0..3 {
                w((c[k] as f32 / 255.0 - 0.5) / SH_C0); // f_dc
            }
            for _ in 0..N_REST {
                w(0.0);
            }
            w(logit(cloud.weight(i).clamp(0.02, 0.98) * opacity0));
            let r = if has_r { cloud.radius[i].max(1e-4) } else { default_radius } * scale_mul;
            w(r.ln());
            w(r.ln());
            w((r * thin).ln());
            let _ = nrm;
            for v in quat_from_z(nrm) {
                w(v);
            }
            row
        })
        .collect();
    for r in rows {
        out.extend_from_slice(&r);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splat_layout() {
        let mut cloud = PointCloud::default();
        cloud.pos.push([1.0, 2.0, 3.0]);
        cloud.col.push([255, 0, 0]);
        cloud.nrm.push([0.0, 0.0, 1.0]);
        cloud.radius.push(0.01);
        cloud.weight.push(1.0);
        let bytes = splats(&cloud, 0.95, 1.0, 0.15, 0.01);
        let text = String::from_utf8_lossy(&bytes).to_string();
        assert!(text.contains("element vertex 1"));
        assert!(text.contains("f_rest_44"));
        assert!(text.contains("rot_3"));
        // 62 properties x 4 bytes after the header
        let hlen = text.find("end_header\n").unwrap() + "end_header\n".len();
        let data = &bytes[hlen..];
        assert_eq!(data.len(), 62 * 4);
        let x = f32::from_le_bytes(data[0..4].try_into().unwrap());
        assert_eq!(x, 1.0);
        // f_dc_0 of pure red: (1 - 0.5)/C0
        let dc = f32::from_le_bytes(data[24..28].try_into().unwrap());
        assert!((dc - 0.5 / SH_C0).abs() < 1e-4);
        // rot_0 (w) for +Z -> +Z is 1
        let w = f32::from_le_bytes(data[(62 - 4) * 4..(62 - 3) * 4].try_into().unwrap());
        assert_eq!(w, 1.0);
    }
}
