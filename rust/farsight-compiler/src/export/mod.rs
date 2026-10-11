//! Writers. OWNER: W6 (GLB, splat); PLY baseline here.
pub mod glb;
pub mod ply;
pub mod splat;

use crate::core::{Mesh, PointCloud};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ExportOptions {
    /// Write scene.glb (glTF 2.0 binary with embedded texture).
    pub glb: bool,
    /// Write splat.ply (INRIA 3DGS surfels).
    pub splat: bool,
    /// Write points.ply / mesh.ply.
    pub ply: bool,
    /// Splat confidence multiplier for opacity (0..1].
    pub splat_opacity: f32,
    /// Splat surfel scale multiplier.
    pub splat_scale: f32,
    /// Normal-axis scale ratio for flat surfels.
    pub splat_thin: f32,
    /// Splat radius when the cloud has none (metres).
    pub splat_default_radius: f32,
}

impl Default for ExportOptions {
    fn default() -> Self {
        ExportOptions { glb: true, splat: true, ply: true, splat_opacity: 0.95, splat_scale: 1.0, splat_thin: 0.15, splat_default_radius: 0.012 }
    }
}

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
    if opts.glb && mesh.triangles() > 0 {
        let p = dir.join("scene.glb");
        let extras = serde_json::json!({
            "points": cloud.len(),
            "interpolated_points": cloud.interpolated_count(),
            "vertices": mesh.pos.len(),
            "triangles": mesh.triangles(),
            "textured": mesh.texture.is_some(),
        });
        std::fs::write(&p, glb::scene(mesh, extras)?)?;
        out.push(p);
    }
    if opts.splat && !cloud.is_empty() {
        let p = dir.join("splat.ply");
        std::fs::write(&p, splat::splats(cloud, opts.splat_opacity, opts.splat_scale, opts.splat_thin, opts.splat_default_radius))?;
        out.push(p);
    }
    Ok(out)
}
