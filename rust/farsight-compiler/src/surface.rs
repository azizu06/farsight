//! Watertight-where-justified surface reconstruction. OWNER: W4.
//! Sparse-grid implicit surface from oriented points (interpolates across
//! holes), marching cubes, trimming of unsupported fill, per-vertex `support`.
use crate::core::{Mesh, PointCloud};
use crate::Stats;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct SurfaceOptions {
    /// Implicit grid voxel size (metres).
    pub voxel: f32,
    /// Remove surface farther than this from any sample (metres).
    pub trim_m: f32,
}
impl Default for SurfaceOptions { fn default() -> Self { SurfaceOptions { voxel: 0.02, trim_m: 0.5 } } }

pub fn reconstruct(_cloud: &PointCloud, _opts: &SurfaceOptions) -> (Mesh, Stats) {
    (Mesh::default(), serde_json::json!({ "stub": true }))
}
