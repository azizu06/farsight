//! Cleanup. OWNER: W2. Voxel-merge duplicates, statistical outlier removal,
//! isolated-cluster removal, flying-pixel streak suppression, and local-spacing
//! estimation into `cloud.radius`. Every filter is parallel and deterministic;
//! all parallel arrays stay in sync via `PointCloud::retain_mask`.
use crate::core::{normalize, PointCloud};
use crate::normals::sym3_eigvals;
use crate::spatial::Grid;
use crate::Stats;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct CleanOptions {
    /// Merge points closer than this (metres): weighted mean position/colour,
    /// accumulated weight. 0 disables.
    pub merge_m: f32,
    /// Statistical outlier removal: neighbour count and std-dev multiplier on
    /// mean kNN distance. k=0 disables.
    pub sor_k: usize,
    pub sor_std: f32,
    /// kNN search cap for the statistics pass (metres). 0 = 8x local spacing.
    pub sor_max_r: f32,
    /// Isolated-cluster removal: drop points with fewer than `iso_min`
    /// neighbours inside `iso_r`. 0 disables.
    pub iso_min: usize,
    /// Isolation radius (metres). 0 = iso_factor x local spacing.
    pub iso_r: f32,
    pub iso_factor: f32,
    /// Flying-pixel streaks: drop sparse points whose neighbourhood is
    /// strongly linear (view-ray streaks between fore- and background).
    /// k neighbours used for the PCA linearity test; 0 disables.
    pub fly_k: usize,
    /// Linearity threshold (lambda0-lambda1)/lambda0 in [0,1].
    pub fly_linearity: f32,
    /// Only neighbourhoods sparser than this multiple of local spacing qualify.
    pub fly_sparsity: f32,
}

impl Default for CleanOptions {
    fn default() -> Self {
        CleanOptions {
            merge_m: 0.0,
            sor_k: 16,
            sor_std: 2.5,
            sor_max_r: 0.0,
            iso_min: 3,
            iso_r: 0.0,
            iso_factor: 3.0,
            fly_k: 16,
            fly_linearity: 0.9,
            fly_sparsity: 2.5,
        }
    }
}

/// 21-bit-biased cell key, same packing as spatial::ukey (BIAS = 2^20).
#[inline]
fn mkey(p: [f32; 3], inv: f32) -> u64 {
    const B: i64 = 1 << 20;
    let x = (p[0] * inv).floor() as i64 + B;
    let y = (p[1] * inv).floor() as i64 + B;
    let z = (p[2] * inv).floor() as i64 + B;
    ((x as u64) << 42) | ((y as u64) << 21) | z as u64
}

/// Voxel-merge at `cell` metres. Weighted means for position/colour/normal,
/// summed weights, OR-ed flags. Returns the number of points removed.
fn voxel_merge(cloud: &mut PointCloud, cell: f32) -> usize {
    let n = cloud.len();
    let inv = 1.0 / cell;
    let mut keyed: Vec<(u64, u32)> =
        cloud.pos.par_iter().enumerate().map(|(i, p)| (mkey(*p, inv), i as u32)).collect();
    keyed.par_sort_unstable();
    // run boundaries
    let mut starts = vec![0usize];
    for i in 1..keyed.len() {
        if keyed[i].0 != keyed[i - 1].0 {
            starts.push(i);
        }
    }
    starts.push(keyed.len());
    let (has_col, has_nrm, has_w, has_r, has_f) = (
        cloud.col.len() == n,
        cloud.nrm.len() == n,
        cloud.weight.len() == n,
        cloud.radius.len() == n,
        cloud.flags.len() == n,
    );
    let merged: Vec<([f32; 3], [u8; 3], [f32; 3], f32, f32, u8)> = starts
        .par_windows(2)
        .map(|w| {
            let run = &keyed[w[0]..w[1]];
            let (mut sw, mut p, mut c, mut nr, mut r, mut f) =
                (0.0f32, [0.0f32; 3], [0.0f32; 3], [0.0f32; 3], 0.0f32, 0u8);
            for &(_, i) in run {
                let i = i as usize;
                let w_i = cloud.weight(i);
                sw += w_i;
                for k in 0..3 {
                    p[k] += w_i * cloud.pos[i][k];
                    if has_col {
                        c[k] += w_i * cloud.col[i][k] as f32;
                    }
                    if has_nrm {
                        nr[k] += w_i * cloud.nrm[i][k];
                    }
                }
                if has_r {
                    r += w_i * cloud.radius[i];
                }
                f |= cloud.flag(i);
            }
            let iw = if sw > 0.0 { 1.0 / sw } else { 1.0 };
            let pos = [p[0] * iw, p[1] * iw, p[2] * iw];
            let col = [
                (c[0] * iw).round().clamp(0.0, 255.0) as u8,
                (c[1] * iw).round().clamp(0.0, 255.0) as u8,
                (c[2] * iw).round().clamp(0.0, 255.0) as u8,
            ];
            (pos, col, normalize(nr), sw.max(1e-6), r * iw, f)
        })
        .collect();
    let removed = n - merged.len();
    cloud.pos = merged.iter().map(|m| m.0).collect();
    if has_col {
        cloud.col = merged.iter().map(|m| m.1).collect();
    }
    if has_nrm {
        cloud.nrm = merged.iter().map(|m| m.2).collect();
    }
    if has_w {
        cloud.weight = merged.iter().map(|m| m.3).collect();
    }
    if has_r {
        cloud.radius = merged.iter().map(|m| m.4).collect();
    }
    if has_f {
        cloud.flags = merged.iter().map(|m| m.5).collect();
    }
    removed
}

/// Fill `cloud.radius` with mean distance to `k` nearest neighbours.
fn fill_radius(cloud: &mut PointCloud, grid_cell: f32, k: usize) {
    let n = cloud.len();
    if n == 0 {
        cloud.radius.clear();
        return;
    }
    let grid = Grid::build(&cloud.pos, grid_cell);
    let (idx, d2) = grid.knn_batch_d2(&cloud.pos, k + 1, grid_cell * 6.0);
    cloud.radius = (0..n)
        .into_par_iter()
        .map(|i| {
            let row_i = &idx[i * (k + 1)..(i + 1) * (k + 1)];
            let row_d = &d2[i * (k + 1)..(i + 1) * (k + 1)];
            let mut s = 0.0;
            let mut c = 0;
            for (&j, &d) in row_i.iter().zip(row_d) {
                if j as usize == i || j == u32::MAX {
                    continue;
                }
                if d.is_finite() {
                    s += d.sqrt();
                    c += 1;
                }
            }
            if c > 0 {
                s / c as f32
            } else {
                grid_cell
            }
        })
        .collect();
}

pub fn run(cloud: &mut PointCloud, opts: &CleanOptions) -> Stats {
    let n0 = cloud.len();
    let merged = if opts.merge_m > 0.0 && n0 > 0 {
        voxel_merge(cloud, opts.merge_m)
    } else {
        0
    };
    let n = cloud.len();
    if n == 0 {
        return serde_json::json!({"in": n0, "out": 0, "merged": merged});
    }

    // Local spacing estimate drives every radius in this stage.
    let probe_cell = (opts.merge_m.max(0.01) * 2.0).max(0.01);
    let probe = Grid::build(&cloud.pos, probe_cell);
    let spacing = probe.median_nn_spacing(4096).max(1e-4);
    let grid = Grid::build(&cloud.pos, (spacing * 2.0).max(0.01));

    let iso_r = if opts.iso_r > 0.0 { opts.iso_r } else { spacing * opts.iso_factor.max(1.0) };
    let k_q = opts.sor_k.max(opts.fly_k).max(opts.iso_min) + 1; // +1: row includes self
    let max_r = if opts.sor_max_r > 0.0 { opts.sor_max_r } else { spacing * 8.0 }.max(iso_r);
    let (idx, d2) = grid.knn_batch_d2(&cloud.pos, k_q, max_r);

    // mean neighbour distance per point (excluding the self-hit at d=0)
    let means: Vec<f32> = (0..n)
        .into_par_iter()
        .map(|i| {
            let row_d = &d2[i * k_q..(i + 1) * k_q];
            let row_i = &idx[i * k_q..(i + 1) * k_q];
            let (mut s, mut c) = (0.0f32, 0usize);
            for (&j, &d) in row_i.iter().zip(row_d) {
                if j as usize == i || j == u32::MAX {
                    continue;
                }
                if d.is_finite() {
                    s += d.sqrt();
                    c += 1;
                }
            }
            if c == 0 {
                f32::MAX
            } else {
                s / c as f32
            }
        })
        .collect();

    // global statistics over non-degenerate means
    let finite: Vec<f32> = means.iter().copied().filter(|m| m.is_finite()).collect();
    let (mu, sd) = if finite.is_empty() {
        (f32::MAX, 0.0)
    } else {
        let m = finite.iter().sum::<f32>() / finite.len() as f32;
        let v = finite.iter().map(|x| (x - m) * (x - m)).sum::<f32>() / finite.len() as f32;
        (m, v.sqrt())
    };

    let iso_r2 = iso_r * iso_r;
    let pos = &cloud.pos;
    // 0 keep, 1 sor, 2 iso, 3 fly — first matching filter wins (for stats).
    let verdict: Vec<u8> = (0..n)
        .into_par_iter()
        .map(|i| {
            let mean_i = means[i];
            if mean_i >= f32::MAX {
                return 2; // no neighbours within search radius: isolated
            }
            if opts.sor_k > 0 && mean_i > mu + opts.sor_std * sd {
                return 1;
            }
            if opts.iso_min > 0 {
                let row_d = &d2[i * k_q..(i + 1) * k_q];
                let row_i = &idx[i * k_q..(i + 1) * k_q];
                let cnt = row_i
                    .iter()
                    .zip(row_d)
                    .filter(|(&j, &d)| j as usize != i && j != u32::MAX && d.is_finite() && d <= iso_r2)
                    .count();
                if cnt < opts.iso_min {
                    return 2;
                }
            }
            if opts.fly_k > 0 && mean_i > opts.fly_sparsity * spacing {
                // sparse candidate: PCA linearity over its kNN neighbourhood
                let row = &idx[i * k_q..(i + 1) * k_q];
                let mut c = [0.0f32; 6]; // xx xy xz yy yz zz
                let mut m = [0.0f32; 3];
                let mut cnt = 0usize;
                for &j in row.iter().filter(|&&j| j != u32::MAX && j as usize != i).take(opts.fly_k) {
                    let p = pos[j as usize];
                    for a in 0..3 {
                        m[a] += p[a];
                    }
                    cnt += 1;
                }
                if cnt >= 4 {
                    for a in 0..3 {
                        m[a] /= cnt as f32;
                    }
                    for &j in row.iter().filter(|&&j| j != u32::MAX && j as usize != i).take(opts.fly_k) {
                        let p = pos[j as usize];
                        let d = [p[0] - m[0], p[1] - m[1], p[2] - m[2]];
                        c[0] += d[0] * d[0];
                        c[1] += d[0] * d[1];
                        c[2] += d[0] * d[2];
                        c[3] += d[1] * d[1];
                        c[4] += d[1] * d[2];
                        c[5] += d[2] * d[2];
                    }
                    let ev = sym3_eigvals(c);
                    if ev[0] > 1e-12 && (ev[0] - ev[1]) / ev[0] > opts.fly_linearity {
                        return 3;
                    }
                }
            }
            0
        })
        .collect();
    let (mut sor_removed, mut iso_removed, mut fly_removed) = (0usize, 0usize, 0usize);
    let mut keep = vec![true; n];
    for (i, &v) in verdict.iter().enumerate() {
        match v {
            1 => sor_removed += 1,
            2 => iso_removed += 1,
            3 => fly_removed += 1,
            _ => {}
        }
        keep[i] = v == 0;
    }
    cloud.retain_mask(&keep);

    // local spacing for downstream stages
    fill_radius(cloud, (spacing * 2.0).max(0.01), 8);

    serde_json::json!({
        "in": n0, "out": cloud.len(), "merged": merged,
        "sor_removed": sor_removed, "iso_removed": iso_removed, "fly_removed": fly_removed,
        "spacing_m": spacing, "sor_mean_m": mu, "sor_std_m": sd,
    })
}
