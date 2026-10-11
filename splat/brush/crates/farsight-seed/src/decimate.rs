//! Adaptive voxel decimation: merge points within a cell, doubling the cell
//! until the count fits the target. Ported from farsight-compiler
//! export/splat.rs merge_voxels/decimate — same biased-key packing, but
//! weighted by per-point weight and carrying all parallel arrays.

use crate::grid::Grid;
use glam::Vec3;
use rayon::prelude::*;
use rustc_hash::FxHashMap;

/// Merge points within `voxel`-sized cells into weighted-mean representatives.
/// Returns `(positions, weights)` — weights are summed (not averaged) so dense
/// cells score higher downstream.
pub fn merge_voxels(pts: &[Vec3], weights: &[f32], voxel: f32) -> (Vec<Vec3>, Vec<f32>) {
    if voxel <= 0.0 || pts.is_empty() {
        return (pts.to_vec(), weights.to_vec());
    }
    let inv = 1.0 / voxel as f64;
    let key = |p: Vec3| -> u64 {
        let mut h = 0x9E37_79B9_7F4A_7C15u64;
        for v in [p.x as f64 * inv, p.y as f64 * inv, p.z as f64 * inv] {
            h ^= (v.floor() as i64 as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
            h = h.rotate_left(31);
        }
        h
    };
    #[derive(Default, Clone, Copy)]
    struct Acc {
        pos: [f64; 3],
        w: f64,
        n: u32,
    }
    let map = (0..pts.len())
        .into_par_iter()
        .fold(FxHashMap::default, |mut m: FxHashMap<u64, Acc>, i| {
            let w = weights.get(i).copied().unwrap_or(1.0) as f64;
            let a = m.entry(key(pts[i])).or_default();
            a.pos[0] += pts[i].x as f64 * w;
            a.pos[1] += pts[i].y as f64 * w;
            a.pos[2] += pts[i].z as f64 * w;
            a.w += w;
            a.n += 1;
            m
        })
        .reduce(FxHashMap::default, |mut a, b| {
            for (k, e) in b {
                let x = a.entry(k).or_default();
                x.pos[0] += e.pos[0];
                x.pos[1] += e.pos[1];
                x.pos[2] += e.pos[2];
                x.w += e.w;
                x.n += e.n;
            }
            a
        });
    let mut cells: Vec<Acc> = map.into_values().collect();
    cells.par_sort_by(|a, b| {
        a.pos[0]
            .total_cmp(&b.pos[0])
            .then(a.pos[1].total_cmp(&b.pos[1]))
            .then(a.pos[2].total_cmp(&b.pos[2]))
    });
    let pos = cells
        .iter()
        .map(|a| {
            let iw = 1.0 / a.w.max(1e-9);
            Vec3::new(
                (a.pos[0] * iw) as f32,
                (a.pos[1] * iw) as f32,
                (a.pos[2] * iw) as f32,
            )
        })
        .collect();
    let w = cells.iter().map(|a| a.w as f32).collect();
    (pos, w)
}

/// Bound the count: merge at `voxel0`, growing until `n <= max`.
/// Returns merged `(positions, weights, final_voxel)`.
pub fn decimate(pts: &[Vec3], weights: &[f32], max: usize, voxel0: f32) -> (Vec<Vec3>, Vec<f32>, f32) {
    if max == 0 || pts.len() <= max {
        return (pts.to_vec(), weights.to_vec(), 0.0);
    }
    let mut v = voxel0.max(1e-3);
    let (mut pos, mut w) = merge_voxels(pts, weights, v);
    while pos.len() > max && v < 1.0 {
        v *= 1.7;
        let m = merge_voxels(pts, weights, v);
        pos = m.0;
        w = m.1;
    }
    (pos, w, v)
}

/// Median NN spacing of the cloud — a good base voxel size.
pub fn median_spacing(pts: &[Vec3], fallback: f32) -> f32 {
    if pts.len() < 8 {
        return fallback;
    }
    let probe = Grid::build(pts, fallback.max(0.01));
    probe.median_nn_spacing(4096).max(1e-4)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;

    #[test]
    fn decimate_bounds_count() {
        let mut rng = Rng::new(3);
        let pts: Vec<Vec3> = (0..10_000)
            .map(|_| Vec3::new(rng.f32(), rng.f32(), rng.f32()))
            .collect();
        let w = vec![1.0f32; pts.len()];
        let (pos, _, v) = decimate(&pts, &w, 500, 0.005);
        assert!(pos.len() <= 500, "{}", pos.len());
        assert!(v > 0.0);
    }
}
