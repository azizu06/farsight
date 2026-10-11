use std::f32::consts::FRAC_1_SQRT_2;

use crate::{
    adam_scaled::{AdamScaled, AdamState},
    appearance::AppearanceBank,
    config::{RefineMode, TrainConfig},
    msg::{RefineStats, TrainStepStats},
    multinomial::multinomial_sample,
    quat_vec::quaternion_vec_multiply,
    splat_init::bounds_from_pos,
    stats::RefineRecord,
};
use brush_dataset::scene::SceneBatch;
use brush_loss::{ImageLossConfig, image_loss};
use brush_render::bwd::render_splats;
use brush_render::gaussian_splats::Splats;
use brush_render::{AlphaMode, bounding_box::BoundingBox};
use burn::{
    module::Param,
    tensor::{
        Bool, Device, Distribution, Gradients, IndexingUpdateOp::Assign, Int, Tensor, TensorData,
        activation::sigmoid, s,
    },
};

use hashbrown::HashSet;
use rand::SeedableRng;
use tracing::{Instrument, trace_span};
use web_time::Instant;

pub const BOUND_PERCENTILE: f32 = 0.8;

const MIN_OPACITY: f32 = 1.0 / 255.0;

/// Fraction of training after which the Mip-Splatting 3D-filter floor stops
/// being recomputed and is held frozen (still applied), so splats settle
/// against a fixed target instead of chasing a moving floor.
const MIN_SCALE_FREEZE_FRAC: f32 = 0.9;

/// The three per-parameter Adam states of a [`Splats`] module, owned directly
/// so the trainer can update LR scaling every step and surgically edit the
/// momentum tensors during refine — all GPU-side, no record round-trips.
struct SplatOptim {
    adam: AdamScaled,
    transforms: AdamState<2>,
    sh_coeffs: AdamState<3>,
    opacities: AdamState<1>,
}

/// EMA of the three param tensors (inner backend). `None` entries mean the
/// state hasn't been (re)anchored since the last refine.
struct EmaState {
    decay: f64,
    transforms: Option<Tensor<2>>,
    sh_coeffs: Option<Tensor<3>>,
    raw_opacities: Option<Tensor<1>>,
}

/// Step one parameter: pull its gradient, run Adam on the inner
/// (autodiff-free) tensor, and re-wrap tracking. Parameters without a
/// gradient this step are left untouched. When `vis_inds` is given, only
/// those rows are gathered and stepped (sparse/visible-only Adam).
fn step_param<const D: usize>(
    adam: &AdamScaled,
    lr: f64,
    param: Param<Tensor<D>>,
    state: &mut AdamState<D>,
    grads: &mut Gradients,
    grad_sq_mean: Option<Tensor<D>>,
    vis_inds: Option<&Tensor<1, Int>>,
) -> Param<Tensor<D>> {
    param.map(|t| {
        let Some(grad) = t.grad_remove(grads) else {
            return t;
        };
        let inner = t.inner();
        let stepped = match vis_inds {
            Some(inds) => adam.step_sparse(lr, &inner, &grad, inds, grad_sq_mean, state),
            None => adam.step(lr, inner, &grad, grad_sq_mean, state),
        };
        Tensor::from_inner(stepped).require_grad()
    })
}

pub struct SplatTrainer {
    config: TrainConfig,
    /// Per-column LR scales for `transforms` (`means(3) + rotations(4) +
    /// log_scales(3)`) with the mean columns zeroed, and a mask of those
    /// columns: the scheduled mean LR is mixed in on the device each step so
    /// the optimizer never waits on a host upload.
    lr_scaling_fixed: Tensor<2>,
    lr_mean_columns: Tensor<2>,
    refine_record: Option<RefineRecord>,
    optim: Option<SplatOptim>,
    ssim_enabled: bool,
    bounds: BoundingBox,
    step_count: u32,
    max_sh_degree: u32,
    rng: rand::rngs::StdRng,
    /// Per-train-view (world center, focal in px at native res) for the
    /// Mip-Splatting 3D filter. Empty disables it. The floor itself lives on
    /// the splats (recomputed at each refine), not here.
    view_cams: Vec<(glam::Vec3, f32)>,
    /// `budget` mode: the splat count when the growth window opened — the
    /// start of the target ramp. Lazily set on first use.
    growth_anchor: Option<u32>,
    /// Run-lifetime accumulated blend weight per splat:
    /// `Σ_steps Σ_views visible·opacity·radius²`. Unlike
    /// [`RefineRecord::contrib`] (reset every refine window) this survives
    /// the whole run — the importance ranking for the export prune. Kept
    /// aligned by `prune_points`; new children start at 0.
    contrib_lifetime: Option<Tensor<1>>,
    /// Wall-clock start of the training loop (set on the first `step` so
    /// dataset loading doesn't count against the budget). Drives every
    /// schedule when `--budget-s` is set.
    train_start: Option<Instant>,
    /// True when the run is ply-seeded (LiDAR init): enables the
    /// `seeded_*` LR tweaks.
    seeded: bool,
    /// Base per-coeff SH LR scaling `[1, C, 1]` (DC = 1, higher bands
    /// `1/lr_coeffs_sh_scale`), built once the SH width is known.
    sh_scale_base: Option<Tensor<3>>,
    /// Currently installed SH scaling: `(locked_band_mask, tensor)` —
    /// rebuilt only when the set of locked bands changes.
    sh_scale_cur: Option<(u64, Tensor<3>)>,
    /// EMA of the params, evaluated/exported instead of the raw params when
    /// `--ema-decay` > 0. Re-anchored at every refine (row identities
    /// change), so its effective window is the time since the last refine.
    ema: Option<EmaState>,
    /// Per-view appearance affine bank (T6); see `appearance.rs`.
    appearance: AppearanceBank,
    #[cfg(not(target_family = "wasm"))]
    lpips: Option<lpips::LpipsModel>,
}

/// Which SH band (degree) a flattened coefficient index belongs to:
/// 0 → DC, 1..4 → degree 1, 4..9 → degree 2, 9..16 → degree 3, ...
fn sh_band_of_coeff(c: usize) -> u32 {
    // Band boundaries are the squares: coeff c sits in degree floor(sqrt(c)).
    (c as f64).sqrt() as u32
}

/// Box-downscale a packed `[h, w]` u32 image by an integer divisor on the
/// host (each entry packs `[r g b a]` u8 little-endian). Averages each
/// div×div block per channel; the right/bottom partial block is cropped.
fn downscale_packed(data: &TensorData, div: usize) -> TensorData {
    debug_assert!(div > 1);
    let (h, w) = (data.shape[0], data.shape[1]);
    let (nh, nw) = (h / div, w / div);
    let src = data.as_slice::<i32>().expect("packed image must be i32");
    let area = (div * div) as u32;
    let round = area / 2;
    let mut out = vec![0i32; nh * nw];
    for y in 0..nh {
        let row = y * div * w;
        for x in 0..nw {
            let mut acc = [0u32; 4];
            let base = row + x * div;
            for dy in 0..div {
                for dx in 0..div {
                    let p = src[base + dy * w + dx] as u32;
                    acc[0] += p & 0xff;
                    acc[1] += (p >> 8) & 0xff;
                    acc[2] += (p >> 16) & 0xff;
                    acc[3] += (p >> 24) & 0xff;
                }
            }
            out[y * nw + x] = (((acc[0] + round) / area)
                | (((acc[1] + round) / area) << 8)
                | (((acc[2] + round) / area) << 16)
                | (((acc[3] + round) / area) << 24)) as i32;
        }
    }
    TensorData::new(out, [nh, nw])
}

fn inv_sigmoid(x: Tensor<1>) -> Tensor<1> {
    (x.clone() / (1.0f32 - x)).log()
}

/// Per-splat world-space scale floor for the Mip-Splatting 3D filter:
/// `f_i = sqrt(factor) · min_v(||mean_i - cam_v|| / focal_px_v)`. `means` and
/// the result are on the inner (non-autodiff) backend; `f` is a frozen
/// constant. Returns `None` if disabled or there are no cameras.
fn compute_min_scale(
    means: &Tensor<2>,
    view_cams: &[(glam::Vec3, f32)],
    factor: f32,
) -> Option<Tensor<1>> {
    if factor <= 0.0 || view_cams.is_empty() {
        return None;
    }
    let device = means.device();
    let n = means.dims()[0] as i32;

    let mut min_ratio: Option<Tensor<1>> = None;
    for (center, focal) in view_cams {
        let c = Tensor::<1>::from_floats([center.x, center.y, center.z], &device).reshape([1, 3]);
        let diff = means.clone() - c;
        let dist = diff.clone().mul(diff).sum_dim(1).sqrt().reshape([n]);
        let ratio = dist.div_scalar(focal.max(1e-6));
        min_ratio = Some(match min_ratio {
            Some(m) => m.min_pair(ratio),
            None => ratio,
        });
    }
    min_ratio.map(|r| r.mul_scalar(factor.sqrt()))
}

pub async fn get_splat_bounds(splats: Splats, percentile: f32) -> BoundingBox {
    let means: Vec<f32> = splats
        .means()
        .into_data_async()
        .await
        .expect("Failed to fetch splat data")
        .try_to_vec()
        .expect("Failed to get means");
    bounds_from_pos(percentile, &means)
}

impl SplatTrainer {
    pub fn new(config: &TrainConfig, device: &Device, bounds: BoundingBox) -> Self {
        Self::new_seeded(config, device, bounds, 42)
    }

    pub fn new_seeded(
        config: &TrainConfig,
        device: &Device,
        bounds: BoundingBox,
        seed: u64,
    ) -> Self {
        let ssim_enabled = config.ssim_weight > 0.0;

        // Optimizer state lives on the inner device.
        let opt_device = device.clone().inner();
        let (rot, scale) = (config.lr_rotation as f32, config.lr_scale as f32);
        let lr_scaling_fixed = Tensor::<1>::from_floats(
            [0.0, 0.0, 0.0, rot, rot, rot, rot, scale, scale, scale],
            &opt_device,
        )
        .reshape([1, 10]);
        let lr_mean_columns = Tensor::<1>::from_floats(
            [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            &opt_device,
        )
        .reshape([1, 10]);

        // Growth is gated on the global iter. LOD phases run past
        // total_train_iters but their refines should never grow — clamp
        // here so growth_stop is never effectively past end-of-training,
        // and growth_start never past growth_stop.
        let mut config = config.clone();
        config.growth_stop_iter = config.growth_stop_iter.min(config.total_train_iters);
        config.growth_start_iter = config.growth_start_iter.min(config.growth_stop_iter);
        config.apply_quality();

        let (app_lr, app_reg) = (config.appearance_lr, config.appearance_reg);
        let ema_decay = config.ema_decay;
        #[cfg(not(target_family = "wasm"))]
        let lpips = (config.lpips_loss_weight > 0.0).then(|| lpips::load_vgg_lpips(device));

        Self {
            config,
            lr_scaling_fixed,
            lr_mean_columns,
            optim: None,
            refine_record: None,
            contrib_lifetime: None,
            ssim_enabled,
            bounds,
            step_count: 0,
            max_sh_degree: 0,
            rng: rand::rngs::StdRng::seed_from_u64(seed),
            view_cams: Vec::new(),
            growth_anchor: None,
            train_start: None,
            seeded: false,
            sh_scale_base: None,
            sh_scale_cur: None,
            ema: (ema_decay > 0.0).then_some(EmaState {
                decay: ema_decay,
                transforms: None,
                sh_coeffs: None,
                raw_opacities: None,
            }),
            appearance: AppearanceBank::new(app_lr, app_reg),
            #[cfg(not(target_family = "wasm"))]
            lpips,
        }
    }

    /// Percentile bounding box of the splats, refreshed on each refine.
    pub fn bounds(&self) -> BoundingBox {
        self.bounds
    }

    /// Supply per-train-view (world center, focal-px at native res) to enable
    /// the Mip-Splatting 3D filter (gated on `config.min_scale_factor > 0`).
    pub fn set_view_cams(&mut self, view_cams: Vec<(glam::Vec3, f32)>) {
        self.view_cams = view_cams;
    }

    /// `budget` mode: the splat-count target at `iter` — the count ramped from
    /// `growth_anchor` (the count when the ramp started, lazily pinned to the
    /// first call's count) to `growth_target_splats` (default `max_splats`)
    /// across `[growth_start_iter, growth_stop_iter)`. The `p^0.35` shape
    /// front-loads the change: ~78% of the ramp completes in the first half of
    /// the window, so an over-budget dense seed is paid down early and a
    /// growing cloud reaches working size quickly.
    fn growth_target_at(&mut self, iter: u32, cur: u32) -> u32 {
        let anchor = *self.growth_anchor.get_or_insert(cur);
        let target = self
            .config
            .growth_target_splats
            .unwrap_or(self.config.max_splats) as f32;
        let start = self.config.growth_start_iter;
        let stop = self.config.growth_stop_iter.max(start + 1);
        let p = ((iter.saturating_sub(start)) as f32 / (stop - start) as f32)
            .clamp(0.0, 1.0)
            .powf(0.35);
        (anchor as f32 + (target - anchor as f32) * p)
            .round()
            .max(1.0) as u32
    }

    /// The run-lifetime accumulated blend weight per splat
    /// (`Σ_steps Σ_views visible·opacity·radius²`) — clone of the live
    /// tensor, for callers that want the export-prune score.
    pub fn contrib_lifetime(&self) -> Option<Tensor<1>> {
        self.contrib_lifetime.clone()
    }

    /// Final-quality prune to `target` splats (e.g. the 300k Quest export
    /// budget): drops the lowest run-lifetime blend-weight splats and keeps
    /// optimizer, refine stats and momentum aligned so training can continue
    /// (a short fine-tune settles the survivors). Ranked by
    /// `contrib_lifetime`; falls back to opacity·area early in a run.
    pub async fn quest_prune(&mut self, splats: Splats, target: u32) -> Splats {
        // Bake any live 3D-filter floor into the params before touching row
        // counts (refine() does the same): `min_scale` is a per-splat [N]
        // tensor the map below would otherwise leave stale.
        let splats = splats.bake_min_scale();
        let n = splats.num_splats();
        if n <= target {
            return splats;
        }
        let device = splats.device();
        let mut optim = self
            .optim
            .take()
            .expect("Can only quest-prune after optimizer is initialized");
        let refiner = self
            .refine_record
            .take()
            .unwrap_or_else(|| RefineRecord::new(n, &device.clone().inner()));

        let score = self.contrib_lifetime.clone().unwrap_or_else(|| {
            splats.opacities()
                * splats
                    .scales()
                    .powi_scalar(2)
                    .sum_dim(1)
                    .squeeze_dim::<1>(1)
        });
        let drop = lowest_contrib_inds(&score, None, n - target).await;
        let keep = keep_inds_tensor(n, &drop, &device);
        let (splats, refiner, _dropped) = select_points(
            splats,
            &mut optim,
            refiner,
            keep,
            self.contrib_lifetime.as_mut(),
        )
        .await;

        self.optim = Some(optim);
        self.refine_record = Some(refiner);
        splats
    }

    /// Mark the run as ply-seeded (LiDAR init): enables the `seeded_*`
    /// config tweaks (lower mean LR, early opacity/colour boost).
    pub fn set_seeded(&mut self, seeded: bool) {
        self.seeded = seeded;
    }

    /// EMA step on the inner param tensors; re-anchors (copies the current
    /// params) whenever a count changed or the state is empty.
    fn ema_update(&mut self, splats: &Splats) {
        let Some(ema) = &mut self.ema else { return };
        let d = ema.decay;
        let t = splats.transforms.val().inner();
        if ema.transforms.as_ref().is_some_and(|m| m.dims() == t.dims()) {
            let m = ema.transforms.take().unwrap();
            ema.transforms = Some(m * d + t * (1.0 - d));
        } else {
            ema.transforms = Some(t);
        }
        let c = splats.sh_coeffs.val().inner();
        if ema.sh_coeffs.as_ref().is_some_and(|m| m.dims() == c.dims()) {
            let m = ema.sh_coeffs.take().unwrap();
            ema.sh_coeffs = Some(m * d + c * (1.0 - d));
        } else {
            ema.sh_coeffs = Some(c);
        }
        let o = splats.raw_opacities.val().inner();
        if ema.raw_opacities.as_ref().is_some_and(|m| m.dims() == o.dims()) {
            let m = ema.raw_opacities.take().unwrap();
            ema.raw_opacities = Some(m * d + o * (1.0 - d));
        } else {
            ema.raw_opacities = Some(o);
        }
    }

    /// The splats to eval/export: the EMA copy when `--ema-decay` is on and
    /// anchored at the current count, else the raw params unchanged.
    pub fn ema_splats(&self, splats: &Splats) -> Splats {
        let Some(e) = &self.ema else { return splats.clone() };
        let (Some(t), Some(c), Some(o)) = (
            e.transforms.clone(),
            e.sh_coeffs.clone(),
            e.raw_opacities.clone(),
        ) else {
            return splats.clone();
        };

        if t.dims() != splats.transforms.dims() {
            return splats.clone();
        }
        let mut out = splats.clone();
        out.transforms = out.transforms.map(|_| t);
        out.sh_coeffs = out.sh_coeffs.map(|_| c);
        out.raw_opacities = out.raw_opacities.map(|_| o);
        out
    }

    /// Fraction of the schedule completed: wall-clock `elapsed/budget`
    /// when `--budget-s` is set, else `iter/total_train_iters`. Every
    /// schedule (LR decay, progressive res, SH unlock, growth window,
    /// refine gating) keys off this so a complete schedule always runs
    /// inside whatever budget the caller set.
    pub fn progress(&self, iter: u32) -> f32 {
        match (self.config.budget_s, self.train_start) {
            (Some(budget), Some(start)) if budget > 0.0 => {
                (start.elapsed().as_secs_f64() / budget).clamp(0.0, 1.0) as f32
            }
            _ => (iter as f32 / self.config.total_train_iters.max(1) as f32).clamp(0.0, 1.0),
        }
    }

    /// True once `--budget-s` has elapsed. The caller should run a final
    /// eval/export and stop. Always false without a budget.
    pub fn budget_exhausted(&self) -> bool {
        matches!(
            (self.config.budget_s, self.train_start),
            (Some(budget), Some(start))
                if budget > 0.0 && start.elapsed().as_secs_f64() >= budget
        )
    }

    /// Resolution divisor for the current schedule point: `res_start_div`
    /// at progress 0, halving at even intervals until full resolution at
    /// `res_full_at` (DashGaussian-style coarse-to-fine).
    fn res_div(&self, progress: f32) -> usize {
        let start = self.config.res_start_div;
        if start <= 1 || !start.is_power_of_two() || progress >= self.config.res_full_at {
            return 1;
        }
        let levels = start.trailing_zeros();
        let done = (progress / self.config.res_full_at.max(1e-6) * levels as f32) as u32;
        (start >> done.min(levels)) as usize
    }

    /// Mask of locked SH coefficients (bit c set = coeff c locked) for the
    /// current progress under `--sh-unlock-at`. 0 = all bands live.
    fn locked_sh_bits(&self, num_coeffs: usize, progress: f32) -> u64 {
        let unlock = &self.config.sh_unlock_at;
        if unlock.is_empty() {
            return 0;
        }
        let mut bits = 0u64;
        for c in 1..num_coeffs.min(64) {
            let band = sh_band_of_coeff(c);
            let at = unlock
                .get(band as usize - 1)
                .or_else(|| unlock.last())
                .copied()
                .unwrap_or(0.0);
            if progress < at {
                bits |= 1 << c;
            }
        }
        bits
    }

    /// One optimizer step over a single view (see [`Self::step_multi`]).
    pub async fn step(&mut self, batch: SceneBatch, splats: Splats) -> (Splats, TrainStepStats) {
        self.step_multi(std::slice::from_ref(&batch), splats).await
    }

    /// One optimizer step over `batches` views (multi-view batching):
    /// renders each view, averages the losses, takes one optimizer step.
    /// Refine stats accumulate per view, as if the views had arrived as
    /// separate steps.
    pub async fn step_multi(
        &mut self,
        batches: &[SceneBatch],
        splats: Splats,
    ) -> (Splats, TrainStepStats) {
        let mut splats = splats;

        // Track max SH degree from the first splats we see.
        if self.step_count == 0 {
            self.max_sh_degree = splats.sh_degree();
            self.train_start = Some(Instant::now());
        }
        self.step_count += 1;
        // Progress at the START of this step drives every schedule below.
        let progress = self.progress(self.step_count - 1);
        let res_div = self.res_div(progress);

        let device = splats.device();
        let base = &self.config.background_color;
        let base_bg = glam::Vec3::new(base[0], base[1], base[2]);
        let background = sample_background_color(
            base_bg,
            self.config.background_noise_strength,
            &mut self.rng,
        );

        let median_scale = self.bounds.median_size();

        // Mean LR: exponential decay over the schedule fraction, scaled by
        // the (percentile) scene size. Under a ply seed, `seeded_mean_lr_scale`
        // keeps the already-accurate positions stable.
        let lr_mean = self.config.lr_mean
            * (self.config.lr_mean_end / self.config.lr_mean).powf(progress as f64)
            * median_scale as f64
            * if self.seeded {
                self.config.seeded_mean_lr_scale
            } else {
                1.0
            };
        // Seeded runs get geometry for free; spend the early schedule on
        // appearance (opacity + SH colour).
        let early_boost = if self.seeded && progress < self.config.seeded_boost_frac {
            self.config.seeded_early_boost
        } else {
            1.0
        };

        // Render + loss per view. GT stays on the GPU as packed `[H, W]`
        // u32 (RGBA u8); all mixing (bg compositing, alpha matching, mask)
        // is folded into the loss kernels. Under progressive res the packed
        // GT is box-downscaled on the host and the camera renders fewer
        // pixels (FOV intrinsics auto-scale to `img_size`).
        let n_views = batches.len() as f32;
        let mut loss: Option<Tensor<1>> = None;
        let mut aux_visible: Option<Tensor<1>> = None;
        let mut aux_opacities: Option<Tensor<1>> = None;
        let mut aux_max_radius: Option<Tensor<1>> = None;
        let mut contrib_delta: Option<Tensor<1>> = None;
        let mut coeffs_grad_sq_sum: Option<Tensor<3>> = None;
        let mut refine_weights: Vec<Tensor<1>> = Vec::with_capacity(batches.len());
        let mut counts_sum: Option<Tensor<1, Int>> = None;
        // Per-view counts rows, cat'd for a single deferred readback — the
        // row-max of counts[2] (true isect total) sizes next step's isect
        // capacity (see brush_render::report_isect_total).
        let mut counts_rows: Option<Tensor<1, Int>> = None;

        for batch in batches {
            let [img_h, img_w] = batch.img_size();
            // GT is pure data — never differentiated. Build it on the inner
            // backend so it doesn't inherit the autodiff device's residual
            // checkpointing flag (the LPIPS `unpack_gt_rgb` path, via
            // `unwrap_wgpu_int`, expects a clean Wgpu tensor).
            // At full res reuse the prefetcher's GPU-resident image when
            // present (skips the host→device copy); for the coarse phase a
            // host box-downscale of the packed GT is higher quality than a
            // strided GPU subsample.
            let (gt_packed, img_size) = if res_div > 1 {
                (
                    Tensor::<2, Int>::from_data(
                        downscale_packed(&batch.img_packed, res_div),
                        &device.clone().inner(),
                    ),
                    glam::uvec2((img_w / res_div) as u32, (img_h / res_div) as u32),
                )
            } else {
                (
                    match batch.img_gpu.clone() {
                        Some(img) => img,
                        None => Tensor::<2, Int>::from_data(
                            batch.img_packed.clone(),
                            &device.clone().inner(),
                        ),
                    },
                    glam::uvec2(img_w as u32, img_h as u32),
                )
            };

            let has_alpha = batch.has_alpha;
            let masked_alpha = batch.alpha_mode == AlphaMode::Masked;
            let do_alpha_match = has_alpha && !masked_alpha && self.config.match_alpha_weight > 0.0;

            // The splats already carry their 3D-filter floor (set at refine);
            // the render path folds it in. Optimizer/refine work on raw params.
            let diff_out = render_splats(splats.clone(), &batch.camera, img_size, background)
                .instrument(trace_span!("Forward"))
                .await;

            let pred_image = diff_out.img;
            // Per-view appearance affine (T6): active after the warm-up
            // fraction; the bank's params stay at identity before that.
            let app_on = self.appearance.enabled()
                && progress >= self.config.appearance_warmup_frac;
            let pred_image = if app_on {
                self.appearance
                    .apply(batch.view_index, pred_image, &device)
            } else {
                pred_image
            };
            let refine_weight_holder = diff_out.refine_weight_holder;
            counts_sum = Some(match counts_sum {
                Some(c) => c + diff_out.counts.clone(),
                None => diff_out.counts.clone(),
            });
            counts_rows = Some(match counts_rows {
                Some(r) => Tensor::cat(vec![r, diff_out.counts], 0),
                None => diff_out.counts,
            });

            // RGB loss is `(1 - w) * L1 + (-w) * SSIM` per pixel. Bg
            // compositing always runs in the kernel; for synthesised opaque
            // alpha or zero bg it's a no-op. Mask multiplies the loss-map
            // by `gt.a`; for synthesised opaque alpha that's a no-op too.
            // Alpha matching needs a real alpha source (synthesised
            // a = 1 would pull predicted alpha to fully opaque); we feed
            // `pred` with 4 channels and the kernel's `c == 3` workgroup
            // emits `|pred.a - gt.a|` into the alpha channel.
            let (l1_w, ssim_w) = if self.ssim_enabled {
                (1.0 - self.config.ssim_weight, -self.config.ssim_weight)
            } else {
                (1.0, 0.0)
            };
            // Only composite when there's a real alpha channel and a non-zero
            // bg to mix in; the kernel skips the per-pixel `(1-a)*bg` math
            // entirely when this is None.
            let composite_bg = (has_alpha && background != glam::Vec3::ZERO).then_some(background);
            let cfg = ImageLossConfig {
                l1_weight: l1_w,
                ssim_weight: ssim_w,
                composite_bg,
                mask: masked_alpha,
                alpha_weight: if do_alpha_match {
                    self.config.match_alpha_weight
                } else {
                    0.0
                },
            };
            // The kernel takes the RGBA image as rendered.
            // `loss` is only reassigned by the LPIPS path below, which is
            // compiled out on wasm — so `mut` is unused there.
            #[cfg_attr(target_family = "wasm", allow(unused_mut))]
            let mut view_loss = image_loss(pred_image.clone(), gt_packed.clone(), cfg);

            // LPIPS still needs an f32 RGB tensor for VGG. Materialising it
            // here costs ~99 MB at 4K, only when LPIPS is enabled.
            #[cfg(not(target_family = "wasm"))]
            if let Some(lpips) = &self.lpips {
                let gt_rgb = brush_loss::unpack_gt_rgb(gt_packed.clone(), composite_bg);
                let gt_rgb_diff: Tensor<3> = Tensor::from_inner(gt_rgb);
                view_loss = view_loss
                    + lpips.lpips(
                        pred_image.clone().slice(s![.., .., 0..3]).unsqueeze_dim(0),
                        gt_rgb_diff.unsqueeze_dim(0),
                    ) * self.config.lpips_loss_weight;
            }

            if app_on {
                view_loss = view_loss
                    + self.appearance.penalty(batch.view_index, &device);
            }

            loss = Some(match loss {
                Some(l) => l + view_loss,
                None => view_loss,
            });
            // Run-lifetime blend weight: per-view Σ visible·opacity·radius² —
            // the export-prune importance score. Cloned; the unions below
            // move `diff_out`'s fields.
            contrib_delta = Some(match contrib_delta {
                Some(c) => {
                    c + diff_out.visible.clone()
                        * diff_out.opacities.clone()
                        * diff_out.max_radius.clone().powi_scalar(2)
                }
                None => {
                    diff_out.visible.clone()
                        * diff_out.opacities.clone()
                        * diff_out.max_radius.clone().powi_scalar(2)
                }
            });
            // Aux merges: union of contributing splats across views.
            aux_visible = Some(match aux_visible {
                Some(v) => v.max_pair(diff_out.visible),
                None => diff_out.visible,
            });
            aux_opacities = Some(match aux_opacities {
                Some(o) => o.max_pair(diff_out.opacities),
                None => diff_out.opacities,
            });
            aux_max_radius = Some(match aux_max_radius {
                Some(r) => r.max_pair(diff_out.max_radius),
                None => diff_out.max_radius,
            });
            coeffs_grad_sq_sum = Some(match coeffs_grad_sq_sum {
                Some(c) => c + diff_out.coeffs_grad_sq_holder,
                None => diff_out.coeffs_grad_sq_holder,
            });
            refine_weights.push(refine_weight_holder);
        }
        let loss = loss.expect("at least one view") / n_views;
        let visible = aux_visible.expect("at least one view");
        let opacities = aux_opacities.expect("at least one view");
        let max_radius = aux_max_radius.expect("at least one view");
        let coeffs_grad_sq_holder = coeffs_grad_sq_sum.expect("at least one view") / n_views;
        let counts = counts_sum.expect("at least one view");
        let counts_rows = counts_rows.expect("at least one view");

        // Accumulate into the run-lifetime blend weight (pruned/relocated
        // rows are zeroed elsewhere so indices stay aligned).
        let delta = contrib_delta.expect("at least one view");
        self.contrib_lifetime = Some(match self.contrib_lifetime.take() {
            Some(c) => c + delta,
            None => delta,
        });

        // Strip the autodiff graph off the loss so consumers can read the
        // scalar later without keeping the backward pass alive.
        let loss_inner = loss.clone().inner();
        let mut grads = splats.bwd_validate(loss).await;
        self.appearance.step(&mut grads);

        let coeffs_grad_sq = coeffs_grad_sq_holder
            .grad_remove(&mut grads)
            .map(Tensor::without_autodiff);

        let refine_weight_list: Vec<Tensor<1>> = refine_weights
            .into_iter()
            .map(|h| {
                h.grad_remove(&mut grads)
                    .expect("XY gradients need to be calculated.")
                    .without_autodiff()
            })
            .collect();

        // The optimizer runs with base LR 1 and these per-column scales.
        let lr_scaling =
            self.lr_scaling_fixed.clone() + self.lr_mean_columns.clone() * lr_mean as f32;

        // The optimizer strips autodiff before stepping, so optimizer state
        // (scaling, momentum) lives on the inner device.
        let opt_device = device.clone().inner();

        // Sparse Adam: indices of the splats that contributed pixels this
        // step (union over the batch's views). One small D2H sync per step;
        // on unified-memory GPUs the indices are nearly free.
        let vis_inds = if self.config.sparse_adam {
            Some(if std::env::var_os("BRUSH_SPARSE_ALL").is_some() {
                Tensor::arange(0..(splats.transforms.dims()[0] as i64), &opt_device)
            } else {
                visible
                    .clone()
                    .greater_elem(0.5)
                    .argwhere_async()
                    .await
                    .squeeze_dim::<1>(1)
            })
        } else {
            None
        };
        let vis_inds = vis_inds.as_ref();

        // SH scaling = base per-band scale × unlock mask. Rebuilt only when
        // the set of locked bands changes (rare — once per unlock point).
        let num_coeffs = splats.sh_coeffs.dims()[1];
        let locked_bits = self.locked_sh_bits(num_coeffs, progress);
        let sh_scaling = match &self.sh_scale_cur {
            Some((bits, t)) if *bits == locked_bits => t.clone(),
            _ => {
                let base = self.sh_scale_base.get_or_insert_with(|| {
                    let mut scales = vec![1.0f32; num_coeffs];
                    let rest_scale = 1.0 / self.config.lr_coeffs_sh_scale;
                    for s in &mut scales[1..] {
                        *s = rest_scale;
                    }
                    Tensor::<1>::from_floats(scales.as_slice(), &opt_device)
                        .reshape([1, num_coeffs as i32, 1])
                });
                let t = if locked_bits == 0 {
                    base.clone()
                } else {
                    let mask: Vec<f32> = (0..num_coeffs)
                        .map(|c| {
                            if locked_bits & (1u64 << c) == 0 {
                                1.0
                            } else {
                                0.0
                            }
                        })
                        .collect();
                    base.clone()
                        * Tensor::<1>::from_floats(mask.as_slice(), &opt_device)
                            .reshape([1, num_coeffs as i32, 1])
                };
                self.sh_scale_cur = Some((locked_bits, t.clone()));
                t
            }
        };

        let optimizer =
            self.optim.get_or_insert_with(|| {
                SplatOptim {
                    adam: AdamScaled::new(1e-15),
                    transforms: AdamState::new(None, false),
                    // `scaling` is filled in per step above (SH unlock mask).
                    sh_coeffs: AdamState::new(None, true),
                    opacities: AdamState::new(None, false),
                }
            });
        optimizer.transforms.scaling = Some(lr_scaling);
        optimizer.sh_coeffs.scaling = Some(sh_scaling);

        let lr_coeffs = self.config.lr_coeffs_dc * early_boost;
        let lr_opac = self.config.lr_opac * early_boost;

        splats = trace_span!("Optimizer step").in_scope(|| {
            splats.transforms = trace_span!("Transforms step").in_scope(|| {
                step_param(
                    &optimizer.adam,
                    1.0,
                    splats.transforms,
                    &mut optimizer.transforms,
                    &mut grads,
                    None,
                    vis_inds,
                )
            });
            splats.sh_coeffs = trace_span!("SH Coeffs step").in_scope(|| {
                step_param(
                    &optimizer.adam,
                    lr_coeffs,
                    splats.sh_coeffs,
                    &mut optimizer.sh_coeffs,
                    &mut grads,
                    coeffs_grad_sq,
                    vis_inds,
                )
            });
            splats.raw_opacities = trace_span!("Opacity step").in_scope(|| {
                step_param(
                    &optimizer.adam,
                    lr_opac,
                    splats.raw_opacities,
                    &mut optimizer.opacities,
                    &mut grads,
                    None,
                    vis_inds,
                )
            });
            splats
        });

        trace_span!("Housekeeping").in_scope(|| {
            // Refine state accumulates on the inner (non-autodiff) device.
            // Kept after the optimizer so it doesn't sit between the
            // backward's gradient gathers and the step that consumes them.
            let device = splats.device().inner();
            let record = self
                .refine_record
                .get_or_insert_with(|| RefineRecord::new(splats.num_splats(), &device));
            for w in refine_weight_list {
                record.gather_stats(
                    w,
                    visible.clone(),
                    max_radius.clone(),
                    opacities.clone(),
                );
            }
        });

        // Noise uses the forward's floored opacities, without building an autodiff graph.
        let samples = Tensor::random(
            [splats.num_splats() as usize, 3],
            Distribution::Normal(0.0, 1.0),
            &splats.device().inner(),
        );

        splats.transforms = splats.transforms.map(|t| {
            let inner = t.inner();
            // Resolve random generation and views before the arithmetic so the
            // gate through means addition can fuse as one rank-2 expression.
            let means = inner.clone().slice(s![.., 0..3]);
            let opacities = opacities.unsqueeze_dim::<2>(1);
            let visible = visible.unsqueeze_dim::<2>(1);
            let inv_opac: Tensor<2> = 1.0 - opacities;
            let noise_weight = inv_opac.powi_scalar(150.0).clamp(0.0, 1.0) * visible;
            let noise_weight_means =
                noise_weight * (lr_mean as f32 * self.config.mean_noise_weight);
            let noise_m = (samples * noise_weight_means).clamp(-median_scale, median_scale);
            let noised_means = means + noise_m;
            let out = inner.slice_assign(s![.., 0..3], noised_means);
            Tensor::from_inner(out).require_grad()
        });

        // splat-turbo T7: the only remaining per-step GPU->CPU read. It is
        // deferred to here — every kernel for this step (forward, loss,
        // backward, optimizer, noise) is already queued, so this wait
        // overlaps real GPU work instead of idling the pipe mid-forward.
        let counts: Vec<u32> = counts
            .into_data_async()
            .await
            .expect("counts readback")
            .try_into_vec::<u32>()
            .expect("counts dtype");
        let num_visible = (counts[0] as f32 / n_views) as u32;

        // Feed next step's isect capacity: per-view max of the unclamped
        // isect total (counts[2]); report_isect_total applies the 1.25x
        // headroom and grow-only rule. Same deferred-readback slot as
        // `counts` — zero extra mid-step syncs.
        let rows: Vec<u32> = counts_rows
            .into_data_async()
            .await
            .expect("counts rows readback")
            .try_into_vec::<u32>()
            .expect("counts dtype");
        brush_render::report_isect_total(
            rows.chunks_exact(3)
                .map(|c| c[2] as u64)
                .max()
                .unwrap_or(0),
        );

        // EMA tracks the stepped params (re-anchored after each refine).
        self.ema_update(&splats);

        let stats = TrainStepStats {
            num_visible,
            lr_mean,
            lr_rotation: self.config.lr_rotation,
            lr_scale: self.config.lr_scale,
            lr_coeffs,
            lr_opac,
            loss: loss_inner,
        };

        (splats, stats)
    }

    pub async fn refine(&mut self, iter: u32, splats: Splats) -> (Splats, RefineStats) {
        let progress = self.progress(iter);
        // Refine manipulates the canonical (un-floored) params, so bake the
        // current 3D-filter floor into them first — split/clone/prune then see
        // the splat's true scales with no double-apply. A freshly recomputed
        // floor is attached at the end (below), once positions/count are known.
        let splats = splats.bake_min_scale();
        let device = splats.device();

        let refiner = self
            .refine_record
            .take()
            .expect("Can only refine if refine stats are initialized");

        let max_allowed_bounds = self.bounds.extent.max_element() * 100.0;

        // If not refining, update splat to step with gradients applied.
        // Prune dead splats. This ALWAYS happen even if we're not "refining" anymore.
        let mut optim = self
            .optim
            .take()
            .expect("Can only refine after optimizer is initialized");
        let alpha_mask = splats.opacities().lower_elem(MIN_OPACITY);
        let scales = splats.scales();

        // Note: we do NOT cull on a minimum scale. A genuinely flat splat
        // (a thin "pancake" representing a surface) legitimately has a tiny
        // smallest axis, so there's no correct min-scale threshold — the
        // non-finite check below still removes actually-degenerate splats.
        let scale_big = scales
            .clone()
            .greater_elem(max_allowed_bounds)
            .any_dim(1)
            .squeeze_dim(1);

        // Remove splats that are way out of bounds.
        let center = self.bounds.center;
        let bound_center =
            Tensor::<1>::from_floats([center.x, center.y, center.z], &device).reshape([1, 3]);
        let splat_dists = (splats.means() - bound_center).abs();
        let bound_mask = splat_dists
            .greater_elem(max_allowed_bounds)
            .any_dim(1)
            .squeeze_dim(1);

        // Prune parameter that's NaN.
        fn row_non_finite(t: &Tensor<2>) -> Tensor<1, Bool> {
            t.clone().is_finite().bool_not().any_dim(1).squeeze_dim(1)
        }
        let transforms_bad = row_non_finite(&splats.transforms.val());
        let sh_bad = row_non_finite(&splats.sh_coeffs.val().flatten(1, 2));
        let opac_bad = row_non_finite(&splats.raw_opacities.val().unsqueeze_dim(1));
        let non_finite_mask = transforms_bad.bool_or(sh_bad).bool_or(opac_bad);
        let num_pruned_non_finite = non_finite_mask
            .clone()
            .int()
            .sum()
            .into_scalar_async::<i32>()
            .await
            .expect("Failed to count non-finite splats") as u32;

        let prune_mask = alpha_mask
            .bool_or(scale_big)
            .bool_or(bound_mask)
            .bool_or(non_finite_mask);

        let mode = self.config.refine_mode;
        let mut split_inds = HashSet::new();
        let mut num_relocated = 0u32;
        let mut num_pruned_low_contrib = 0u32;

        // Speedy-Splat contribution cull: inside the configured progress
        // window, the lowest-contribution `prune_contrib_fraction` of splats
        // also dies — relocated under `mcmc`, permanently pruned otherwise.
        let contrib_kill = if self.config.prune_contrib_fraction > 0.0
            && progress >= self.config.prune_contrib_start_frac
            && progress < self.config.prune_contrib_end_frac
        {
            (splats.num_splats() as f32 * self.config.prune_contrib_fraction).round() as u32
        } else {
            0
        };

        // HARD count cap: `growth_target_splats` when set, else `max_splats`.
        // Upstream's `max_splats` only bounded the score-growth branch — the
        // resample and oversized-split paths bypassed it (the Mac run grew
        // 300k -> 2.18M). Here every growth path is budget-checked and any
        // over-cap count is pruned at refine entry.
        let hard_cap = self
            .config
            .growth_target_splats
            .unwrap_or(self.config.max_splats)
            .min(self.config.max_splats);

        let (mut splats, refiner, pruned_count) = if mode == RefineMode::Mcmc {
            // MCMC: dead ∪ low-contribution splats are relocated onto live
            // donors — the splat count never changes.
            let mut relocate_mask = prune_mask;
            if contrib_kill > 0 {
                let extra =
                    lowest_contrib_inds(&refiner.contrib, Some(&relocate_mask), contrib_kill).await;
                if !extra.is_empty() {
                    let mask = inds_to_mask(&extra, splats.num_splats(), &splats.device().inner());
                    relocate_mask = relocate_mask.bool_or(mask);
                    num_pruned_low_contrib = extra.len() as u32;
                }
            }
            let (s, r, moved) = relocate_points(
                splats,
                &mut optim,
                refiner,
                relocate_mask,
                &mut self.rng,
                &self.config,
                self.contrib_lifetime.as_mut(),
            )
            .await;
            num_relocated = moved;
            let mut s = s;
            let mut r = r;
            // Over the hard cap even after relocation (e.g. a seed denser
            // than the cap, or oversized splits below): permanently drop
            // the lowest-contribution splats.
            let over = s.num_splats().saturating_sub(hard_cap);
            let pruned_count = if over > 0 {
                let drop = lowest_contrib_inds(&r.contrib, None, over).await;
                if drop.is_empty() {
                    0
                } else {
                    let keep = keep_inds_tensor(s.num_splats(), &drop, &s.device());
                    let (s2, r2, dropped) = select_points(
                        s,
                        &mut optim,
                        r,
                        keep,
                        self.contrib_lifetime.as_mut(),
                    )
                    .await;
                    s = s2;
                    r = r2;
                    dropped
                }
            } else {
                0
            };
            (s, r, pruned_count)
        } else {
            let (mut splats, mut refiner, pruned_count) = prune_points(
                splats,
                &mut optim,
                refiner,
                prune_mask,
                self.contrib_lifetime.as_mut(),
            )
            .await;

            // Permanent contribution removals: the Speedy-Splat fraction
            // inside the window plus any count over the mode's cap — the
            // `budget` ramp target, else the hard cap — one ranked removal,
            // never resampled.
            let mut drop_n = contrib_kill;
            let cap_now = if mode == RefineMode::Budget {
                self.growth_target_at(iter, splats.num_splats())
            } else {
                hard_cap
            };
            drop_n = drop_n.max(splats.num_splats().saturating_sub(cap_now));
            if drop_n > 0 {
                let drop = lowest_contrib_inds(&refiner.contrib, None, drop_n).await;
                if !drop.is_empty() {
                    let keep =
                        keep_inds_tensor(splats.num_splats(), &drop, &splats.device());
                    let (s, r, dropped) = select_points(
                        splats,
                        &mut optim,
                        refiner,
                        keep,
                        self.contrib_lifetime.as_mut(),
                    )
                    .await;
                    splats = s;
                    refiner = r;
                    num_pruned_low_contrib = dropped;
                }
            }
            (splats, refiner, pruned_count)
        };

        // Replace dead gaussians so the pruned budget is reused — `brush`
        // mode only (`mcmc` relocates in place; `budget` regrows to target
        // by score below).
        if mode == RefineMode::Brush && pruned_count > 0 {
            // Replacement weighting: opacity × visibility. Capped by the
            // remaining hard-cap room (upstream ignored it here).
            let room = hard_cap.saturating_sub(splats.num_splats() + split_inds.len() as u32);
            let take = pruned_count.min(room);
            if take > 0 {
                let vis_f = refiner.vis_mask().float();
                let resampled_weights = splats.opacities() * vis_f.clone();
                let resampled_weights = resampled_weights
                    .into_data_async()
                    .await
                    .expect("Failed to get weights")
                    .try_into_vec::<f32>()
                    .expect("Failed to read weights");
                let resampled_inds = multinomial_sample(&mut self.rng, &resampled_weights, take);
                split_inds.extend(resampled_inds);
            }
        }

        // Force-split splats that are too big on screen (every refine). Rather
        // than killing them (the old `kill_at_screen_size`), we split them and
        // shrink the children down to `split_at_screen_size` on screen — see
        // `refine_splats`. Capped by the remaining count budget (`max_splats`,
        // or the `budget` mode ramp target).
        let count_cap = match mode {
            RefineMode::Budget => self.growth_target_at(iter, splats.num_splats()),
            _ => hard_cap,
        };
        let pre_oversized = split_inds.len();
        if self.config.split_at_screen_size > 0.0 {
            let oversized = refiner.above_screen_size(self.config.split_at_screen_size);
            let oversized_inds = oversized.argwhere_async().await;
            if oversized_inds.dims()[0] > 0 {
                let oversized_inds = oversized_inds
                    .squeeze_dim::<1>(1)
                    .into_data_async()
                    .await
                    .expect("Failed to get oversized indices")
                    .try_into_vec::<i32>()
                    .expect("Failed to read oversized indices");
                let mut budget =
                    count_cap.saturating_sub(splats.num_splats() + split_inds.len() as u32);
                for ind in oversized_inds {
                    if budget == 0 {
                        break;
                    }
                    if split_inds.insert(ind) {
                        budget -= 1;
                    }
                }
            }
        }
        let num_split_oversized = (split_inds.len() - pre_oversized) as u32;

        let pre_high_grad = split_inds.len();
        // The growth window is expressed as schedule fractions so it means
        // the same thing in iter mode and --budget-s mode.
        let growth_start =
            self.config.growth_start_iter as f32 / self.config.total_train_iters.max(1) as f32;
        let growth_stop =
            self.config.growth_stop_iter as f32 / self.config.total_train_iters.max(1) as f32;
        if progress >= growth_start && progress < growth_stop && mode != RefineMode::Mcmc {
            let above_threshold = refiner.above_threshold(self.config.growth_grad_threshold);

            let threshold_count = above_threshold
                .clone()
                .int()
                .sum()
                .into_scalar_async::<i32>()
                .await
                .expect("Failed to get threshold") as u32;

            let cur_splats = splats.num_splats() + split_inds.len() as u32;
            // `budget` fills up to the ramp target by score; `brush` takes the
            // classic `growth_select_fraction` of threshold-crossing splats.
            let grow_count = if mode == RefineMode::Budget {
                let target = self.growth_target_at(iter, splats.num_splats());
                threshold_count.min(target.saturating_sub(cur_splats))
            } else {
                let grow_count =
                    (threshold_count as f32 * self.config.growth_select_fraction).round() as u32;
                grow_count
                    .saturating_sub(pruned_count)
                    .min(count_cap.saturating_sub(cur_splats))
            };

            if grow_count > 0 {
                // Sample within the threshold-crossing pool, weighted by the
                // accumulated refine score. When the whole pool fits the
                // budget take all of it — no sampling needed.
                let growth_inds = if grow_count >= threshold_count {
                    above_threshold
                        .argwhere_async()
                        .await
                        .squeeze_dim::<1>(1)
                        .into_data_async()
                        .await
                        .expect("Failed to get growth indices")
                        .try_into_vec::<i32>()
                        .expect("Failed to read growth indices")
                } else {
                    let weights = above_threshold.float() * refiner.refine_weight_norm.clone();
                    let weights = weights
                        .into_data_async()
                        .await
                        .expect("Failed to get weights")
                        .try_into_vec::<f32>()
                        .expect("Failed to read weights");
                    multinomial_sample(&mut self.rng, &weights, grow_count)
                };
                split_inds.extend(growth_inds);
            }
        }

        let num_split_high_grad = (split_inds.len() - pre_high_grad) as u32;
        let refine_count = split_inds.len();
        // Per-splat max on-screen extent, used by `refine_splats` to cap the
        // split shrink so oversized splats' children land at `split_at_screen_size`.
        let screen_sizes = refiner.max_screen_size.clone();
        splats = self.refine_splats(&device, optim, splats, split_inds, screen_sizes, iter);

        // Children appended by `refine_splats` get zero lifetime blend weight.
        if let Some(lt) = self.contrib_lifetime.take() {
            let n_new = splats.num_splats() - lt.dims()[0] as u32;
            self.contrib_lifetime = Some(if n_new > 0 {
                Tensor::cat(
                    vec![lt, Tensor::<1>::zeros([n_new as usize], &device)],
                    0,
                )
            } else {
                lt
            });
        }

        // Update current bounds based on the splats.
        self.bounds = get_splat_bounds(splats.clone(), BOUND_PERCENTILE).await;
        device.memory_cleanup();

        // Recompute the per-splat 3D-filter floor against the new positions/
        // count and attach it — the floor is part of the splat from here until
        // the next refine. Past the freeze fraction we stop refreshing and leave
        // it baked in, so the tail settles against fixed params.
        if progress < MIN_SCALE_FREEZE_FRAC {
            // `splats` is already on the inner backend here, so `means()` is too.
            // No-op when there are no view cameras (e.g. unit tests).
            let means = splats.means();
            if let Some(f) =
                compute_min_scale(&means, &self.view_cams, self.config.min_scale_factor)
            {
                splats = splats.with_min_scale(f);
            }
        }

        let splat_count = splats.num_splats();

        (
            splats,
            RefineStats {
                num_added: refine_count as u32,
                num_split_oversized,
                num_split_high_grad,
                num_pruned: pruned_count,
                num_pruned_non_finite,
                num_relocated,
                num_pruned_low_contrib,
                total_splats: splat_count,
            },
        )
    }

    fn refine_splats(
        &mut self,
        device: &Device,
        mut optim: SplatOptim,
        mut splats: Splats,
        split_inds: HashSet<i32>,
        screen_sizes: Tensor<1>,
        iter: u32,
    ) -> Splats {
        let refine_count = split_inds.len();

        if refine_count > 0 {
            let refine_inds = Tensor::from_data(
                TensorData::new(split_inds.into_iter().collect::<Vec<_>>(), [refine_count]),
                device,
            );

            let cur_transforms = splats.transforms.val().select(0, refine_inds.clone());
            let cur_means = cur_transforms.clone().slice(s![.., 0..3]);
            let cur_rots_raw = cur_transforms.clone().slice(s![.., 3..7]);
            let magnitudes = Tensor::clamp_min(
                Tensor::sum_dim(cur_rots_raw.clone().powi_scalar(2), 1).sqrt(),
                1e-32,
            );
            let cur_rots = cur_rots_raw.clone() / magnitudes;
            let cur_log_scale = cur_transforms.slice(s![.., 7..10]);
            let cur_sh_coeffs = splats.sh_coeffs.val().select(0, refine_inds.clone());
            let cur_raw_opac = splats.raw_opacities.val().select(0, refine_inds.clone());

            let cur_scales = cur_log_scale.clone().exp();

            let cur_opac = sigmoid(cur_raw_opac);
            let inv_opac: Tensor<1> = 1.0 - cur_opac;
            // Post-split child opacity as a power law in transmittance,
            // p = 0.5 would keep the transmittance for cloning splats but as we offset them
            // choose a higher p.
            let new_opac: Tensor<1> = 1.0 - inv_opac.powf_scalar(FRAC_1_SQRT_2);
            let new_raw_opac = inv_sigmoid(new_opac.clamp(MIN_OPACITY, 1.0 - MIN_OPACITY));

            // Smooth covariance-aware split. Per-axis shrink + mass-conserving
            // deterministic offset (one child at +offset, the other at -offset).
            // Children inherit the
            // parent's rotation; the split is the scale shrink + ±offset.
            let cur_scales_sq = cur_scales.clone().powi_scalar(2);
            let max_scale_sq = cur_scales_sq.clone().max_dim(1).clamp_min(1e-30);
            let ratio = cur_scales_sq / max_scale_sq;
            // Max-axis shrink factor `k` (per splat). The standard split uses
            // 1/√2 (mass-conserving). When `split_at_screen_size` is set, splats
            // that are too big on screen shrink harder so their children land at
            // (at most) the cap: `k = min(1/√2, split_at_screen_size / screen)`.
            // Splats already within √2× of the cap are unaffected (min → 1/√2).
            let k_per_axis: Tensor<2> = if self.config.split_at_screen_size > 0.0 {
                let k_max = screen_sizes
                    .select(0, refine_inds.clone())
                    .unsqueeze_dim(1)
                    .clamp_min(1e-6)
                    .recip()
                    .mul_scalar(self.config.split_at_screen_size)
                    .clamp_max(FRAC_1_SQRT_2);
                -(ratio * (-k_max + 1.0)) + 1.0
            } else {
                -(ratio * (1.0_f32 - FRAC_1_SQRT_2)) + 1.0
            };
            let offset_factor = (-k_per_axis.clone().powi_scalar(2) + 1.0)
                .clamp_min(0.0)
                .sqrt();
            let offset_local = offset_factor * cur_scales;
            let samples = quaternion_vec_multiply(cur_rots.clone(), offset_local);
            let new_log_scales = cur_log_scale + k_per_axis.log();
            let child_rots = cur_rots;

            let parent_transforms = Tensor::cat(
                vec![
                    cur_means.clone() - samples.clone(),
                    cur_rots_raw,
                    new_log_scales.clone(),
                ],
                1,
            );
            splats.transforms = splats
                .transforms
                .map(|t| t.select_assign(0, refine_inds.clone(), parent_transforms, Assign));
            splats.raw_opacities = splats
                .raw_opacities
                .map(|m| m.select_assign(0, refine_inds.clone(), new_raw_opac.clone(), Assign));

            // Child sits at parent_mean + samples (parent moves to
            // parent_mean - samples) — anti-correlated, centroid-preserving.
            // Build new transforms row: means(3) + rotations(4) + log_scales(3)
            let new_transforms =
                Tensor::cat(vec![cur_means + samples, child_rots, new_log_scales], 1);

            // Optimizer state lives on the inner (non-autodiff) device.
            let opt_device = device.clone().inner();
            let opt_inds = refine_inds.to_device(&opt_device);

            splats = map_splats_and_opt(
                splats,
                &mut optim,
                |x| Tensor::cat(vec![x, new_transforms], 0),
                |x| Tensor::cat(vec![x, cur_sh_coeffs], 0),
                |x| Tensor::cat(vec![x, new_raw_opac], 0),
                |x: Tensor<2>| {
                    let d1 = x.dims()[1];
                    let zeros = Tensor::zeros([refine_count, d1], &opt_device);
                    let x = x.select_assign(0, opt_inds.clone(), zeros.clone(), Assign);
                    Tensor::cat(vec![x, zeros], 0)
                },
                |x: Tensor<3>| {
                    let [_, d1, d2] = x.dims();
                    let zeros = Tensor::zeros([refine_count, d1, d2], &opt_device);
                    let x = x.select_assign(0, opt_inds.clone(), zeros.clone(), Assign);
                    Tensor::cat(vec![x, zeros], 0)
                },
                |x: Tensor<1>| {
                    let zeros = Tensor::zeros([refine_count], &opt_device);
                    let x = x.select_assign(0, opt_inds.clone(), zeros.clone(), Assign);
                    Tensor::cat(vec![x, zeros], 0)
                },
            );
        }

        let train_t = self.progress(iter);
        let t_shrink_strength = 1.0 - train_t;
        let minus_opac = self.config.opac_decay * t_shrink_strength;

        // Lower opacity slowly over time.
        splats.raw_opacities = splats.raw_opacities.map(|f| {
            let new_opac = sigmoid(f) - minus_opac;
            inv_sigmoid(new_opac.clamp(1e-12, 1.0 - 1e-12))
        });

        // Row identities changed — the EMA restarts from these params.
        self.ema_update(&splats);

        self.optim = Some(optim);
        splats
    }
}

fn map_splats_and_opt(
    mut splats: Splats,
    optim: &mut SplatOptim,
    map_transforms: impl FnOnce(Tensor<2>) -> Tensor<2>,
    map_sh_coeffs: impl FnOnce(Tensor<3>) -> Tensor<3>,
    map_opac: impl FnOnce(Tensor<1>) -> Tensor<1>,

    map_opt_transforms: impl Fn(Tensor<2>) -> Tensor<2>,
    map_opt_sh_coeffs: impl Fn(Tensor<3>) -> Tensor<3>,
    map_opt_opac: impl Fn(Tensor<1>) -> Tensor<1>,
) -> Splats {
    splats.transforms = splats.transforms.map(map_transforms);
    optim.transforms.map_momentum(map_opt_transforms);
    splats.sh_coeffs = splats.sh_coeffs.map(map_sh_coeffs);
    optim.sh_coeffs.map_momentum(map_opt_sh_coeffs);
    splats.raw_opacities = splats.raw_opacities.map(map_opac);
    optim.opacities.map_momentum(map_opt_opac);
    splats
}

/// Indices of the `n` lowest-contribution splats, skipping rows marked in
/// `excluded` (already-dead rows needn't be re-picked). One host readback.
async fn lowest_contrib_inds(
    contrib: &Tensor<1>,
    excluded: Option<&Tensor<1, Bool>>,
    n: u32,
) -> Vec<i32> {
    if n == 0 {
        return Vec::new();
    }
    let score = match excluded {
        Some(mask) => contrib.clone() + mask.clone().float().mul_scalar(f32::MAX),
        None => contrib.clone(),
    };
    let scores = score
        .into_data_async()
        .await
        .expect("Failed to read contrib")
        .try_into_vec::<f32>()
        .expect("Failed to convert contrib");
    let mut order: Vec<(i32, f32)> = scores
        .iter()
        .copied()
        .enumerate()
        .map(|(i, s)| (i as i32, s))
        .collect();
    order.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
    order
        .into_iter()
        .take(n as usize)
        .filter(|(_, s)| *s < f32::MAX)
        .map(|(i, _)| i)
        .collect()
}

/// Build a bool `[n]` mask with `inds` set, for feeding `prune_points`.
/// Only safe for small index sets — the scatter backend drops indices
/// beyond `u16::MAX` on lavapipe (observed: a 101k-index assign landed
/// 65,535). Large removals must use `keep_inds_tensor` + `select_points`.
#[allow(dead_code)]
fn inds_to_mask(inds: &[i32], n: u32, device: &Device) -> Tensor<1, Bool> {
    let k = inds.len();
    let idx = Tensor::from_data(TensorData::new(inds.to_vec(), [k]), device);
    Tensor::<1>::zeros([n as usize], device)
        .select_assign(0, idx, Tensor::<1>::ones([k], device), Assign)
        .greater_elem(0.5)
}

/// Keep-index tensor: every row in `0..n` not listed in `drop`, ascending
/// (select preserves splat order). Built on the host — one O(n) pass — for
/// removals where an index-scatter mask would be too large to survive.
fn keep_inds_tensor(n: u32, drop: &[i32], device: &Device) -> Tensor<1, Int> {
    let mut dead = vec![false; n as usize];
    for &i in drop {
        dead[i as usize] = true;
    }
    let keep: Vec<i32> = (0..n as i32).filter(|&i| !dead[i as usize]).collect();
    let k = keep.len();
    Tensor::from_data(TensorData::new(keep, [k]), device)
}

/// Keep splats by explicit indices — the large-removal path. Same selects
/// as `prune_points` but skips the bool-mask scatter + `argwhere` round
/// trip entirely: safe past the `u16::MAX` index-scatter limit.
async fn select_points(
    mut splats: Splats,
    optim: &mut SplatOptim,
    mut refiner: RefineRecord,
    keep_inds: Tensor<1, Int>,
    contrib_lifetime: Option<&mut Tensor<1>>,
) -> (Splats, RefineRecord, u32) {
    let start_splats = splats.num_splats();
    let new_points = keep_inds.dims()[0] as u32;
    if new_points == 0 || new_points >= start_splats {
        return (splats, refiner, 0);
    }
    let inner_keep = keep_inds.clone().without_autodiff();
    splats = map_splats_and_opt(
        splats,
        optim,
        |x| x.select(0, keep_inds.clone()),
        |x| x.select(0, keep_inds.clone()),
        |x| x.select(0, keep_inds.clone()),
        |x| x.select(0, keep_inds.clone()),
        |x| x.select(0, keep_inds.clone()),
        |x| x.select(0, keep_inds.clone()),
    );
    if let Some(f) = splats.min_scale.take() {
        splats.min_scale = Some(f.select(0, inner_keep.clone()));
    }
    refiner = refiner.keep(inner_keep.clone());
    if let Some(lt) = contrib_lifetime {
        *lt = lt.clone().select(0, inner_keep);
    }
    (splats, refiner, start_splats - new_points)
}

/// MCMC-style relocation (3DGS-MCMC): every splat under `relocate_mask` is
/// overwritten in place by a perturbed copy of a live donor sampled
/// ∝ opacity·visibility. Count and array layout are preserved — dead rows are
/// repurposed rather than removed, and their Adam momentum is zeroed.
async fn relocate_points(
    mut splats: Splats,
    optim: &mut SplatOptim,
    mut refiner: RefineRecord,
    relocate_mask: Tensor<1, Bool>,
    rng: &mut rand::rngs::StdRng,
    config: &TrainConfig,
    contrib_lifetime: Option<&mut Tensor<1>>,
) -> (Splats, RefineRecord, u32) {
    let device = splats.device();
    let dead_inds = relocate_mask.clone().argwhere_async().await.squeeze_dim(1);
    let k = dead_inds.dims()[0] as u32;
    if k == 0 {
        return (splats, refiner, 0);
    }

    // Donor weights: opacity × visible, zeroed on dead rows so a dead splat
    // can't be its own donor.
    let weights =
        splats.opacities() * refiner.vis_mask().float() * relocate_mask.bool_not().float();
    let weights = weights
        .into_data_async()
        .await
        .expect("Failed to get weights")
        .try_into_vec::<f32>()
        .expect("Failed to read weights");
    let donors = multinomial_sample(rng, &weights, k);
    if donors.is_empty() {
        // No live donors to copy — leave the dead rows (they'll die on the
        // next refine through the same mask) rather than empty the splats.
        return (splats, refiner, 0);
    }
    let k = donors.len();
    let donor_inds = Tensor::from_data(TensorData::new(donors, [k]), &device);
    let dead_inds_host = dead_inds.clone();

    let donor_t = splats.transforms.val().select(0, donor_inds.clone());
    let donor_sh = splats.sh_coeffs.val().select(0, donor_inds.clone());
    let donor_opac = splats.raw_opacities.val().select(0, donor_inds);

    // Perturb positions by a fraction of the donor's own scale; the copied
    // splat stays in the donor's neighborhood while free to re-fit.
    let donor_scales = donor_t.clone().slice(s![.., 7..10]).exp();
    let donor_max_scale = donor_scales.max_dim(1);
    let noise = Tensor::random([k, 3], Distribution::Normal(0.0, 1.0), &device)
        * donor_max_scale
        * config.mcmc_noise_frac;
    let new_means = donor_t.clone().slice(s![.., 0..3]) + noise;
    let new_transforms = Tensor::cat(vec![new_means, donor_t.slice(s![.., 3..10])], 1);
    let new_opac = if config.mcmc_opac_reset > 0.0 {
        let p = config.mcmc_opac_reset.clamp(1e-6, 1.0 - 1e-6);
        Tensor::<1>::ones([k], &device).mul_scalar((p / (1.0 - p)).ln())
    } else {
        donor_opac
    };

    splats.transforms = splats
        .transforms
        .map(|t| t.select_assign(0, dead_inds_host.clone(), new_transforms, Assign));
    splats.sh_coeffs = splats
        .sh_coeffs
        .map(|t| t.select_assign(0, dead_inds_host.clone(), donor_sh, Assign));
    splats.raw_opacities = splats
        .raw_opacities
        .map(|t| t.select_assign(0, dead_inds_host.clone(), new_opac, Assign));

    // Zero Adam momentum for relocated rows — they're new samples.
    let opt_inds = dead_inds_host.clone().to_device(&device.clone().inner());
    optim.transforms.map_momentum(|x| {
        let mut d = x.dims();
        d[0] = k;
        x.select_assign(0, opt_inds.clone(), Tensor::<2>::zeros(d, &device), Assign)
    });
    optim.sh_coeffs.map_momentum(|x| {
        let mut d = x.dims();
        d[0] = k;
        x.select_assign(0, opt_inds.clone(), Tensor::<3>::zeros(d, &device), Assign)
    });
    optim.opacities.map_momentum(|x| {
        let mut d = x.dims();
        d[0] = k;
        x.select_assign(0, opt_inds.clone(), Tensor::<1>::zeros(d, &device), Assign)
    });

    // Reset the refine stats for relocated rows — they're new samples and
    // stale history would re-mark them for removal.
    let zeros_k = Tensor::<1>::zeros([k], &device);
    refiner.refine_weight_norm = refiner.refine_weight_norm.select_assign(
        0,
        dead_inds_host.clone(),
        zeros_k.clone(),
        Assign,
    );
    refiner.vis_weight =
        refiner
            .vis_weight
            .select_assign(0, dead_inds_host.clone(), zeros_k.clone(), Assign);
    refiner.max_screen_size =
        refiner
            .max_screen_size
            .select_assign(0, dead_inds_host.clone(), zeros_k.clone(), Assign);
    refiner.contrib = refiner
        .contrib
        .select_assign(0, dead_inds_host.clone(), zeros_k.clone(), Assign);
    if let Some(lt) = contrib_lifetime {
        *lt = lt
            .clone()
            .select_assign(0, dead_inds_host, zeros_k, Assign);
    }

    (splats, refiner, k as u32)
}

// Prunes points based on the given mask.
//
// Args:
//   mask: bool[n]. If True, prune this Gaussian.
async fn prune_points(
    mut splats: Splats,
    optim: &mut SplatOptim,
    mut refiner: RefineRecord,
    prune: Tensor<1, Bool>,
    contrib_lifetime: Option<&mut Tensor<1>>,
) -> (Splats, RefineRecord, u32) {
    assert_eq!(
        prune.dims()[0] as u32,
        splats.num_splats(),
        "Prune mask must have same number of elements as splats"
    );

    let prune_count = prune.dims()[0];
    if prune_count == 0 {
        return (splats, refiner, 0);
    }

    let valid_inds = prune.bool_not().argwhere_async().await;

    if valid_inds.dims()[0] == 0 {
        log::warn!("Trying to create empty splat!");
        return (splats, refiner, 0);
    }

    let start_splats = splats.num_splats();
    let new_points = valid_inds.dims()[0] as u32;
    if new_points < start_splats {
        let valid_inds = valid_inds.squeeze_dim(1);
        let inner_valid_inds = valid_inds.clone().without_autodiff();
        splats = map_splats_and_opt(
            splats,
            optim,
            |x| x.select(0, valid_inds.clone()),
            |x| x.select(0, valid_inds.clone()),
            |x| x.select(0, valid_inds.clone()),
            |x| x.select(0, valid_inds.clone()),
            |x| x.select(0, valid_inds.clone()),
            |x| x.select(0, valid_inds.clone()),
        );
        refiner = refiner.keep(inner_valid_inds.clone());
        if let Some(lt) = contrib_lifetime {
            *lt = lt.clone().select(0, inner_valid_inds);
        }
    }
    (splats, refiner, start_splats - new_points)
}

/// Sample a background color: base + uniform noise in [-strength, +strength], clamped to [0, 1].
fn sample_background_color<R: rand::Rng + ?Sized>(
    base: glam::Vec3,
    strength: f32,
    rng: &mut R,
) -> glam::Vec3 {
    if strength <= 0.0 {
        return base.clamp(glam::Vec3::ZERO, glam::Vec3::ONE);
    }
    use rand::RngExt as _;
    let noise = glam::Vec3::new(
        rng.random_range(-strength..strength),
        rng.random_range(-strength..strength),
        rng.random_range(-strength..strength),
    );
    (base + noise).clamp(glam::Vec3::ZERO, glam::Vec3::ONE)
}
