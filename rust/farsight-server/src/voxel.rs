//! Bounded spatial accumulation; points are averaged only within the same voxel.
//! Port of jetson/mapping/voxel.py (OrderedDict -> indexmap for identical LRU order).

use indexmap::IndexMap;
use rustc_hash::FxBuildHasher;

type FxIndexMap<K, V> = IndexMap<K, V, FxBuildHasher>;

#[derive(Debug, Clone, Copy)]
pub struct Cell {
    pub pos: [f64; 3],
    pub color: [f64; 3],
    pub weight: u32,
}

pub struct VoxelMap {
    pub size: f64,
    pub capacity: usize,
    pub cells: FxIndexMap<(i64, i64, i64), Cell>,
}

impl VoxelMap {
    pub fn new(size: f64, capacity: usize) -> Self {
        assert!(size.is_finite() && size > 0.0 && capacity > 0, "Voxel size and capacity must be positive");
        Self { size, capacity, cells: FxIndexMap::with_hasher(FxBuildHasher) }
    }

    pub fn clear(&mut self) {
        self.cells.clear();
    }

    pub fn integrate(&mut self, points: &[[f32; 3]], colors: &[[u8; 3]]) {
        if points.is_empty() {
            return;
        }
        // Group samples by voxel key (np.unique(keys, axis=0) equivalent).
        let mut groups: FxIndexMap<(i64, i64, i64), ([f64; 3], [f64; 3], u32)> = FxIndexMap::with_hasher(FxBuildHasher);
        for (p, c) in points.iter().zip(colors.iter()) {
            if !p.iter().all(|v| v.is_finite()) {
                continue;
            }
            let key = (
                (p[0] as f64 / self.size).floor() as i64,
                (p[1] as f64 / self.size).floor() as i64,
                (p[2] as f64 / self.size).floor() as i64,
            );
            let entry = groups.entry(key).or_insert(([0.0; 3], [0.0; 3], 0));
            for a in 0..3 {
                entry.0[a] += p[a] as f64;
                entry.1[a] += c[a] as f64;
            }
            entry.2 += 1;
        }
        for (key, (xyz, rgb, count)) in groups {
            let n = count as f64;
            let pos = [xyz[0] / n, xyz[1] / n, xyz[2] / n];
            let color = [rgb[0] / n, rgb[1] / n, rgb[2] / n];
            match self.cells.get_mut(&key) {
                Some(cell) => {
                    // Bound observation weight so new evidence can still update a cell.
                    let alpha = 1.0 / (cell.weight.min(30) + 1) as f64;
                    for a in 0..3 {
                        cell.pos[a] = cell.pos[a] * (1.0 - alpha) + pos[a] * alpha;
                        cell.color[a] = cell.color[a] * (1.0 - alpha) + color[a] * alpha;
                    }
                    cell.weight = (cell.weight + 1).min(30);
                }
                None => {
                    self.cells.insert(key, Cell { pos, color, weight: 1 });
                }
            }
            // move_to_end(key): refresh LRU position for both paths.
            let idx = self.cells.get_index_of(&key).unwrap();
            let last = self.cells.len() - 1;
            self.cells.move_index(idx, last);
        }
        while self.cells.len() > self.capacity {
            self.cells.shift_remove_index(0);
        }
    }

    /// Snapshot averaged points and clamped uint8 colors.
    pub fn snapshot(&self) -> (Vec<[f32; 3]>, Vec<[u8; 3]>) {
        let mut points = Vec::with_capacity(self.cells.len());
        let mut colors = Vec::with_capacity(self.cells.len());
        for cell in self.cells.values() {
            points.push([cell.pos[0] as f32, cell.pos[1] as f32, cell.pos[2] as f32]);
            colors.push([
                cell.color[0].clamp(0.0, 255.0) as u8,
                cell.color[1].clamp(0.0, 255.0) as u8,
                cell.color[2].clamp(0.0, 255.0) as u8,
            ]);
        }
        (points, colors)
    }
}

impl Default for VoxelMap {
    fn default() -> Self {
        Self::new(0.04, 120_000)
    }
}
