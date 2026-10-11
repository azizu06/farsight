//! Reconstruction quality vs ground truth (Tanks&Temples-style precision /
//! recall / F-score at tau, plus recall inside deliberate holes). OWNER: orchestrator.

use crate::core::*;
use crate::spatial::Grid;
use rayon::prelude::*;
use serde::Serialize;

#[derive(Clone, Debug, Serialize)]
pub struct Quality {
    pub tau_m: f32,
    /// Fraction of reconstruction within tau of the true surface.
    pub precision: f32,
    /// Fraction of true visible surface within tau of the reconstruction.
    pub recall: f32,
    pub fscore: f32,
    /// Recall restricted to true surface inside unobserved holes (interpolation score).
    pub hole_recall: f32,
    pub mean_abs_sdf_m: f32,
    pub samples: usize,
}

/// `recon`: reconstruction sample points; `truth`: true visible surface samples;
/// `sdf`: exact distance to true surface; `in_hole`: hole membership.
pub fn evaluate(recon: &[V3], truth: &[V3], tau: f32, sdf: impl Fn(V3) -> f32 + Sync, in_hole: impl Fn(V3) -> bool + Sync) -> Quality {
    if recon.is_empty() || truth.is_empty() {
        return Quality { tau_m: tau, precision: 0.0, recall: 0.0, fscore: 0.0, hole_recall: 0.0, mean_abs_sdf_m: f32::NAN, samples: recon.len() };
    }
    let abs: Vec<f32> = recon.par_iter().map(|p| sdf(*p).abs()).collect();
    let precision = abs.iter().filter(|d| **d <= tau).count() as f32 / recon.len() as f32;
    let mean_abs = abs.iter().sum::<f32>() / abs.len() as f32;
    let grid = Grid::build(recon, tau.max(0.005));
    let hits: Vec<(bool, bool)> = truth.par_iter().map(|t| (grid.nearest(*t, tau).is_some(), in_hole(*t))).collect();
    let recall = hits.iter().filter(|h| h.0).count() as f32 / truth.len() as f32;
    let holes = hits.iter().filter(|h| h.1).count();
    let hole_recall = if holes == 0 { f32::NAN } else { hits.iter().filter(|h| h.0 && h.1).count() as f32 / holes as f32 };
    let fscore = if precision + recall > 0.0 { 2.0 * precision * recall / (precision + recall) } else { 0.0 };
    Quality { tau_m: tau, precision, recall, fscore, hole_recall, mean_abs_sdf_m: mean_abs, samples: recon.len() }
}

/// Area-uniform surface samples of a mesh (deterministic).
pub fn sample_mesh(mesh: &Mesh, n: usize, seed: u64) -> Vec<V3> {
    let tris: Vec<[V3; 3]> = mesh.idx.chunks_exact(3).map(|t| [mesh.pos[t[0] as usize], mesh.pos[t[1] as usize], mesh.pos[t[2] as usize]]).collect();
    if tris.is_empty() { return Vec::new(); }
    let mut cdf = Vec::with_capacity(tris.len());
    let mut acc = 0.0f64;
    for t in &tris { acc += 0.5 * length(cross(sub(t[1], t[0]), sub(t[2], t[0]))) as f64; cdf.push(acc); }
    let mut rng = Rng::new(seed);
    (0..n).map(|_| {
        let x = rng.f32() as f64 * acc;
        let i = cdf.partition_point(|c| *c < x).min(tris.len() - 1);
        let (mut a, mut b) = (rng.f32(), rng.f32());
        if a + b > 1.0 { a = 1.0 - a; b = 1.0 - b; }
        let t = &tris[i];
        add(t[0], add(scale(sub(t[1], t[0]), a), scale(sub(t[2], t[0]), b)))
    }).collect()
}

/// Mean colour error (sRGB 0-255 L1 per channel) of mesh vertex colours vs `truth_color`.
pub fn color_error(mesh: &Mesh, truth_color: impl Fn(V3) -> [u8; 3] + Sync) -> f32 {
    if mesh.col.is_empty() { return f32::NAN; }
    let e: f32 = mesh.pos.par_iter().zip(&mesh.col).map(|(p, c)| {
        let t = truth_color(*p);
        (0..3).map(|k| (c[k] as f32 - t[k] as f32).abs()).sum::<f32>() / 3.0
    }).sum();
    e / mesh.pos.len() as f32
}

/// Flatness quality on ground-truth planar regions (W2 planes metric):
/// `flat_rms_mm` = RMS distance of reconstructed points to the exact GT
/// plane; `flat_normal_dev_deg` = mean |angle| between reconstructed and GT
/// plane normals (area-weighted for meshes, per-point otherwise).
#[derive(Clone, Debug, Serialize)]
pub struct Flatness {
    pub flat_rms_mm: f32,
    pub flat_normal_dev_deg: f32,
    /// Fraction of samples that matched a GT plane within `eps`.
    pub planar_frac: f32,
    pub samples: usize,
}

/// Flatness of a point set with optional normals (nrm empty -> dev is NaN).
pub fn flatness_points(pos: &[V3], nrm: &[V3], scene: &crate::synth::Scene, eps: f32) -> Flatness {
    let has_nrm = nrm.len() == pos.len() && !nrm.is_empty();
    let (mut s2, mut dev, mut hit) = (0.0f64, 0.0f64, 0usize);
    for (i, p) in pos.iter().enumerate() {
        if let Some((n, d)) = scene.planar_truth(*p, eps) {
            let r = dot(n, *p) + d;
            s2 += (r as f64) * (r as f64);
            hit += 1;
            if has_nrm {
                dev += dot(nrm[i], n).abs().clamp(-1.0, 1.0).acos().to_degrees() as f64;
            }
        }
    }
    Flatness {
        flat_rms_mm: if hit > 0 { (s2 / hit as f64).sqrt() as f32 * 1000.0 } else { f32::NAN },
        flat_normal_dev_deg: if has_nrm && hit > 0 { (dev / hit as f64) as f32 } else { f32::NAN },
        planar_frac: if pos.is_empty() { 0.0 } else { hit as f32 / pos.len() as f32 },
        samples: pos.len(),
    }
}

/// Flatness of a mesh: per-vertex RMS distance to GT planes + area-weighted
/// angle between geometric face normals and the GT plane normal. A face is
/// evaluated when its centroid lands on a GT plane.
pub fn flatness_mesh(mesh: &Mesh, scene: &crate::synth::Scene, eps: f32) -> Flatness {
    let base = flatness_points(&mesh.pos, &[], scene, eps);
    let (mut dev, mut area_sum, mut hit) = (0.0f64, 0.0f64, 0usize);
    for t in mesh.idx.chunks_exact(3) {
        let (a, b, c) = (mesh.pos[t[0] as usize], mesh.pos[t[1] as usize], mesh.pos[t[2] as usize]);
        let centroid = scale(add(add(a, b), c), 1.0 / 3.0);
        if let Some((gn, _)) = scene.planar_truth(centroid, eps.max(0.03)) {
            let fn_ = cross(sub(b, a), sub(c, a));
            let area = length(fn_) * 0.5;
            if area > 1e-10 {
                let ang = dot(normalize(fn_), gn).abs().clamp(-1.0, 1.0).acos().to_degrees() as f64;
                dev += ang * area as f64;
                area_sum += area as f64;
                hit += 1;
            }
        }
    }
    Flatness {
        flat_rms_mm: base.flat_rms_mm,
        flat_normal_dev_deg: if area_sum > 0.0 { (dev / area_sum) as f32 } else { f32::NAN },
        planar_frac: base.planar_frac,
        samples: hit,
    }
}
