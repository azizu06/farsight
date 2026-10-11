//! The `farsight-splat run` pipeline: capture -> cull -> seed -> train
//! (wall-clock budget) -> export .ply/.splat -> held-out score.
//! `bench` runs the same pipeline once per budget.

use std::path::PathBuf;
use std::time::Instant;

use anyhow::{Result, bail};
use brush_process::config::TrainStreamConfig;
use serde::Serialize;

use crate::capture::{Capture, Frame};
use crate::cull::CullReport;
use crate::eval;
use crate::export;
use crate::hooks::Pipeline;
use crate::score::ScoreReport;
use crate::seed::{SeedKind, SeedReport};
use crate::train::{self, PeakMem, TrainOutcome};

/// How the wall-clock budget reaches the trainer.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, clap::ValueEnum, Serialize)]
pub enum ScheduleKind {
    /// T3's in-trainer schedule: `TrainConfig::budget_s` paces LR decay,
    /// progressive res, SH unlock and the refine window by wall clock, and
    /// the trainer stops when the budget elapses.
    #[default]
    Budget,
    /// Upstream behaviour: fixed `--total-train-iters` on the stock
    /// iter-fraction schedule (T1 measures steps/s and converts).
    Stock,
}

#[derive(Clone)]
pub struct RunOptions {
    pub capture_dir: PathBuf,
    pub out_dir: PathBuf,
    /// Wall-clock budget for the whole pipeline in seconds (None = no limit).
    pub budget_s: Option<f64>,
    /// Fixed step count (skips calibration & schedule).
    pub steps: Option<u32>,
    /// Calibration run length in steps (0 = skip; use schedule fallback).
    pub calib_steps: u32,
    /// Reserve seconds for export+score inside the budget.
    pub reserve_s: f64,
    /// Only keep frames[a..=b] before culling (find_drives --cut).
    pub drive: Option<(usize, usize)>,
    /// Skip cull entirely.
    pub no_cull: bool,
    /// Dot sources to seed from; empty = keep capture's own ply_file_path.
    pub seed_inputs: Vec<PathBuf>,
    /// Seeder: raw dots (T1 port), T5 farsight-seed binary, or none.
    pub seed_kind: SeedKind,
    /// Extra args forwarded to the farsight-seed binary.
    pub seed_args: Vec<String>,
    /// Schedule: in-trainer wall-clock budget vs stock fixed iters.
    pub schedule: ScheduleKind,
    /// Which frame index serves as eval split period (Brush loader semantic).
    pub eval_split_every: usize,
    /// Progress print cadence.
    pub log_every: u32,
    /// Full Brush config override (all CLI flags flattened into it).
    pub config: TrainStreamConfig,
    /// Resolved GPU backend+adapter receipt string (set by the CLI).
    pub backend: Option<String>,
}

impl Default for RunOptions {
    fn default() -> Self {
        Self {
            capture_dir: PathBuf::new(),
            out_dir: PathBuf::new(),
            budget_s: None,
            steps: None,
            calib_steps: 48,
            reserve_s: 3.0,
            drive: None,
            no_cull: false,
            seed_inputs: vec![],
            seed_kind: SeedKind::default(),
            seed_args: vec![],
            schedule: ScheduleKind::default(),
            eval_split_every: 8,
            log_every: 50,
            config: TrainStreamConfig::default(),
            backend: None,
        }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct PhaseMs {
    pub load_ms: u64,
    pub cull_ms: u64,
    pub write_ms: u64,
    pub seed_ms: u64,
    pub calib_ms: u64,
    pub train_ms: u64,
    pub export_ms: u64,
    pub score_ms: u64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct RunReport {
    pub capture: String,
    pub out: String,
    pub budget_s: Option<f64>,
    pub wall_s: f64,
    pub phases: PhaseMs,
    pub frames_in: usize,
    pub frames_kept: usize,
    pub cull: Option<CullReport>,
    pub seed: Option<SeedReport>,
    pub calib_steps_per_s: Option<f64>,
    pub plan_note: Option<String>,
    pub train: TrainOutcome,
    pub score: Option<ScoreReport>,
    pub peak_mem_bytes: u64,
    pub scene_ply: Option<String>,
    pub scene_splat: Option<String>,
    pub run_json: Option<String>,
    /// GPU backend+adapter that ran (e.g. "vulkan llvmpipe").
    pub backend: Option<String>,
}

/// Full pipeline. `pipeline` carries the lane hooks; `opts` carries the config.
pub async fn run(opts: &RunOptions, pipeline: &Pipeline) -> Result<RunReport> {
    let t0 = Instant::now();
    let deadline = opts.budget_s.map(|b| t0 + std::time::Duration::from_secs_f64(b));
    let mut rep = RunReport {
        capture: opts.capture_dir.display().to_string(),
        out: opts.out_dir.display().to_string(),
        budget_s: opts.budget_s,
        backend: opts.backend.clone(),
        ..Default::default()
    };
    let mut phases = PhaseMs::default();
    let mut mem = PeakMem::default();

    // --- load ---
    let mut t = Instant::now();
    let mut cap = Capture::load(&opts.capture_dir)?;
    rep.frames_in = cap.transforms.frames.len();
    phases.load_ms = t.elapsed().as_millis() as u64;
    mem.sample();

    // --- optional drive cut ---
    if let Some((a, b)) = opts.drive {
        let hi = b.min(cap.transforms.frames.len().saturating_sub(1));
        if a > hi {
            bail!("--drive {a} {b}: outside 0..{}", cap.transforms.frames.len());
        }
        cap.transforms.frames = cap.transforms.frames[a..=hi].to_vec();
    }

    // --- cull / view selection ---
    t = Instant::now();
    let (keep, note) = if opts.no_cull {
        (cap.transforms.frames.clone(), "no-cull".to_string())
    } else {
        pipeline.views.select(&cap)?
    };
    rep.frames_kept = keep.len();
    rep.cull = serde_json::from_str(&note).ok();
    phases.cull_ms = t.elapsed().as_millis() as u64;
    if keep.is_empty() {
        bail!("no frames left after culling");
    }

    // --- write prepared dataset ---
    t = Instant::now();
    let prepared = opts.out_dir.join("prepared");
    std::fs::create_dir_all(&prepared)?;
    cap.write_subset(&prepared, &keep)?;
    let mut prep_cap = Capture::load(&prepared)?;
    phases.write_ms = t.elapsed().as_millis() as u64;
    mem.sample();

    // --- seed ---
    t = Instant::now();
    // The dataset dir training sees: `prepared` for raw/none seeding, or the
    // farsight-seed output dir (it rewrites transforms.json + links images).
    let mut dataset_dir = prepared.clone();
    if let Some(seed_hook) = &pipeline.seed {
        let srep = seed_hook.seed(&mut prep_cap, &prepared)?;
        prep_cap.save(&prepared)?;
        rep.seed = Some(srep);
    } else {
        match opts.seed_kind {
            SeedKind::Farsight => {
                let seeded = opts.out_dir.join("seeded");
                let srep = crate::seed::run_farsight_seed(&prepared, &seeded, &opts.seed_args)?;
                rep.seed = Some(srep);
                dataset_dir = seeded;
                prep_cap = Capture::load(&dataset_dir)?;
            }
            SeedKind::Raw if !opts.seed_inputs.is_empty() => {
                let srep =
                    crate::seed::seed_from_dots(&mut prep_cap, &opts.seed_inputs, &prepared)?;
                prep_cap.save(&prepared)?;
                rep.seed = Some(srep);
            }
            _ => {}
        }
    }
    phases.seed_ms = t.elapsed().as_millis() as u64;
    mem.sample();

    // --- training config ---
    let mut cfg = opts.config.clone();
    cfg.load_config.eval_split_every = Some(opts.eval_split_every);
    cfg.process_config.eval_save_to_disk = true;
    cfg.process_config.export_path = opts.out_dir.display().to_string();
    pipeline.densify.tune(&mut cfg);

    // --- decide step count / schedule ---
    // `--schedule budget` hands the remaining wall clock to T3's in-trainer
    // schedule (progress = elapsed/budget); the trainer stops itself via
    // budget_exhausted and our deadline break is the backstop. `--steps`
    // pins iters; `--schedule stock` measures steps/s then converts.
    let mut rate: Option<f64> = None;
    let budget_schedule = opts.schedule == ScheduleKind::Budget
        && opts.budget_s.is_some()
        && opts.steps.is_none();
    let total_iters = if let Some(s) = opts.steps {
        rep.plan_note = Some(format!("--steps {s}"));
        s
    } else if budget_schedule {
        let remaining = (opts.budget_s.unwrap_or(0.0)
            - t0.elapsed().as_secs_f64()
            - opts.reserve_s)
            .max(1.0);
        cfg.train_config.budget_s = Some(remaining);
        rep.plan_note = Some(format!(
            "schedule=budget train_budget={remaining:.1}s"
        ));
        // High cap: the wall-clock budget, not iters, ends the run.
        1_000_000
    } else {
        if opts.calib_steps > 0 {
            t = Instant::now();
            let calib_deadline = deadline;
            let mut calib_cfg = cfg.clone();
            calib_cfg.train_config.total_train_iters = opts.calib_steps;
            calib_cfg.train_config.budget_s = None;
            calib_cfg.process_config.eval_every = u32::MAX;
            calib_cfg.process_config.export_every = u32::MAX;
            calib_cfg.process_config.export_path =
                opts.out_dir.join("calib").display().to_string();
            let h = train::train_once(&dataset_dir, calib_cfg, calib_deadline, 0).await?;
            rate = Some(h.outcome.steps_per_s);
            rep.calib_steps_per_s = rate;
            phases.calib_ms = t.elapsed().as_millis() as u64;
            mem.sample();
            if calib_deadline.is_some_and(|d| Instant::now() >= d) {
                bail!("calibration ran past the budget");
            }
        }
        let remaining = opts
            .budget_s
            .map(|b| b - t0.elapsed().as_secs_f64() - opts.reserve_s)
            .unwrap_or(60.0)
            .max(1.0);
        let plan = pipeline.schedule.plan(remaining, rate);
        rep.plan_note = Some(plan.note);
        plan.total_iters
    };

    // --- train ---
    t = Instant::now();
    cfg.train_config.total_train_iters = total_iters;
    cfg.process_config.eval_every = total_iters; // eval fires at the last step
    cfg.process_config.export_every = total_iters;
    cfg.process_config.export_name = "export_{iter}.ply".to_string();
    let handle = train::train_once(&dataset_dir, cfg, deadline, opts.log_every).await?;
    let outcome = handle.outcome.clone();
    rep.train = outcome.clone();
    phases.train_ms = t.elapsed().as_millis() as u64;
    mem.sample();

    // --- export ---
    t = Instant::now();
    if let Some(splats) = handle.splats.clone() {
        export::export_ply(splats.clone(), &opts.out_dir, None).await?;
        rep.scene_ply = Some(opts.out_dir.join("scene.ply").display().to_string());
        export::export_splat(splats, &opts.out_dir).await?;
        rep.scene_splat = Some(
            opts.out_dir
                .join("scene.splat")
                .display()
                .to_string(),
        );
    }
    phases.export_ms = t.elapsed().as_millis() as u64;
    mem.sample();

    // --- score ---
    t = Instant::now();
    let eval_views: Vec<&Frame> = prep_cap
        .transforms
        .frames
        .iter()
        .enumerate()
        .filter(|(i, _)| i % opts.eval_split_every == 0)
        .map(|(_, f)| f)
        .collect();
    if let Some(splats) = splat_fallback(&handle) {
        // Score from the freshly exported splats — identical whether the
        // schedule finished or the deadline cut it.
        let (score, _brush_psnr, _brush_ssim) = eval::eval_current(
            splats,
            &prep_cap,
            &eval_views,
            outcome.iters_done,
            &opts.out_dir,
            &handle.device,
        )
        .await?;
        rep.score = Some(score);
    } else if let Some(eval_dir) = crate::score::resolve_eval_dir(&opts.out_dir, outcome.eval_iter)
    {
        let mut score = crate::score::score_eval_dir(&prepared, &eval_dir)?;
        score.ssim_mean = outcome.eval_ssim.map(|s| s as f64);
        rep.score = Some(score);
    }
    phases.score_ms = t.elapsed().as_millis() as u64;
    mem.sample();

    rep.phases = phases;
    rep.peak_mem_bytes = mem.peak;
    rep.wall_s = t0.elapsed().as_secs_f64();
    let run_json = opts.out_dir.join("run.json");
    std::fs::write(&run_json, serde_json::to_string_pretty(&rep)?)?;
    rep.run_json = Some(run_json.display().to_string());
    Ok(rep)
}

fn splat_fallback(
    h: &train::RunHandle,
) -> Option<brush_render::gaussian_splats::Splats> {
    h.splats.clone()
}

/// `farsight-splat bench`: one full pipeline run per budget, ascending.
pub async fn bench(
    base: &RunOptions,
    budgets: &[f64],
    pipeline: &Pipeline,
) -> Result<Vec<RunReport>> {
    let mut rows = Vec::new();
    for &b in budgets {
        let mut opts = base.clone();
        opts.budget_s = Some(b);
        opts.out_dir = base
            .out_dir
            .join(format!("b{}", b as u64));
        let rep = run(&opts, pipeline).await?;
        rows.push(rep);
    }
    Ok(rows)
}
