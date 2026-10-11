//! Normal estimation + consistent orientation + local spacing. OWNER: W2.
//!
//! Weighted-PCA normals over k nearest neighbours (closed-form symmetric 3x3
//! eigensolver), then global sign consistency:
//!  * with known scan viewpoints (`estimate_with_views` / `opts.viewpoints`)
//!    each normal is flipped to face the view that saw it;
//!  * without views, sign propagates through the kNN graph along a maximum
//!    |dot| spanning forest (parity union-find — equivalent to the Hoppe
//!    Riemannian MST), and each connected component's sign is fixed by a
//!    "normals point into free space" vote toward the interior of the cloud.
//!
//! `cloud.radius` is filled with the mean kNN distance (local sample spacing).

use crate::core::{dot, normalize, sub, PointCloud, V3};
use crate::spatial::Grid;
use crate::Stats;
use rayon::prelude::*;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct NormalOptions {
    /// PCA neighbourhood size.
    pub k: usize,
    /// Graph degree used for orientation propagation (0 = min(k, 12)).
    pub k_orient: usize,
    /// Down-weight far neighbours in the covariance (recommended on).
    pub robust: bool,
    /// Propagate signs over the kNN graph (recommended on).
    pub orient: bool,
    /// Known scanner/camera positions; when non-empty, normals are oriented
    /// toward these instead of using graph propagation.
    pub viewpoints: Vec<V3>,
}

impl Default for NormalOptions {
    fn default() -> Self {
        NormalOptions { k: 16, k_orient: 0, robust: true, orient: true, viewpoints: Vec::new() }
    }
}

/// Eigenvalues (descending) of a symmetric 3x3 packed [xx xy xz yy yz zz].
/// Closed-form trigonometric method; shared with clean.rs.
pub(crate) fn sym3_eigvals(c: [f32; 6]) -> [f32; 3] {
    let (a, b, cc, d, e, f) = (c[0], c[1], c[2], c[3], c[4], c[5]);
    let p1 = b * b + cc * cc + e * e;
    if p1 < 1e-20 {
        let mut v = [a, d, f];
        v.sort_by(|x, y| y.total_cmp(x));
        return v;
    }
    let q = (a + d + f) / 3.0;
    let p2 = (a - q) * (a - q) + (d - q) * (d - q) + (f - q) * (f - q) + 2.0 * p1;
    let p = (p2 / 6.0).sqrt();
    // B = (A - q I) / p
    let b11 = (a - q) / p;
    let b22 = (d - q) / p;
    let b33 = (f - q) / p;
    let b12 = b / p;
    let b13 = cc / p;
    let b23 = e / p;
    let det_b = b11 * (b22 * b33 - b23 * b23) - b12 * (b12 * b33 - b13 * b23) + b13 * (b12 * b23 - b13 * b22);
    let r = (det_b / 2.0).clamp(-1.0, 1.0);
    let phi = r.acos() / 3.0;
    let l0 = q + 2.0 * p * phi.cos();
    let l2 = q + 2.0 * p * (phi + 2.0 * std::f32::consts::PI / 3.0).cos();
    let l1 = 3.0 * q - l0 - l2;
    [l0, l1, l2]
}

/// Smallest eigenvector + eigenvalues of symmetric 3x3 packed [xx xy xz yy yz zz].
/// Returns (normal, [l0>=l1>=l2]).
pub(crate) fn sym3_min_eigvec(c: [f32; 6]) -> ([f32; 3], [f32; 3]) {
    let ev = sym3_eigvals(c);
    let l = ev[2];
    // eigenvector of (A - lI): largest cross product of two rows
    let r0 = [c[0] - l, c[1], c[2]];
    let r1 = [c[1], c[3] - l, c[4]];
    let r2 = [c[2], c[4], c[5] - l];
    let cross = |u: V3, v: V3| -> V3 {
        [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    };
    let c01 = cross(r0, r1);
    let c02 = cross(r0, r2);
    let c12 = cross(r1, r2);
    let (s01, s02, s12) = (dot(c01, c01), dot(c02, c02), dot(c12, c12));
    let n = if s01 >= s02 && s01 >= s12 {
        c01
    } else if s02 >= s12 {
        c02
    } else {
        c12
    };
    let n = normalize(n);
    // degenerate (near-isotropic) neighbourhood: fall back to +Y
    let n = if dot(n, n) < 0.5 { [0.0, 1.0, 0.0] } else { n };
    (n, ev)
}

/// Weighted covariance (packed symmetric) of `pts[rows]` around its weighted
/// mean. `ws` is caller scratch (per-thread, not per-point).
fn weighted_cov(
    pos: &[V3],
    rows: &[u32],
    w: &dyn Fn(usize) -> f32,
    dist2: &[f32],
    robust: bool,
    ws: &mut Vec<f32>,
) -> [f32; 6] {
    let mut m = [0.0f32; 3];
    let mut sw = 0.0f32;
    ws.clear();
    let mut dmax = 0.0f32;
    for &d in dist2 {
        if d.is_finite() && d > dmax {
            dmax = d;
        }
    }
    let h2 = if robust && dmax > 0.0 { dmax * 0.5 } else { f32::INFINITY };
    for (t, &j) in rows.iter().enumerate() {
        let wj = w(j as usize) * (-dist2[t] / h2).exp();
        ws.push(wj);
        sw += wj;
        let p = pos[j as usize];
        for a in 0..3 {
            m[a] += wj * p[a];
        }
    }
    if sw <= 0.0 {
        return [0.0; 6];
    }
    for a in 0..3 {
        m[a] /= sw;
    }
    let mut c = [0.0f32; 6];
    for (t, &j) in rows.iter().enumerate() {
        let p = pos[j as usize];
        let d = [p[0] - m[0], p[1] - m[1], p[2] - m[2]];
        let wj = ws[t];
        c[0] += wj * d[0] * d[0];
        c[1] += wj * d[0] * d[1];
        c[2] += wj * d[0] * d[2];
        c[3] += wj * d[1] * d[1];
        c[4] += wj * d[1] * d[2];
        c[5] += wj * d[2] * d[2];
    }
    for v in c.iter_mut() {
        *v /= sw;
    }
    c
}

/// Parity union-find: tracks a sign relation flag[x] = sign(x -> parent).
struct ParUf {
    parent: Vec<u32>,
    parity: Vec<u8>,
    rank: Vec<u8>,
}

impl ParUf {
    fn new(n: usize) -> Self {
        ParUf { parent: (0..n as u32).collect(), parity: vec![0; n], rank: vec![0; n] }
    }
    /// (root, parity of x relative to root). Path-halving keeps finds near O(1).
    fn find(&mut self, x: u32) -> (u32, u8) {
        let mut p = 0u8;
        let mut cur = x;
        while self.parent[cur as usize] != cur {
            let next = self.parent[cur as usize];
            let grand = self.parent[next as usize];
            // fold next->grand into cur->grand and accumulate that edge's parity
            self.parity[cur as usize] ^= self.parity[next as usize];
            self.parent[cur as usize] = grand;
            p ^= self.parity[cur as usize];
            cur = grand;
        }
        (cur, p)
    }
    /// Union with constraint parity(a) ^ parity(b) = rel. Returns false if same root.
    fn union(&mut self, a: u32, b: u32, rel: u8) -> bool {
        let (ra, pa) = self.find(a);
        let (rb, pb) = self.find(b);
        if ra == rb {
            return false;
        }
        // attach lower rank under higher
        let (ra, rb, pa, pb) = if self.rank[ra as usize] < self.rank[rb as usize] {
            (rb, ra, pb, pa)
        } else {
            (ra, rb, pa, pb)
        };
        self.parent[rb as usize] = ra;
        // parity(rb->ra) = pa ^ pb ^ rel
        self.parity[rb as usize] = pa ^ pb ^ rel;
        if self.rank[ra as usize] == self.rank[rb as usize] {
            self.rank[ra as usize] += 1;
        }
        true
    }
}

/// Estimate normals + spacing in place. Views unknown: graph propagation.
pub fn estimate(cloud: &mut PointCloud, opts: &NormalOptions) -> Stats {
    let views = opts.viewpoints.clone();
    estimate_inner(cloud, opts, if views.is_empty() { None } else { Some(&views) })
}

/// Same as `estimate` but with explicit scanner/camera positions: normals are
/// oriented toward the views (correct when capture is known).
pub fn estimate_with_views(cloud: &mut PointCloud, opts: &NormalOptions, views: &[V3]) -> Stats {
    estimate_inner(cloud, opts, if views.is_empty() { None } else { Some(views) })
}

fn estimate_inner(cloud: &mut PointCloud, opts: &NormalOptions, views: Option<&[V3]>) -> Stats {
    let n = cloud.len();
    if n == 0 {
        cloud.nrm.clear();
        return serde_json::json!({"points": 0});
    }
    // grid cell ~2x local spacing for fast kNN; prefer clean.rs's `radius`
    // estimate when present (it is the same quantity).
    let spacing = {
        let mut rs: Vec<f32> = cloud.radius.iter().copied().filter(|r| r.is_finite() && *r > 0.0).collect();
        if rs.len() >= 8 {
            rs.sort_by(|a, b| a.total_cmp(b));
            rs[rs.len() / 2]
        } else {
            Grid::build(&cloud.pos, 0.03).median_nn_spacing(2048)
        }
    }
    .max(1e-4);
    let cell = (spacing * 2.0).max(0.008);
    let grid = Grid::build(&cloud.pos, cell);

    let k = opts.k.max(4);
    let k_orient = if opts.k_orient > 0 { opts.k_orient } else { k.min(12) };
    let k_q = k.max(k_orient) + 1;
    let max_r = spacing * 10.0;
    let (idx, d2) = grid.knn_batch_d2(&cloud.pos, k_q, max_r);

    // --- PCA normals (unoriented) + mean neighbour distance for `radius` ---
    let pos = &cloud.pos;
    let weight_of = |j: usize| cloud.weight(j);
    let mut pca = vec![([0.0f32; 3], 0.0f32, 0.0f32); n];
    const CHUNK: usize = 4096;
    pca.par_chunks_mut(CHUNK).enumerate().for_each(|(ci, chunk)| {
        let mut rows = Vec::with_capacity(k);
        let mut ds = Vec::with_capacity(k);
        let mut ws = Vec::with_capacity(k);
        for (t, out) in chunk.iter_mut().enumerate() {
            let i = ci * CHUNK + t;
            let row_i = &idx[i * k_q..(i + 1) * k_q];
            let row_d = &d2[i * k_q..(i + 1) * k_q];
            rows.clear();
            ds.clear();
            let mut sd_sum = 0.0f32;
            let mut cnt = 0;
            for u in 0..k_q {
                let (j, d) = (row_i[u], row_d[u]);
                if j == u32::MAX || j as usize == i || !d.is_finite() {
                    continue;
                }
                if rows.len() < k {
                    rows.push(j);
                    ds.push(d);
                }
                sd_sum += d.sqrt();
                cnt += 1;
            }
            let spacing_i = if cnt > 0 { sd_sum / cnt as f32 } else { cell };
            if rows.len() < 3 {
                *out = ([0.0f32, 1.0, 0.0], 0.0, spacing_i);
                continue;
            }
            let cov = weighted_cov(pos, &rows, &weight_of, &ds, opts.robust, &mut ws);
            let (nrm, ev) = sym3_min_eigvec(cov);
            let tot = ev[0] + ev[1] + ev[2];
            let curv = if tot > 1e-12 { ev[2] / tot } else { 0.0 };
            *out = (nrm, curv, spacing_i);
        }
    });
    cloud.nrm = pca.iter().map(|p| p.0).collect();
    cloud.radius = pca.iter().map(|p| p.2).collect();
    let med_curv = {
        let mut c: Vec<f32> = pca.iter().map(|p| p.1).collect();
        c.sort_by(|a, b| a.total_cmp(b));
        c[n / 2]
    };

    // --- Orientation ---
    let mut flipped = 0usize;
    let mut components = 0usize;
    if let Some(vs) = views {
        // point each normal toward its nearest view
        for i in 0..n {
            let p = cloud.pos[i];
            let (mut bx, mut bd) = (0usize, f32::INFINITY);
            for (vi, v) in vs.iter().enumerate() {
                let d = crate::core::dist2(*v, p);
                if d < bd {
                    bd = d;
                    bx = vi;
                }
            }
            let dir = sub(vs[bx], p);
            if dot(cloud.nrm[i], dir) < 0.0 {
                cloud.nrm[i] = [-cloud.nrm[i][0], -cloud.nrm[i][1], -cloud.nrm[i][2]];
                flipped += 1;
            }
        }
    } else if opts.orient {
        // Riemannian-MST-equivalent sign propagation via parity union-find,
        // unions applied in decreasing |n_i . n_j| order.
        let mut edges: Vec<(f32, u32, u32)> = Vec::with_capacity(n * k_orient / 2);
        for i in 0..n {
            let row_i = &idx[i * k_q..(i + 1) * k_q];
            let mut taken = 0;
            for &j in row_i.iter() {
                if j == u32::MAX {
                    break;
                }
                let j = j as usize;
                if j == i {
                    continue;
                }
                if j > i {
                    let w = dot(cloud.nrm[i], cloud.nrm[j]).abs();
                    edges.push((w, i as u32, j as u32));
                    taken += 1;
                    if taken >= k_orient {
                        break;
                    }
                }
            }
        }
        edges.par_sort_unstable_by(|a, b| b.0.total_cmp(&a.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
        let mut uf = ParUf::new(n);
        for (w, a, b) in edges {
            if w < 0.05 {
                break; // nearly orthogonal: no useful sign constraint
            }
            let rel = if dot(cloud.nrm[a as usize], cloud.nrm[b as usize]) < 0.0 { 1u8 } else { 0u8 };
            uf.union(a, b, rel);
        }
        // resolve each point's root + parity, then per-component interior vote
        let mut root = vec![0u32; n];
        let mut par = vec![0u8; n];
        for i in 0..n {
            let (r, p) = uf.find(i as u32);
            root[i] = r;
            par[i] = p;
        }
        let mut votes: FxHashMap<u32, f64> = FxHashMap::default();
        let centre = {
            let b = cloud.bounds();
            crate::core::scale(crate::core::add(b.min, b.max), 0.5)
        };
        for i in 0..n {
            let dir = normalize(sub(centre, cloud.pos[i]));
            let s = if par[i] == 1 { -1.0 } else { 1.0 };
            let n_i = [cloud.nrm[i][0] * s, cloud.nrm[i][1] * s, cloud.nrm[i][2] * s];
            *votes.entry(root[i]).or_default() += dot(n_i, dir) as f64;
        }
        components = votes.len();
        let mut root_sign: FxHashMap<u32, u8> = FxHashMap::default();
        root_sign.reserve(votes.len());
        for (r, v) in votes {
            root_sign.insert(r, if v < 0.0 { 1 } else { 0 });
        }
        for i in 0..n {
            let flip = par[i] ^ root_sign.get(&root[i]).copied().unwrap_or(0);
            if flip == 1 {
                cloud.nrm[i] = [-cloud.nrm[i][0], -cloud.nrm[i][1], -cloud.nrm[i][2]];
                flipped += 1;
            }
        }
    }

    serde_json::json!({
        "points": n, "k": k, "spacing_m": spacing,
        "oriented": views.is_some() || opts.orient,
        "via_views": views.is_some(),
        "flipped": flipped, "components": components,
        "median_curvature": med_curv,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::synth::{simulate, ScanOptions, Scene};

    fn scan_cloud() -> (PointCloud, Scene, Vec<V3>) {
        let scene = Scene::room();
        let opts = ScanOptions { depth_w: 48, depth_h: 36, image_w: 64, image_h: 48, yaw_steps: 4, ..Default::default() };
        let scan = simulate(&scene, &opts);
        let views: Vec<V3> = scan.capture.cameras.iter().map(|c| c.position()).collect();
        (scan.cloud, scene, views)
    }

    #[test]
    fn eig_on_flat_patch() {
        // xy-plane points -> normal z
        let pos: Vec<V3> = (0..30).map(|i| [(i % 6) as f32, (i / 6) as f32, 0.0]).collect();
        let rows: Vec<u32> = (0..30).collect();
        let ds: Vec<f32> = rows.iter().map(|&j| crate::core::dist2(pos[j as usize], pos[0])).collect();
        let cov = weighted_cov(&pos, &rows, &|_| 1.0, &ds, false, &mut Vec::new());
        let (n, ev) = sym3_min_eigvec(cov);
        assert!(n[2].abs() > 0.99, "{n:?}");
        assert!(ev[2].abs() < 1e-5, "{ev:?}");
    }

    #[test]
    fn normals_oriented_toward_views() {
        let (mut cloud, scene, views) = scan_cloud();
        let opts = NormalOptions { k: 12, ..Default::default() };
        estimate_with_views(&mut cloud, &opts, &views);
        assert!(cloud.has_normals());
        let mut errs = Vec::new();
        let mut flipped = 0usize;
        for (i, p) in cloud.pos.iter().enumerate() {
            let gt = scene.normal(*p);
            let d = dot(cloud.nrm[i], gt);
            if d < 0.0 {
                flipped += 1;
            }
            errs.push(d.abs().clamp(-1.0, 1.0).acos());
        }
        errs.sort_by(|a, b| a.total_cmp(b));
        let med = errs[errs.len() / 2].to_degrees();
        let flip_pct = flipped as f32 / cloud.len() as f32 * 100.0;
        eprintln!("median angular err {med:.2} deg, flipped {flip_pct:.2}%");
        assert!(med < 15.0, "median {med}");
        assert!(flip_pct < 5.0, "flipped {flip_pct}%");
    }

    #[test]
    fn normals_oriented_no_views() {
        let (mut cloud, scene, _views) = scan_cloud();
        let opts = NormalOptions { k: 12, ..Default::default() };
        estimate(&mut cloud, &opts);
        let mut errs = Vec::new();
        let mut flipped = 0usize;
        for (i, p) in cloud.pos.iter().enumerate() {
            let gt = scene.normal(*p);
            let d = dot(cloud.nrm[i], gt);
            if d < 0.0 {
                flipped += 1;
            }
            errs.push(d.abs().clamp(-1.0, 1.0).acos());
        }
        errs.sort_by(|a, b| a.total_cmp(b));
        let med = errs[errs.len() / 2].to_degrees();
        let flip_pct = flipped as f32 / cloud.len() as f32 * 100.0;
        eprintln!("no-views median angular err {med:.2} deg, flipped {flip_pct:.2}%");
        assert!(med < 15.0, "median {med}");
        assert!(flip_pct < 12.0, "flipped {flip_pct}%");
    }
}
