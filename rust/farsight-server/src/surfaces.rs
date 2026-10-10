//! Bounded measured surface keyframes, without filling unobserved holes.
//! Port of jetson/mapping/surfaces.py.

use std::collections::VecDeque;

use crate::geometry::Mat4;
use crate::phone::Mesh;

pub struct SurfaceMap {
    pub budget: usize,
    pub keyframes: VecDeque<Mesh>,
    pub count: usize,
    pub last_pose: Option<Mat4>,
    pub revision: u64,
}

impl SurfaceMap {
    pub fn new(triangle_budget: usize) -> Self {
        Self {
            budget: triangle_budget,
            keyframes: VecDeque::new(),
            count: 0,
            last_pose: None,
            revision: 0,
        }
    }

    pub fn clear(&mut self) {
        self.keyframes.clear();
        self.count = 0;
        self.last_pose = None;
        self.revision = 0;
    }

    pub fn integrate(&mut self, mesh: Mesh, pose: &Mat4) {
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
        while self.count > self.budget {
            if let Some(old) = self.keyframes.pop_front() {
                self.count -= old.indices.len();
            } else {
                break;
            }
        }
        self.last_pose = Some(*pose);
        self.revision += 1;
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
