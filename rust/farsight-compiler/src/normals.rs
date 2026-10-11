//! Normal estimation + consistent orientation + local spacing. OWNER: W2.
//! Fills `cloud.nrm` (unit, consistently oriented outward from surfaces
//! towards free space) and `cloud.radius` (local sample spacing).
use crate::core::PointCloud;
use crate::Stats;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct NormalOptions { pub k: usize }
impl Default for NormalOptions { fn default() -> Self { NormalOptions { k: 16 } } }

pub fn estimate(cloud: &mut PointCloud, _opts: &NormalOptions) -> Stats {
    serde_json::json!({ "stub": true, "points": cloud.len() })
}
