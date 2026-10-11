//! farsight-seed: turn the rover's LiDAR dot cloud into a Brush-ready
//! Gaussian splat seed.
//!
//! Pipeline (all deterministic, parallel):
//!   1. `clean`    — SOR + isolated-cluster removal
//!   2. `decimate` — adaptive voxel merge to `target_splats`
//!   3. `pca`      — per-point tangent frame: normal + spacing + eigvals
//!   4. `colour`   — occlusion-tested robust-median photo colour (also emits
//!                   sparse inverse depth per view for the T2/T3 depth loss)
//!   5. `fill`     — optional synthesized splats in photo-visible dot holes
//!   6. `emit`     — `SeedSplat` -> INRIA PLY + sparse-depth files

pub mod clean;
pub mod colour;
pub mod core;
pub mod decimate;
pub mod emit;
pub mod fill;
pub mod grid;
pub mod pca;
pub mod views;

use glam::Vec3;
use rayon::prelude::*;

pub use clean::CleanConfig;
pub use colour::{ColourConfig, DepthSample, SparseDepthView};
pub use emit::SeedSplat;
pub use fill::{FillConfig, FillPoint};
pub use pca::PcaConfig;
pub use views::{NerfCapture, SeedCamera, SeedView};

#[derive(Clone, Debug)]
pub struct SeedConfig {
    /// Splats in the emitted seed (adaptive voxel decimation target).
    pub target_splats: usize,
    /// Thin-axis fraction of local spacing for the normal direction scale.
    pub thin: f32,
    /// Tangent scale multiplier relative to local spacing (≈splat radius).
    pub scale_mul: f32,
    /// Opacity range mapped from photo coverage count.
    pub opacity_lo: f32,
    pub opacity_hi: f32,
    /// Coverage count at which opacity_hi is reached.
    pub opacity_full_samples: usize,
    /// Photo-colour for synthesized fill splats gets this opacity.
    pub fill_opacity: f32,
    /// Use dot rgb only when photo sampling fails.
    pub dot_colour_fallback: bool,
    pub clean: CleanConfig,
    pub pca: PcaConfig,
    pub colour: ColourConfig,
    pub fill: Option<FillConfig>,
}

impl Default for SeedConfig {
    fn default() -> Self {
        SeedConfig {
            target_splats: 300_000,
            thin: 0.2,
            scale_mul: 0.5,
            opacity_lo: 0.5,
            opacity_hi: 0.8,
            opacity_full_samples: 6,
            fill_opacity: 0.3,
            dot_colour_fallback: true,
            clean: CleanConfig::default(),
            pca: PcaConfig::default(),
            colour: ColourConfig::default(),
            fill: None,
        }
    }
}

pub struct SeedOutput {
    pub splats: Vec<SeedSplat>,
    /// Sparse inverse depth per view (already written by emit::write_sparse_depth).
    pub depth: Vec<SparseDepthView>,
    pub stats: SeedStats,
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct SeedStats {
    pub dots_in: usize,
    pub after_clean: usize,
    pub after_decimate: usize,
    pub fill_added: usize,
    pub emitted: usize,
    pub spacing_m: f32,
    pub voxel_m: f32,
    pub colour_views: usize,
    pub depth_views: usize,
    pub uncoloured: usize,
    pub clean: serde_json::Value,
}

/// The whole pipeline, in-memory. `dots` are world-frame positions (the
/// init.ply cloud); `dot_col` optional matching rgb. `views` carry decoded
/// images only where colour sampling/fill should look at them.
pub fn run(
    dots: &[Vec3],
    dot_col: Option<&[[u8; 3]]>,
    views: &[SeedView],
    cfg: &SeedConfig,
) -> SeedOutput {
    // --- 1. clean ---
    // dot colours are intentionally unused: seed colour comes from the
    // photos (spec), so the .ply rgb only feeds possible future fallbacks.
    let _ = dot_col;
    let (keep, cstats) = clean::clean_mask(dots, &cfg.clean);
    let mut pts: Vec<Vec3> = Vec::with_capacity(cstats.kept);
    for (i, &k) in keep.iter().enumerate() {
        if k {
            pts.push(dots[i]);
        }
    }

    // --- 2. decimate ---
    let weights = vec![1.0f32; pts.len()];
    let base_v = decimate::median_spacing(&pts, cstats.spacing_m.max(1e-3));
    let (dpos, _dweight, voxel) = decimate::decimate(&pts, &weights, cfg.target_splats, base_v);

    // --- 3. PCA frames (on decimated points; spacing from the raw cloud) ---
    let view_pos: Vec<Vec3> = views.iter().map(|v| v.camera.position()).collect();
    let spacing_hint = decimate::median_spacing(&dpos, base_v);
    let (frames, med_spacing) = pca::frames(&dpos, &cfg.pca, &view_pos, spacing_hint);

    // --- 4. colour + sparse depth (one projection pass over the DECIMATED
    //         cloud for colour; depth export uses the RAW cleaned cloud so
    //         supervision stays dense) ---
    let col_stride = if cfg.colour.colour_stride > 0 {
        cfg.colour.colour_stride
    } else {
        colour::auto_stride(views.len(), 96)
    };
    let dep_stride = if cfg.colour.depth_stride > 0 {
        cfg.colour.depth_stride
    } else {
        col_stride
    };
    let colres = colour::project(&dpos, views, &cfg.colour, col_stride, dep_stride);

    // sparse depth for supervision uses the full cleaned cloud, not just the
    // decimated splats — T3 can thin it further if needed. colour_stride of
    // usize::MAX disables colour sampling here (only vi==0 matches, and its
    // samples are discarded anyway).
    let depth = colour::project(&pts, views, &cfg.colour, usize::MAX, dep_stride).depth;
    let depth_views = depth.len();

    // --- 5. optional fill ---
    let mut fill_pts: Vec<fill::FillPoint> = Vec::new();
    if let Some(fc) = &cfg.fill {
        fill_pts = fill::synthesize(&pts, views, fc);
    }
    let n_fill = fill_pts.len();

    // --- 6. emit ---
    let n = dpos.len();
    let uncoloured = AtomicCount::new();
    let mut splats: Vec<SeedSplat> = (0..n)
        .into_par_iter()
        .map(|i| {
            let f = &frames[i];
            let cov = colres.sample_counts[i] as usize;
            let rgb = colres.colours[i];
            if rgb.is_none() {
                uncoloured.inc();
            }
            let rgb = rgb.unwrap_or([128, 128, 128]);
            let t = (cov.min(cfg.opacity_full_samples)) as f32
                / cfg.opacity_full_samples.max(1) as f32;
            let opacity = cfg.opacity_lo + (cfg.opacity_hi - cfg.opacity_lo) * t;
            let s_t = (f.spacing * cfg.scale_mul).max(1e-4);
            let s_n = (f.spacing * cfg.thin).max(1e-5);
            SeedSplat {
                pos: dpos[i],
                rot_cols: f.rot_cols,
                scales: [s_t, s_t, s_n],
                rgb,
                opacity,
            }
        })
        .collect();
    let fill_scale = cfg.fill.as_ref().map(|f| f.scale).unwrap_or(0.02);
    for fp in &fill_pts {
        splats.push(SeedSplat {
            pos: fp.pos,
            rot_cols: normal_cols(fp.normal),
            scales: [fill_scale; 3],
            rgb: fp.rgb,
            opacity: cfg.fill_opacity,
        });
    }

    SeedOutput {
        stats: SeedStats {
            dots_in: dots.len(),
            after_clean: pts.len(),
            after_decimate: n,
            fill_added: n_fill,
            emitted: splats.len(),
            spacing_m: med_spacing,
            voxel_m: voxel,
            colour_views: views.len().div_ceil(col_stride),
            depth_views,
            uncoloured: uncoloured.get(),
            clean: serde_json::json!({
                "sor_removed": cstats.sor_removed,
                "iso_removed": cstats.iso_removed,
                "spacing_m": cstats.spacing_m,
            }),
        },
        splats,
        depth,
    }
}

fn normal_cols(n: Vec3) -> [[f32; 3]; 3] {
    let up = if n.y.abs() > 0.9 { Vec3::X } else { Vec3::Y };
    let t0 = n.cross(up).normalize_or_zero();
    let t1 = n.cross(t0).normalize_or_zero();
    [[t0.x, t1.x, n.x], [t0.y, t1.y, n.y], [t0.z, t1.z, n.z]]
}

struct AtomicCount(std::sync::atomic::AtomicUsize);
impl AtomicCount {
    fn new() -> Self {
        Self(std::sync::atomic::AtomicUsize::new(0))
    }
    fn inc(&self) {
        self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    }
    fn get(&self) -> usize {
        self.0.load(std::sync::atomic::Ordering::Relaxed)
    }
}
