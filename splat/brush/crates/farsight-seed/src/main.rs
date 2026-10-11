//! farsight-seed CLI: capture dir -> seeded dataset dir.
//!
//!   farsight-seed <capture_dir> <out_dir> [flags]
//!
//! Writes into <out_dir>:
//!   transforms.json   (copy of the capture's, with ply_file_path -> seed.ply)
//!   seed.ply          (INRIA-layout splat PLY, Brush-ready)
//!   depth/            (farsight.sparse_depth.v1 for the T2/T3 depth loss)
//!   images/           (hard-linked frames; falls back to copies)
//!   seed_stats.json   (counts + timings for the ablation doc)

use anyhow::{Context, Result};
use clap::Parser;
use farsight_seed as fs;
use fs::views;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Parser)]
#[command(version, about = "LiDAR dots -> Brush splat seed")]
struct Args {
    /// Capture dir: images/ + transforms.json (nerfstudio) + init.ply
    /// (or `ply_file_path` inside transforms.json).
    capture_dir: PathBuf,
    /// Output dataset dir (created).
    out_dir: PathBuf,
    /// Splats in the emitted seed.
    #[arg(long, default_value_t = 300_000)]
    target_splats: usize,
    /// Thin (normal-axis) scale as fraction of local spacing.
    #[arg(long, default_value_t = 0.2)]
    thin: f32,
    /// Tangent scale multiplier on local spacing.
    #[arg(long, default_value_t = 0.5)]
    scale_mul: f32,
    /// SOR neighbours / std threshold.
    #[arg(long, default_value_t = 16)]
    sor_k: usize,
    #[arg(long, default_value_t = 2.0)]
    sor_std: f32,
    /// Every Nth view used for colour sampling (0 = auto).
    #[arg(long, default_value_t = 0)]
    colour_stride: usize,
    /// Every Nth view used for sparse-depth export (0 = colour_stride).
    #[arg(long, default_value_t = 0)]
    depth_stride: usize,
    /// Synthesize splats in photo-visible dot holes (every Nth view).
    #[arg(long)]
    fill: Option<usize>,
    /// Also write the raw (non-decimated) dot cloud PLY for comparison.
    #[arg(long)]
    dump_clean: bool,
}

fn main() -> Result<()> {
    let t0 = Instant::now();
    let args = Args::parse();

    let cap = views::read_capture(&args.capture_dir)
        .with_context(|| format!("reading {:?}", args.capture_dir))?;
    let dots_file = views::dots_path(&args.capture_dir, &cap);
    anyhow::ensure!(dots_file.exists(), "no dot cloud at {dots_file:?}");
    let (dots, dot_col) =
        views::read_dot_ply(&dots_file).with_context(|| format!("reading {dots_file:?}"))?;
    eprintln!(
        "[seed] capture: {} frames, {} dots ({:?})",
        cap.views.len(),
        dots.len(),
        t0.elapsed()
    );

    let mut cfg = fs::SeedConfig {
        target_splats: args.target_splats,
        thin: args.thin,
        scale_mul: args.scale_mul,
        ..Default::default()
    };
    cfg.clean.sor_k = args.sor_k;
    cfg.clean.sor_std = args.sor_std;
    cfg.colour.colour_stride = args.colour_stride;
    cfg.colour.depth_stride = args.depth_stride;
    if let Some(fs) = args.fill {
        cfg.fill = Some(fs::FillConfig {
            stride: fs.max(1),
            ..Default::default()
        });
    }

    // decode images only for the views that will actually be used
    let mut views = cap.views;
    let cstride = if cfg.colour.colour_stride > 0 {
        cfg.colour.colour_stride
    } else {
        fs::colour::auto_stride(views.len(), 96)
    };
    let fstride = cfg.fill.as_ref().map(|f| f.stride).unwrap_or(usize::MAX);
    let want: Vec<usize> = (0..views.len())
        .filter(|i| i % cstride == 0 || i % fstride == 0)
        .collect();
    views::load_images(&args.capture_dir, &mut views, &want);
    eprintln!(
        "[seed] images decoded for {} / {} views ({:?})",
        want.len(),
        views.len(),
        t0.elapsed()
    );

    let out = fs::run(&dots, dot_col.as_deref(), &views, &cfg);
    eprintln!(
        "[seed] pipeline: {} dots -> {} clean -> {} splats (+{} fill), {:?}",
        out.stats.dots_in, out.stats.after_clean, out.stats.after_decimate,
        out.stats.fill_added, t0.elapsed()
    );

    std::fs::create_dir_all(&args.out_dir)?;
    let ply = fs::emit::splats_ply(&out.splats);
    std::fs::write(args.out_dir.join("seed.ply"), &ply)?;

    // patched transforms.json: same frames, ply_file_path -> seed.ply
    let mut tf = cap.raw;
    tf["ply_file_path"] = serde_json::json!("seed.ply");
    std::fs::write(
        args.out_dir.join("transforms.json"),
        serde_json::to_vec_pretty(&tf)?,
    )?;

    // hard-link images (cheap on the same fs; copy as fallback)
    for v in &views {
        let src = args.capture_dir.join(&v.file_path);
        let dst = args.out_dir.join(&v.file_path);
        if let Some(p) = dst.parent() {
            std::fs::create_dir_all(p)?;
        }
        if std::fs::hard_link(&src, &dst).is_err() {
            std::fs::copy(&src, &dst)?;
        }
    }

    fs::emit::write_sparse_depth(&args.out_dir.join("depth"), &out.depth)?;

    let mut stats = serde_json::to_value(&out.stats)?; // SeedStats: Serialize
    stats["secs"] = serde_json::json!(t0.elapsed().as_secs_f64());
    stats["config"] = serde_json::json!({
        "target_splats": cfg.target_splats,
        "thin": cfg.thin,
        "scale_mul": cfg.scale_mul,
        "sor_k": cfg.clean.sor_k,
        "sor_std": cfg.clean.sor_std,
        "colour_stride": cstride,
        "depth_stride": cfg.colour.depth_stride,
        "fill": cfg.fill.is_some(),
    });
    std::fs::write(
        args.out_dir.join("seed_stats.json"),
        serde_json::to_vec_pretty(&stats)?,
    )?;
    eprintln!(
        "[seed] wrote {} splats -> {:?} ({:?} total)",
        out.splats.len(),
        args.out_dir,
        t0.elapsed()
    );
    Ok(())
}
