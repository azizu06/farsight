//! Brush (ArthurBrussee/brush v0.3.0) training presets for the refine stage.
//! OWNER: W7. See `tools/compiler/brush_fast.md` for the rationale and the
//! measured numbers behind each choice.
//!
//! The goal is maximum held-out PSNR inside ~45-50 s on an Apple M5 Pro:
//! Brush is seeded with our compiled splat cloud (`ply_file_path` in
//! transforms.json), so positions are already good — the presets trade
//! learning-rate schedule, SH degree, resolution and densification window
//! for PSNR per second rather than peak PSNR.

/// One Brush CLI configuration. All fields map 1:1 to `brush_app` flags;
/// `args()` emits them in `brush_app`'s `--kebab-case` order.
#[derive(Clone, Debug)]
pub struct RefinePreset {
    /// Short tag used on the command line and in reports.
    pub name: &'static str,
    /// `--total-steps`: stop after this many optimisation steps.
    pub total_steps: u32,
    /// `--max-resolution`: longest image side at load time.
    pub max_resolution: u32,
    /// `--sh-degree`: spherical-harmonic degree kept per splat (0 = flat RGB).
    pub sh_degree: u32,
    /// `--refine-every`: densify/prune interval (Brush default 200).
    pub refine_every: u32,
    /// `--growth-stop-iter`: no new splats after this step (Brush default 15000).
    /// Low values cap fill growth early; 0 disables growth entirely.
    pub growth_stop_iter: u32,
    /// `--growth-select-fraction`: fraction of over-threshold splats that grow
    /// per refine step (Brush default 0.1).
    pub growth_select_fraction: f32,
    /// `--max-splats`: hard cap on splat count.
    pub max_splats: u32,
    /// `--subsample-points`: keep every nth init point (1 = all).
    pub subsample_points: u32,
    /// `--subsample-frames`: keep every nth frame (1 = all).
    pub subsample_frames: u32,
    /// `--eval-split-every`: hold out every nth view for the eval set.
    /// 8 matches the score_heldout.py acceptance method.
    pub eval_split_every: u32,
    /// `--eval-every`: run held-out eval at this step interval.
    pub eval_every: u32,
    /// `--export-every`: write `export_{iter}.ply` at this interval.
    pub export_every: u32,
    /// `--lr-mean` / `--lr-mean-end`: position learning rate range.
    /// Low when seeded from accurate geometry.
    pub lr_mean: f64,
    pub lr_mean_end: f64,
    /// `--lr-coeffs-dc`: base colour (SH-0) learning rate.
    pub lr_coeffs_dc: f64,
    /// `--lr-opac`: opacity learning rate.
    pub lr_opac: f64,
    /// `--lr-scale` / `--lr-scale-end`: scale learning rate range.
    pub lr_scale: f64,
    pub lr_scale_end: f64,
    /// `--lr-rotation`: rotation learning rate.
    pub lr_rotation: f64,
    /// `--ssim-weight`: SSIM share of the photometric loss.
    pub ssim_weight: f32,
    /// `--aux-loss-time`: fraction of training that aux losses run (0..1).
    pub aux_loss_time: f32,
}

impl RefinePreset {
    /// `brush_app` command-line arguments for this preset.
    pub fn args(&self) -> Vec<String> {
        vec![
            "--total-steps".into(), self.total_steps.to_string(),
            "--max-resolution".into(), self.max_resolution.to_string(),
            "--sh-degree".into(), self.sh_degree.to_string(),
            "--refine-every".into(), self.refine_every.to_string(),
            "--growth-stop-iter".into(), self.growth_stop_iter.to_string(),
            "--growth-select-fraction".into(), self.growth_select_fraction.to_string(),
            "--max-splats".into(), self.max_splats.to_string(),
            "--subsample-points".into(), self.subsample_points.to_string(),
            "--subsample-frames".into(), self.subsample_frames.to_string(),
            "--eval-split-every".into(), self.eval_split_every.to_string(),
            "--eval-every".into(), self.eval_every.to_string(),
            "--export-every".into(), self.export_every.to_string(),
            "--lr-mean".into(), fmt_f(self.lr_mean),
            "--lr-mean-end".into(), fmt_f(self.lr_mean_end),
            "--lr-coeffs-dc".into(), fmt_f(self.lr_coeffs_dc),
            "--lr-opac".into(), fmt_f(self.lr_opac),
            "--lr-scale".into(), fmt_f(self.lr_scale),
            "--lr-scale-end".into(), fmt_f(self.lr_scale_end),
            "--lr-rotation".into(), fmt_f(self.lr_rotation),
            "--ssim-weight".into(), self.ssim_weight.to_string(),
            "--aux-loss-time".into(), self.aux_loss_time.to_string(),
        ]
    }
}

fn fmt_f(v: f64) -> String {
    format!("{v:e}")
}

/// The presets. Order = preferred order; `balanced` is the acceptance
/// candidate for the ~45 s / 19.3 dB target on M5 Pro.
///
/// Timing model (see brush_fast.md): ~40-60 steps/s on Metal at 800 px,
/// SH 1, ~150-300 k splats. Refine steps cost roughly a full step each, so
/// `refine_every` is kept sparse after the early fill window.
pub const PRESETS: &[RefinePreset] = &[
    // ~15-20 s: smoke / preview pass. SH0 keeps memory tiny; growth off
    // because the seed is already dense; position lr nearly frozen.
    RefinePreset {
        name: "fast",
        total_steps: 1000,
        max_resolution: 640,
        sh_degree: 1,
        refine_every: 200,
        growth_stop_iter: 400,
        growth_select_fraction: 0.05,
        max_splats: 300_000,
        subsample_points: 2,
        subsample_frames: 1,
        eval_split_every: 8,
        eval_every: 250,
        export_every: 500,
        lr_mean: 2e-6,
        lr_mean_end: 5e-7,
        lr_coeffs_dc: 3e-3,
        lr_opac: 1e-2,
        lr_scale: 1e-2,
        lr_scale_end: 6e-3,
        lr_rotation: 1e-3,
        ssim_weight: 0.2,
        aux_loss_time: 0.8,
    },
    // ~45-50 s: acceptance candidate. Full seeded cloud, modest growth
    // window to fill residual holes, ~3-4 k steps at 800 px / SH1.
    RefinePreset {
        name: "balanced",
        total_steps: 3000,
        max_resolution: 800,
        sh_degree: 1,
        refine_every: 150,
        growth_stop_iter: 1200,
        growth_select_fraction: 0.1,
        max_splats: 350_000,
        subsample_points: 1,
        subsample_frames: 1,
        eval_split_every: 8,
        eval_every: 500,
        export_every: 1000,
        lr_mean: 5e-6,
        lr_mean_end: 1e-6,
        lr_coeffs_dc: 2e-3,
        lr_opac: 1e-2,
        lr_scale: 1e-2,
        lr_scale_end: 6e-3,
        lr_rotation: 1e-3,
        ssim_weight: 0.2,
        aux_loss_time: 0.85,
    },
    // ~90 s: quality headroom. Two-phase schedule hint: run this preset at
    // 640 px, then relaunch with --max-resolution 1280 using its export as
    // the new init.ply (resolution is a load-time setting in Brush, so a
    // schedule = two invocations; see brush_fast.md).
    RefinePreset {
        name: "ultra",
        total_steps: 6000,
        max_resolution: 1280,
        sh_degree: 2,
        refine_every: 200,
        growth_stop_iter: 3000,
        growth_select_fraction: 0.1,
        max_splats: 500_000,
        subsample_points: 1,
        subsample_frames: 1,
        eval_split_every: 8,
        eval_every: 1000,
        export_every: 1000,
        lr_mean: 1e-5,
        lr_mean_end: 1e-6,
        lr_coeffs_dc: 2e-3,
        lr_opac: 1e-2,
        lr_scale: 1e-2,
        lr_scale_end: 6e-3,
        lr_rotation: 1e-3,
        ssim_weight: 0.2,
        aux_loss_time: 0.9,
    },
];

/// Look up a preset by `name` (`fast` | `balanced` | `ultra`).
pub fn preset(name: &str) -> Option<&'static RefinePreset> {
    PRESETS.iter().find(|p| p.name == name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_preset_emits_cli_args() {
        for p in PRESETS {
            let a = p.args();
            assert!(a.windows(2).any(|w| w[0] == "--total-steps"));
            assert!(a.windows(2).any(|w| w[0] == "--eval-split-every" && w[1] == "8"),
                "{} must keep eval_split_every=8", p.name);
        }
        assert!(preset("balanced").is_some());
        assert!(preset("nope").is_none());
    }
}
