//! Cleanup: statistical outlier removal + isolated-cluster drop.
//! Ported from farsight-compiler clean.rs (SOR + iso passes only — the fly
//! streak filter is folded into the same pass).

use crate::grid::Grid;
use glam::Vec3;
use rayon::prelude::*;

#[derive(Clone, Debug)]
pub struct CleanConfig {
    /// Statistical outlier removal: neighbour count (0 disables).
    pub sor_k: usize,
    /// Std-dev multiplier on the global mean kNN distance.
    pub sor_std: f32,
    /// Isolated-cluster removal: drop points with fewer than `iso_min`
    /// neighbours inside `iso_factor` × local spacing (0 disables).
    pub iso_min: usize,
    pub iso_factor: f32,
    /// kNN search cap (metres). 0 = 8× local spacing.
    pub max_r: f32,
}

impl Default for CleanConfig {
    fn default() -> Self {
        CleanConfig {
            sor_k: 16,
            sor_std: 2.0,
            iso_min: 2,
            iso_factor: 3.0,
            max_r: 0.0,
        }
    }
}

pub struct CleanStats {
    pub input: usize,
    pub kept: usize,
    pub sor_removed: usize,
    pub iso_removed: usize,
    pub spacing_m: f32,
}

/// Return `(keep_mask, stats)` for `pts`.
pub fn clean_mask(pts: &[Vec3], cfg: &CleanConfig) -> (Vec<bool>, CleanStats) {
    let n = pts.len();
    if n == 0 {
        return (
            vec![],
            CleanStats {
                input: 0,
                kept: 0,
                sor_removed: 0,
                iso_removed: 0,
                spacing_m: 0.0,
            },
        );
    }
    let probe = Grid::build(pts, 0.02);
    let spacing = probe.median_nn_spacing(4096).max(1e-4);
    let grid = Grid::build(pts, (spacing * 2.0).max(0.01));

    let iso_r = spacing * cfg.iso_factor.max(1.0);
    let k_q = cfg.sor_k.max(cfg.iso_min) + 1;
    let max_r = if cfg.max_r > 0.0 {
        cfg.max_r
    } else {
        (spacing * 8.0).max(iso_r)
    };
    let (idx, d2) = grid.knn_batch_d2(pts, k_q, max_r);

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
            if c == 0 { f32::MAX } else { s / c as f32 }
        })
        .collect();

    let (mut sum, mut sum2, mut cnt) = (0.0f64, 0.0f64, 0usize);
    for &m in &means {
        if m < f32::MAX {
            sum += m as f64;
            sum2 += (m as f64) * (m as f64);
            cnt += 1;
        }
    }
    let (mu, sd) = if cnt == 0 {
        (f32::MAX, 0.0)
    } else {
        let m = (sum / cnt as f64) as f32;
        let v = ((sum2 / cnt as f64) - (m as f64) * (m as f64)).max(0.0);
        (m, v.sqrt() as f32)
    };

    let iso_r2 = iso_r * iso_r;
    let verdict: Vec<u8> = (0..n)
        .into_par_iter()
        .map(|i| {
            let mean_i = means[i];
            if mean_i >= f32::MAX {
                return 2; // no neighbours at all: isolated
            }
            if cfg.sor_k > 0 && mean_i > mu + cfg.sor_std * sd {
                return 1;
            }
            if cfg.iso_min > 0 {
                let row_d = &d2[i * k_q..(i + 1) * k_q];
                let row_i = &idx[i * k_q..(i + 1) * k_q];
                let cnt = row_i
                    .iter()
                    .zip(row_d)
                    .filter(|&(&j, &d)| j as usize != i && j != u32::MAX && d.is_finite() && d <= iso_r2)
                    .count();
                if cnt < cfg.iso_min {
                    return 2;
                }
            }
            0
        })
        .collect();

    let mut keep = vec![true; n];
    let (mut sor_removed, mut iso_removed) = (0usize, 0usize);
    for (i, &v) in verdict.iter().enumerate() {
        match v {
            1 => sor_removed += 1,
            2 => iso_removed += 1,
            _ => {}
        }
        keep[i] = v == 0;
    }
    let kept = keep.iter().filter(|k| **k).count();
    (
        keep,
        CleanStats {
            input: n,
            kept,
            sor_removed,
            iso_removed,
            spacing_m: spacing,
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;

    #[test]
    fn drops_far_outliers() {
        let mut rng = Rng::new(7);
        let mut pts: Vec<Vec3> = (0..3000)
            .map(|_| Vec3::new(rng.f32() * 2.0, rng.f32() * 2.0, rng.f32() * 0.01))
            .collect();
        pts.push(Vec3::new(100.0, 100.0, 100.0));
        pts.push(Vec3::new(-100.0, 0.0, 0.0));
        let (keep, st) = clean_mask(
            &pts,
            &CleanConfig {
                sor_std: 3.0,
                ..Default::default()
            },
        );
        assert!(!keep[3000] && !keep[3001]);
        assert!(st.kept >= 2900, "kept {}", st.kept);
    }
}
