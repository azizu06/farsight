//! Interpolation / densification. OWNER: W3. Produces a denser, gap-filled
//! cloud: MLS projection + upsampling, hole filling across unobserved gaps,
//! colour interpolation (optionally guided by capture images). Interpolated
//! points get FLAG_INTERPOLATED. Requires normals on input (may be empty in stub).
use crate::core::{Capture, PointCloud};
use crate::Stats;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct DensifyOptions {
    /// Target spacing of the output cloud (metres).
    pub target_spacing: f32,
    /// Largest gap diameter to bridge with interpolated samples (metres).
    pub max_gap: f32,
}
impl Default for DensifyOptions { fn default() -> Self { DensifyOptions { target_spacing: 0.01, max_gap: 0.6 } } }

pub fn run(cloud: &PointCloud, _capture: Option<&Capture>, _opts: &DensifyOptions) -> (PointCloud, Stats) {
    (cloud.clone(), serde_json::json!({ "stub": true }))
}
