//! Mac-side assembly of incremental measured mesh chunks (phone ARKit mesh
//! anchors). Combines keyed chunks into the same `mesh.bin` wire format
//! `farsight-server` serves, so Quest decodes assembled geometry with zero
//! client changes. Chunk ingest framing is the contract under discussion on
//! issue #24 — this module is the contract-independent combine + encode core.

use indexmap::IndexMap;
use rustc_hash::FxBuildHasher;

/// One measured mesh chunk keyed by its producer anchor id. Same layout as
/// farsight-server's `phone::Mesh`.
pub struct ChunkMesh {
    pub positions: Vec<[f32; 3]>,
    pub colors: Vec<[u8; 3]>,
    /// One entry per triangle: 3 vertex indices into this chunk's positions.
    pub indices: Vec<[u32; 3]>,
}

/// Bounded map of the newest chunk per anchor, evicting the least recently
/// written anchor once the triangle budget is exceeded.
pub struct AnchorGeometry {
    budget: usize,
    chunks: IndexMap<u64, ChunkMesh, FxBuildHasher>,
    count: usize,
    revision: u64,
}

impl AnchorGeometry {
    pub fn new(triangle_budget: usize) -> Self {
        Self {
            budget: triangle_budget,
            chunks: IndexMap::with_hasher(FxBuildHasher::default()),
            count: 0,
            revision: 0,
        }
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn triangles(&self) -> usize {
        self.count
    }

    pub fn anchors(&self) -> usize {
        self.chunks.len()
    }

    /// Upsert the newest chunk for an anchor (the producer replaces its own
    /// geometry as ARKit refines it). A single chunk larger than the budget
    /// is rejected whole, matching SurfaceMap's over-budget keyframe rule.
    pub fn upsert(&mut self, anchor_id: u64, mesh: ChunkMesh) -> bool {
        if mesh.indices.is_empty() || mesh.indices.len() > self.budget {
            return false;
        }
        if let Some(old) = self.chunks.shift_remove(&anchor_id) {
            self.count -= old.indices.len();
        }
        self.count += mesh.indices.len();
        self.chunks.insert(anchor_id, mesh);
        while self.count > self.budget {
            if let Some((_, evicted)) = self.chunks.shift_remove_index(0) {
                self.count -= evicted.indices.len();
            } else {
                break;
            }
        }
        self.revision += 1;
        true
    }

    /// Remove an anchor's chunk (producer reports the anchor deleted).
    pub fn remove(&mut self, anchor_id: u64) -> bool {
        if let Some(old) = self.chunks.shift_remove(&anchor_id) {
            self.count -= old.indices.len();
            self.revision += 1;
            true
        } else {
            false
        }
    }

    /// Session reset: drop every anchor and bump the revision so consumers
    /// re-fetch a clean snapshot.
    pub fn clear(&mut self) {
        self.chunks.clear();
        self.count = 0;
        self.revision += 1;
    }

    /// Exact `mesh.bin` wire format served by farsight-server: u32 vertex
    /// count, u32 scalar index count, then 16-byte vertex records
    /// (xyz f32 + rgb + pad), then u32 indices with per-chunk offsets applied.
    /// Port of surfaces.SurfaceMap.encode over keyed chunks.
    pub fn encode(&self) -> Vec<u8> {
        let index_scalars = self.count * 3;
        let mut vertex_count: u32 = 0;
        for mesh in self.chunks.values() {
            vertex_count += mesh.positions.len() as u32;
        }
        let mut out = Vec::with_capacity(8 + vertex_count as usize * 16 + index_scalars * 4);
        out.extend_from_slice(&vertex_count.to_le_bytes());
        out.extend_from_slice(&(index_scalars as u32).to_le_bytes());
        for mesh in self.chunks.values() {
            for (p, c) in mesh.positions.iter().zip(mesh.colors.iter()) {
                for v in p {
                    out.extend_from_slice(&v.to_le_bytes());
                }
                out.extend_from_slice(c);
                out.push(0);
            }
        }
        let mut offset = 0u32;
        for mesh in self.chunks.values() {
            for tri in &mesh.indices {
                for &i in tri {
                    out.extend_from_slice(&(i + offset).to_le_bytes());
                }
            }
            offset += mesh.positions.len() as u32;
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tri_mesh(offset: f32, tris: usize) -> ChunkMesh {
        // `tris` independent triangles: 3 vertices each.
        let mut positions = Vec::new();
        let mut colors = Vec::new();
        let mut indices = Vec::new();
        for t in 0..tris {
            let base = positions.len() as u32;
            for v in 0..3 {
                positions.push([offset + t as f32, v as f32, 0.0]);
                colors.push([10, 20, 30]);
            }
            indices.push([base, base + 1, base + 2]);
        }
        ChunkMesh { positions, colors, indices }
    }

    #[test]
    fn encode_matches_mesh_bin_wire_format() {
        let mut g = AnchorGeometry::new(100);
        g.upsert(7, tri_mesh(1.0, 2));
        g.upsert(3, tri_mesh(5.0, 1));
        let buf = g.encode();
        let vcount = u32::from_le_bytes(buf[0..4].try_into().unwrap());
        let icount = u32::from_le_bytes(buf[4..8].try_into().unwrap());
        assert_eq!((vcount, icount), (9, 9));
        let verts = &buf[8..8 + 9 * 16];
        assert_eq!(&verts[..4], &1.0f32.to_le_bytes()); // anchor 7 first
        assert_eq!(&verts[12..15], &[10u8, 20, 30]);
        let idx = &buf[8 + 9 * 16..];
        assert_eq!(idx.len(), 9 * 4);
        // Second chunk's first index (scalar position 6) carries the +6 vertex offset.
        let second = u32::from_le_bytes(idx[6 * 4..7 * 4].try_into().unwrap());
        assert_eq!(second, 6);
    }

    #[test]
    fn upsert_replace_remove_and_budget_eviction() {
        let mut g = AnchorGeometry::new(3);
        assert!(g.upsert(1, tri_mesh(0.0, 2)));
        assert!(g.upsert(2, tri_mesh(0.0, 1)));
        assert_eq!(g.triangles(), 3);
        // Re-upsert replaces accounting and refreshes eviction order.
        assert!(g.upsert(1, tri_mesh(0.0, 1)));
        assert_eq!(g.triangles(), 2);
        // Next upsert overflows the budget; anchor 2 is now oldest.
        assert!(g.upsert(3, tri_mesh(0.0, 2)));
        assert_eq!(g.triangles(), 3);
        assert!(!g.chunks.contains_key(&2));
        assert!(g.remove(1));
        assert!(!g.remove(99));
        assert_eq!(g.triangles(), 2);
        // Over-budget single chunk is rejected.
        assert!(!g.upsert(9, tri_mesh(0.0, 5)));
    }

    #[test]
    fn revision_tracks_mutation() {
        let mut g = AnchorGeometry::new(10);
        let r0 = g.revision();
        g.upsert(1, tri_mesh(0.0, 1));
        g.upsert(1, tri_mesh(0.0, 2));
        g.remove(1);
        assert_eq!(g.revision(), r0 + 3);
        let r = g.revision();
        g.clear();
        assert_eq!(g.revision(), r + 1);
        assert_eq!(g.triangles(), 0);
    }
}

#[cfg(test)]
mod bench {
    use super::*;

    #[test]
    fn combine_cost_and_wire_budget_100_anchors() {
        // ~Realistic assembled scene: 100 anchors x 100 tris = 10k tris/30k verts.
        let mut g = AnchorGeometry::new(200_000);
        for a in 0..100u64 {
            g.upsert(a, {
                let mut positions = Vec::new();
                let mut colors = Vec::new();
                let mut indices = Vec::new();
                for t in 0..100u32 {
                    let base = positions.len() as u32;
                    for v in 0..3u32 {
                        positions.push([a as f32 + t as f32 * 0.01, v as f32, (a % 7) as f32]);
                        colors.push([a as u8, 128, 200]);
                    }
                    indices.push([base, base + 1, base + 2]);
                }
                ChunkMesh { positions, colors, indices }
            });
        }
        let t0 = std::time::Instant::now();
        let buf = g.encode();
        let encode_ms = t0.elapsed().as_secs_f64() * 1000.0;
        eprintln!("anchors=100 tris=10000 verts=30000 wire={}B encode={encode_ms:.2}ms", buf.len());
        // Delta-style: re-upsert one anchor only bumps revision; encode stays O(map).
        let t1 = std::time::Instant::now();
        g.upsert(42, ChunkMesh { positions: vec![[0.0; 3]; 3], colors: vec![[0; 3]; 3], indices: vec![[0, 1, 2]] });
        eprintln!("single-anchor upsert={:.3}ms -> revision {}", t1.elapsed().as_secs_f64() * 1000.0, g.revision());
        assert!(buf.len() > 500_000);
    }
}
