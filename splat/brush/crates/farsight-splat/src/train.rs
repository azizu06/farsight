//! Drive a Brush training stream under a wall-clock budget.
//!
//! `train_once` runs `brush_process::create_process` on a prepared capture dir
//! and consumes the message stream: it collects step rates, eval metrics and
//! the final splat count, and when `deadline` passes it reads the live
//! `splat_view`, exports it, and stops — so a `--budget-s` run always ends
//! with the best completed state, LR-schedule-complete when the plan fits.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::Result;
use brush_process::config::TrainStreamConfig;
use brush_process::message::{ProcessMessage, TrainMessage};
use brush_process::{DataSource, RunningProcess};
use serde::Serialize;
use tokio_stream::StreamExt;

/// What one training run produced.
#[derive(Clone, Debug, Default, Serialize)]
pub struct TrainOutcome {
    pub iters_done: u32,
    pub iters_planned: u32,
    pub train_seconds: f64, // sum of trainer step+refine time (TrainStep)
    pub wall_seconds: f64,
    pub steps_per_s: f64, // measured over the back half of the run
    pub splats: u32,
    /// Last eval (iter, avg_psnr, avg_ssim) emitted by Brush, if any.
    pub eval_iter: Option<u32>,
    pub eval_psnr: Option<f32>,
    pub eval_ssim: Option<f32>,
    pub finished_schedule: bool, // reached total_iters
    pub stopped_at_deadline: bool,
    pub eval_dir: Option<String>, // <out>/eval_<iter> when written
    pub warnings: Vec<String>,
}

pub struct RunHandle {
    pub outcome: TrainOutcome,
    /// Final splats pulled from the live slot (for .ply/.splat export).
    pub splats: Option<brush_render::gaussian_splats::Splats>,
    /// Device the process ran on (for scoring renders).
    pub device: brush_process::ProcessDevice,
}

/// Peak-RSS sampler (Linux/macOS via memory-stats). Poll on a timer; the
/// trainer runs in the same process so this catches everything it allocates.
#[derive(Default)]
pub struct PeakMem {
    pub peak: u64,
}
impl PeakMem {
    pub fn sample(&mut self) {
        if let Some(m) = memory_stats::memory_stats() {
            self.peak = self.peak.max(m.physical_mem as u64);
        }
    }
}

/// Launch one training run on `dataset_dir`.
///
/// - `cfg`: full Brush TrainStreamConfig; `total_iters` is the plan.
/// - `deadline`: hard stop — at/after it, current splats are exported and the
///   stream dropped. `None` runs to schedule.
/// - `log_every`: progress print cadence in iters (0 = quiet).
pub async fn train_once(
    dataset_dir: &Path,
    mut cfg: TrainStreamConfig,
    deadline: Option<Instant>,
    log_every: u32,
) -> Result<RunHandle> {
    cfg.process_config.eval_save_to_disk = true;
    let planned = cfg.train_config.total_iters();
    let export_path = cfg.process_config.export_path.clone();
    let cfg_for_init = cfg.clone();

    let proc: RunningProcess = brush_process::create_process(
        DataSource::Path(dataset_dir.to_string_lossy().into_owned()),
        move |init| {
            let cli = cfg_for_init.clone();
            async move {
                // Merge CLI-ish config over the dataset's args.txt baseline,
                // same as brush-cli's build_process.
                Some(brush_process::args_file::merge_configs(&init, &cli))
            }
        },
    );
    let mut stream = proc.stream;
    let splat_view = proc.splat_view;
    let device = proc.device;

    let start = Instant::now();
    let mut out = TrainOutcome {
        iters_planned: planned,
        ..Default::default()
    };
    let mut last_step_elapsed = Duration::ZERO;
    // For the steady-rate estimate: (iter, total_elapsed) samples.
    let mut samples: Vec<(u32, Duration)> = vec![];
    let mut mem = PeakMem::default();

    while let Some(msg) = stream.next().await {
        let msg = match msg {
            Ok(m) => m,
            Err(e) => {
                out.warnings.push(format!("stream error: {e:#}"));
                break;
            }
        };
        mem.sample();
        match msg {
            ProcessMessage::TrainMessage(tm) => match tm {
                TrainMessage::TrainStep {
                    iter,
                    total_elapsed,
                    ..
                } => {
                    out.iters_done = iter;
                    last_step_elapsed = total_elapsed;
                    samples.push((iter, total_elapsed));
                    if log_every > 0 && iter % log_every == 0 {
                        let splats = splat_view.latest().map(|s| s.num_splats()).unwrap_or(0);
                        log::info!(
                            "step {iter}/{planned} | {:.1} it/s | {splats} splats",
                            rate_of(&samples)
                        );
                    }
                }
                TrainMessage::EvalResult {
                    iter,
                    avg_psnr,
                    avg_ssim,
                } => {
                    out.eval_iter = Some(iter);
                    out.eval_psnr = Some(avg_psnr);
                    out.eval_ssim = Some(avg_ssim);
                }
                TrainMessage::DoneTraining => {
                    out.finished_schedule = true;
                }
                _ => {}
            },
            ProcessMessage::Warning { error } => {
                out.warnings.push(format!("{error:#}"));
            }
            _ => {}
        }
        if out.finished_schedule {
            break;
        }
        if let Some(dl) = deadline
            && Instant::now() >= dl
        {
            out.stopped_at_deadline = true;
            break;
        }
    }
    drop(stream);

    out.train_seconds = last_step_elapsed.as_secs_f64();
    out.wall_seconds = start.elapsed().as_secs_f64();
    out.steps_per_s = rate_of(&samples);
    out.splats = splat_view.latest().map(|s| s.num_splats()).unwrap_or(0);
    let splats = splat_view.latest();

    // Eval renders land at <export>/eval_<iter>/... — record the newest.
    if out.eval_iter.is_some() {
        let base = dataset_dir
            .parent()
            .map(|p| p.join(&export_path))
            .unwrap_or_else(|| PathBuf::from(&export_path));
        out.eval_dir = Some(
            base.join(format!("eval_{}", out.eval_iter.unwrap()))
                .display()
                .to_string(),
        );
    }
    Ok(RunHandle {
        outcome: out,
        splats,
        device,
    })
}

/// Steps/s over the back half of recorded steps — the autotune-heavy head
/// poisons a whole-run average.
fn rate_of(samples: &[(u32, Duration)]) -> f64 {
    if samples.len() < 3 {
        if let (Some(a), Some(b)) = (samples.first(), samples.last()) {
            let di = (b.0 - a.0) as f64;
            let dt = (b.1 - a.1).as_secs_f64();
            return if dt > 0.0 { di / dt } else { 0.0 };
        }
        return 0.0;
    }
    let mid = samples.len() / 2;
    let (i0, t0) = samples[mid - 1];
    let (i1, t1) = samples[samples.len() - 1];
    let di = (i1 - i0) as f64;
    let dt = (t1 - t0).as_secs_f64();
    if dt > 0.0 { di / dt } else { 0.0 }
}
