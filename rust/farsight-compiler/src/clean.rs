//! Cleanup. OWNER: W2. Dedupe/voxel-merge, statistical + radius outlier removal,
//! flying-pixel / thin-shell suppression. Must preserve every parallel array.
use crate::core::PointCloud;
use crate::Stats;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct CleanOptions {
    /// Merge points closer than this (metres). 0 disables.
    pub merge_m: f32,
    /// Statistical outlier removal: neighbours and std-dev multiplier. k=0 disables.
    pub sor_k: usize,
    pub sor_std: f32,
}
impl Default for CleanOptions { fn default() -> Self { CleanOptions { merge_m: 0.0, sor_k: 16, sor_std: 2.5 } } }

pub fn run(cloud: &mut PointCloud, _opts: &CleanOptions) -> Stats {
    serde_json::json!({ "stub": true, "points": cloud.len() })
}
