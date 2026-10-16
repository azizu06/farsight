#![recursion_limit = "256"]

use std::path::PathBuf;

use anyhow::Result;
use brush_process::config::TrainStreamConfig;
use clap::{Args, Parser, Subcommand};
use farsight_splat::hooks::{PassthroughSeed, Pipeline};
use farsight_splat::{Capture, RunOptions, bench, cull, eval, export, score, seed, synth};

#[derive(Parser)]
#[command(name = "farsight-splat", about = "Fast splat pipeline: capture -> cull -> seed -> train -> export -> score")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Args, Clone)]
struct PipelineArgs {
    /// Fixed step count (skips calibration + scheduling).
    #[arg(long)]
    steps: Option<u32>,
    /// Calibration run length in steps for rate measurement (0 = off).
    #[arg(long, default_value = "48")]
    calib_steps: u32,
    /// Seconds reserved inside the budget for export+score.
    #[arg(long, default_value = "3.0")]
    reserve_s: f64,
    /// Keep only frames A..=B before culling (find_drives --cut).
    #[arg(long, num_args = 2, value_names = ["A", "B"])]
    drive: Option<Vec<usize>>,
    /// Skip culling (keep every decodable, normal-tracking frame).
    #[arg(long)]
    no_cull: bool,
    /// Seeder: T5 farsight-seed | T1 raw-dot port | none (capture's own ply).
    /// (field is `seed_kind`: ProcessConfig::seed already owns the `seed` arg id)
    #[arg(long = "seed", value_enum)]
    seed_kind: Option<farsight_splat::seed::SeedKind>,
    /// Dot sources for --seed raw (points.bin / *.xyzrgb / *.ply).
    #[arg(long, num_args = 1..)]
    dots: Vec<PathBuf>,
    /// Use an existing ply as the seed verbatim.
    #[arg(long)]
    seed_ply: Option<PathBuf>,
    /// Extra args forwarded verbatim to the farsight-seed binary.
    #[arg(long, num_args = 1.., allow_hyphen_values = true)]
    seed_args: Vec<String>,
    /// Schedule: in-trainer wall-clock budget (T3) vs stock fixed iters.
    #[arg(long, value_enum)]
    schedule: Option<farsight_splat::pipeline::ScheduleKind>,
    /// Progress print cadence in iters.
    #[arg(long, default_value = "50")]
    log_every: u32,
    /// Full Brush training config passthrough (all upstream flags incl.
    /// --budget-s, --eval-split-every, --refine-mode, --sparse-adam, ...).
    #[clap(flatten)]
    config: TrainStreamConfig,
}

#[derive(Subcommand)]
enum Cmd {
    /// End-to-end run under a wall-clock budget.
    Run {
        capture_dir: PathBuf,
        #[arg(long, default_value = "out")]
        out: PathBuf,
        #[clap(flatten)]
        args: PipelineArgs,
    },
    /// One full run per budget; prints a table + writes bench.json.
    Bench {
        capture_dir: PathBuf,
        #[arg(long, default_value = "out")]
        out: PathBuf,
        #[arg(long, default_value = "30,60,120,300", value_delimiter = ',')]
        budgets: Vec<f64>,
        #[clap(flatten)]
        args: PipelineArgs,
    },
    /// Generate a synthetic phone-like capture.
    Synth {
        out: PathBuf,
        #[arg(long, default_value = "150")]
        frames: usize,
        #[arg(long, default_value = "640")]
        w: u32,
        #[arg(long, default_value = "480")]
        h: u32,
        #[arg(long, default_value = "80000")]
        dots: usize,
        #[arg(long, default_value = "7")]
        seed: u64,
    },
    /// List continuous drives in a capture (find_drives.py).
    Drives { capture_dir: PathBuf },
    /// Cut frames A..=B into a new capture dir (find_drives.py --cut).
    Cut {
        capture_dir: PathBuf,
        a: usize,
        b: usize,
        out: PathBuf,
    },
    /// Cull a capture into a new dir (cull.py).
    Cull { src: PathBuf, dst: PathBuf },
    /// Seed a dataset dir from dot files (seed_from_dots.py).
    Seed {
        dataset_dir: PathBuf,
        inputs: Vec<PathBuf>,
    },
    /// Brush .ply -> antimatter15 .splat (ply2splat.py).
    Ply2splat { src: PathBuf, dst: PathBuf },
    /// PSNR of eval renders vs photos (score_heldout.py).
    Score {
        images_dir: PathBuf,
        eval_dirs: Vec<PathBuf>,
    },
}

fn pipeline_of(args: &PipelineArgs) -> Pipeline {
    let mut p = Pipeline::default();
    if let Some(ply) = &args.seed_ply {
        p = p.with_seed(Box::new(PassthroughSeed { ply: ply.clone() }));
    }
    p
}

fn opts_of(capture_dir: &PathBuf, out: &PathBuf, args: &PipelineArgs) -> RunOptions {
    let budget_s = args.config.train_config.budget_s;
    let mut dots = args.dots.clone();
    // Raw seeding defaults to the dot files the capture already carries.
    if dots.is_empty() {
        for cand in ["points.bin", "dense.xyzrgb", "init.ply"] {
            let p = capture_dir.join(cand);
            if p.exists() {
                dots.push(p);
                break;
            }
        }
    }
    let seed_kind = match args.seed_kind {
        Some(k) => k,
        None if args.seed_ply.is_some() => farsight_splat::seed::SeedKind::None,
        None => farsight_splat::seed::SeedKind::Raw,
    };
    RunOptions {
        capture_dir: capture_dir.clone(),
        out_dir: out.clone(),
        budget_s,
        steps: args.steps,
        calib_steps: args.calib_steps,
        reserve_s: args.reserve_s,
        drive: args.drive.as_ref().map(|v| (v[0], v[1])),
        no_cull: args.no_cull,
        seed_inputs: dots,
        seed_kind,
        seed_args: args.seed_args.clone(),
        schedule: args.schedule.unwrap_or_default(),
        eval_split_every: args.config.load_config.eval_split_every.unwrap_or(8),
        log_every: args.log_every,
        config: args.config.clone(),
        backend: None,
    }
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format_timestamp_secs()
        .init();
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Synth {
            out,
            frames,
            w,
            h,
            dots,
            seed,
        } => {
            let rep = synth::synth(
                &out,
                &synth::SynthOpts {
                    frames,
                    w,
                    h,
                    dots,
                    seed,
                    ..Default::default()
                },
            )?;
            println!("{}", serde_json::to_string_pretty(&rep)?);
        }
        Cmd::Drives { capture_dir } => {
            let cap = Capture::load(&capture_dir)?;
            let drives = cull::find_drives(&cap.transforms.frames, 60.0);
            for d in &drives {
                println!(
                    "frames {:4}-{:4} ({:3})  {:5.0} s  path {:5.2} m  moving {:3}",
                    d.start, d.end, d.frames, d.seconds, d.path_m, d.moving
                );
            }
            if drives.is_empty() {
                println!("no motion in this run");
            }
        }
        Cmd::Cut {
            capture_dir,
            a,
            b,
            out,
        } => {
            let cap = Capture::load(&capture_dir)?;
            let keep: Vec<_> = cap.transforms.frames[a..=b].to_vec();
            let n = cap.write_subset(&out, &keep)?;
            println!("cut {n} frames -> {}", out.display());
        }
        Cmd::Cull { src, dst } => {
            let cap = Capture::load(&src)?;
            let (keep, rep) = cull::cull(&cap);
            cap.write_subset(&dst, &keep)?;
            println!("{}", serde_json::to_string_pretty(&rep)?);
        }
        Cmd::Seed { dataset_dir, inputs } => {
            let mut cap = Capture::load(&dataset_dir)?;
            let rep = seed::seed_from_dots(&mut cap, &inputs, &dataset_dir)?;
            cap.save(&dataset_dir)?;
            println!("{}", serde_json::to_string_pretty(&rep)?);
        }
        Cmd::Ply2splat { src, dst } => {
            let n = export::ply_to_splat(&src, &dst)?;
            println!("{n} splats -> {}", dst.display());
        }
        Cmd::Score {
            images_dir,
            eval_dirs,
        } => {
            for d in &eval_dirs {
                let rep = score::score_eval_dir(&images_dir, d)?;
                println!(
                    "{}: {} views, PSNR mean {:.2} median {:.2} min {:.2} max {:.2}",
                    d.display(),
                    rep.views,
                    rep.psnr_mean,
                    rep.psnr_median,
                    rep.psnr_min,
                    rep.psnr_max
                );
            }
        }
        Cmd::Run {
            capture_dir,
            out,
            args,
        } => {
            let (_dev, backend) = farsight_splat::device::init_device().await;
            log::info!("backend: {backend}");
            let pipeline = pipeline_of(&args);
            let mut opts = opts_of(&capture_dir, &out, &args);
            opts.backend = Some(backend);
            let rep = farsight_splat::run(&opts, &pipeline).await?;
            print_run(&rep);
        }
        Cmd::Bench {
            capture_dir,
            out,
            budgets,
            args,
        } => {
            let (_dev, backend) = farsight_splat::device::init_device().await;
            log::info!("backend: {backend}");
            let pipeline = pipeline_of(&args);
            let mut opts = opts_of(&capture_dir, &out, &args);
            opts.backend = Some(backend.clone());
            let rows = bench(&opts, &budgets, &pipeline).await?;
            std::fs::create_dir_all(&out)?;
            std::fs::write(
                out.join("bench.json"),
                serde_json::to_string_pretty(&rows)?,
            )?;
            println!(
                "{:>7} {:>7} {:>7} {:>9} {:>8} {:>8} {:>9}",
                "budget", "steps", "it/s", "splats", "psnr", "ssim", "wall_s"
            );
            for r in &rows {
                println!(
                    "{:>7.0} {:>7} {:>7.1} {:>9} {:>8} {:>8} {:>9.1}",
                    r.budget_s.unwrap_or(0.0),
                    r.train.iters_done,
                    r.train.steps_per_s,
                    r.train.splats,
                    r.score
                        .as_ref()
                        .map(|s| format!("{:.2}", s.psnr_mean))
                        .unwrap_or("-".into()),
                    r.score
                        .as_ref()
                        .and_then(|s| s.ssim_mean)
                        .map(|s| format!("{:.3}", s))
                        .unwrap_or("-".into()),
                    r.wall_s
                );
            }
            println!("bench.json -> {}", out.join("bench.json").display());
        }
    }
    Ok(())
}

fn print_run(r: &farsight_splat::RunReport) {
    let sc = r.score.as_ref();
    println!(
        "run: {} frames -> {} kept | {} steps ({:.1}/s) | {} splats | PSNR {} | wall {:.1}s | {}",
        r.frames_in,
        r.frames_kept,
        r.train.iters_done,
        r.train.steps_per_s,
        r.train.splats,
        sc.map(|s| format!("{:.2} dB", s.psnr_mean))
            .unwrap_or("n/a".into()),
        r.wall_s,
        r.out
    );
    // MAC_RECEIPT-format row: Prep/Train/Export/Score/Total/PSNR
    let prep_s = (r.phases.load_ms
        + r.phases.cull_ms
        + r.phases.write_ms
        + r.phases.seed_ms
        + r.phases.calib_ms) as f64
        / 1000.0;
    println!(
        "receipt Prep={:.1}s Train={:.1}s Export={:.1}s Score={:.1}s Total={:.1}s PSNR={}",
        prep_s,
        r.phases.train_ms as f64 / 1000.0,
        r.phases.export_ms as f64 / 1000.0,
        r.phases.score_ms as f64 / 1000.0,
        r.wall_s,
        sc.map(|s| format!("{:.2}", s.psnr_mean))
            .unwrap_or("n/a".into())
    );
    let _ = eval::eval_frames; // keep eval module referenced
}

#[cfg(test)]
mod cli_tests {
    use super::*;
    use clap::CommandFactory;

    /// Every subcommand's clap spec must pass clap's own definition checks
    /// (duplicate arg ids, bad defaults, ...). Regressed once when our
    /// `--seed` enum collided with `ProcessConfig::seed` (RNG seed u64).
    #[test]
    fn clap_debug_assert() {
        Cli::command().debug_assert();
    }

    #[test]
    fn parse_run_and_bench() {
        let run = Cli::try_parse_from([
            "farsight-splat",
            "run",
            "cap",
            "--out",
            "o",
            "--budget-s",
            "60",
            "--seed",
            "raw",
            "--schedule",
            "stock",
            "--refine-mode",
            "budget",
        ])
        .expect("run parse");
        let Cmd::Run { args, .. } = run.cmd else {
            panic!("not run");
        };
        assert_eq!(args.config.train_config.budget_s, Some(60.0));
        assert_eq!(args.seed_kind, Some(farsight_splat::seed::SeedKind::Raw));
        assert_eq!(
            args.schedule,
            Some(farsight_splat::pipeline::ScheduleKind::Stock)
        );
        let bench = Cli::try_parse_from([
            "farsight-splat",
            "bench",
            "cap",
            "--out",
            "o",
            "--budgets",
            "30,60",
            "--seed",
            "farsight",
        ])
        .expect("bench parse");
        let Cmd::Bench { budgets, .. } = bench.cmd else {
            panic!("not bench");
        };
        assert_eq!(budgets, vec![30.0, 60.0]);
    }
}
