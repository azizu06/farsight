//! LiDAR-dot seeding: port of `seed_from_dots.py` — read the jetson's dot
//! formats, statistically de-outlier them, and write a Brush `init.ply`
//! (binary little-endian xyz + rgb) that `transforms.json` points at.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use ball_tree::BallTree;
use serde::Serialize;

use crate::capture::Capture;

/// One seed dot: world position + display color.
#[derive(Clone, Copy, Debug)]
pub struct Dot {
    pub p: [f32; 3],
    pub rgb: [u8; 3],
}

/// Jetson `points.bin`: u32 LE count, then count 16 B records
/// `[f32 x y z][u8 r g b pad]`.
pub fn read_points_bin(path: &Path) -> Result<Vec<Dot>> {
    let raw = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    if raw.len() < 4 {
        bail!("{}: too short for points.bin", path.display());
    }
    let n = u32::from_le_bytes(raw[0..4].try_into().unwrap()) as usize;
    let body = &raw[4..];
    if body.len() < n * 16 {
        bail!(
            "{}: {} bytes for {} records, want {}",
            path.display(),
            body.len(),
            n,
            n * 16
        );
    }
    Ok(parse_records(&body[..n * 16]))
}

/// Dense map dump (`dense.xyzrgb`): 16 B records, no header.
pub fn read_dense_xyzrgb(path: &Path) -> Result<Vec<Dot>> {
    let raw = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    if raw.len() % 16 != 0 {
        bail!("{}: {} bytes not a multiple of 16", path.display(), raw.len());
    }
    Ok(parse_records(&raw))
}

fn parse_records(raw: &[u8]) -> Vec<Dot> {
    raw.chunks_exact(16)
        .map(|r| Dot {
            p: [
                f32::from_le_bytes(r[0..4].try_into().unwrap()),
                f32::from_le_bytes(r[4..8].try_into().unwrap()),
                f32::from_le_bytes(r[8..12].try_into().unwrap()),
            ],
            rgb: [r[12], r[13], r[14]],
        })
        .collect()
}

/// An existing `init.ply` (our binary writer or Dechante's, plus a plain-ascii
/// fallback) read back as dots. Honors `subsample` like Brush's loader.
pub fn read_init_ply(path: &Path) -> Result<Vec<Dot>> {
    let raw = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let end = raw
        .windows(b"end_header\n".len())
        .position(|w| w == b"end_header\n")
        .map(|i| i + b"end_header\n".len())
        .context("init.ply: no end_header")?;
    let header = String::from_utf8_lossy(&raw[..end]);
    let binary = header.contains("binary_little_endian");
    let n: usize = header
        .lines()
        .find(|l| l.starts_with("element vertex"))
        .and_then(|l| l.split_whitespace().last())
        .and_then(|v| v.parse().ok())
        .context("init.ply: no vertex count")?;
    let props: Vec<(String, String)> = header
        .lines()
        .filter(|l| l.starts_with("property"))
        .map(|l| {
            let mut w = l.split_whitespace().skip(1);
            (w.next().unwrap_or("").to_string(), w.next().unwrap_or("").to_string())
        })
        .collect();
    let idx = |name: &str| props.iter().position(|(_, p)| p == name);
    let sz = |t: &str| -> usize {
        match t {
            "float" | "float32" | "int" | "int32" | "uint" | "uint32" => 4,
            "double" | "float64" | "int64" | "uint64" => 8,
            "short" | "ushort" | "int16" | "uint16" => 2,
            _ => 1,
        }
    };
    let mut offs = Vec::with_capacity(props.len());
    let mut rec = 0usize;
    for (t, _) in &props {
        offs.push(rec);
        rec += sz(t);
    }
    let (ix, iy, iz) = (idx("x"), idx("y"), idx("z"));
    let (ir, ig, ib) = (idx("red"), idx("green"), idx("blue"));
    let (Some(ix), Some(iy), Some(iz)) = (ix, iy, iz) else {
        bail!("init.ply: no x/y/z properties");
    };
    let mut dots = Vec::with_capacity(n);
    if binary {
        // Byte offsets from declared property types; the standard init.ply
        // (x,y,z f32 + r,g,b u8) lands colors at bytes 12,13,14.
        for i in 0..n {
            let r = &raw[end + i * rec..end + (i + 1) * rec];
            let f = |o: usize| f32::from_le_bytes(r[o..o + 4].try_into().unwrap());
            dots.push(Dot {
                p: [f(offs[ix]), f(offs[iy]), f(offs[iz])],
                rgb: [
                    ir.and_then(|i| r.get(offs[i]).copied()).unwrap_or(255),
                    ig.and_then(|i| r.get(offs[i]).copied()).unwrap_or(255),
                    ib.and_then(|i| r.get(offs[i]).copied()).unwrap_or(255),
                ],
            });
        }
    } else {
        for line in String::from_utf8_lossy(&raw[end..]).lines().take(n) {
            let v: Vec<&str> = line.split_whitespace().collect();
            if v.len() <= iz {
                continue;
            }
            let f = |i: usize| v.get(i).and_then(|s| s.parse::<f32>().ok()).unwrap_or(0.0);
            dots.push(Dot {
                p: [f(ix), f(iy), f(iz)],
                rgb: [
                    ir.map(|i| f(i) as u8).unwrap_or(255),
                    ig.map(|i| f(i) as u8).unwrap_or(255),
                    ib.map(|i| f(i) as u8).unwrap_or(255),
                ],
            });
        }
    }
    Ok(dots)
}

/// Write the Brush seed file Dechante's script writes: binary LE ply with
/// float xyz + uchar rgb, and set `ply_file_path` in transforms.json.
pub fn write_init_ply(path: &Path, dots: &[Dot]) -> Result<()> {
    let hdr = format!(
        "ply\nformat binary_little_endian 1.0\nelement vertex {}\nproperty float x\n\
         property float y\nproperty float z\nproperty uchar red\nproperty uchar green\n\
         property uchar blue\nend_header\n",
        dots.len()
    );
    let mut out = hdr.into_bytes();
    out.reserve(dots.len() * 15);
    for d in dots {
        out.extend_from_slice(&d.p[0].to_le_bytes());
        out.extend_from_slice(&d.p[1].to_le_bytes());
        out.extend_from_slice(&d.p[2].to_le_bytes());
        out.extend_from_slice(&d.rgb);
    }
    std::fs::write(path, out)?;
    Ok(())
}

/// Open3D `remove_statistical_outlier(k=16, std_ratio=2.0)`: mean distance to
/// the k nearest neighbors per point; drop points above mean + 2*std of those
/// averages. Subsamples first when `dots` is huge so the kNN pass stays fast.
pub fn remove_statistical_outliers(dots: Vec<Dot>, k: usize, std_ratio: f64) -> Vec<Dot> {
    if dots.len() <= k + 1 {
        return dots;
    }
    let pts: Vec<[f64; 3]> = dots
        .iter()
        .map(|d| [d.p[0] as f64, d.p[1] as f64, d.p[2] as f64])
        .collect();
    let tree = BallTree::new(pts.clone(), vec![(); pts.len()]);
    // Mean distance to k nearest neighbors (excluding the point itself).
    let means: Vec<f64> = pts
        .iter()
        .map(|p| {
            let mut q = tree.query();
            let mut dists: Vec<f64> = q.nn(p).take(k + 1).map(|item| item.1).collect();
            if dists.len() > 1 {
                dists.remove(0); // self at distance 0
            }
            if dists.is_empty() {
                0.0
            } else {
                dists.iter().sum::<f64>() / dists.len() as f64
            }
        })
        .collect();
    let n = means.len() as f64;
    let mean = means.iter().sum::<f64>() / n;
    let std = (means.iter().map(|m| (m - mean).powi(2)).sum::<f64>() / n).sqrt();
    let thresh = mean + std_ratio * std;
    dots.into_iter()
        .zip(means)
        .filter(|(_, m)| *m <= thresh)
        .map(|(d, _)| d)
        .collect()
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct SeedReport {
    pub source: String,
    pub input_points: usize,
    pub kept_points: usize,
    pub ply: Option<String>,
}

/// Seed `cap_dir`'s transforms.json with an init.ply built from the given
/// inputs. `inputs` is a list of point sources; each may be a points.bin
/// (u32-count header), a dense .xyzrgb dump, or an existing .ply.
pub fn seed_from_dots(cap: &mut Capture, inputs: &[PathBuf], out_dir: &Path) -> Result<SeedReport> {
    let mut dots = Vec::new();
    let mut srcs = Vec::new();
    for p in inputs {
        let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("");
        let part = match ext {
            "bin" => read_points_bin(p)?,
            "xyzrgb" => read_dense_xyzrgb(p)?,
            "ply" => read_init_ply(p)?,
            _ => bail!("{}: unknown dots extension .{}", p.display(), ext),
        };
        srcs.push(format!("{}({})", p.display(), part.len()));
        dots.extend(part);
    }
    let input = dots.len();
    let kept = remove_statistical_outliers(dots, 16, 2.0);
    let ply = out_dir.join("init.ply");
    write_init_ply(&ply, &kept)?;
    cap.transforms.ply_file_path = Some("init.ply".to_string());
    Ok(SeedReport {
        source: srcs.join(" + "),
        input_points: input,
        kept_points: kept.len(),
        ply: Some(ply.display().to_string()),
    })
}

/// Which seeder a run uses.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, clap::ValueEnum, serde::Serialize)]
pub enum SeedKind {
    /// No seeding: train from whatever `ply_file_path` the capture has.
    None,
    /// T1's port of seed_from_dots.py: dot files -> SOR -> init.ply.
    #[default]
    Raw,
    /// T5's farsight-seed binary (decimated, view-coloured seed + depth).
    Farsight,
}

/// Locate the `farsight-seed` binary: $FARSIGHT_SEED, next to our own exe,
/// the workspace target dir, then PATH.
fn farsight_seed_bin() -> Result<PathBuf> {
    if let Ok(p) = std::env::var("FARSIGHT_SEED") {
        return Ok(PathBuf::from(p));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            let p = d.join("farsight-seed");
            if p.exists() {
                return Ok(p);
            }
        }
    }
    for p in [
        "target/release/farsight-seed",
        "splat/brush/target/release/farsight-seed",
        "../farsight-seed/target/release/farsight-seed",
        "farsight-seed",
    ] {
        if Path::new(p).exists() || p == "farsight-seed" {
            return Ok(PathBuf::from(p));
        }
    }
    Ok(PathBuf::from("farsight-seed"))
}

/// Run T5's `farsight-seed <capture_dir> <out_dir>` (it writes transforms.json
/// with ply_file_path -> seed.ply plus depth/ and linked images).
pub fn run_farsight_seed(capture_dir: &Path, out_dir: &Path, extra: &[String]) -> Result<SeedReport> {
    let bin = farsight_seed_bin()?;
    let t0 = std::time::Instant::now();
    let status = std::process::Command::new(&bin)
        .arg(capture_dir)
        .arg(out_dir)
        .args(extra)
        .status()
        .with_context(|| format!("spawning {}", bin.display()))?;
    if !status.success() {
        anyhow::bail!("farsight-seed exited {status}");
    }
    let stats = out_dir.join("seed_stats.json");
    let mut rep = SeedReport {
        source: "farsight-seed".to_string(),
        ply: Some(out_dir.join("seed.ply").display().to_string()),
        ..Default::default()
    };
    if let Ok(s) = std::fs::read_to_string(&stats) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&s) {
            rep.input_points = v["input_points"].as_u64().unwrap_or(0) as usize;
            rep.kept_points = v["splats"]
                .as_u64()
                .or_else(|| v["kept_points"].as_u64())
                .unwrap_or(0) as usize;
        }
    }
    Ok(rep)
}
