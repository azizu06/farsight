//! Stage orchestration. OWNER: orchestrator. Stage owners change only their
//! own module; wire new options through their `*Options` struct (serde default).

use crate::core::{Capture, Mesh, PointCloud};
use crate::{clean, densify, export, meshops, normals, surface, texture, Stats};
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct CompileOptions {
    pub clean: clean::CleanOptions,
    pub normals: normals::NormalOptions,
    pub densify: densify::DensifyOptions,
    pub surface: surface::SurfaceOptions,
    pub mesh: meshops::MeshOptions,
    pub texture: texture::TextureOptions,
    pub export: export::ExportOptions,
}

impl CompileOptions {
    /// fast: live preview (seconds); balanced: default; ultra: final showpiece.
    pub fn preset(name: &str) -> anyhow::Result<Self> {
        let mut o = CompileOptions::default();
        match name {
            "fast" => {
                o.densify.target_spacing = 0.02;
                o.surface.voxel = 0.04;
                o.mesh.max_triangles = 150_000;
                o.mesh.smooth_iters = 2;
                o.texture.texel_m = 0.01;
                o.texture.max_atlas = 4096;
                o.export.splat_max = 750_000;
            }
            "balanced" => {
                o.mesh.max_triangles = 400_000;
            }
            "ultra" => {
                o.densify.target_spacing = 0.006;
                o.surface.voxel = 0.012;
                o.mesh.max_triangles = 4_000_000;
                o.texture.texel_m = 0.0025;
                o.texture.max_atlas = 16384;
                o.export.splat_max = 4_000_000;
            }
            other => anyhow::bail!("unknown preset {other} (fast|balanced|ultra)"),
        }
        Ok(o)
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct StageReport {
    pub stage: &'static str,
    pub ms: f64,
    pub stats: Stats,
}

pub struct Output {
    pub cloud: PointCloud,
    pub mesh: Mesh,
    pub report: Vec<StageReport>,
}

fn timed<T>(report: &mut Vec<StageReport>, stage: &'static str, f: impl FnOnce() -> (T, Stats)) -> T {
    let t = Instant::now();
    let (v, stats) = f();
    let ms = t.elapsed().as_secs_f64() * 1e3;
    eprintln!("[compile] {stage:<9} {ms:>9.1} ms  {stats}");
    report.push(StageReport { stage, ms, stats });
    v
}

pub fn compile(mut cloud: PointCloud, capture: Option<&Capture>, opts: &CompileOptions) -> Output {
    let mut report = Vec::new();
    cloud.validate();
    timed(&mut report, "clean", || ((), clean::run(&mut cloud, &opts.clean)));
    cloud.validate();
    timed(&mut report, "normals", || ((), normals::estimate(&mut cloud, &opts.normals)));
    cloud.validate();
    let dense = timed(&mut report, "densify", || densify::run(&cloud, capture, &opts.densify));
    dense.validate();
    let mut mesh = timed(&mut report, "surface", || surface::reconstruct(&dense, &opts.surface));
    mesh.validate();
    timed(&mut report, "meshops", || ((), meshops::run(&mut mesh, &dense, &opts.mesh)));
    mesh.validate();
    timed(&mut report, "texture", || ((), texture::bake(&mut mesh, &dense, capture, &opts.texture)));
    mesh.validate();
    Output { cloud: dense, mesh, report }
}
