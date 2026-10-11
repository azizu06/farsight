//! Dominant plane detection and planar hole filling. Rooms are mostly planar;
//! large planes (walls, floor, ceiling, furniture faces) are extended across
//! unobserved gaps by rasterising their inlier set in a tangent frame and
//! running a bounded chamfer flood from measured cells into empty ones.

use super::fit::{fit_plane, tangent_basis, PlaneFit};
use crate::core::*;
use crate::spatial::Grid;
use rustc_hash::{FxHashMap, FxHashSet};

/// A detected dominant plane with its inlier count and inlier uv bounding box.
#[derive(Clone, Debug)]
pub struct Plane {
    pub fit: PlaneFit,
    pub inliers: usize,
}

/// Detect dominant planes: bucket points by quantized normal direction, run a
/// 1D offset histogram per bucket, then refine each peak by weighted least
/// squares over its inliers. Deterministic (buckets processed in sorted order).
///
/// * `min_inliers` — required inlier count for a plane instance.
/// * `eps` — inlier distance threshold (m).
/// * `normal_cos` — inlier normal agreement threshold.
pub fn detect(cloud: &PointCloud, min_inliers: usize, eps: f32, normal_cos: f32) -> Vec<Plane> {
    let n = cloud.len();
    if !cloud.has_normals() || n < min_inliers * 4 {
        return Vec::new();
    }
    // Bucket by quantized normal (resolution ~11 deg). Offsets + normal sums per bucket.
    let mut buckets: FxHashMap<(i8, i8, i8), (Vec<f32>, V3)> = FxHashMap::default();
    for i in 0..n {
        let nr = cloud.nrm[i];
        let key = (
            (nr[0] * 4.0).round() as i8,
            (nr[1] * 4.0).round() as i8,
            (nr[2] * 4.0).round() as i8,
        );
        let e = buckets.entry(key).or_default();
        e.0.push(dot(cloud.pos[i], nr));
        e.1 = add(e.1, nr);
    }
    let mut keys: Vec<(i8, i8, i8)> = buckets.keys().copied().collect();
    keys.sort_unstable();

    let bin_w = 0.05f32; // 5 cm offset bins
    let mut planes: Vec<Plane> = Vec::new();
    for key in keys {
        let Some((offs, _)) = buckets.get(&key) else { continue };
        if offs.len() < min_inliers {
            continue;
        }
        let mut hist: FxHashMap<i32, u32> = FxHashMap::default();
        for &o in offs {
            *hist.entry((o / bin_w).floor() as i32).or_default() += 1;
        }
        let mut bins: Vec<(i32, u32)> = hist.into_iter().collect();
        bins.sort_unstable();
        // Greedy peaks: any bin above threshold seeds a cluster merged with
        // adjacent bins >= 20% of it (keeps histogram noise from splitting a plane).
        let mut i = 0;
        while i < bins.len() {
            let (b0, c0) = bins[i];
            if (c0 as usize) < min_inliers {
                i += 1;
                continue;
            }
            let mut j = i + 1;
            let mut tot = c0 as u64;
            let mut acc = b0 as i64 * c0 as i64;
            while j < bins.len() && bins[j].0 == bins[j - 1].0 + 1 && bins[j].1 as u64 * 5 >= c0 as u64 {
                tot += bins[j].1 as u64;
                acc += bins[j].0 as i64 * bins[j].1 as i64;
                j += 1;
            }
            let center_off = (acc as f64 / tot as f64 + 0.5) * bin_w as f64;
            let nrm = normalize(buckets[&key].1);
            let fit = refine(cloud, nrm, center_off as f32, eps, normal_cos);
            if let Some(f) = fit {
                planes.push(Plane { fit: f, inliers: tot as usize });
            }
            i = j;
        }
    }
    // Dedupe near-identical planes (same normal & offset), keep the larger.
    planes.sort_by(|a, b| b.inliers.cmp(&a.inliers));
    let mut out: Vec<Plane> = Vec::new();
    for p in planes {
        let dup = out.iter().any(|q| {
            dot(p.fit.n, q.fit.n).abs() > 0.995 && (p.fit.signed_dist(q.fit.c)).abs() < 0.03
        });
        if !dup {
            out.push(p);
        }
    }
    out.truncate(48);
    out
}

/// Weighted least-squares refit over the plane's inliers; reorients `n` to
/// agree with the majority of inlier normals.
fn refine(cloud: &PointCloud, n0: V3, d0: f32, eps: f32, normal_cos: f32) -> Option<PlaneFit> {
    let mut pts: Vec<(V3, f32)> = Vec::new();
    for i in 0..cloud.len() {
        let d = dot(cloud.pos[i], n0) - d0;
        if d.abs() < eps * 2.0 && dot(cloud.nrm[i], n0).abs() > normal_cos * 0.5 {
            let w = cloud.weight(i);
            pts.push((cloud.pos[i], w));
        }
    }
    let mut f = fit_plane(&pts)?;
    if dot(f.n, n0) < 0.0 {
        f.n = scale(f.n, -1.0);
    }
    // Sign the normal the way most inlier normals point.
    let mut s = 0.0f32;
    for i in 0..cloud.len() {
        let d = dot(cloud.pos[i], n0) - d0;
        if d.abs() < eps * 2.0 {
            s += dot(cloud.nrm[i], f.n).signum();
        }
    }
    if s < 0.0 {
        f.n = scale(f.n, -1.0);
    }
    Some(f)
}

/// Candidate fill points produced by planar extension.
pub struct PlaneFill {
    pub pos: Vec<V3>,
    pub nrm: Vec<V3>,
    /// Distance (m) from the nearest measured inlier cell — the support length.
    pub support: Vec<f32>,
}

/// Extend every detected plane across gaps enclosed by its measured footprint.
/// A raster cell is filled only when it is unreachable from the raster border
/// through empty cells (a true hole) and within `reach` of a measured inlier.
/// Open regions connected to the border — silhouette edges, beyond-corner
/// space — are never extended.
///
/// `ts` — raster cell size (target spacing); `occupied` — cells already
/// covered (measured cloud at cell size `ts`, plus previously emitted fills).
pub fn fill(
    planes: &[Plane],
    cloud: &PointCloud,
    ts: f32,
    reach: f32,
    occupied: &mut FxHashSet<(i32, i32, i32)>,
    meas_grid: &Grid,
) -> PlaneFill {
    let mut out = PlaneFill { pos: Vec::new(), nrm: Vec::new(), support: Vec::new() };
    let inv = 1.0 / ts;
    let reach_cells = (reach * inv).ceil() as i32;
    // Inlier tolerance must swallow depth noise (~sigma ~ 1.5 cm at 3 m) plus
    // quantisation, or genuinely measured wall cells look like gaps.
    let eps_inlier = (4.0 * ts).max(0.04);

    for plane in planes {
        let f = &plane.fit;
        let (u, v) = tangent_basis(f.n);
        // Gather inliers of this plane and their uv coords.
        let mut uvs: Vec<(f32, f32)> = Vec::new();
        let mut uvmin = [f32::INFINITY; 2];
        let mut uvmax = [f32::NEG_INFINITY; 2];
        for i in 0..cloud.len() {
            if f.signed_dist(cloud.pos[i]).abs() > eps_inlier || dot(cloud.nrm[i], f.n).abs() < 0.5 {
                continue;
            }
            let d = sub(cloud.pos[i], f.c);
            let (uu, vv) = (dot(d, u), dot(d, v));
            uvmin[0] = uvmin[0].min(uu);
            uvmin[1] = uvmin[1].min(vv);
            uvmax[0] = uvmax[0].max(uu);
            uvmax[1] = uvmax[1].max(vv);
            uvs.push((uu, vv));
        }
        if uvs.is_empty() {
            continue;
        }
        // Raster extents: tight measured bbox.
        let i0 = (uvmin[0] * inv).floor() as i32;
        let j0 = (uvmin[1] * inv).floor() as i32;
        let i1 = (uvmax[0] * inv).floor() as i32;
        let j1 = (uvmax[1] * inv).floor() as i32;
        let ext_i0 = i0;
        let ext_j0 = j0;
        let w = (i1 - ext_i0 + 1) as usize;
        let h = (j1 - ext_j0 + 1) as usize;
        if w == 0 || h == 0 || w.saturating_mul(h) > 64_000_000 {
            continue;
        }
        // Occupancy + chamfer distance (two-pass, 8-connected, unit/diagonal 1.5).
        let mut dist = vec![i32::MAX / 4; w * h];
        for &(uu, vv) in &uvs {
            let i = (uu * inv).floor() as i32 - ext_i0;
            let j = (vv * inv).floor() as i32 - ext_j0;
            if i >= 0 && j >= 0 && (i as usize) < w && (j as usize) < h {
                dist[(j as usize) * w + i as usize] = 0;
            }
        }
        // Forward pass.
        for j in 0..h {
            for i in 0..w {
                let d = dist[j * w + i];
                if d == 0 {
                    continue;
                }
                let mut m = d;
                if i > 0 {
                    m = m.min(dist[j * w + i - 1].saturating_add(10));
                }
                if j > 0 {
                    m = m.min(dist[(j - 1) * w + i].saturating_add(10));
                    if i > 0 {
                        m = m.min(dist[(j - 1) * w + i - 1].saturating_add(15));
                    }
                    if i + 1 < w {
                        m = m.min(dist[(j - 1) * w + i + 1].saturating_add(15));
                    }
                }
                dist[j * w + i] = m;
            }
        }
        // Backward pass.
        for j in (0..h).rev() {
            for i in (0..w).rev() {
                let d = dist[j * w + i];
                if d == 0 {
                    continue;
                }
                let mut m = d;
                if i + 1 < w {
                    m = m.min(dist[j * w + i + 1].saturating_add(10));
                }
                if j + 1 < h {
                    m = m.min(dist[(j + 1) * w + i].saturating_add(10));
                    if i + 1 < w {
                        m = m.min(dist[(j + 1) * w + i + 1].saturating_add(15));
                    }
                    if i > 0 {
                        m = m.min(dist[(j + 1) * w + i - 1].saturating_add(15));
                    }
                }
                dist[j * w + i] = m;
            }
        }
        // Enclosure: empty cells reachable from the raster border are open
        // space (silhouette/outside), unreachable cells are true gaps.
        let mut open = vec![false; w * h];
        let mut queue: Vec<i32> = Vec::new();
        for j in 0..h as i32 {
            for i in 0..w as i32 {
                if (i == 0 || j == 0 || i == w as i32 - 1 || j == h as i32 - 1)
                    && dist[(j as usize) * w + i as usize] > 0
                {
                    let idx = (j as usize) * w + i as usize;
                    if !open[idx] {
                        open[idx] = true;
                        queue.push((j * w as i32 + i) as i32);
                    }
                }
            }
        }
        while let Some(qi) = queue.pop() {
            let (qj, qi) = (qi / w as i32, qi % w as i32);
            for (dj, di) in [(1i32, 0i32), (-1, 0), (0, 1), (0, -1)] {
                let (nj, ni) = (qj + dj, qi + di);
                if nj < 0 || ni < 0 || nj >= h as i32 || ni >= w as i32 {
                    continue;
                }
                let idx = (nj as usize) * w + ni as usize;
                // Flood only through cells well clear of inliers: cells within
                // ~2.5 cells of a measured point act as barrier so sparse rim
                // sampling cannot leak the flood into an enclosed gap.
                if !open[idx] && dist[idx] > 25 {
                    open[idx] = true;
                    queue.push(nj * w as i32 + ni);
                }
            }
        }
        // Emit: unreachable (enclosed) cells within reach.
        let reach10 = reach_cells * 10;
        for j in 0..h as i32 {
            for i in 0..w as i32 {
                let idx = (j as usize) * w + i as usize;
                let d = dist[idx];
                if d == 0 || open[idx] || d > reach10 {
                    continue;
                }
                let gi = i + ext_i0;
                let gj = j + ext_j0;
                let pu = (gi as f32 + 0.5) * ts;
                let pv = (gj as f32 + 0.5) * ts;
                let p = add(f.c, add(scale(u, pu), scale(v, pv)));
                // Skip if a measured point already covers this spot.
                if meas_grid.nearest(p, ts * 0.8).is_some() {
                    continue;
                }
                let key = (
                    (p[0] * inv).floor() as i32,
                    (p[1] * inv).floor() as i32,
                    (p[2] * inv).floor() as i32,
                );
                if !occupied.insert(key) {
                    continue;
                }
                out.pos.push(p);
                out.nrm.push(f.n);
                out.support.push(d as f32 * ts * 0.1);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;

    fn plane_cloud(half: f32, ts: f32, hole_half: f32) -> PointCloud {
        // Plane z=0 sampled every ts, with a square hole around the origin.
        let mut rng = Rng::new(3);
        let mut c = PointCloud::default();
        let n = (2.0 * half / ts) as usize;
        for i in 0..n {
            for j in 0..n {
                let x = -half + i as f32 * ts;
                let y = -half + j as f32 * ts;
                if x.abs() < hole_half && y.abs() < hole_half {
                    continue;
                }
                c.pos.push([x, y, 0.002 * rng.normal()]);
                c.nrm.push([0.0, 0.0, 1.0]);
            }
        }
        c
    }

    #[test]
    fn detects_and_fills_square_hole() {
        let ts = 0.02f32;
        let cloud = plane_cloud(1.0, ts, 0.2);
        let planes = detect(&cloud, 100, 0.02, 0.9);
        assert_eq!(planes.len(), 1);
        assert!(planes[0].fit.n[2].abs() > 0.95);

        let grid = Grid::build(&cloud.pos, ts);
        let mut occ: FxHashSet<(i32, i32, i32)> = FxHashSet::default();
        for p in &cloud.pos {
            occ.insert(((p[0] / ts).floor() as i32, (p[1] / ts).floor() as i32, (p[2] / ts).floor() as i32));
        }
        let fill = fill(&planes, &cloud, ts, 0.4, &mut occ, &grid);
        // Enclosure semantics: exactly the ~400 enclosed hole cells fill.
        assert!(fill.pos.len() >= 350, "filled {}", fill.pos.len());
        // All fills on the plane and inside the hole square-ish region.
        assert!(fill.pos.iter().all(|p| p[2].abs() < 0.015));
        assert!(fill.pos.iter().any(|p| p[0].abs() < 0.1 && p[1].abs() < 0.1));
    }
}
