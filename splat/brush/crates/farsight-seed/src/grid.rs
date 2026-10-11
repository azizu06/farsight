//! Uniform hash-grid spatial index. Ported from farsight-compiler
//! (branch `compiler`, rust/farsight-compiler/src/spatial.rs) — same
//! cell-key packing and expanding-box kNN.

use glam::Vec3;
use rayon::prelude::*;
use rustc_hash::FxHashMap;

const BIAS: i64 = 1 << 20;

#[inline]
fn cell_of(p: Vec3, inv: f32) -> (i32, i32, i32) {
    (
        (p.x * inv).floor() as i32,
        (p.y * inv).floor() as i32,
        (p.z * inv).floor() as i32,
    )
}

#[inline]
fn ukey(x: i32, y: i32, z: i32) -> u64 {
    ((x as i64 + BIAS) as u64) << 42 | ((y as i64 + BIAS) as u64) << 21 | (z as i64 + BIAS) as u64
}

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
    pts: Vec<Vec3>,
}

impl Grid {
    pub fn build(pts: &[Vec3], cell: f32) -> Self {
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
        Grid {
            cell,
            inv,
            cells,
            order,
            pts: pts.to_vec(),
        }
    }

    pub fn cell_size(&self) -> f32 {
        self.cell
    }
    pub fn len(&self) -> usize {
        self.pts.len()
    }
    pub fn points(&self) -> &[Vec3] {
        &self.pts
    }

    #[inline]
    fn cell_lb2(&self, q: Vec3, x: i32, y: i32, z: i32) -> f32 {
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

    #[inline]
    fn collect_cells(&self, q: Vec3, r: f32, cands: &mut Vec<Cand>) {
        let lo = cell_of(q - Vec3::splat(r), self.inv);
        let hi = cell_of(q + Vec3::splat(r), self.inv);
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

    #[inline]
    fn insert_sorted(out: &mut Vec<(u32, f32)>, k: usize, idx: u32, d2: f32) {
        if out.len() >= k && (d2 > out[k - 1].1 || (d2 == out[k - 1].1 && idx >= out[k - 1].0)) {
            return;
        }
        let pos = out.partition_point(|e| e.1 < d2 || (e.1 == d2 && e.0 < idx));
        out.insert(pos, (idx, d2));
        out.truncate(k);
    }

    fn knn_into(&self, q: Vec3, k: usize, max_r: f32, cands: &mut Vec<Cand>, out: &mut Vec<(u32, f32)>) {
        out.clear();
        if k == 0 || self.pts.is_empty() {
            return;
        }
        let max_r2 = max_r * max_r;
        let mut r = (self.cell * 1.51).min(max_r);
        loop {
            cands.clear();
            self.collect_cells(q, r, cands);
            cands.sort_unstable_by(|a, b| a.lb2.total_cmp(&b.lb2));
            out.clear();
            let mut covered = true;
            for c in cands.iter() {
                if out.len() >= k && c.lb2 > out[k - 1].1 {
                    covered = false;
                    break;
                }
                let end = (c.start + c.len) as usize;
                for &i in &self.order[c.start as usize..end] {
                    let d2 = self.pts[i as usize].distance_squared(q);
                    if d2 <= max_r2 {
                        Self::insert_sorted(out, k, i, d2);
                    }
                }
            }
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

    pub fn knn(&self, q: Vec3, k: usize, max_r: f32, out: &mut Vec<(u32, f32)>) {
        let mut cands = Vec::with_capacity(64);
        self.knn_into(q, k, max_r, &mut cands, out);
    }

    /// Batched kNN: flat `queries.len() * k` (u32::MAX / f32::INFINITY padded).
    pub fn knn_batch_d2(&self, queries: &[Vec3], k: usize, max_r: f32) -> (Vec<u32>, Vec<f32>) {
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

    /// Median nearest-neighbour distance over a strided sample.
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
                out.iter()
                    .find(|(j, _)| *j != i as u32)
                    .map(|(_, d)| d.sqrt())
                    .unwrap_or(self.cell)
            })
            .collect();
        ds.sort_by(f32::total_cmp);
        ds[ds.len() / 2]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;

    #[test]
    fn knn_matches_brute_force() {
        let mut rng = Rng::new(1);
        let pts: Vec<Vec3> = (0..2000)
            .map(|_| Vec3::new(rng.f32(), rng.f32(), rng.f32()))
            .collect();
        let g = Grid::build(&pts, 0.05);
        let mut out = Vec::new();
        for t in 0..50 {
            let q = pts[t * 7];
            g.knn(q, 8, 1.0, &mut out);
            let mut brute: Vec<(u32, f32)> = pts
                .iter()
                .enumerate()
                .map(|(i, p)| (i as u32, p.distance_squared(q)))
                .collect();
            brute.sort_by(|a, b| a.1.total_cmp(&b.1));
            for j in 0..8 {
                assert!((out[j].1 - brute[j].1).abs() < 1e-9);
            }
        }
    }
}
