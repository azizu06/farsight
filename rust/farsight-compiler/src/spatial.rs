//! Spatial index over points. OWNER: W2 (spatial/clean/normals).
//!
//! Uniform hash grid on packed u64 cell keys. The public API (`build`,
//! `radius`, `nearest`, `knn`) is the stable contract other stages use; this
//! implementation adds allocation-free batch variants (`knn_batch*`,
//! `nearest_batch`, `radius_batch`) that drive the per-point work of every
//! downstream stage.
//!
//! Query strategy: enumerate occupied cells in the query box, sort them by
//! their squared lower-bound distance to the query, then scan points in that
//! order and stop as soon as the current k-th worst distance can no longer be
//! beaten. For dense self-queries (kNN of the cloud against itself) the first
//! ~3x3x3..5x5x5 box usually suffices; the box doubles until `max_r`.

use crate::core::{dist2, V3};
use rayon::prelude::*;
use rustc_hash::FxHashMap;

/// Cell coordinates are biased into 21-bit unsigned fields: ±1M cells per axis.
/// BIAS must be half the field modulus (2^20) so biased coords stay < 2^21.
const BIAS: i64 = 1 << 20;

#[inline]
fn cell_of(p: V3, inv: f32) -> (i32, i32, i32) {
    (
        (p[0] * inv).floor() as i32,
        (p[1] * inv).floor() as i32,
        (p[2] * inv).floor() as i32,
    )
}

#[inline]
fn ukey(x: i32, y: i32, z: i32) -> u64 {
    ((x as i64 + BIAS) as u64) << 42 | ((y as i64 + BIAS) as u64) << 21 | (z as i64 + BIAS) as u64
}

/// One occupied cell inside the current query box, with the squared minimum
/// distance from the query point to the cell's AABB.
#[derive(Clone, Copy)]
struct Cand {
    lb2: f32,
    start: u32,
    len: u32,
}

pub struct Grid {
    cell: f32,
    inv: f32,
    cells: FxHashMap<u64, (u32, u32)>,
    order: Vec<u32>,
    pts: Vec<V3>,
}

impl Grid {
    /// Build over `pts` with the given cell size (metres). Indices returned by
    /// queries are indices into `pts`.
    pub fn build(pts: &[V3], cell: f32) -> Self {
        let cell = cell.max(1e-4);
        let inv = 1.0 / cell;
        let mut keyed: Vec<(u64, u32)> = pts
            .par_iter()
            .enumerate()
            .map(|(i, p)| {
                let (x, y, z) = cell_of(*p, inv);
                (ukey(x, y, z), i as u32)
            })
            .collect();
        keyed.par_sort_unstable();
        let mut cells = FxHashMap::default();
        cells.reserve(keyed.len().min(1 << 22));
        let mut order = Vec::with_capacity(pts.len());
        let mut i = 0;
        while i < keyed.len() {
            let k = keyed[i].0;
            let start = i;
            while i < keyed.len() && keyed[i].0 == k {
                order.push(keyed[i].1);
                i += 1;
            }
            cells.insert(k, (start as u32, (i - start) as u32));
        }
        Grid { cell, inv, cells, order, pts: pts.to_vec() }
    }

    pub fn cell_size(&self) -> f32 {
        self.cell
    }
    pub fn points(&self) -> &[V3] {
        &self.pts
    }
    pub fn len(&self) -> usize {
        self.pts.len()
    }
    pub fn is_empty(&self) -> bool {
        self.pts.is_empty()
    }

    /// Squared distance from `q` to the AABB of cell (x,y,z). 0 when inside.
    #[inline]
    fn cell_lb2(&self, q: V3, x: i32, y: i32, z: i32) -> f32 {
        let c = [x, y, z];
        let mut s = 0.0f32;
        for a in 0..3 {
            let lo = c[a] as f32 * self.cell;
            let hi = lo + self.cell;
            let d = (lo - q[a]).max(0.0).max(q[a] - hi);
            s += d * d;
        }
        s
    }

    /// Push every occupied cell intersecting `[q-r, q+r]` into `cands`
    /// (unsorted; caller sorts).
    #[inline]
    fn collect_cells(&self, q: V3, r: f32, cands: &mut Vec<Cand>) {
        let lo = cell_of([q[0] - r, q[1] - r, q[2] - r], self.inv);
        let hi = cell_of([q[0] + r, q[1] + r, q[2] + r], self.inv);
        for x in lo.0..=hi.0 {
            for y in lo.1..=hi.1 {
                for z in lo.2..=hi.2 {
                    if let Some(&(s, n)) = self.cells.get(&ukey(x, y, z)) {
                        cands.push(Cand {
                            lb2: self.cell_lb2(q, x, y, z),
                            start: s,
                            len: n,
                        });
                    }
                }
            }
        }
    }

    /// Insert `(idx, d2)` into the sorted (by d2 then idx) bounded list `out`,
    /// keeping at most `k` entries. `out` must already be sorted.
    #[inline]
    fn insert_sorted(out: &mut Vec<(u32, f32)>, k: usize, idx: u32, d2: f32) {
        if out.len() >= k && (d2 > out[k - 1].1 || (d2 == out[k - 1].1 && idx >= out[k - 1].0)) {
            return;
        }
        let pos = out.partition_point(|e| e.1 < d2 || (e.1 == d2 && e.0 < idx));
        out.insert(pos, (idx, d2));
        out.truncate(k);
    }

    /// Core kNN: `out` receives up to `k` (index, d2) pairs sorted ascending.
    /// `cands` is scratch reused by batch callers (per-thread, not per-point).
    fn knn_into(&self, q: V3, k: usize, max_r: f32, cands: &mut Vec<Cand>, out: &mut Vec<(u32, f32)>) {
        out.clear();
        if k == 0 || self.pts.is_empty() {
            return;
        }
        let max_r2 = max_r * max_r;
        // Start with a small box; expand until k neighbours are provably found.
        let mut r = (self.cell * 1.51).min(max_r);
        loop {
            cands.clear();
            self.collect_cells(q, r, cands);
            cands.sort_unstable_by(|a, b| a.lb2.total_cmp(&b.lb2));
            out.clear();
            let mut covered = true;
            for c in cands.iter() {
                // Once the next cell can't beat the k-th neighbour, stop.
                if out.len() >= k && c.lb2 > out[k - 1].1 {
                    covered = false; // still unfinished cells, but provably beaten
                    break;
                }
                let end = (c.start + c.len) as usize;
                for &i in &self.order[c.start as usize..end] {
                    let d2 = dist2(self.pts[i as usize], q);
                    if d2 <= max_r2 {
                        Self::insert_sorted(out, k, i, d2);
                    }
                }
            }
            // Done when: found k and no unseen point can be closer than worst.
            // Unseen (outside box) points are >= r - cell away.
            if out.len() >= k {
                if !covered {
                    return;
                }
                let edge = r - self.cell;
                if edge <= 0.0 || edge * edge >= out[k - 1].1 || r >= max_r {
                    return;
                }
            }
            if r >= max_r {
                return;
            }
            r = (r * 2.0).min(max_r);
        }
    }

    /// All indices within distance `r` of `q` (unordered), appended to `out` after clearing.
    pub fn radius(&self, q: V3, r: f32, out: &mut Vec<u32>) {
        out.clear();
        let r2 = r * r;
        let lo = cell_of([q[0] - r, q[1] - r, q[2] - r], self.inv);
        let hi = cell_of([q[0] + r, q[1] + r, q[2] + r], self.inv);
        for x in lo.0..=hi.0 {
            for y in lo.1..=hi.1 {
                for z in lo.2..=hi.2 {
                    if let Some(&(s, n)) = self.cells.get(&ukey(x, y, z)) {
                        for &i in &self.order[s as usize..(s + n) as usize] {
                            if dist2(self.pts[i as usize], q) <= r2 {
                                out.push(i);
                            }
                        }
                    }
                }
            }
        }
    }

    /// Nearest point within `max_r`: (index, squared distance).
    pub fn nearest(&self, q: V3, max_r: f32) -> Option<(u32, f32)> {
        let mut cands = Vec::with_capacity(64);
        let mut out = Vec::with_capacity(1);
        self.knn_into(q, 1, max_r, &mut cands, &mut out);
        out.first().copied()
    }

    /// k nearest neighbours within `max_r`, sorted ascending by squared distance.
    pub fn knn(&self, q: V3, k: usize, max_r: f32, out: &mut Vec<(u32, f32)>) {
        let mut cands = Vec::with_capacity(64);
        self.knn_into(q, k, max_r, &mut cands, out);
    }

    /// Batched kNN: returns `(idx, d2)` both flat `queries.len() * k`
    /// (u32::MAX / f32::INFINITY padding where fewer than k exist).
    /// Parallel, one scratch buffer per chunk — no per-point allocation.
    pub fn knn_batch_d2(&self, queries: &[V3], k: usize, max_r: f32) -> (Vec<u32>, Vec<f32>) {
        let n = queries.len();
        let mut idx = vec![u32::MAX; n * k];
        let mut d2 = vec![f32::INFINITY; n * k];
        const CHUNK: usize = 4096;
        idx.par_chunks_mut(CHUNK * k)
            .zip(d2.par_chunks_mut(CHUNK * k))
            .zip(queries.par_chunks(CHUNK))
            .for_each(|((ic, dc), qs)| {
                let mut cands = Vec::with_capacity(128);
                let mut out = Vec::with_capacity(k);
                for (j, q) in qs.iter().enumerate() {
                    self.knn_into(*q, k, max_r, &mut cands, &mut out);
                    for (t, &(i, d)) in out.iter().enumerate() {
                        ic[j * k + t] = i;
                        dc[j * k + t] = d;
                    }
                }
            });
        (idx, d2)
    }

    /// Batched kNN, indices only (u32::MAX padded).
    pub fn knn_batch(&self, queries: &[V3], k: usize, max_r: f32) -> Vec<u32> {
        self.knn_batch_d2(queries, k, max_r).0
    }

    /// Batched nearest neighbour within `max_r`:
    /// (index, squared distance) or (u32::MAX, f32::INFINITY).
    pub fn nearest_batch(&self, queries: &[V3], max_r: f32) -> Vec<(u32, f32)> {
        let mut out = vec![(u32::MAX, f32::INFINITY); queries.len()];
        out.par_chunks_mut(8192)
            .zip(queries.par_chunks(8192))
            .for_each(|(oc, qs)| {
                let mut cands = Vec::with_capacity(128);
                let mut tmp = Vec::with_capacity(1);
                for (o, q) in oc.iter_mut().zip(qs) {
                    self.knn_into(*q, 1, max_r, &mut cands, &mut tmp);
                    if let Some(&v) = tmp.first() {
                        *o = v;
                    }
                }
            });
        out
    }

    /// Batched radius query: returns (flat indices, offsets of len n+1).
    pub fn radius_batch(&self, queries: &[V3], r: f32) -> (Vec<u32>, Vec<u64>) {
        const CHUNK: usize = 8192;
        let parts: Vec<(Vec<u32>, Vec<u64>)> = queries
            .par_chunks(CHUNK)
            .map(|qs| {
                let mut flat = Vec::new();
                let mut lens = Vec::with_capacity(qs.len());
                let mut out = Vec::new();
                for q in qs {
                    self.radius(*q, r, &mut out);
                    flat.extend_from_slice(&out);
                    lens.push(out.len() as u64);
                }
                (flat, lens)
            })
            .collect();
        let total: usize = parts.iter().map(|p| p.0.len()).sum();
        let mut idx = Vec::with_capacity(total);
        let mut offsets = Vec::with_capacity(queries.len() + 1);
        offsets.push(0u64);
        for (flat, lens) in parts {
            idx.extend_from_slice(&flat);
            for l in lens {
                offsets.push(*offsets.last().unwrap() + l);
            }
        }
        (idx, offsets)
    }

    /// Median nearest-neighbour distance over a strided sample of the cloud —
    /// a cheap estimate of local sample spacing.
    pub fn median_nn_spacing(&self, samples: usize) -> f32 {
        let n = self.pts.len();
        if n < 2 {
            return self.cell;
        }
        let step = (n / samples.max(1)).max(1);
        let mut ds: Vec<f32> = (0..n)
            .step_by(step)
            .take(samples)
            .collect::<Vec<usize>>()
            .par_iter()
            .map(|&i| {
                let mut cands = Vec::with_capacity(64);
                let mut out = Vec::with_capacity(2);
                self.knn_into(self.pts[i], 2, self.cell * 4.0, &mut cands, &mut out);
                out.iter().find(|(j, _)| *j != i as u32).map(|(_, d)| d.sqrt()).unwrap_or(self.cell)
            })
            .collect();
        ds.sort_by(|a, b| a.total_cmp(b));
        ds[ds.len() / 2]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;

    fn cloud(n: usize, seed: u64) -> Vec<V3> {
        let mut rng = Rng::new(seed);
        (0..n).map(|_| [rng.f32() * 4.0, rng.f32() * 2.0, rng.f32() * 4.0]).collect()
    }

    #[test]
    fn knn_matches_brute_force() {
        let pts: Vec<V3> = {
            let mut rng = Rng::new(1);
            (0..2000).map(|_| [rng.f32(), rng.f32(), rng.f32()]).collect()
        };
        let g = Grid::build(&pts, 0.05);
        let mut out = Vec::new();
        for t in 0..50 {
            let q = pts[t * 7];
            g.knn(q, 8, 1.0, &mut out);
            let mut brute: Vec<(u32, f32)> =
                pts.iter().enumerate().map(|(i, p)| (i as u32, dist2(*p, q))).collect();
            brute.sort_by(|a, b| a.1.total_cmp(&b.1));
            for j in 0..8 {
                assert!((out[j].1 - brute[j].1).abs() < 1e-9);
            }
            let (_, d) = g.nearest([q[0] + 0.01, q[1], q[2]], 1.0).unwrap();
            assert!(d <= 1.01e-4, "{d}");
        }
    }

    #[test]
    fn batch_matches_single() {
        let pts = cloud(5000, 3);
        let g = Grid::build(&pts, 0.05);
        let queries: Vec<V3> = (0..200).map(|i| pts[i * 17]).collect();
        let (idx, d2) = g.knn_batch_d2(&queries, 12, 0.5);
        let mut single = Vec::new();
        for (qi, q) in queries.iter().enumerate() {
            g.knn(*q, 12, 0.5, &mut single);
            for t in 0..12 {
                if t < single.len() {
                    assert_eq!(idx[qi * 12 + t], single[t].0);
                    assert_eq!(d2[qi * 12 + t], single[t].1);
                } else {
                    assert_eq!(idx[qi * 12 + t], u32::MAX);
                }
            }
        }
    }

    #[test]
    fn radius_and_nearest_batch() {
        let pts = cloud(4000, 5);
        let g = Grid::build(&pts, 0.08);
        let queries: Vec<V3> = (0..100).map(|i| pts[i * 31]).collect();
        let nn = g.nearest_batch(&queries, 1.0);
        let (flat, offs) = g.radius_batch(&queries, 0.2);
        assert_eq!(offs.len(), queries.len() + 1);
        assert_eq!(flat.len(), *offs.last().unwrap() as usize);
        let mut out = Vec::new();
        for (qi, q) in queries.iter().enumerate() {
            let (i0, d0) = g.nearest(*q, 1.0).unwrap_or((u32::MAX, f32::INFINITY));
            assert_eq!(nn[qi], (i0, d0));
            g.radius(*q, 0.2, &mut out);
            assert_eq!(out.len() as u64, offs[qi + 1] - offs[qi]);
        }
    }
}
