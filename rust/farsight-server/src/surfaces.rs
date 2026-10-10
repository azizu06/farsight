//! Bounded measured surface keyframes, without filling unobserved holes.
//! Port of jetson/mapping/surfaces.py.

use std::collections::VecDeque;

use crate::geometry::Mat4;
use crate::phone::Mesh;

pub struct SurfaceMap {
    pub budget: usize,
    pub keyframes: VecDeque<Mesh>,
    /// Coarse triangulations of each keyframe's depth grid, aligned with
    /// `keyframes` — (stride, mesh) pairs for the capped visual export.
    pub lods: VecDeque<Vec<(usize, Mesh)>>,
    pub count: usize,
    pub last_pose: Option<Mat4>,
    pub revision: u64,
}

impl SurfaceMap {
    pub fn new(triangle_budget: usize) -> Self {
        Self {
            budget: triangle_budget,
            keyframes: VecDeque::new(),
            lods: VecDeque::new(),
            count: 0,
            last_pose: None,
            revision: 0,
        }
    }

    pub fn clear(&mut self) {
        self.keyframes.clear();
        self.lods.clear();
        self.count = 0;
        self.last_pose = None;
        self.revision = 0;
    }

    pub fn integrate(&mut self, mesh: Mesh, pose: &Mat4, lods: Vec<(usize, Mesh)>) {
        if mesh.indices.is_empty() {
            return;
        }
        if let Some(last) = &self.last_pose {
            let delta = ((pose[0][3] - last[0][3]).powi(2)
                + (pose[1][3] - last[1][3]).powi(2)
                + (pose[2][3] - last[2][3]).powi(2))
            .sqrt();
            // angle = acos(clip((trace(R_last^T R_pose) - 1) / 2, -1, 1))
            let mut trace = 0.0;
            for i in 0..3 {
                for j in 0..3 {
                    trace += last[j][i] * pose[j][i];
                }
            }
            let angle = ((trace - 1.0) / 2.0).clamp(-1.0, 1.0).acos();
            if delta < 0.18 && angle < 14.0f64.to_radians() {
                return;
            }
        }
        if mesh.indices.len() > self.budget {
            return;
        }
        self.count += mesh.indices.len();
        self.keyframes.push_back(mesh);
        self.lods.push_back(lods);
        while self.count > self.budget {
            if let Some(old) = self.keyframes.pop_front() {
                self.count -= old.indices.len();
                self.lods.pop_front();
            } else {
                break;
            }
        }
        self.last_pose = Some(*pose);
        self.revision += 1;
    }

    /// Connected low-detail meshes for a `limit`-triangle visual export, or
    /// None. Finest stored LOD at which every keyframe fits; past that, the
    /// newest whole keyframes at the coarsest LOD. Port of surfaces.visual().
    pub fn visual(&self, limit: usize) -> Option<Vec<&Mesh>> {
        if self.keyframes.is_empty() {
            return None;
        }
        // Sorted strides present in EVERY keyframe's stored LODs.
        let mut steps: Vec<usize> = match self.lods.front() {
            Some(l) => l.iter().map(|(s, _)| *s).collect(),
            None => return None,
        };
        for lod in self.lods.iter().skip(1) {
            let keys: std::collections::HashSet<usize> = lod.iter().map(|(s, _)| *s).collect();
            steps.retain(|st| keys.contains(st));
        }
        steps.sort_unstable();
        if steps.is_empty() {
            return None;
        }
        let mut coarsest: Option<Vec<&Mesh>> = None;
        for &step in &steps {
            let meshes: Vec<&Mesh> = self
                .lods
                .iter()
                .filter_map(|lod| lod.iter().find(|(s, _)| *s == step).map(|(_, m)| m))
                .collect();
            if meshes.len() != self.lods.len() {
                return None;
            }
            coarsest = Some(meshes.clone());
            if meshes.iter().map(|m| m.indices.len()).sum::<usize>() <= limit {
                return Some(meshes);
            }
        }
        // Coarsest level still over budget: newest whole keyframes that fit.
        let meshes = coarsest?;
        let mut kept: Vec<&Mesh> = Vec::new();
        let mut total = 0usize;
        for mesh in meshes.iter().rev() {
            if total + mesh.indices.len() > limit {
                break;
            }
            kept.push(mesh);
            total += mesh.indices.len();
        }
        kept.reverse();
        Some(kept)
    }

    /// mesh.bin wire bytes for an arbitrary mesh list — port of surfaces.pack.
    pub fn pack(meshes: &[&Mesh]) -> Vec<u8> {
        let vertex_count: u32 = meshes.iter().map(|m| m.positions.len() as u32).sum();
        let index_scalars: u32 = meshes.iter().map(|m| m.indices.len() as u32 * 3).sum();
        let mut out = Vec::with_capacity(8 + vertex_count as usize * 16 + index_scalars as usize * 4);
        out.extend_from_slice(&vertex_count.to_le_bytes());
        out.extend_from_slice(&index_scalars.to_le_bytes());
        for mesh in meshes {
            for (p, c) in mesh.positions.iter().zip(mesh.colors.iter()) {
                for v in p {
                    out.extend_from_slice(&v.to_le_bytes());
                }
                out.extend_from_slice(c);
                out.push(0);
            }
        }
        let mut offset = 0u32;
        for mesh in meshes {
            for tri in &mesh.indices {
                for &i in tri {
                    out.extend_from_slice(&(i + offset).to_le_bytes());
                }
            }
            offset += mesh.positions.len() as u32;
        }
        out
    }

    /// Header: u32 vertex_count, u32 scalar index count; then 16-byte vertex
    /// records, then u32 indices with per-keyframe offsets applied. Port of encode().
    pub fn encode(&self) -> Vec<u8> {
        let index_scalars: usize = self.count * 3;
        let mut out = Vec::with_capacity(8 + self.count * 16 + index_scalars * 4);
        let mut vertex_count: u32 = 0;
        for mesh in &self.keyframes {
            vertex_count += mesh.positions.len() as u32;
        }
        out.extend_from_slice(&vertex_count.to_le_bytes());
        out.extend_from_slice(&(index_scalars as u32).to_le_bytes());
        for mesh in &self.keyframes {
            for (p, c) in mesh.positions.iter().zip(mesh.colors.iter()) {
                for v in p {
                    out.extend_from_slice(&v.to_le_bytes());
                }
                out.extend_from_slice(c);
                out.push(0);
            }
        }
        let mut offset = 0u32;
        for mesh in &self.keyframes {
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

impl Default for SurfaceMap {
    fn default() -> Self {
        Self::new(160_000)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::IDENTITY;

    fn mesh(tris: usize) -> Mesh {
        let mut positions = Vec::new();
        let mut colors = Vec::new();
        let mut indices = Vec::new();
        for _ in 0..tris {
            let base = positions.len() as u32;
            positions.extend_from_slice(&[[0.0; 3], [1.0; 3], [0.0, 1.0, 0.0]]);
            colors.extend_from_slice(&[[5, 6, 7]; 3]);
            indices.push([base, base + 1, base + 2]);
        }
        Mesh { positions, colors, indices }
    }

    fn pose_shifted(dx: f64) -> Mat4 {
        let mut p = IDENTITY;
        p[0][3] = dx;
        p
    }

    #[test]
    fn visual_picks_finest_fitting_lod_then_newest_keyframes() {
        let mut s = SurfaceMap::new(1_000);
        // Three keyframes, each with stride-4 (8 tris) and stride-8 (4 tris).
        let lod = |t4: usize, t8: usize| vec![(4usize, mesh(t4)), (8usize, mesh(t8))];
        s.integrate(mesh(20), &pose_shifted(0.0), lod(8, 4));
        s.integrate(mesh(20), &pose_shifted(1.0), lod(8, 4));
        s.integrate(mesh(20), &pose_shifted(2.0), lod(8, 4));
        // 24 tris total at stride-4 <= 30 -> whole set at finest level.
        let v = s.visual(30).unwrap();
        assert_eq!(v.len(), 3);
        assert_eq!(v.iter().map(|m| m.indices.len()).sum::<usize>(), 24);
        // Cap 14: stride-4 (24) over, stride-8 (12) fits -> whole set coarse.
        let v = s.visual(14).unwrap();
        assert_eq!(v.len(), 3);
        assert_eq!(v.iter().map(|m| m.indices.len()).sum::<usize>(), 12);
        // Cap 5: even stride-8 (12) over -> newest whole keyframes only.
        let v = s.visual(5).unwrap();
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].indices.len(), 4);
        // No stored LODs -> fallback signal for the caller.
        let mut plain = SurfaceMap::new(100);
        plain.integrate(mesh(10), &IDENTITY, Vec::new());
        assert!(plain.visual(5).is_none());
    }

    #[test]
    fn pack_matches_mesh_bin_wire_shape() {
        let a = mesh(2);
        let b = mesh(1);
        let out = SurfaceMap::pack(&[&a, &b]);
        let vcount = u32::from_le_bytes(out[0..4].try_into().unwrap());
        let icount = u32::from_le_bytes(out[4..8].try_into().unwrap());
        assert_eq!((vcount, icount), (9, 9));
        assert_eq!(out.len(), 8 + 9 * 16 + 9 * 4);
        // Second mesh's indices are offset past the first mesh's vertices.
        let idx_off = 8 + 9 * 16;
        // First mesh contributes 6 index scalars (2 tris) = 24 bytes.
        let second_first = u32::from_le_bytes(
            out[idx_off + 24..idx_off + 28].try_into().unwrap(),
        );
        assert_eq!(second_first, 6);
    }
}
