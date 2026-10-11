//! Spatial index over points. OWNER: W2 (spatial/clean/normals).
//! Baseline: sorted uniform hash grid. The public API below is the stable
//! contract other stages use; owners may add methods and optimise internals.

use crate::core::{dist2, V3};
use rustc_hash::FxHashMap;

pub struct Grid {
    cell: f32,
    inv: f32,
    cells: FxHashMap<(i32, i32, i32), (u32, u32)>,
    order: Vec<u32>,
    pts: Vec<V3>,
}

#[inline]
fn key(p: V3, inv: f32) -> (i32, i32, i32) {
    ((p[0] * inv).floor() as i32, (p[1] * inv).floor() as i32, (p[2] * inv).floor() as i32)
}

impl Grid {
    /// Build over `pts` with the given cell size (metres). Indices returned by
    /// queries are indices into `pts`.
    pub fn build(pts: &[V3], cell: f32) -> Self {
        let cell = cell.max(1e-4);
        let inv = 1.0 / cell;
        let mut keyed: Vec<((i32, i32, i32), u32)> = pts.iter().enumerate().map(|(i, p)| (key(*p, inv), i as u32)).collect();
        keyed.sort_unstable();
        let mut cells = FxHashMap::default();
        let mut order = Vec::with_capacity(pts.len());
        let mut i = 0;
        while i < keyed.len() {
            let k = keyed[i].0;
            let start = i;
            while i < keyed.len() && keyed[i].0 == k { order.push(keyed[i].1); i += 1; }
            cells.insert(k, (start as u32, (i - start) as u32));
        }
        Grid { cell, inv, cells, order, pts: pts.to_vec() }
    }

    pub fn cell_size(&self) -> f32 { self.cell }
    pub fn points(&self) -> &[V3] { &self.pts }
    pub fn len(&self) -> usize { self.pts.len() }
    pub fn is_empty(&self) -> bool { self.pts.is_empty() }

    #[inline]
    fn cell_slice(&self, k: (i32, i32, i32)) -> &[u32] {
        match self.cells.get(&k) { Some(&(s, n)) => &self.order[s as usize..(s + n) as usize], None => &[] }
    }

    /// All indices within distance `r` of `q` (unordered), appended to `out` after clearing.
    pub fn radius(&self, q: V3, r: f32, out: &mut Vec<u32>) {
        out.clear();
        let r2 = r * r;
        let lo = key([q[0] - r, q[1] - r, q[2] - r], self.inv);
        let hi = key([q[0] + r, q[1] + r, q[2] + r], self.inv);
        for x in lo.0..=hi.0 { for y in lo.1..=hi.1 { for z in lo.2..=hi.2 {
            for &i in self.cell_slice((x, y, z)) { if dist2(self.pts[i as usize], q) <= r2 { out.push(i); } }
        }}}
    }

    /// Nearest point within `max_r`: (index, squared distance).
    pub fn nearest(&self, q: V3, max_r: f32) -> Option<(u32, f32)> {
        let mut best: Option<(u32, f32)> = None;
        let c = key(q, self.inv);
        let rings = (max_r * self.inv).ceil() as i32 + 1;
        for ring in 0..=rings {
            // any point in ring `ring` is at least (ring-1)*cell away
            if let Some((_, d2)) = best { let lb = (ring - 1).max(0) as f32 * self.cell; if lb * lb > d2 { break; } }
            for x in -ring..=ring { for y in -ring..=ring { for z in -ring..=ring {
                if x.abs().max(y.abs()).max(z.abs()) != ring { continue; }
                for &i in self.cell_slice((c.0 + x, c.1 + y, c.2 + z)) {
                    let d2 = dist2(self.pts[i as usize], q);
                    if d2 <= max_r * max_r && best.map_or(true, |b| d2 < b.1) { best = Some((i, d2)); }
                }
            }}}
        }
        best
    }

    /// k nearest neighbours within `max_r`, sorted ascending by squared distance.
    pub fn knn(&self, q: V3, k: usize, max_r: f32, out: &mut Vec<(u32, f32)>) {
        out.clear();
        if k == 0 { return; }
        let c = key(q, self.inv);
        let rings = (max_r * self.inv).ceil() as i32 + 1;
        let max_r2 = max_r * max_r;
        for ring in 0..=rings {
            if out.len() >= k { let lb = (ring - 1).max(0) as f32 * self.cell; if lb * lb > out[k - 1].1 { break; } }
            for x in -ring..=ring { for y in -ring..=ring { for z in -ring..=ring {
                if x.abs().max(y.abs()).max(z.abs()) != ring { continue; }
                for &i in self.cell_slice((c.0 + x, c.1 + y, c.2 + z)) {
                    let d2 = dist2(self.pts[i as usize], q);
                    if d2 > max_r2 { continue; }
                    if out.len() < k { out.push((i, d2)); out.sort_unstable_by(|a, b| a.1.total_cmp(&b.1)); }
                    else if d2 < out[k - 1].1 { out[k - 1] = (i, d2); out.sort_unstable_by(|a, b| a.1.total_cmp(&b.1)); }
                }
            }}}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;
    #[test]
    fn knn_matches_brute_force() {
        let mut rng = Rng::new(1);
        let pts: Vec<V3> = (0..2000).map(|_| [rng.f32(), rng.f32(), rng.f32()]).collect();
        let g = Grid::build(&pts, 0.05);
        let mut out = Vec::new();
        for t in 0..50 {
            let q = pts[t * 7];
            g.knn(q, 8, 1.0, &mut out);
            let mut brute: Vec<(u32, f32)> = pts.iter().enumerate().map(|(i, p)| (i as u32, dist2(*p, q))).collect();
            brute.sort_by(|a, b| a.1.total_cmp(&b.1));
            for j in 0..8 { assert!((out[j].1 - brute[j].1).abs() < 1e-9); }
            let (_, d) = g.nearest([q[0] + 0.01, q[1], q[2]], 1.0).unwrap();
            assert!(d <= 1.01e-4, "{d}");
        }
    }
}
