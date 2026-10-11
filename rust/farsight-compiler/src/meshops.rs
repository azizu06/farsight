//! Mesh post-processing. OWNER: W5. Manifold cleanup, small-component removal,
//! boundary hole filling, feature-preserving smoothing, vertex colour transfer
//! from the cloud, quadric decimation to a triangle budget, normals.
use crate::core::{Mesh, PointCloud};
use crate::Stats;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct MeshOptions {
    /// Final triangle budget (0 = keep all).
    pub max_triangles: usize,
    pub smooth_iters: usize,
}
impl Default for MeshOptions { fn default() -> Self { MeshOptions { max_triangles: 1_500_000, smooth_iters: 4 } } }

pub fn run(_mesh: &mut Mesh, _cloud: &PointCloud, _opts: &MeshOptions) -> Stats {
    serde_json::json!({ "stub": true })
}
