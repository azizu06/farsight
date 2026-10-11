use brush_render::gaussian_splats::SplatRenderMode;
use clap::{Parser, ValueEnum};
use serde::{Deserialize, Serialize};

/// How the splat count is managed at refine time.
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, ValueEnum, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RefineMode {
    /// Upstream behaviour: threshold-driven growth + replace-pruned splits.
    #[default]
    Brush,
    /// Taming-3DGS-style budgeted growth: a target count ramped to
    /// `--growth-target-splats` linearly over the growth window
    /// (`growth_start_iter`..`growth_stop_iter`). Over-target prunes the
    /// lowest-contribution splats; under-target grows score-sampled splits.
    Budget,
    /// 3DGS-MCMC-style relocation: pruned splats are teleported onto donors
    /// sampled ∝ opacity·visibility, keeping the count constant (no growth).
    Mcmc,
}

#[derive(Clone, Parser, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub struct TrainConfig {
    /// Total number of steps to train for.
    #[arg(long, help_heading = "Training options", default_value = "30000")]
    pub total_train_iters: u32,

    #[arg(long, help_heading = "Training options")]
    pub render_mode: Option<SplatRenderMode>,

    /// Start learning rate for the mean parameters.
    #[arg(long, help_heading = "Training options", default_value = "2e-5")]
    pub lr_mean: f64,

    /// Start learning rate for the mean parameters.
    #[arg(long, help_heading = "Training options", default_value = "2e-7")]
    pub lr_mean_end: f64,

    /// How much noise to add to the mean parameters of low opacity gaussians.
    #[arg(long, help_heading = "Training options", default_value = "50.0")]
    pub mean_noise_weight: f32,

    /// Learning rate for the base SH (RGB) coefficients.
    #[arg(long, help_heading = "Training options", default_value = "2e-3")]
    pub lr_coeffs_dc: f64,

    /// How much to divide the learning rate by for higher SH orders.
    #[arg(long, help_heading = "Training options", default_value = "10.0")]
    pub lr_coeffs_sh_scale: f32,

    /// Learning rate for the opacity parameter.
    #[arg(long, help_heading = "Training options", default_value = "0.012")]
    pub lr_opac: f64,

    /// Learning rate for the scale parameters.
    #[arg(long, help_heading = "Training options", default_value = "5e-3")]
    pub lr_scale: f64,

    /// Learning rate for the rotation parameters.
    #[arg(long, help_heading = "Training options", default_value = "2e-3")]
    pub lr_rotation: f64,

    /// Max nr. of splats. This is only an upper bound, the actual final number of splats is NOT determined by this.
    #[arg(long, help_heading = "Refine options", default_value = "10000000")]
    pub max_splats: u32,

    /// Frequency of 'refinement' where gaussians are replaced and densified. This should
    /// roughly be the number of images it takes to properly "cover" your scene.
    #[arg(
        long,
        help_heading = "Refine options",
        default_value = "200",
        value_parser = clap::value_parser!(u32).range(1..)
    )]
    pub refine_every: u32,

    /// Threshold to control splat growth. Lower means faster growth.
    #[arg(long, help_heading = "Refine options", default_value = "0.0025")]
    pub growth_grad_threshold: f32,

    /// What fraction of splats that are deemed as needing to grow do actually grow.
    /// Increase this to make splats grow more aggressively.
    #[arg(long, help_heading = "Refine options", default_value = "0.25")]
    pub growth_select_fraction: f32,

    /// Iteration at which splat growth starts. Clamped to `growth_stop_iter`.
    #[arg(long, help_heading = "Refine options", default_value = "0")]
    pub growth_start_iter: u32,

    /// Period after which splat growth stops.
    #[arg(long, help_heading = "Refine options", default_value = "15000")]
    pub growth_stop_iter: u32,

    /// Splat-count management strategy (see `RefineMode`).
    #[arg(long, help_heading = "Refine options", default_value = "brush")]
    pub refine_mode: RefineMode,

    /// `budget` mode: splat count at the end of the growth window. Defaults
    /// to `max_splats`. With a dense seed larger than the target, count is
    /// pruned down the ramp instead of grown.
    #[arg(long, help_heading = "Refine options")]
    pub growth_target_splats: Option<u32>,

    /// Speedy-Splat contribution pruning: inside the progress window, each
    /// refine drops this fraction of splats by accumulated contribution
    /// (visible·opacity·screen-area). Never resampled — a permanent cut. 0 off.
    #[arg(long, help_heading = "Refine options", default_value = "0.0")]
    pub prune_contrib_fraction: f32,

    /// Progress (0-1) at which contribution pruning starts.
    #[arg(long, help_heading = "Refine options", default_value = "0.05")]
    pub prune_contrib_start_frac: f32,

    /// Progress (0-1) after which contribution pruning stops.
    #[arg(long, help_heading = "Refine options", default_value = "0.6")]
    pub prune_contrib_end_frac: f32,

    /// Importance-decimate the initial point cloud to this count before
    /// training (KNN-spacing score), instead of the strided subsample to
    /// `max_splats`. 0 = off.
    #[arg(long, help_heading = "Refine options", default_value = "0")]
    pub init_decimate: u32,

    /// `mcmc` mode: per-splat noise scale applied to relocated positions, as
    /// a fraction of the donor's max axis. The MCMC paper's ε.
    #[arg(long, help_heading = "Refine options", default_value = "0.3")]
    pub mcmc_noise_frac: f32,

    /// `mcmc` mode: opacity the relocated splat is re-initialized to
    /// (sigmoid-space value). 0 keeps the donor's opacity.
    #[arg(long, help_heading = "Refine options", default_value = "0.05")]
    pub mcmc_opac_reset: f32,

    /// Split any splat whose max screen-space extent exceeds this fraction of
    /// the image dimension, shrinking the children so they land at (at most)
    /// this size on screen. 0 disables.
    #[arg(long, help_heading = "Refine options", default_value = "0.5")]
    pub split_at_screen_size: f32,

    /// Weight of SSIM loss (compared to l1 loss)
    #[clap(long, help_heading = "Training options", default_value = "0.2")]
    pub ssim_weight: f32,

    /// Factor of the opacity decay.
    #[arg(long, help_heading = "Training options", default_value = "0.004")]
    pub opac_decay: f32,

    /// Mip-Splatting 3D-filter strength (the paper's `s`): each splat gets a
    /// per-splat world-space scale floor `sqrt(min_scale_factor) · pixel size
    /// at the nearest observing camera`, i.e. a ~0.32px std-dev floor at the
    /// default. Folded into scales/opacity at render (and baked at export),
    /// never optimized. 0 disables the filter.
    #[arg(long, help_heading = "Training options", default_value = "0.1")]
    pub min_scale_factor: f32,

    /// Weight of l1 loss on alpha if input view has transparency.
    #[arg(long, help_heading = "Refine options", default_value = "0.1")]
    pub match_alpha_weight: f32,

    #[arg(long, help_heading = "Refine options", default_value = "0.0")]
    pub lpips_loss_weight: f32,

    /// Base background color (R,G,B) used during training.
    #[arg(
        long,
        help_heading = "Training options",
        default_value = "0,0,0",
        value_delimiter = ',',
        num_args = 3
    )]
    pub background_color: Vec<f32>,

    /// Strength of random noise added to the background color each step.
    /// Noise is uniform in [-strength, +strength], clamped to [0, 1].
    #[arg(long, help_heading = "Training options", default_value = "0.1")]
    pub background_noise_strength: f32,

    /// Number of LOD levels to generate after initial training (0 = disabled).
    #[arg(long, help_heading = "LOD options", default_value = "0")]
    pub lod_levels: u32,

    /// Number of refinement training steps per LOD level.
    #[arg(long, help_heading = "LOD options", default_value = "5000")]
    pub lod_refine_steps: u32,

    /// Percentage of gaussians to keep at each LOD level (1-100).
    #[arg(long, help_heading = "LOD options", default_value = "50")]
    pub lod_decimation_keep: u32,

    /// Percentage to scale source images at each LOD level (1-100).
    #[arg(long, help_heading = "LOD options", default_value = "50")]
    pub lod_image_scale: u32,

    /// Scene scale used for random splat initialization.
    /// When no init is provided, splats are randomly placed
    /// inside camera frustums up to this depth. By default this is
    /// estimated from the camera spacing (with a 1m minimum).
    #[arg(long, help_heading = "Training options")]
    pub random_init_scene_scale: Option<f32>,

    /// Wall-clock training budget in seconds. When set, every schedule —
    /// mean-LR decay, progressive resolution, SH-band unlocks, the growth
    /// window and refine gating — becomes a function of elapsed/budget
    /// instead of iter/total-steps, so a complete schedule runs inside the
    /// budget at whatever step rate the machine sustains. Training stops
    /// (after a final eval/export) once the budget elapses. Should exceed
    /// --total-steps in practice: set total-steps high and let the budget
    /// cut the run short.
    #[arg(long, help_heading = "Training options")]
    pub budget_s: Option<f64>,

    /// Progressive resolution (DashGaussian-style coarse-to-fine): start
    /// training at 1/`res_start_div` of full resolution and halve the
    /// divisor at even intervals, reaching full resolution at
    /// `res_full_at` of the schedule. Must be a power of two; 1 disables.
    #[arg(
        long,
        help_heading = "Training options",
        default_value = "1",
        value_parser = clap::value_parser!(u32).range(1..=8)
    )]
    pub res_start_div: u32,

    /// Fraction of the schedule at which progressive resolution reaches
    /// full resolution.
    #[arg(long, help_heading = "Training options", default_value = "0.6")]
    pub res_full_at: f32,

    /// Sparse (visible-only) Adam: update only the parameters of splats
    /// that contributed pixels to the current view (Taming-3DGS style).
    /// Invisible splats get neither a parameter update nor momentum decay,
    /// and the step only touches visible-row tensors.
    #[arg(long, help_heading = "Training options", default_value = "false")]
    pub sparse_adam: bool,

    /// SH band unlock points as schedule fractions: `sh_unlock_at[d]` is
    /// the fraction at which SH degree d+1 unlocks (the DC band is always
    /// live). E.g. for `--sh-degree 1`, `--sh-unlock-at 0.4` runs
    /// degree 0 for the first 40% then enables degree 1. Lists shorter
    /// than the max degree repeat the last value; empty unlocks
    /// everything from step 0 (previous behaviour).
    #[arg(long, help_heading = "Training options", value_delimiter = ',')]
    pub sh_unlock_at: Vec<f32>,

    /// Mean-LR multiplier applied when the run is seeded from a ply
    /// (e.g. LiDAR dots): the seed already carries accurate geometry, so a
    /// lower mean LR keeps positions stable while colour/opacity learn.
    #[arg(long, help_heading = "Training options", default_value = "1.0")]
    pub seeded_mean_lr_scale: f64,

    /// Boost applied to the opacity and SH-colour LRs for the first
    /// `seeded_boost_frac` of a seeded run — geometry comes free from the
    /// seed, so the remaining budget should go to appearance.
    #[arg(long, help_heading = "Training options", default_value = "1.0")]
    pub seeded_early_boost: f64,

    /// Fraction of the schedule `seeded_early_boost` applies to.
    #[arg(long, help_heading = "Training options", default_value = "0.15")]
    pub seeded_boost_frac: f32,

    /// Number of views per training step (multi-view batching): >1
    /// renders that many views each step and takes one optimizer step on
    /// the mean loss. Gradient estimates improve; per-step cost scales
    /// roughly linearly.
    #[arg(
        long,
        help_heading = "Training options",
        default_value = "1",
        value_parser = clap::value_parser!(u32).range(1..=16)
    )]
    pub views_per_step: u32,
}

impl Default for TrainConfig {
    fn default() -> Self {
        Self::parse_from([""])
    }
}

impl TrainConfig {
    pub fn total_iters(&self) -> u32 {
        self.total_train_iters + self.lod_levels * self.lod_refine_steps
    }
}
