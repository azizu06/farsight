//! Writers. OWNER: W6 (GLB, splat); PLY baseline here.
pub mod ply;

use crate::core::{Mesh, PointCloud};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ExportOptions {
    pub glb: bool,
    pub splat: bool,
    pub ply: bool,
}
impl Default for ExportOptions { fn default() -> Self { ExportOptions { glb: true, splat: true, ply: true } } }

/// Write every enabled artefact into `dir`; returns written paths.
pub fn write_all(dir: &Path, cloud: &PointCloud, mesh: &Mesh, opts: &ExportOptions) -> Result<Vec<PathBuf>> {
    std::fs::create_dir_all(dir)?;
    let mut out = Vec::new();
    if opts.ply {
        let p = dir.join("points.ply");
        std::fs::write(&p, ply::points(cloud))?;
        out.push(p);
        if mesh.triangles() > 0 {
            let p = dir.join("mesh.ply");
            std::fs::write(&p, ply::mesh(mesh))?;
            out.push(p);
        }
    }
    Ok(out)
}
