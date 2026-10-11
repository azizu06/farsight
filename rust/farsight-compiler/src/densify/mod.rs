//! Interpolation / densification. OWNER: W3. Produces a denser, gap-filled
//! cloud: MLS projection denoise, dominant-plane extension across holes,
//! crust (marching-front) fill for curved regions, capture-guided colour.
//! Interpolated points get FLAG_INTERPOLATED with weight decaying away from
//! measured support.

pub mod crust;
pub mod fit;
pub mod planes;
pub mod vispaint;

use crate::core::*;
use crate::spatial::Grid;
use crate::Stats;
use rayon::prelude::*;
use rustc_hash::FxHashSet;
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct DensifyOptions {
    /// Target spacing of the output cloud (metres).
    pub target_spacing: f32,
    /// Largest gap diameter to bridge with interpolated samples (metres).
    pub max_gap: f32,
    /// MLS denoise iterations over the measured cloud (0 = off).
    pub mls_iters: u32,
    /// Neighbourhood size for MLS / local fits.
    pub fit_k: usize,
    /// Extend dominant planes across in-footprint gaps.
    pub plane_fill: bool,
    /// Marching-front fill for curved / non-dominant surfaces (also upsamples).
    /// Off by default: swept-front fills hurt precision more than they help;
    /// keep for explicit opt-in only.
    pub crust_fill: bool,
    /// Minimum inlier count for a dominant plane instance.
    pub min_plane_points: usize,
    /// Edge guard: maximum normal spread (degrees) inside a fit neighbourhood.
    pub edge_deg: f32,
    /// Sample fill colours from posed capture images when available.
    pub color_from_capture: bool,
    /// Reject fill points contradicting observed free space (needs capture).
    pub carve: bool,
    /// Safety cap on generated points.
    pub max_fill: usize,
}

impl Default for DensifyOptions {
    fn default() -> Self {
        DensifyOptions {
            target_spacing: 0.01,
            max_gap: 1.0,
            mls_iters: 1,
            fit_k: 20,
            plane_fill: true,
            crust_fill: false,
            min_plane_points: 1500,
            edge_deg: 55.0,
            color_from_capture: true,
            carve: true,
            max_fill: 3_000_000,
        }
    }
}

/// Stage entry: cleaned cloud (normals/radius optional — estimated locally if
/// absent) -> measured (optionally MLS-denoised) + interpolated fill points.
/// `depths` = learned per-view metric depth maps (learned::complete_depth);
/// consumed by hole filling (W3 contract: Option<&[DepthMap]> before `opts`).
pub fn run(cloud: &PointCloud, capture: Option<&Capture>, depths: Option<&[crate::learned::DepthMap]>, opts: &DensifyOptions) -> (PointCloud, Stats) {
    let _ = depths; // TODO(W3): guide plane_fill/crust_fill by learned depth
    let ts = opts.target_spacing.max(0.002);
    let reach = (opts.max_gap * 0.5).max(2.0 * ts);
    let edge_cos = opts.edge_deg.to_radians().cos();
    let mut st = serde_json::json!({});

    // --- Phase 0: local geometry (normals/radius) where missing -------------
    let mut t = Instant::now();
    let mut cur = cloud.clone();
    let cams: Vec<V3> = capture
        .map(|c| c.cameras.iter().map(|c| c.position()).collect())
        .unwrap_or_default();
    if !cur.has_normals() || cur.radius.is_empty() {
        estimate_geometry(&mut cur, &cams, ts);
    }
    if cur.weight.is_empty() {
        cur.weight = vec![1.0; cur.len()];
    }
    if cur.flags.is_empty() {
        cur.flags = vec![0; cur.len()];
    }
    if cur.col.is_empty() {
        cur.col = vec![[200, 200, 200]; cur.len()];
    }
    st["t_geometry_ms"] = (t.elapsed().as_secs_f64() * 1e3).round().into();

    // --- Phase 1: MLS denoise of measured points -----------------------------
    let mut moved = 0.0f64;
    t = Instant::now();
    for _ in 0..opts.mls_iters {
        moved += mls_pass(&mut cur, ts, opts.fit_k);
    }
    if opts.mls_iters > 0 {
        st["t_mls_ms"] = (t.elapsed().as_secs_f64() * 1e3).round().into();
        st["mls_mean_shift_mm"] = (moved * 1e3 / (opts.mls_iters.max(1) as f64)).round().into();
    }

    // --- Phase 2: dominant-plane extension fill ------------------------------
    t = Instant::now();
    let meas_grid = Grid::build(&cur.pos, ts);
    let mut occupied: FxHashSet<(i32, i32, i32)> = FxHashSet::default();
    let inv = 1.0 / ts;
    for p in &cur.pos {
        occupied.insert(cell_of(*p, inv));
    }
    let (planes_found, fill_a) = if opts.plane_fill {
        let planes = planes::detect(&cur, opts.min_plane_points, 0.04, edge_cos.max(0.8));
        let n = planes.len();
        let f = planes::fill(&planes, &cur, ts, reach, &mut occupied, &meas_grid);
        (n, f)
    } else {
        (0, planes::PlaneFill { pos: vec![], nrm: vec![], support: vec![] })
    };
    st["planes"] = planes_found.into();
    st["fill_plane"] = fill_a.pos.len().into();
    st["t_planes_ms"] = (t.elapsed().as_secs_f64() * 1e3).round().into();

    // --- Phase 3: crust fill --------------------------------------------------
    t = Instant::now();
    let mut fill_b = crust::CrustOut::default();
    if opts.crust_fill && fill_a.pos.len() < opts.max_fill {
        let all_pos: Vec<V3> = cur.pos.iter().chain(&fill_a.pos).copied().collect();
        let all_nrm: Vec<V3> = cur.nrm.iter().chain(&fill_a.nrm).copied().collect();
        let out = crust::grow(
            &all_pos,
            &all_nrm,
            cur.len(),
            ts,
            reach,
            edge_cos,
            1.5,
            opts.max_fill - fill_a.pos.len(),
        );
        fill_b = out;
    }
    st["fill_crust"] = fill_b.pos.len().into();
    st["crust_rejected_snap"] = fill_b.rejected_snap.into();
    st["crust_rejected_edge"] = fill_b.rejected_edge.into();
    st["crust_rejected_sparse"] = fill_b.rejected_sparse.into();
    st["crust_rejected_boundary"] = fill_b.rejected_boundary.into();
    st["crust_duplicates"] = fill_b.duplicate.into();
    st["t_crust_ms"] = (t.elapsed().as_secs_f64() * 1e3).round().into();

    // --- Merge fills -----------------------------------------------------------
    let mut fpos = fill_a.pos;
    let mut fnrm = fill_a.nrm;
    let mut fsup = fill_a.support;
    fpos.append(&mut fill_b.pos);
    fnrm.append(&mut fill_b.nrm);
    fsup.append(&mut fill_b.support);
    // Sort by position for full determinism regardless of hash/queue order.
    let mut order: Vec<usize> = (0..fpos.len()).collect();
    order.sort_unstable_by(|&a, &b| {
        fpos[a]
            .iter()
            .zip(&fpos[b])
            .map(|(x, y)| x.total_cmp(y))
            .find(|o| *o != std::cmp::Ordering::Equal)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let sort3 = |v: &Vec<V3>| -> Vec<V3> { order.iter().map(|&i| v[i]).collect() };
    fpos = sort3(&fpos);
    fnrm = sort3(&fnrm);
    let fsup_s: Vec<f32> = order.iter().map(|&i| fsup[i]).collect();
    fsup = fsup_s;

    // --- Phase 4: visibility carving -------------------------------------------
    t = Instant::now();
    let depths = if capture.is_some() && (opts.carve || opts.color_from_capture) {
        vispaint::render_depths(&cur.pos, capture.unwrap())
    } else {
        Vec::new()
    };
    if let (Some(cap), true) = (capture, opts.carve) {
        let keep = vispaint::carve(&fpos, cap, &depths, 0.045);
        let mut j = 0usize;
        let mut carved = 0usize;
        for c in 0..fpos.len() {
            if keep[c] {
                fpos[j] = fpos[c];
                fnrm[j] = fnrm[c];
                fsup[j] = fsup[c];
                j += 1;
            } else {
                carved += 1;
            }
        }
        fpos.truncate(j);
        fnrm.truncate(j);
        fsup.truncate(j);
        st["carved"] = carved.into();
    }
    st["t_carve_ms"] = (t.elapsed().as_secs_f64() * 1e3).round().into();

    // --- Phase 5: colours --------------------------------------------------------
    t = Instant::now();
    let idw_grid = Grid::build(&cur.pos, 0.05);
    let cap_opt = if opts.color_from_capture { capture } else { None };
    let (fcol, n_img, n_idw) = vispaint::paint_counted(
        &fpos,
        &fnrm,
        cap_opt,
        &depths,
        0.045,
        &idw_grid,
        &cur.col,
    );
    st["color_from_image"] = n_img.into();
    st["color_idw"] = n_idw.into();
    st["t_paint_ms"] = (t.elapsed().as_secs_f64() * 1e3).round().into();

    // --- Compose output -----------------------------------------------------------
    let mut out = PointCloud {
        pos: cur.pos,
        col: cur.col,
        nrm: cur.nrm,
        weight: cur.weight,
        radius: cur.radius,
        flags: cur.flags,
    };
    let n_fill = fpos.len();
    out.pos.extend_from_slice(&fpos);
    out.col.extend_from_slice(&fcol);
    out.nrm.extend_from_slice(&fnrm);
    // Flat, sub-measured confidence: fills are priors — they must never
    // out-vote noisy measured neighbours in the mesher's sign field.
    out.weight.extend(fsup.iter().map(|_| 0.35f32));
    out.radius.extend(std::iter::repeat(ts).take(n_fill));
    out.flags.extend(std::iter::repeat(FLAG_INTERPOLATED).take(n_fill));
    out.validate();

    st["input_points"] = cloud.len().into();
    st["output_points"] = out.len().into();
    st["interpolated"] = n_fill.into();
    (out, st)
}

#[inline]
fn cell_of(p: V3, inv: f32) -> (i32, i32, i32) {
    (
        (p[0] * inv).floor() as i32,
        (p[1] * inv).floor() as i32,
        (p[2] * inv).floor() as i32,
    )
}

/// Estimate missing normals + radius by k-NN PCA; orients normals toward the
/// capture cameras (or the cloud centroid when no capture is given).
fn estimate_geometry(cur: &mut PointCloud, cams: &[V3], ts: f32) {
    let grid = Grid::build(&cur.pos, ts.max(0.01));
    let need_nrm = !cur.has_normals();
    let need_rad = cur.radius.is_empty();
    let res: Vec<(V3, f32)> = cur
        .pos
        .par_iter()
        .map(|p| {
            let mut knn: Vec<(u32, f32)> = Vec::new();
            grid.knn(*p, 16, 8.0 * ts, &mut knn);
            let mut n = [0.0, 0.0, 1.0];
            if knn.len() >= 3 {
                let nb: Vec<(V3, f32)> = knn
                    .iter()
                    .map(|&(i, d2)| (grid.points()[i as usize], 1.0 / (d2 + 1e-6)))
                    .collect();
                if let Some(f) = fit::fit_plane(&nb) {
                    n = f.n;
                }
            }
            // Orient: toward seeing cameras, else toward centroid-ish origin.
            let mut s = 0.0f32;
            if !cams.is_empty() {
                for c in cams {
                    let d = sub(*c, *p);
                    s += dot(n, normalize(d)) / (1.0 + dot(d, d) * 0.05);
                }
            } else {
                s = dot(n, scale(*p, -1.0));
            }
            if s < 0.0 {
                n = scale(n, -1.0);
            }
            let rad = if knn.len() > 2 {
                (knn.iter().take(6).map(|k| k.1).sum::<f32>() / knn.len().min(6) as f32).sqrt()
            } else {
                ts
            };
            (n, rad)
        })
        .collect();
    if need_nrm {
        cur.nrm = res.iter().map(|r| r.0).collect();
    }
    if need_rad {
        cur.radius = res.iter().map(|r| r.1).collect();
    }
}

/// One MLS denoise iteration: project each point onto the weighted local
/// plane fit of its neighbourhood. Returns mean absolute shift (m).
fn mls_pass(cur: &mut PointCloud, ts: f32, k: usize) -> f64 {
    let grid = Grid::build(&cur.pos, (4.0 * ts).max(0.02));
    let h2 = (2.5 * ts) * (2.5 * ts);
    let has_n = cur.has_normals();
    let out: Vec<(V3, V3)> = cur
        .pos
        .par_iter()
        .enumerate()
        .map(|(i, p)| {
            let mut knn: Vec<(u32, f32)> = Vec::new();
            grid.knn(*p, k, 6.0 * ts, &mut knn);
            if knn.len() < 4 {
                return (*p, if has_n { cur.nrm[i] } else { [0.0, 0.0, 1.0] });
            }
            let n0 = if has_n { cur.nrm[i] } else { [0.0, 0.0, 1.0] };
            let nb: Vec<(V3, f32)> = knn
                .iter()
                .map(|&(j, d2)| {
                    let mut w = (-d2 / h2).exp();
                    if has_n {
                        let a = 1.0 - dot(cur.nrm[j as usize], n0);
                        w *= (-(a * a) / 0.12).exp();
                    }
                    (grid.points()[j as usize], w)
                })
                .collect();
            match fit::fit_plane(&nb) {
                Some(f) => {
                    let mut n = f.n;
                    if dot(n, n0) < 0.0 {
                        n = scale(n, -1.0);
                    }
                    // Cap the snap well below the noise sigma so measured
                    // points stay within their own uncertainty.
                    let d = f.signed_dist(*p).clamp(-0.008, 0.008);
                    (sub(*p, scale(n, d)), n)
                }
                None => (*p, n0),
            }
        })
        .collect();
    let mut shift = 0.0f64;
    for (i, (p, n)) in out.iter().enumerate() {
        shift += length(sub(*p, cur.pos[i])) as f64;
        cur.pos[i] = *p;
        if has_n || !cur.nrm.is_empty() {
            if cur.nrm.len() == cur.pos.len() {
                cur.nrm[i] = *n;
            }
        }
    }
    shift / cur.len().max(1) as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn options_roundtrip() {
        let o = DensifyOptions::default();
        let v = serde_json::to_value(&o).unwrap();
        let o2: DensifyOptions = serde_json::from_value(v).unwrap();
        assert_eq!(o.target_spacing, o2.target_spacing);
        // Partial JSON keeps defaults.
        let o3: DensifyOptions = serde_json::from_value(serde_json::json!({"max_gap": 2.0})).unwrap();
        assert_eq!(o3.max_gap, 2.0);
        assert_eq!(o3.target_spacing, o.target_spacing);
    }

    #[test]
    fn fills_wall_hole_end_to_end() {
        // Wall x=0 (normal +X) sampled at 10 mm with a 0.5 m hole.
        let ts = 0.01f32;
        let mut c = PointCloud::default();
        let n = (2.0 / ts) as usize;
        for i in 0..n {
            for j in 0..n {
                let y = -1.0 + i as f32 * ts;
                let z = -1.0 + j as f32 * ts;
                if y.abs() < 0.25 && z.abs() < 0.25 {
                    continue;
                }
                c.pos.push([0.0, y, z]);
                c.nrm.push([1.0, 0.0, 0.0]);
                c.col.push([100, 120, 140]);
            }
        }
        let (out, st) = run(&c, None, None, &DensifyOptions::default());
        out.validate();
        let interp = out.interpolated_count();
        assert!(interp > 1000, "interp={interp} stats={st}");
        assert!(out.pos[c.len()..].iter().all(|p| p[0].abs() < 0.02));
        assert!(out.pos[c.len()..].iter().any(|p| p[1].abs() < 0.05 && p[2].abs() < 0.05));
        assert!(out.flags[c.len()..].iter().all(|f| *f & FLAG_INTERPOLATED != 0));
    }
}
