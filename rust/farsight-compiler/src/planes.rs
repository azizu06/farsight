//! Dominant plane detection over the cleaned, normal-estimated cloud.
//! OWNER: W2. Shared contract consumed by densify / surface / meshops /
//! texture so every flat surface (walls, floor, ceiling, tabletops, cabinet
//! faces) is reconstructed exactly flat with crisp plane-to-plane edges.
//!
//! Method: parallel union-find over kNN adjacency edges, uniting points whose
//! normals agree and which are mutually consistent with each other's tangent
//! plane (equivalent to curvature-seeded region growing but deterministic and
//! thread-safe). Each component is refit by weighted least squares + one IRLS
//! round, disconnected coplanar segments are merged, and when the normal
//! distribution supports it planes are snapped onto a Manhattan frame
//! (near-orthogonal / near-parallel normals become exactly orthogonal /
//! parallel when within `manhattan_snap_deg`).

use crate::core::*;
use crate::normals::{sym3_eigvals, sym3_min_eigvec};
use crate::spatial::Grid;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

/// One detected plane: n·x + d = 0, unit `n`.
#[derive(Clone, Copy, Debug)]
pub struct Plane {
    pub n: V3,
    pub d: f32,
    pub inliers: u32,
    /// Planar area estimate (inlier count * spacing²).
    pub area_m2: f32,
    /// RMS residual of inliers — the local sensor-noise sigma.
    pub rms_m: f32,
}

/// Plane assignment for every point of the input cloud.
pub struct Planes {
    pub planes: Vec<Plane>,
    /// Per point: index into `planes`, or u32::MAX when not on a plane.
    pub label: Vec<u32>,
}

impl Planes {
    /// Nearest plane whose normal agrees with `n` within `max_angle_deg` and
    /// whose plane is within `max_dist` of `p`. Returns the `planes` index.
    pub fn nearest(&self, p: V3, n: V3, max_dist: f32, max_angle_deg: f32) -> Option<u32> {
        let cos_a = (max_angle_deg * std::f32::consts::PI / 180.0).cos();
        let mut best: Option<(f32, u32)> = None;
        for (i, pl) in self.planes.iter().enumerate() {
            if dot(pl.n, n).abs() < cos_a {
                continue;
            }
            let dist = (dot(pl.n, p) + pl.d).abs();
            if dist < max_dist && best.map_or(true, |(b, _)| dist < b) {
                best = Some((dist, i as u32));
            }
        }
        best.map(|(_, i)| i)
    }

    /// Signed distance of `p` to plane `i`.
    #[inline]
    pub fn signed_dist(&self, i: u32, p: V3) -> f32 {
        let pl = &self.planes[i as usize];
        dot(pl.n, p) + pl.d
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct PlaneOptions {
    /// Master switch; false -> empty Planes.
    pub enabled: bool,
    /// kNN size used for the adjacency graph.
    pub k: usize,
    /// Max normal angle (deg) across an adjacency edge.
    pub grow_angle_deg: f32,
    /// Inlier distance threshold (m). 0 = auto (1.8 * local spacing).
    pub dist_m: f32,
    /// Min component size kept as a plane.
    pub min_inliers: usize,
    /// Merge disconnected coplanar components: angle (deg) and gap (m).
    pub merge_angle_deg: f32,
    pub merge_gap_m: f32,
    /// Snap near-orthogonal/parallel normals onto a Manhattan frame.
    pub manhattan: bool,
    pub manhattan_snap_deg: f32,
    /// Cap on reported planes (largest kept).
    pub max_planes: usize,
    /// Max RMS residual (m) for a reported plane — rejects curved-surface
    /// chains (spheres, fillets) the local adjacency test lets through.
    /// 0 = auto (1.5 * eps).
    pub max_rms_m: f32,
    /// Post-pass: give unlabelled points to a fitting plane.
    pub assign_unlabelled: bool,
}

impl Default for PlaneOptions {
    fn default() -> Self {
        PlaneOptions {
            enabled: true,
            k: 10,
            grow_angle_deg: 15.0,
            dist_m: 0.0,
            min_inliers: 600,
            merge_angle_deg: 5.0,
            merge_gap_m: 0.05,
            manhattan: true,
            manhattan_snap_deg: 2.0,
            max_planes: 96,
            max_rms_m: 0.0,
            assign_unlabelled: false,
        }
    }
}

/// LSQ (+ optional IRLS round) plane fit over `members` of `pos`.
/// Returns (n, d, centroid, rms).
fn refit(pos: &[V3], members: &[u32], weights: Option<&[f32]>, irls: bool) -> Option<(V3, f32, V3, f32)> {
    let fit_once = |sel: &mut dyn Iterator<Item = u32>| -> Option<(V3, [f32; 6], usize)> {
        let mut c = [0.0f64; 3];
        let mut wsum = 0.0f64;
        let mut n = 0usize;
        let mut wv = Vec::new();
        for i in sel {
            let w = weights.map_or(1.0, |ws| ws[i as usize] as f64);
            c[0] += pos[i as usize][0] as f64 * w;
            c[1] += pos[i as usize][1] as f64 * w;
            c[2] += pos[i as usize][2] as f64 * w;
            wsum += w;
            wv.push((i, w));
            n += 1;
        }
        if n < 3 || wsum <= 0.0 {
            return None;
        }
        let c = [c[0] / wsum, c[1] / wsum, c[2] / wsum];
        let mut cov = [0.0f64; 6];
        for (i, w) in wv {
            let d = [
                pos[i as usize][0] as f64 - c[0],
                pos[i as usize][1] as f64 - c[1],
                pos[i as usize][2] as f64 - c[2],
            ];
            cov[0] += w * d[0] * d[0];
            cov[1] += w * d[0] * d[1];
            cov[2] += w * d[0] * d[2];
            cov[3] += w * d[1] * d[1];
            cov[4] += w * d[1] * d[2];
            cov[5] += w * d[2] * d[2];
        }
        Some((
            [c[0] as f32, c[1] as f32, c[2] as f32],
            cov.map(|x| x as f32),
            n,
        ))
    };
    let (c, cov, _cnt) = fit_once(&mut members.iter().copied())?;
    let (nrm, _) = sym3_min_eigvec(cov);
    let mut n = normalize(nrm);
    if dot(n, n) < 1e-6 {
        return None;
    }
    let mut c = c;
    let mut rms = {
        let d0 = -dot(n, c);
        let (mut s, mut wsum) = (0.0f64, 0.0f64);
        for &i in members {
            let w = weights.map_or(1.0, |ws| ws[i as usize]) as f64;
            let r = dot(n, pos[i as usize]) + d0;
            s += w * (r as f64) * (r as f64);
            wsum += w;
        }
        (s / wsum).sqrt() as f32
    };

    if irls {
        // One Huber-flavoured reweight at 2.5 sigma.
        let d0 = -dot(n, c);
        let s2 = (rms * 2.5).max(1e-4);
        let mut wc = [0.0f64; 3];
        let mut cov = [0.0f64; 6];
        let mut wsum = 0.0f64;
        for &i in members {
            let p = pos[i as usize];
            let w0 = weights.map_or(1.0, |ws| ws[i as usize]);
            let r = (dot(n, p) + d0).abs();
            let w = (w0 / (1.0 + (r / s2) * (r / s2))) as f64;
            wsum += w;
            for k in 0..3 {
                wc[k] += p[k] as f64 * w;
            }
            cov[0] += w * (p[0] as f64) * (p[0] as f64);
            cov[1] += w * (p[0] as f64) * (p[1] as f64);
            cov[2] += w * (p[0] as f64) * (p[2] as f64);
            cov[3] += w * (p[1] as f64) * (p[1] as f64);
            cov[4] += w * (p[1] as f64) * (p[2] as f64);
            cov[5] += w * (p[2] as f64) * (p[2] as f64);
        }
        if wsum > 0.0 {
            let cc = [wc[0] / wsum, wc[1] / wsum, wc[2] / wsum];
            let mut mcov = [0.0f32; 6];
            mcov[0] = (cov[0] / wsum - cc[0] * cc[0]) as f32;
            mcov[1] = (cov[1] / wsum - cc[0] * cc[1]) as f32;
            mcov[2] = (cov[2] / wsum - cc[0] * cc[2]) as f32;
            mcov[3] = (cov[3] / wsum - cc[1] * cc[1]) as f32;
            mcov[4] = (cov[4] / wsum - cc[1] * cc[2]) as f32;
            mcov[5] = (cov[5] / wsum - cc[2] * cc[2]) as f32;
            let (nrm2, _) = sym3_min_eigvec(mcov);
            if dot(nrm2, nrm2) > 1e-6 {
                let n2 = normalize(nrm2);
                if dot(n2, n) > 0.0 {
                    n = n2;
                } else {
                    n = scale(n2, -1.0);
                }
                c = [cc[0] as f32, cc[1] as f32, cc[2] as f32];
                // eigvals are descending: [2] is the thinnest (off-plane) axis.
                rms = (sym3_eigvals(mcov)[2]).max(0.0).sqrt();
            }
        }
    }
    Some((n, -dot(n, c), c, rms))
}

/// Canonical plane-normal sign: first largest-|component| axis positive.
#[inline]
fn canon(n: V3) -> V3 {
    let a = [n[0].abs(), n[1].abs(), n[2].abs()];
    let i = if a[0] >= a[1] && a[0] >= a[2] {
        0
    } else if a[1] >= a[2] {
        1
    } else {
        2
    };
    if n[i] < 0.0 {
        scale(n, -1.0)
    } else {
        n
    }
}

/// Detect planes. `cloud.nrm` must be filled (run normals::estimate first);
/// with no normals every edge fails the angle test and the result is empty.
pub fn detect(cloud: &PointCloud, opts: &PlaneOptions) -> (Planes, crate::Stats) {
    let n = cloud.len();
    let empty = Planes { planes: Vec::new(), label: vec![u32::MAX; n] };
    if !opts.enabled || n < opts.min_inliers * 2 || !cloud.has_normals() {
        return (empty, serde_json::json!({ "enabled": opts.enabled, "points": n, "planes": 0 }));
    }

    // Local spacing from the radius array (written by clean/normals), else probe.
    let mut r: Vec<f32> = cloud.radius.iter().copied().filter(|x| x.is_finite() && *x > 0.0).collect();
    let spacing = if r.is_empty() {
        let probe = Grid::build(&cloud.pos[..n.min(65_536)], 0.05);
        probe.median_nn_spacing(2048)
    } else {
        let mid = r.len() / 2;
        *r.select_nth_unstable_by(mid, |a, b| a.total_cmp(b)).1
    }
    .max(1e-4);

    let eps = if opts.dist_m > 0.0 { opts.dist_m } else { (1.8 * spacing).max(0.012) };
    let cos_g = (opts.grow_angle_deg * std::f32::consts::PI / 180.0).cos();

    // One kNN pass serves both BFS adjacency and seed curvature.
    let grid = Grid::build(&cloud.pos, (2.0 * spacing).max(0.004));
    let (idx, d2) = grid.knn_batch_d2(&cloud.pos, opts.k + 1, 2.5 * spacing);
    let kq = opts.k + 1;
    let _ = d2;

    // Per-point curvature (lambda0 / trace) from the kNN covariance — seeds
    // run flattest-first.
    let curv: Vec<f32> = (0..n)
        .into_par_iter()
        .map(|i| {
            let row_i = &idx[i * kq..(i + 1) * kq];
            let mut c = [0.0f32; 6];
            let mut m = [0.0f32; 3];
            let mut cnt = 0usize;
            for &j in row_i {
                if j == u32::MAX {
                    continue;
                }
                let p = cloud.pos[j as usize];
                for a in 0..3 {
                    m[a] += p[a];
                }
                cnt += 1;
            }
            if cnt < 4 {
                return f32::MAX;
            }
            for a in 0..3 {
                m[a] /= cnt as f32;
            }
            for &j in row_i {
                if j == u32::MAX {
                    continue;
                }
                let d = sub(cloud.pos[j as usize], m);
                c[0] += d[0] * d[0];
                c[1] += d[0] * d[1];
                c[2] += d[0] * d[2];
                c[3] += d[1] * d[1];
                c[4] += d[1] * d[2];
                c[5] += d[2] * d[2];
            }
            let ev = sym3_eigvals(c); // descending
            let tr = ev[0] + ev[1] + ev[2];
            if tr > 0.0 {
                ev[2] / tr // surface variation: smallest eigenvalue share
            } else {
                f32::MAX
            }
        })
        .collect();

    // Seeded region growing, sequential and deterministic: each region grows
    // against its frozen seed plane over the kNN adjacency, so chained growth
    // across curved surfaces is impossible (deviation accumulates past eps).
    let mut seeds: Vec<u32> = (0..n as u32).collect();
    seeds.sort_unstable_by(|&a, &b| curv[a as usize].total_cmp(&curv[b as usize]).then(a.cmp(&b)));
    let mut label = vec![u32::MAX; n];
    let mut comp: Vec<Vec<u32>> = Vec::new();
    let mut queue: Vec<u32> = Vec::with_capacity(1024);
    for &s in &seeds {
        if label[s as usize] != u32::MAX || curv[s as usize] == f32::MAX {
            continue;
        }
        let rid = comp.len() as u32;
        let pn = cloud.nrm[s as usize];
        if dot(pn, pn) < 1e-6 {
            continue;
        }
        let pd = -dot(pn, cloud.pos[s as usize]);
        queue.clear();
        queue.push(s);
        label[s as usize] = rid;
        let mut members: Vec<u32> = Vec::with_capacity(256);
        while let Some(i) = queue.pop() {
            members.push(i);
            let row_i = &idx[i as usize * kq..(i as usize + 1) * kq];
            for &j in row_i {
                if j == u32::MAX || label[j as usize] != u32::MAX {
                    continue;
                }
                let jj = j as usize;
                if dot(pn, cloud.nrm[jj]).abs() < cos_g {
                    continue;
                }
                if (dot(pn, cloud.pos[jj]) + pd).abs() > eps {
                    continue;
                }
                label[jj] = rid;
                queue.push(j);
            }
        }
        let mlen = members.len();
        if mlen >= (opts.min_inliers / 4).max(8) {
            comp.push(members);
        } else {
            for &i in &members {
                label[i as usize] = u32::MAX;
            }
        }
    }

    // Refit each sufficiently large region (parallel); rms-capped to reject
    // curved-surface or corner-fused groups.
    if std::env::var("PLANES_DEBUG").is_ok() {
        let mut sizes: Vec<usize> = comp.iter().map(|m| m.len()).collect();
        sizes.sort_unstable_by(|a, b| b.cmp(a));
        eprintln!("[planes dbg] comp={} top_sizes={:?} min_inliers={}", comp.len(), &sizes[..sizes.len().min(12)], opts.min_inliers);
    }
    let mut fits: Vec<(u32, usize, V3, f32, V3, f32)> = comp
        .par_iter()
        .enumerate()
        .filter_map(|(root, members)| {
            if members.len() < opts.min_inliers {
                return None;
            }
            let w = if cloud.weight.len() == n { Some(&cloud.weight[..]) } else { None };
            let rms_cap = if opts.max_rms_m > 0.0 { opts.max_rms_m } else { eps * 1.5 };
            let out = refit(&cloud.pos, members, w, true)
                .filter(|(_, _, _, rms)| *rms <= rms_cap)
                .map(|(nr, d, c, rms)| (root as u32, members.len(), nr, d, c, rms));
            if std::env::var("PLANES_DEBUG").is_ok() && out.is_none() {
                let rr = refit(&cloud.pos, members, w, true);
                eprintln!("[planes dbg] reject root={root} len={} refit={:?} cap={rms_cap}", members.len(), rr.map(|x| (x.3)));
            }
            out
        })
        .collect();
    // Canonical sign + sort by size desc for determinism.
    for f in fits.iter_mut() {
        let cn = canon(f.2);
        if cn != f.2 {
            f.2 = cn;
            f.3 = -f.3; // d = -n·c flips with n
        }
    }
    fits.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    fits.truncate(opts.max_planes * 4);

    // Merge coplanar pairs (same canonical normal dir within merge_angle,
    // |d| close) — joins coplanar segments disconnected by occluders.
    let cos_m = (opts.merge_angle_deg * std::f32::consts::PI / 180.0).cos();
    let m = fits.len();
    let mut puf: Vec<u32> = (0..m as u32).collect();
    fn findp(p: &mut Vec<u32>, mut x: u32) -> u32 {
        while p[x as usize] != x {
            p[x as usize] = p[p[x as usize] as usize];
            x = p[x as usize];
        }
        x
    }
    let merge_with = |puf: &mut Vec<u32>, fits: &mut Vec<(u32, usize, V3, f32, V3, f32)>, i: usize, j: usize, cos_m: f32, gap: f32| {
        if dot(fits[i].2, fits[j].2) >= cos_m && (fits[i].3 - fits[j].3).abs() <= gap {
            let (ri, rj) = (findp(puf, i as u32), findp(puf, j as u32));
            if ri != rj {
                puf[rj as usize] = ri;
            }
        }
    };
    for i in 0..m {
        for j in (i + 1)..m {
            merge_with(&mut puf, &mut fits, i, j, cos_m, opts.merge_gap_m);
        }
    }

    // Manhattan frame snap: dominant perpendicular axes.
    let mut snapped = 0usize;
    if opts.manhattan && fits.len() >= 2 {
        let sin_s = (opts.manhattan_snap_deg * std::f32::consts::PI / 180.0).sin();
        let cos_s = (opts.manhattan_snap_deg * std::f32::consts::PI / 180.0).cos();
        let w = |i: usize| fits[i].1;
        // axis0 = heaviest normal; axis1 = heaviest near-perpendicular; axis2 = cross.
        let a0 = fits[0].2;
        let mut best1: Option<(usize, V3)> = None;
        for i in 1..m {
            let ni = fits[i].2;
            if dot(ni, a0).abs() <= sin_s && best1.map_or(true, |(b, _)| w(i) > w(b)) {
                best1 = Some((i, normalize(sub(ni, scale(a0, dot(ni, a0))))));
            }
        }
        if let Some((_, a1)) = best1 {
            let a2 = normalize(cross(a0, a1));
            let axes = [a0, a1, a2];
            for f in fits.iter_mut() {
                for ax in axes {
                    if dot(f.2, ax).abs() >= cos_s {
                        let s = if dot(f.2, ax) < 0.0 { -1.0 } else { 1.0 };
                        f.2 = scale(ax, s);
                        f.3 = -dot(f.2, f.4); // d = -n·centroid
                        snapped += 1;
                        break;
                    }
                }
            }
            // Re-merge planes that snapped onto the same axis.
            let mut puf2: Vec<u32> = (0..m as u32).collect();
            for i in 0..m {
                for j in (i + 1)..m {
                    merge_with(&mut puf2, &mut fits, i, j, 1.0 - 1e-6, opts.merge_gap_m);
                }
            }
            puf = puf2;
        }
    }

    // Canonical plane list = merge-groups sorted by total inliers desc.
    let mut groups: rustc_hash::FxHashMap<u32, Vec<usize>> = rustc_hash::FxHashMap::default();
    for i in 0..m {
        groups.entry(findp(&mut puf, i as u32)).or_default().push(i);
    }
    let mut group_list: Vec<Vec<usize>> = groups.into_values().collect();
    group_list.sort_by_key(|g| std::cmp::Reverse(g.iter().map(|&i| fits[i].1).sum::<usize>()));
    group_list.truncate(opts.max_planes);

    let mut planes: Vec<Plane> = Vec::with_capacity(group_list.len());
    let mut plane_of_fit = vec![u32::MAX; m];
    for (gi, g) in group_list.iter().enumerate() {
        // Rep the group by its largest component's fit (refit over all members
        // would need another pass — largest member dominates anyway).
        let &rep = g.iter().max_by_key(|&&i| fits[i].1).unwrap();
        let (n_, d, _c, rms) = (fits[rep].2, fits[rep].3, fits[rep].4, fits[rep].5);
        let inliers: usize = g.iter().map(|&i| fits[i].1).sum();
        for &i in g {
            plane_of_fit[i] = gi as u32;
        }
        planes.push(Plane { n: n_, d, inliers: inliers as u32, area_m2: inliers as f32 * spacing * spacing, rms_m: rms.max(1e-4) });
    }

    // Labels: component root -> fit index -> plane index.
    let mut fit_of_root: rustc_hash::FxHashMap<u32, u32> = rustc_hash::FxHashMap::default();
    for (fi, f) in fits.iter().enumerate() {
        fit_of_root.insert(f.0, fi as u32);
    }
    let mut label = vec![u32::MAX; n];
    for (root, members) in comp.iter().enumerate() {
        if members.is_empty() {
            continue;
        }
        if let Some(&fi) = fit_of_root.get(&(root as u32)) {
            let pl = plane_of_fit[fi as usize];
            if pl != u32::MAX {
                for &i in members {
                    label[i as usize] = pl;
                }
            }
        }
    }

    if std::env::var("PLANES_DEBUG").is_ok() {
        for (i, p) in planes.iter().enumerate() {
            eprintln!("[planes dbg] #{i} n={:?} d={:.3} inl={} rms={:.4}", p.n, p.d, p.inliers, p.rms_m);
        }
    }

    // Post-assign stragglers to a fitting plane (boundaries, missed inliers).
    let mut assigned = 0usize;
    if opts.assign_unlabelled && !planes.is_empty() {
        let pls = Planes { planes: planes.clone(), label: Vec::new() };
        assigned = label
            .par_iter_mut()
            .enumerate()
            .map(|(i, l)| {
                if *l == u32::MAX {
                    if let Some(p) = pls.nearest(cloud.pos[i], cloud.nrm[i], eps * 0.75, opts.grow_angle_deg * 1.5) {
                        *l = p;
                        return 1usize;
                    }
                }
                0
            })
            .sum();
        // Refresh inlier counts after assignment.
        let mut cnt = vec![0u32; planes.len()];
        for &l in &label {
            if l != u32::MAX {
                cnt[l as usize] += 1;
            }
        }
        for (i, pl) in planes.iter_mut().enumerate() {
            pl.inliers = cnt[i];
            pl.area_m2 = cnt[i] as f32 * spacing * spacing;
        }
    }

    let labelled = label.iter().filter(|&&l| l != u32::MAX).count();
    let n_planes = planes.len();
    (
        Planes { planes, label },
        serde_json::json!({
            "points": n, "planes": n_planes, "labelled": labelled,
            "snapped": snapped, "eps_m": eps, "spacing_m": spacing,
            "assigned_extra": assigned,
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{clean, normals, synth};

    #[test]
    fn detects_room_planes() {
        let scene = synth::Scene::room();
        let opts = synth::ScanOptions { depth_w: 96, depth_h: 72, image_w: 64, image_h: 48, yaw_steps: 4, ..Default::default() };
        let scan = synth::simulate(&scene, &opts);
        let mut cloud = scan.cloud;
        clean::run(&mut cloud, &clean::CleanOptions::default());
        normals::estimate(&mut cloud, &normals::NormalOptions::default());
        let (planes, _st) = detect(&cloud, &PlaneOptions::default());
        assert!(planes.planes.len() >= 4, "found {} planes", planes.planes.len());
        let labelled = planes.label.iter().filter(|&&l| l != u32::MAX).count();
        assert!(labelled as f32 / cloud.len() as f32 > 0.5, "labelled {labelled}/{}", cloud.len());
        // Biggest plane should be near-axis-aligned (Manhattan) and tight.
        let big = &planes.planes[0];
        assert!(big.rms_m < 0.02, "rms {}", big.rms_m);
        let ax = big.n.iter().map(|v| v.abs()).fold(0.0f32, f32::max);
        assert!(ax > 0.999, "manhattan snap: n={:?}", big.n);
        // nearest() finds a horizontal plane under a floor-ish query.
        let hit = planes.nearest([0.0, 0.02, 0.0], [0.0, 1.0, 0.0], 0.05, 10.0);
        assert!(hit.is_some());
    }
}
