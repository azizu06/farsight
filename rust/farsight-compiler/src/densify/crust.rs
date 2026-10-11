//! Crust fill: a breadth-first front that marches the local fitted surface
//! across unobserved gaps. Handles everything the global planes cannot —
//! curved furniture, small patches, and sparse upsampling. Cell size is the
//! target spacing; candidates are snapped to a weighted PCA plane of their
//! neighbourhood (measured points + previously accepted fills). An
//! edge-guard keeps the front from bridging across creases.

use super::fit::fit_plane;
use crate::core::*;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};

/// Output of the crust pass.
#[derive(Default)]
pub struct CrustOut {
    pub pos: Vec<V3>,
    pub nrm: Vec<V3>,
    pub support: Vec<f32>,
    pub rejected_snap: usize,
    pub rejected_edge: usize,
    pub rejected_sparse: usize,
    /// Rejected by the two-sided support test (open boundary / extension).
    pub rejected_boundary: usize,
    pub duplicate: usize,
}

/// Incremental point grid: cell -> intrusive linked list of point indices.
/// Points are appended to `pos`/`nrm` as fills are accepted.
struct PointGrid {
    inv: f32,
    head: FxHashMap<(i32, i32, i32), i32>,
    next: Vec<i32>,
    pos: Vec<V3>,
    nrm: Vec<V3>,
}

impl PointGrid {
    fn cell(&self, p: V3) -> (i32, i32, i32) {
        (
            (p[0] * self.inv).floor() as i32,
            (p[1] * self.inv).floor() as i32,
            (p[2] * self.inv).floor() as i32,
        )
    }
    fn insert(&mut self, p: V3, n: V3) -> usize {
        let idx = self.pos.len();
        let c = self.cell(p);
        let h = self.head.get(&c).copied().unwrap_or(-1);
        self.pos.push(p);
        self.nrm.push(n);
        self.next.push(h);
        self.head.insert(c, idx as i32);
        idx
    }
    /// Visit point indices in cells within Chebyshev distance `r` of cell `c`.
    fn for_each_near(&self, c: (i32, i32, i32), r: i32, mut f: impl FnMut(usize)) {
        for dx in -r..=r {
            for dy in -r..=r {
                for dz in -r..=r {
                    let k = (c.0 + dx, c.1 + dy, c.2 + dz);
                    let mut i = self.head.get(&k).copied().unwrap_or(-1);
                    while i >= 0 {
                        f(i as usize);
                        i = self.next[i as usize];
                    }
                }
            }
        }
    }
}

/// Grow the crust.
///
/// * `pos`,`nrm` — all currently accepted points (measured first, then plane
///   fills) — the support set the crust grows from.
/// * `ts` — cell size / target spacing; `reach` — max support distance (m).
/// * `meas_count` — leading entries of `pos` that are measured (not fills);
///   only they count as support for the two-sided interpolation test.
/// * `edge_cos` — cos of the edge-guard angle: neighbours whose normal
///   disagrees with the reference normal by more than this are excluded.
/// * `snap_max` — max snap distance as a multiple of `ts`.
/// * `max_fill` — cap on emitted points.
pub fn grow(
    pos: &[V3],
    nrm: &[V3],
    meas_count: usize,
    ts: f32,
    reach: f32,
    edge_cos: f32,
    snap_max: f32,
    max_fill: usize,
) -> CrustOut {
    let mut g = PointGrid {
        inv: 1.0 / ts,
        head: FxHashMap::default(),
        next: vec![-1; pos.len()],
        pos: pos.to_vec(),
        nrm: nrm.to_vec(),
    };
    let mut out = CrustOut::default();
    for (i, p) in pos.iter().enumerate() {
        let c = g.cell(*p);
        let h = g.head.get(&c).copied().unwrap_or(-1);
        g.next[i] = h;
        g.head.insert(c, i as i32);
    }

    // Measured-occupancy map at 2 ts carrying summed normals — the support
    // test only counts hits whose normals agree with the candidate's fitted
    // normal, so unrelated surfaces behind/below the sheet don't count as
    // support (interpolation vs extrapolation discriminator).
    let cs = 2.0 * ts;
    let cinv = 1.0 / cs;
    let mut meas: FxHashMap<(i32, i32, i32), V3> = FxHashMap::default();
    for i in 0..meas_count.min(pos.len()) {
        let p = pos[i];
        let c = (
            (p[0] * cinv).floor() as i32,
            (p[1] * cinv).floor() as i32,
            (p[2] * cinv).floor() as i32,
        );
        let e = meas.entry(c).or_insert([0.0; 3]);
        *e = add(*e, nrm[i]);
    }
    let span_m = 1.25 * reach;

    // Occupied-cell set = every cell with points.
    let mut occupied: FxHashSet<(i32, i32, i32)> = g.head.keys().copied().collect();
    // Blocked cells failed a candidate evaluation; never retried.
    let mut blocked: FxHashSet<(i32, i32, i32)> = FxHashSet::default();

    // Seed frontier: cells containing points that touch an empty neighbour.
    // For measured surfaces this is the ~1-cell boundary shell.
    let max_depth = (reach * g.inv).ceil() as i32;
    let mut frontier: Vec<((i32, i32, i32), i32)> = Vec::new(); // (cell, depth)
    {
        // Only cells that contain points can seed; gather them sorted for
        // determinism.
        let mut seeds: Vec<(i32, i32, i32)> = occupied.iter().copied().collect();
        seeds.sort_unstable();
        for c in seeds {
            if has_empty_neighbour(&occupied, c) {
                frontier.push((c, 0));
            }
        }
    }

    let snap_max_m = snap_max * ts;
    let mut round = 0i32;
    while !frontier.is_empty() && out.pos.len() < max_fill {
        round += 1;
        // Candidate empty cells adjacent to the frontier: dedupe keeping the
        // smallest support depth.
        let mut cand: FxHashMap<(i32, i32, i32), i32> = FxHashMap::default();
        for &(c, depth) in &frontier {
            if depth >= max_depth {
                continue;
            }
            for dx in -1..=1 {
                for dy in -1..=1 {
                    for dz in -1..=1 {
                        let k = (c.0 + dx, c.1 + dy, c.2 + dz);
                        if occupied.contains(&k) || blocked.contains(&k) {
                            continue;
                        }
                        cand.entry(k).and_modify(|d| *d = (*d).min(depth + 1)).or_insert(depth + 1);
                    }
                }
            }
        }
        if cand.is_empty() {
            break;
        }
        let mut cells: Vec<((i32, i32, i32), i32)> = cand.into_iter().collect();
        cells.sort_unstable();

        // Evaluate candidates in parallel: snap to the weighted local plane of
        // neighbours (measured + fills so far), guarded against edges.
        let results: Vec<Candidate> = cells
            .par_iter()
            .map(|&(cell, depth)| {
                evaluate(&g, cell, depth, snap_max_m, edge_cos, ts, &meas, cinv, span_m)
            })
            .collect();

        let mut next: Vec<((i32, i32, i32), i32)> = Vec::new();
        for ((cell, depth), r) in cells.iter().zip(results) {
            match r {
                Candidate::Ok(p, n) => {
                    occupied.insert(*cell);
                    g.insert(p, n);
                    out.pos.push(p);
                    out.nrm.push(n);
                    out.support.push(*depth as f32 * ts);
                    next.push((*cell, *depth));
                }
                Candidate::RejectSnap => {
                    out.rejected_snap += 1;
                    blocked.insert(*cell);
                }
                Candidate::RejectEdge => {
                    out.rejected_edge += 1;
                    blocked.insert(*cell);
                }
                Candidate::RejectSparse => {
                    out.rejected_sparse += 1;
                    blocked.insert(*cell);
                }
                Candidate::RejectDup => {
                    out.duplicate += 1;
                    blocked.insert(*cell);
                }
                Candidate::RejectBoundary => {
                    out.rejected_boundary += 1;
                    blocked.insert(*cell);
                }
            }
        }
        frontier = next;
        if round > max_depth + 64 {
            break;
        }
    }
    out
}

fn has_empty_neighbour(occ: &FxHashSet<(i32, i32, i32)>, c: (i32, i32, i32)) -> bool {
    for dx in -1..=1 {
        for dy in -1..=1 {
            for dz in -1..=1 {
                if (dx | dy | dz) != 0 && !occ.contains(&(c.0 + dx, c.1 + dy, c.2 + dz)) {
                    return true;
                }
            }
        }
    }
    false
}

enum Candidate {
    Ok(V3, V3),
    RejectSnap,
    RejectEdge,
    RejectSparse,
    RejectDup,
    RejectBoundary,
}

/// Snap one empty cell to the neighbourhood surface. Candidates deeper than
/// the immediate upsampling ring must also pass the two-sided support test so
/// the front fills enclosed gaps but never extrapolates past silhouettes.
#[allow(clippy::too_many_arguments)]
fn evaluate(
    g: &PointGrid,
    cell: (i32, i32, i32),
    depth: i32,
    snap_max: f32,
    edge_cos: f32,
    ts: f32,
    meas: &FxHashMap<(i32, i32, i32), V3>,
    cinv: f32,
    span_m: f32,
) -> Candidate {
    let center = [
        (cell.0 as f32 + 0.5) * ts,
        (cell.1 as f32 + 0.5) * ts,
        (cell.2 as f32 + 0.5) * ts,
    ];
    // Gather neighbours within radius r.
    let r_cells = 2i32;
    let r = 2.5 * ts;
    let r2 = r * r;
    let mut nb: Vec<(usize, f32)> = Vec::with_capacity(32);
    g.for_each_near(cell, r_cells, |i| {
        let d2 = dist2(g.pos[i], center);
        if d2 <= r2 {
            nb.push((i, d2));
        }
    });
    if nb.len() < 4 {
        return Candidate::RejectSparse;
    }
    nb.sort_unstable_by(|a, b| a.1.total_cmp(&b.1));
    // Edge guard: reference normal = closest neighbour's; keep agreeing points.
    let n0 = g.nrm[nb[0].0];
    let keep: Vec<(V3, f32)> = nb
        .iter()
        .filter(|&&(i, _)| dot(g.nrm[i], n0) >= edge_cos)
        .map(|&(i, d2)| (g.pos[i], 1.0 / (d2 + 0.25 * ts * ts)))
        .collect();
    if keep.len() < 3 {
        return Candidate::RejectEdge;
    }
    let Some(f) = fit_plane(&keep) else {
        return Candidate::RejectEdge;
    };
    // Degenerate (near-collinear) support can produce an in-plane "normal";
    // require agreement with the reference surface normal.
    if dot(f.n, n0).abs() < edge_cos {
        return Candidate::RejectEdge;
    }
    let snap = f.signed_dist(center).abs();
    if snap > snap_max {
        return Candidate::RejectSnap;
    }
    let p = f.project(center);
    // Reject if the projected point lands within ~one spacing of an existing
    // point — fills must add coverage, not thicken the sheet.
    let dn2 = nb.iter().map(|&(i, _)| dist2(p, g.pos[i])).fold(f32::MAX, f32::min);
    if dn2 < (0.8 * ts) * (0.8 * ts) {
        return Candidate::RejectDup;
    }
    let mut n = f.n;
    if dot(n, n0) < 0.0 {
        n = scale(n, -1.0);
    }
    // Beyond the 2-cell upsampling ring, require measured support on at least
    // two opposite direction pairs — interpolation, not extrapolation.
    if depth > 2 && !two_sided(meas, cinv, p, n, span_m) {
        return Candidate::RejectBoundary;
    }
    Candidate::Ok(p, n)
}

/// Ray-march the measured-occupancy map along 8 in-plane directions from `p`.
/// A hit counts only when the cell's summed normal agrees with `n` (same
/// sheet). The candidate interpolates iff both directions of at least two
/// opposite direction pairs are supported.
fn two_sided(
    meas: &FxHashMap<(i32, i32, i32), V3>,
    cinv: f32,
    p: V3,
    n: V3,
    span_m: f32,
) -> bool {
    let cs = 1.0 / cinv;
    let (u, v) = super::fit::tangent_basis(n);
    let ax = if n[0].abs() >= n[1].abs() && n[0].abs() >= n[2].abs() {
        0
    } else if n[1].abs() >= n[2].abs() {
        1
    } else {
        2
    };
    let mut hit = [false; 8];
    for (k, h) in hit.iter_mut().enumerate() {
        let a = (k as f32) * std::f32::consts::FRAC_PI_4;
        let d = add(scale(u, a.cos()), scale(v, a.sin()));
        let mut t = cs;
        while t <= span_m {
            let q = add(p, scale(d, t));
            let c = (
                (q[0] * cinv).floor() as i32,
                (q[1] * cinv).floor() as i32,
                (q[2] * cinv).floor() as i32,
            );
            let mut o = [0i32; 3];
            o[ax] = 1;
            for s in -1i32..=1 {
                let cc = (c.0 + s * o[0], c.1 + s * o[1], c.2 + s * o[2]);
                if let Some(sn) = meas.get(&cc) {
                    if dot(*sn, n) > 0.0 {
                        *h = true;
                        break;
                    }
                }
            }
            if *h {
                break;
            }
            t += cs;
        }
    }
    let mut pairs = 0;
    for k in 0..4 {
        let fwd = hit[k] || hit[(k + 7) % 8] || hit[(k + 1) % 8];
        let bwd = hit[k + 4] || hit[(k + 3) % 8] || hit[(k + 5) % 8];
        if fwd && bwd {
            pairs += 1;
        }
    }
    pairs >= 2
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plane_with_hole(half: f32, ts: f32, hole_half: f32) -> (Vec<V3>, Vec<V3>, Vec<bool>) {
        let n = (2.0 * half / ts) as usize;
        let (mut pos, mut nrm, mut m) = (Vec::new(), Vec::new(), Vec::new());
        for i in 0..n {
            for j in 0..n {
                let x = -half + i as f32 * ts;
                let y = -half + j as f32 * ts;
                if x.abs() < hole_half && y.abs() < hole_half {
                    continue;
                }
                pos.push([x, y, 0.0]);
                nrm.push([0.0, 0.0, 1.0]);
                m.push(true);
            }
        }
        (pos, nrm, m)
    }

    #[test]
    fn fills_planar_hole() {
        let ts = 0.02f32;
        let (pos, nrm, _m) = plane_with_hole(1.0, ts, 0.15);
        let out = grow(&pos, &nrm, pos.len(), ts, 0.5, 0.5, 1.5, 100_000);
        assert!(out.pos.len() > 30, "got {}", out.pos.len());
        assert!(out.pos.iter().all(|p| p[2].abs() < 1e-4));
        assert!(out.pos.iter().any(|p| p[0].abs() < 0.05 && p[1].abs() < 0.05));
    }

    #[test]
    fn does_not_bridge_sharp_edge() {
        // Two half-planes meeting at 90 degrees along x=0: z=0 for x<0, x=0 for z>0.
        let ts = 0.02f32;
        let (mut pos, mut nrm, mut m) = (Vec::new(), Vec::new(), Vec::new());
        for i in 0..50 {
            for j in 0..50 {
                let x = -1.0 + i as f32 * ts;
                let y = -0.5 + j as f32 * ts;
                pos.push([x, y, 0.0]);
                nrm.push([0.0, 0.0, 1.0]);
                m.push(true);
            }
        }
        for i in 0..50 {
            for j in 0..50 {
                let z = 0.0 + i as f32 * ts;
                let y = -0.5 + j as f32 * ts;
                pos.push([0.0, y, z]);
                nrm.push([1.0, 0.0, 0.0]);
                m.push(true);
            }
        }
        let out = grow(&pos, &nrm, pos.len(), ts, 0.5, 0.5, 1.5, 100_000);
        // Fills must hug one of the two planes, never float in the wedge.
        for p in &out.pos {
            let near_floor = p[2].abs() < 0.6 * ts;
            let near_wall = p[0].abs() < 0.6 * ts;
            assert!(near_floor || near_wall, "floating fill at {p:?}");
        }
    }
}
