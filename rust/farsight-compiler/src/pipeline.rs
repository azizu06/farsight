//! Stage orchestration. OWNER: orchestrator. Stage owners change only their
//! own module; wire new options through their `*Options` struct (serde default).

use crate::core::{dot, Capture, Mesh, PointCloud, V3};
use rayon::prelude::*;
use crate::{clean, densify, export, meshops, normals, planes, spatial, surface, texture, Stats};
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct CompileOptions {
    pub clean: clean::CleanOptions,
    pub normals: normals::NormalOptions,
    pub planes: planes::PlaneOptions,
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
    // Shared plane detection (W2) + inlier snapping onto planes (<2.5 sigma).
    let planes = timed(&mut report, "planes", || {
        let (pl, st) = planes::detect(&cloud, &opts.planes);
        let snap = if std::env::var("FS_NO_SNAP").is_ok() {
            serde_json::json!({"snapped":0})
        } else {
            clean::snap_to_planes(&mut cloud, &pl, &opts.clean)
        };
        let mut st = st;
        st["snapped"] = snap["snapped"].clone();
        (pl, st)
    });
    cloud.validate();
    let dense = timed(&mut report, "densify", || densify::run(&cloud, capture, &opts.densify));
    dense.validate();
    let mut mesh = timed(&mut report, "surface", || surface::reconstruct(&dense, &opts.surface));
    mesh.validate();
    // Label each dense point by evaluating the plane equations directly
    // (same contract as meshops' internal fallback: dense coverage, nearest
    // plane within eps). Input-cloud labels are only used upstream.
    let planes_nd: Vec<(V3, f32)> = planes.planes.iter().map(|p| (p.n, p.d)).collect();
    let dense_labels: Vec<u32> = if planes_nd.is_empty() {
        Vec::new()
    } else {
        let mut r: Vec<f32> = cloud.radius.iter().copied().filter(|x| x.is_finite() && *x > 0.0).collect();
        let spacing = if r.is_empty() { 0.05 } else { r.sort_by(|a, b| a.total_cmp(b)); r[r.len() / 2] };
        let eps2 = (1.8 * spacing * 1.8 * spacing).max(1e-6);
        dense
            .pos
            .par_iter()
            .map(|p| {
                let mut best = u32::MAX;
                let mut bd = eps2;
                for (l, &(n, d)) in planes_nd.iter().enumerate() {
                    let t = dot(n, *p) + d;
                    if t * t < bd {
                        bd = t * t;
                        best = l as u32;
                    }
                }
                best
            })
            .collect()
    };
    timed(&mut report, "meshops", || {
        ((), if planes_nd.is_empty() {
            meshops::run(&mut mesh, &dense, &opts.mesh)
        } else {
            meshops::run_with_planes(&mut mesh, &dense, &opts.mesh, &planes_nd, &dense_labels)
        })
    });
    mesh.validate();
    timed(&mut report, "texture", || ((), texture::bake(&mut mesh, &dense, capture, &opts.texture)));
    mesh.validate();
    Output { cloud: dense, mesh, report }
}
