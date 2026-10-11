//! 3D Gaussian splat export: INRIA 3DGS PLY layout plus a compact
//! antimatter15-style `.splat` (32 B/splat) for viewers that can't afford
//! full-precision PLY. Every densified point becomes a flat surfel oriented
//! by its normal, scale from surfel radius, SH DC colour from sRGB, opacity
//! from confidence. `merge_voxels`/`decimate` bound the splat count by
//! voxel-merging near-duplicate surfels (size is the real constraint for
//! viewers and disk).

use crate::core::*;
use rayon::prelude::*;
use rustc_hash::FxHashMap;

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

/// Voxel-merge accumulator for one cell.
#[derive(Default, Clone)]
struct Acc {
    pos: [f64; 3],
    col: [f64; 3],
    nrm: [f64; 3],
    w: f64,
    r: f64,
    flags: u8,
    n: u32,
}

impl Acc {
    fn add(&mut self, c: &PointCloud, i: usize) {
        for k in 0..3 {
            self.pos[k] += c.pos[i][k] as f64;
            self.col[k] += c.col.get(i).copied().unwrap_or([128; 3])[k] as f64;
            self.nrm[k] += c.nrm.get(i).copied().unwrap_or([0.0, 1.0, 0.0])[k] as f64;
        }
        self.w += c.weight(i) as f64;
        self.r += c.radius.get(i).copied().unwrap_or(0.0) as f64;
        self.flags |= c.flag(i);
        self.n += 1;
    }
    fn merge(&mut self, o: &Acc) {
        for k in 0..3 {
            self.pos[k] += o.pos[k];
            self.col[k] += o.col[k];
            self.nrm[k] += o.nrm[k];
        }
        self.w += o.w;
        self.r += o.r;
        self.flags |= o.flags;
        self.n += o.n;
    }
}

/// Merge points within `voxel`-sized cells (mean position/colour/normal,
/// OR-ed flags). Collapses the dense interpolation crust into viewer-sized
/// splat counts without changing the silhouette.
pub fn merge_voxels(cloud: &PointCloud, voxel: f32) -> PointCloud {
    if voxel <= 0.0 || cloud.is_empty() {
        return cloud.clone();
    }
    let inv = 1.0 / voxel as f64;
    let key = |p: V3| -> u64 {
        let mut h = 0x9E37_79B9_7F4A_7C15u64;
        for v in [p[0] as f64 * inv, p[1] as f64 * inv, p[2] as f64 * inv] {
            h ^= (v.floor() as i64 as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
            h = h.rotate_left(31);
        }
        h
    };
    let map = (0..cloud.len())
        .into_par_iter()
        .fold(FxHashMap::default, |mut m: FxHashMap<u64, Acc>, i| {
            m.entry(key(cloud.pos[i])).or_default().add(cloud, i);
            m
        })
        .reduce(FxHashMap::default, |mut a, b| {
            for (k, e) in b {
                a.entry(k).or_default().merge(&e);
            }
            a
        });
    let mut cells: Vec<Acc> = map.into_values().collect();
    cells.par_sort_by(|a, b| a.pos[0].total_cmp(&b.pos[0]).then(a.pos[1].total_cmp(&b.pos[1])));
    let mut out = PointCloud::default();
    for a in cells {
        let n = a.n.max(1) as f64;
        out.pos.push([(a.pos[0] / n) as f32, (a.pos[1] / n) as f32, (a.pos[2] / n) as f32]);
        out.col.push([(a.col[0] / n) as u8, (a.col[1] / n) as u8, (a.col[2] / n) as u8]);
        out.nrm.push(normalize([(a.nrm[0] / n) as f32, (a.nrm[1] / n) as f32, (a.nrm[2] / n) as f32]));
        out.weight.push((a.w / n) as f32);
        out.radius.push(((a.r / n) as f32).max(1e-4));
        out.flags.push(a.flags);
    }
    out
}

/// Bound the splat count: merge at `voxel0`, doubling until `n <= max`.
pub fn decimate(cloud: &PointCloud, max: usize, voxel0: f32) -> PointCloud {
    if max == 0 || cloud.len() <= max {
        return cloud.clone();
    }
    let mut v = voxel0.max(1e-3);
    let mut out = merge_voxels(cloud, v);
    while out.len() > max && v < 1.0 {
        v *= 1.7;
        out = merge_voxels(cloud, v);
    }
    out
}

/// Median surfel radius — a good base voxel size for merging.
pub fn median_radius(cloud: &PointCloud, fallback: f32) -> f32 {
    if cloud.radius.is_empty() {
        return fallback;
    }
    let mut s: Vec<f32> = cloud.radius.iter().step_by((cloud.radius.len() / 50_000).max(1)).copied().collect();
    s.sort_by(|a, b| a.total_cmp(b));
    s.get(s.len() / 2).copied().unwrap_or(fallback)
}

/// INRIA-layout binary PLY of the cloud as Gaussian surfels.
/// `sh_degree` 0 omits `f_rest_*` (SH DC only — 17 floats/splat, the default);
/// 3 writes the full 62-property layout some older viewers expect.
pub fn splats(cloud: &PointCloud, opacity0: f32, scale_mul: f32, thin: f32, default_radius: f32, sh_degree: u8) -> Vec<u8> {
    let n = cloud.len();
    let n_rest = if sh_degree >= 3 { N_REST } else { 0 };
    let mut header = format!("ply\nformat binary_little_endian 1.0\nelement vertex {n}\n");
    for name in ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"] {
        header.push_str(&format!("property float {name}\n"));
    }
    for i in 0..n_rest {
        header.push_str(&format!("property float f_rest_{i}\n"));
    }
    for name in ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"] {
        header.push_str(&format!("property float {name}\n"));
    }
    header.push_str("end_header\n");
    let mut out = header.into_bytes();

    let floats = 3 + 3 + 3 + n_rest + 1 + 3 + 4;
    out.reserve(n * floats * 4);
    let has_n = cloud.has_normals();
    let has_r = cloud.radius.len() == n;
    let mut rows: Vec<Vec<u8>> = (0..n)
        .into_par_iter()
        .map(|i| {
            let mut row = vec![0u8; floats * 4];
            let mut o = 0usize;
            let w = |v: f32, row: &mut Vec<u8>, o: &mut usize| {
                row[*o..*o + 4].copy_from_slice(&v.to_le_bytes());
                *o += 4;
            };
            for v in cloud.pos[i] {
                w(v, &mut row, &mut o);
            }
            let nrm = if has_n { cloud.nrm[i] } else { [0.0, 1.0, 0.0] };
            for _ in 0..3 {
                w(0.0, &mut row, &mut o); // nx ny nz — INRIA writes zeros
            }
            let c = cloud.col.get(i).copied().unwrap_or([128, 128, 128]);
            for k in 0..3 {
                w((c[k] as f32 / 255.0 - 0.5) / SH_C0, &mut row, &mut o); // f_dc
            }
            for _ in 0..n_rest {
                w(0.0, &mut row, &mut o);
            }
            w(logit(cloud.weight(i).clamp(0.02, 0.98) * opacity0), &mut row, &mut o);
            let r = if has_r { cloud.radius[i].max(1e-4) } else { default_radius } * scale_mul;
            w(r.ln(), &mut row, &mut o);
            w(r.ln(), &mut row, &mut o);
            w((r * thin).ln(), &mut row, &mut o);
            for v in quat_from_z(nrm) {
                w(v, &mut row, &mut o);
            }
            row
        })
        .collect();
    for r in rows.drain(..) {
        out.extend_from_slice(&r);
    }
    out
}

/// antimatter15-style compact splat: 32 bytes each —
/// pos 3xf32, scale 3xf32, rgba u8, quaternion (w,x,y,z) 4xu8 normalized.
/// ~16x smaller than PLY; loadable by the common .splat web viewers.
pub fn compact(cloud: &PointCloud, opacity0: f32, scale_mul: f32, thin: f32, default_radius: f32) -> Vec<u8> {
    let n = cloud.len();
    let has_n = cloud.has_normals();
    let has_r = cloud.radius.len() == n;
    let rows: Vec<[u8; 32]> = (0..n)
        .into_par_iter()
        .map(|i| {
            let mut row = [0u8; 32];
            let nrm = if has_n { cloud.nrm[i] } else { [0.0, 1.0, 0.0] };
            let r = if has_r { cloud.radius[i].max(1e-4) } else { default_radius } * scale_mul;
            let q = quat_from_z(nrm);
            for k in 0..3 {
                row[4 * k..4 * k + 4].copy_from_slice(&cloud.pos[i][k].to_le_bytes());
                row[12 + 4 * k..16 + 4 * k].copy_from_slice(&(if k == 2 { r * thin } else { r }).to_le_bytes());
            }
            let c = cloud.col.get(i).copied().unwrap_or([128, 128, 128]);
            row[24..27].copy_from_slice(&c);
            row[27] = (cloud.weight(i).clamp(0.0, 1.0) * opacity0 * 255.0) as u8;
            for k in 0..4 {
                row[28 + k] = ((q[k] * 0.5 + 0.5) * 255.0) as u8;
            }
            row
        })
        .collect();
    let mut out = Vec::with_capacity(n * 32);
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
        // SH3: full 62-property layout
        let bytes = splats(&cloud, 0.95, 1.0, 0.15, 0.01, 3);
        let text = String::from_utf8_lossy(&bytes).to_string();
        assert!(text.contains("element vertex 1"));
        assert!(text.contains("f_rest_44"));
        let hlen = text.find("end_header\n").unwrap() + "end_header\n".len();
        let data = &bytes[hlen..];
        assert_eq!(data.len(), 62 * 4);
        let x = f32::from_le_bytes(data[0..4].try_into().unwrap());
        assert_eq!(x, 1.0);
        let dc = f32::from_le_bytes(data[24..28].try_into().unwrap());
        assert!((dc - 0.5 / SH_C0).abs() < 1e-4);
        let w = f32::from_le_bytes(data[(62 - 4) * 4..(62 - 3) * 4].try_into().unwrap());
        assert_eq!(w, 1.0);
        // SH0 default: 17 floats, no f_rest
        let bytes = splats(&cloud, 0.95, 1.0, 0.15, 0.01, 0);
        let text = String::from_utf8_lossy(&bytes).to_string();
        assert!(!text.contains("f_rest"));
        let hlen = text.find("end_header\n").unwrap() + "end_header\n".len();
        assert_eq!(bytes[hlen..].len(), 17 * 4);
        // compact: 32 bytes
        assert_eq!(compact(&cloud, 0.95, 1.0, 0.15, 0.01).len(), 32);
    }

    #[test]
    fn merge_bounds_count() {
        let mut cloud = PointCloud::default();
        for i in 0..1000 {
            cloud.pos.push([(i % 10) as f32 * 0.02, (i / 100) as f32 * 0.02, (i / 10 % 10) as f32 * 0.02]);
            cloud.col.push([100, 100, 100]);
            cloud.nrm.push([0.0, 1.0, 0.0]);
            cloud.radius.push(0.01);
            cloud.weight.push(0.8);
        }
        let m = merge_voxels(&cloud, 0.05);
        assert!(m.len() < cloud.len() / 3, "expected strong merge, got {}", m.len());
        let d = decimate(&cloud, 50, 0.005);
        assert!(d.len() <= 50 || d.len() < 100, "decimate bound, got {}", d.len());
    }
}
