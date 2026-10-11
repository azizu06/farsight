use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use farsight_compiler::{core::*, export, io, metrics, pipeline, synth, view};
use std::path::PathBuf;
use std::time::Instant;

#[derive(Parser)]
#[command(name = "farsight-compile", about = "Point cloud -> photoreal 3D environment compiler")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Compile a point cloud (file or farsight URL) into mesh/GLB/splat artefacts.
    Compile {
        /// Point source: PLY / points.bin / dense pages / mesh.bin path, or http(s)://host:port of a farsight relay/server.
        #[arg(long)]
        input: String,
        /// Optional nerfstudio capture dir (transforms.json + images/) for texturing.
        #[arg(long)]
        capture: Option<PathBuf>,
        #[arg(long, default_value = "compiled")]
        out: PathBuf,
        #[arg(long, default_value = "balanced")]
        preset: String,
        /// JSON file overriding CompileOptions fields.
        #[arg(long)]
        config: Option<PathBuf>,
    },
    /// Write the synthetic ground-truth room scan (cloud.ply, capture/, truth.ply, scene.json).
    Synth {
        #[arg(long, default_value = "synth")]
        out: PathBuf,
        #[arg(long, default_value_t = 7)]
        seed: u64,
    },
    /// Serve the three.js viewer for a compiled output dir (scene.glb, mesh.ply, splat.ply, points.ply, report.json).
    View {
        /// Compiler output directory to serve.
        out: PathBuf,
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 8765)]
        port: u16,
        /// Open the viewer URL in the default browser.
        #[arg(long)]
        open: bool,
    },
    /// Synthetic scan -> compile -> quality metrics vs ground truth.
    Bench {
        #[arg(long, default_value = "bench-out")]
        out: PathBuf,
        #[arg(long, default_value = "balanced")]
        preset: String,
        #[arg(long, default_value_t = 7)]
        seed: u64,
        /// Skip texturing images (geometry-only bench).
        #[arg(long)]
        no_capture: bool,
        #[arg(long)]
        config: Option<PathBuf>,
    },
}

fn options(preset: &str, config: &Option<PathBuf>) -> Result<pipeline::CompileOptions> {
    let mut opts = pipeline::CompileOptions::preset(preset)?;
    if let Some(path) = config {
        let mut base = serde_json::to_value(&opts)?;
        let patch: serde_json::Value = serde_json::from_slice(&std::fs::read(path)?)?;
        merge(&mut base, patch);
        opts = serde_json::from_value(base)?;
    }
    Ok(opts)
}

fn merge(a: &mut serde_json::Value, b: serde_json::Value) {
    match (a, b) {
        (serde_json::Value::Object(a), serde_json::Value::Object(b)) => for (k, v) in b { merge(a.entry(k).or_insert(serde_json::Value::Null), v); },
        (a, b) => *a = b,
    }
}

fn write_report(out: &PathBuf, out_put: &pipeline::Output, extra: serde_json::Value) -> Result<()> {
    let total: f64 = out_put.report.iter().map(|r| r.ms).sum();
    let doc = serde_json::json!({
        "total_ms": total, "stages": out_put.report,
        "points_out": out_put.cloud.len(), "interpolated_points": out_put.cloud.interpolated_count(),
        "vertices": out_put.mesh.pos.len(), "triangles": out_put.mesh.triangles(),
        "textured": out_put.mesh.texture.is_some(), "extra": extra,
    });
    std::fs::write(out.join("report.json"), serde_json::to_vec_pretty(&doc)?)?;
    Ok(())
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Compile { input, capture, out, preset, config } => {
            let opts = options(&preset, &config)?;
            let t = Instant::now();
            let cloud = io::load_source(&input).with_context(|| format!("loading {input}"))?;
            let cap = capture.as_deref().map(io::load_capture).transpose()?;
            eprintln!("[compile] loaded {} points{} in {:.0} ms", cloud.len(),
                cap.as_ref().map(|c| format!(" + {} images", c.images.len())).unwrap_or_default(), t.elapsed().as_secs_f64() * 1e3);
            let result = pipeline::compile(cloud, cap.as_ref(), &opts);
            let files = export::write_all(&out, &result.cloud, &result.mesh, &opts.export)?;
            write_report(&out, &result, serde_json::json!({ "input": input, "preset": preset }))?;
            for f in files { eprintln!("[compile] wrote {}", f.display()); }
        }
        Cmd::Synth { out, seed } => {
            let scene = synth::Scene::room();
            let scan = synth::simulate(&scene, &synth::ScanOptions { seed, ..Default::default() });
            std::fs::create_dir_all(&out)?;
            std::fs::write(out.join("cloud.ply"), export::ply::points(&scan.cloud))?;
            let truth = PointCloud { pos: scan.truth.clone(), ..Default::default() };
            std::fs::write(out.join("truth.ply"), export::ply::points(&truth))?;
            std::fs::write(out.join("scene.json"), serde_json::to_vec_pretty(&scene)?)?;
            synth::write_capture(&out.join("capture"), &scan.capture)?;
            eprintln!("[synth] {} measured points, {} truth samples, {} images -> {}", scan.cloud.len(), scan.truth.len(), scan.capture.images.len(), out.display());
        }
        Cmd::View { out, host, port, open } => {
            view::serve(&out, &host, port, open)?;
        }
        Cmd::Bench { out, preset, seed, no_capture, config } => {
            let opts = options(&preset, &config)?;
            let scene = synth::Scene::room();
            let t = Instant::now();
            let scan = synth::simulate(&scene, &synth::ScanOptions { seed, ..Default::default() });
            eprintln!("[bench] synth {} pts in {:.0} ms", scan.cloud.len(), t.elapsed().as_secs_f64() * 1e3);
            let input_q = metrics::evaluate(&scan.cloud.pos, &scan.truth, 0.02, |p| scene.sdf(p).0, |p| scene.in_hole(p));
            let result = pipeline::compile(scan.cloud.clone(), if no_capture { None } else { Some(&scan.capture) }, &opts);
            let cloud_q = metrics::evaluate(&result.cloud.pos, &scan.truth, 0.02, |p| scene.sdf(p).0, |p| scene.in_hole(p));
            let mesh_pts = metrics::sample_mesh(&result.mesh, 400_000, seed);
            let mesh_q = metrics::evaluate(&mesh_pts, &scan.truth, 0.02, |p| scene.sdf(p).0, |p| scene.in_hole(p));
            let color = metrics::color_error(&result.mesh, |p| {
                let (_, id) = scene.sdf(p);
                let c = scene.shade(p, scene.normal(p), id);
                [linear_to_srgb(c[0]), linear_to_srgb(c[1]), linear_to_srgb(c[2])]
            });
            let files = export::write_all(&out, &result.cloud, &result.mesh, &opts.export)?;
            let q = serde_json::json!({ "input": input_q, "densified": cloud_q, "mesh": mesh_q, "mesh_vertex_color_err": color, "preset": preset, "seed": seed });
            write_report(&out, &result, q.clone())?;
            println!("{}", serde_json::to_string_pretty(&q)?);
            for f in files { eprintln!("[bench] wrote {}", f.display()); }
        }
    }
    Ok(())
}
