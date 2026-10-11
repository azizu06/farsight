//! Appearance. OWNER: W6. Projective texture baking from posed capture images
//! into a UV atlas (visibility, view selection, seam blending, exposure
//! compensation); falls back to cloud colours when no capture is given.
use crate::core::{Capture, Mesh, PointCloud};
use crate::Stats;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct TextureOptions {
    /// Atlas texel size in metres.
    pub texel_m: f32,
    pub max_atlas: u32,
}
impl Default for TextureOptions { fn default() -> Self { TextureOptions { texel_m: 0.005, max_atlas: 8192 } } }

pub fn bake(_mesh: &mut Mesh, _cloud: &PointCloud, _capture: Option<&Capture>, _opts: &TextureOptions) -> Stats {
    serde_json::json!({ "stub": true })
}
