//! Plug-in hooks for the other splat-turbo lanes.
//!
//! `farsight-splat run` is a `Pipeline` of these stages; each lane owns a trait
//! it can replace (see `Pipeline::with_*`). Defaults reproduce Dechante's
//! python pipeline exactly.
//!
//! | Lane | Hook trait | What it sees / returns |
//! |------|------------|------------------------|
//! | T5   | [`SeedHook`]      | capture dir + dot inputs -> init.ply (or a whole `Splats` later) |
//! | T6   | [`ViewsHook`]     | parsed frames -> kept frames (drives/cull/live-view select) |
//! | T3   | [`ScheduleHook`]  | budget s + measured steps/s -> total steps & config tweaks |
//! | T4   | [`DensifyHook`]   | `TrainStreamConfig` mutate before launch (refine/growth) |
//!
//! Everything is `Send + Sync` and object-safe so lanes register impls without
//! touching this file's callers.

use std::path::{Path, PathBuf};

use anyhow::Result;
use brush_process::config::TrainStreamConfig;

use crate::capture::{Capture, Frame};
use crate::cull;
use crate::seed::{self, SeedReport};

/// T6 — view selection & appearance. Replaces drive-cut + parked/blur cull.
pub trait ViewsHook: Send + Sync {
    /// Filter/transform the capture's frames before the dataset is written.
    /// Return kept frames in order plus a human-readable note for bench.json.
    fn select(&self, cap: &Capture) -> Result<(Vec<Frame>, String)>;

    /// Optional appearance hook (per-frame color/exposure normalization): the
    /// prepared capture dir is passed; mutate images in place if wanted.
    fn appearance(&self, _prepared_dir: &Path) -> Result<()> {
        Ok(())
    }
}

/// Default: Dechante's cull.py port ([`cull::cull`]).
pub struct DefaultViews;
impl ViewsHook for DefaultViews {
    fn select(&self, cap: &Capture) -> Result<(Vec<Frame>, String)> {
        let (keep, rep) = cull::cull(cap);
        Ok((keep, serde_json::to_string(&rep)?))
    }
}

/// T5 — splat seeding. Replaces seed_from_dots.py.
pub trait SeedHook: Send + Sync {
    /// Produce the seed for `cap` in `out_dir` (usually an init.ply whose
    /// path is then set as transforms.ply_file_path).
    fn seed(&self, cap: &mut Capture, out_dir: &Path) -> Result<SeedReport>;
}

/// Default: jetson points.bin (+ optional dense.xyzrgb / existing ply inputs)
/// -> statistical outlier removal -> binary init.ply.
pub struct DotsSeed {
    pub inputs: Vec<PathBuf>,
}
impl SeedHook for DotsSeed {
    fn seed(&self, cap: &mut Capture, out_dir: &Path) -> Result<SeedReport> {
        seed::seed_from_dots(cap, &self.inputs, out_dir)
    }
}

/// Seed straight from an existing init.ply already referenced by the capture
/// (or by --seed ply path): copies it into the prepared dir unchanged.
pub struct PassthroughSeed {
    pub ply: PathBuf,
}
impl SeedHook for PassthroughSeed {
    fn seed(&self, cap: &mut Capture, out_dir: &Path) -> Result<SeedReport> {
        let dst = out_dir.join("init.ply");
        if self.ply != dst {
            std::fs::copy(&self.ply, &dst)?;
        }
        cap.transforms.ply_file_path = Some("init.ply".to_string());
        let n = seed::read_init_ply(&dst).map(|d| d.len()).unwrap_or(0);
        Ok(SeedReport {
            source: self.ply.display().to_string(),
            input_points: n,
            kept_points: n,
            ply: Some(dst.display().to_string()),
        })
    }
}

/// Plan for one training run inside a wall-clock budget.
#[derive(Clone, Debug)]
pub struct TrainPlan {
    /// Steps the trainer's LR schedule completes in.
    pub total_iters: u32,
    /// Anything the lane wants recorded in bench.json.
    pub note: String,
}

/// T3 — budget -> step-count/LR scheduling. `rate` is steps/s measured by a
/// short calibration run on this same machine & dataset (or a prior run);
/// `remaining_s` is wall clock left after load/cull/seed (+reserve).
pub trait ScheduleHook: Send + Sync {
    fn plan(&self, remaining_s: f64, rate_steps_per_s: Option<f64>) -> TrainPlan;
}

/// Default: rate * remaining * safety, hard deadline as backstop.
pub struct RateSchedule {
    /// Fraction of the theoretical step count to schedule (default 0.85):
    /// step rate decays as splats densify, so full-rate overshoots.
    pub safety: f64,
    /// Used when no measured rate exists (first run, --no-calib).
    pub fallback_steps: u32,
}
impl ScheduleHook for RateSchedule {
    fn plan(&self, remaining_s: f64, rate: Option<f64>) -> TrainPlan {
        let total = match rate {
            Some(r) if r > 0.0 => (r * remaining_s * self.safety) as u32,
            _ => self.fallback_steps,
        };
        TrainPlan {
            total_iters: total.max(1),
            note: format!(
                "rate={:.1}/s safety={:.2} remaining={:.1}s -> {} iters",
                rate.unwrap_or(0.0),
                self.safety,
                remaining_s,
                total
            ),
        }
    }
}

/// T4 — densify/refine tuning. Runs just before each training launch.
pub trait DensifyHook: Send + Sync {
    fn tune(&self, cfg: &mut TrainStreamConfig);
}
/// No-op default (upstream defaults stand).
pub struct DefaultDensify;
impl DensifyHook for DefaultDensify {
    fn tune(&self, _cfg: &mut TrainStreamConfig) {}
}

/// The assembled pipeline — defaults = Dechante's python flow.
pub struct Pipeline {
    pub views: Box<dyn ViewsHook>,
    pub seed: Option<Box<dyn SeedHook>>,
    pub schedule: Box<dyn ScheduleHook>,
    pub densify: Box<dyn DensifyHook>,
}

impl Default for Pipeline {
    fn default() -> Self {
        Self {
            views: Box::new(DefaultViews),
            seed: None,
            schedule: Box::new(RateSchedule {
                safety: 0.85,
                fallback_steps: 2000,
            }),
            densify: Box::new(DefaultDensify),
        }
    }
}

impl Pipeline {
    pub fn with_views(mut self, v: Box<dyn ViewsHook>) -> Self {
        self.views = v;
        self
    }
    pub fn with_seed(mut self, s: Box<dyn SeedHook>) -> Self {
        self.seed = Some(s);
        self
    }
    pub fn with_schedule(mut self, s: Box<dyn ScheduleHook>) -> Self {
        self.schedule = s;
        self
    }
    pub fn with_densify(mut self, d: Box<dyn DensifyHook>) -> Self {
        self.densify = d;
        self
    }
}
