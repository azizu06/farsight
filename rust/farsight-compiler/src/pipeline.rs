//! Stage orchestration. OWNER: orchestrator. Stage owners change only their
//! own module; wire new options through their `*Options` struct (serde default).

use crate::core::{Capture, Mesh, PointCloud};
use crate::{clean, densify, export, learned, meshops, normals, planes, refine, surface, texture, Stats};
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct CompileOptions {
    pub clean: clean::CleanOptions,
    pub normals: normals::NormalOptions,
    pub planes: planes::PlaneOptions,
    pub learned: learned::LearnedOptions,
    pub densify: densify::DensifyOptions,
    pub surface: surface::SurfaceOptions,
    pub mesh: meshops::MeshOptions,
    pub texture: texture::TextureOptions,
    pub export: export::ExportOptions,
    /// Optional Brush splat-refine stage (default off; additive W6 knob).
    pub refine: refine::RefineOptions,
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
    /// Spawned Brush refine job when refine.enabled; collect via refine::finish.
    pub refine: Option<refine::RefineHandle>,
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
    // Learned depth completion (Neural Engine / ONNX). Skips fast when no
    // model is configured; maps are consumed by densify (W3 contract:
    // Option<&[DepthMap]> inserted before `opts`).
    let lopts = opts.learned.resolve();
    let mut depth_maps: Vec<learned::DepthMap> = Vec::new();
    if let (Some(cap), false) = (capture, lopts.depth_model.is_empty()) {
        timed(&mut report, "learned", || {
            depth_maps = learned::complete_depth_opts(cap, &cloud, &lopts);
            let v = depth_maps.len();
            let rmse: f32 = if v == 0 { 0.0 } else { depth_maps.iter().map(|m| m.fit_rmse).sum::<f32>() / v as f32 };
            ((), serde_json::json!({"views": v, "mean_fit_rmse_m": (rmse * 1000.0).round() / 1000.0}))
        });
    }
    let depths_arg = (!depth_maps.is_empty()).then_some(depth_maps.as_slice());
    let dense = timed(&mut report, "densify", || densify::run(&cloud, capture, depths_arg, &opts.densify));
    dense.validate();
    let pl = timed(&mut report, "planes", || planes::detect(&dense, &opts.planes));
    let mut mesh = timed(&mut report, "surface", || surface::reconstruct(&dense, &opts.surface));
    mesh.validate();
    let pnd: Vec<(crate::core::V3, f32)> = pl.planes.iter().map(|p| (p.n, p.d)).collect();
    timed(&mut report, "meshops", || ((), meshops::run_with_planes(&mut mesh, &dense, &opts.mesh, &pnd, &pl.label)));
    mesh.validate();
    // Refine seed is written here so Brush trains while texture bake runs (W6).
    let refine_handle = refine::start(&dense, &mesh, capture, &opts.refine);
    timed(&mut report, "texture", || ((), texture::bake(&mut mesh, &dense, capture, &opts.texture)));
    mesh.validate();
    Output { cloud: dense, mesh, report, refine: refine_handle }
}
