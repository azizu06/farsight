//! farsight-views: phone-capture conditioning for Brush splat training.
//!
//! Subcommands (see `splat/docs/views.md` for ablations):
//!   select   blur cull + parked-duplicate cull + coverage-max subset with a
//!            held-out split identical to nerfstudio eval-split-every
//!   masks    temporal-median transient (people/ghost) masks -> masks/images/
//!   exposure per-view affine colour/exposure normalization toward a shared
//!            dataset tone (fair eval: eval images can be fit on half the
//!            frame only)

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use serde_json::Value;
#[cfg(all(target_os = "macos", feature = "vision"))]
mod vision_mac;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Parser)]
#[command(name = "farsight-views")]
struct Args {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Cull blurry/duplicate views and pick a coverage-maximising subset.
    Select {
        #[arg(long)] in_dir: PathBuf,
        #[arg(long)] out_dir: PathBuf,
        /// Max number of TRAIN views to keep (0 = keep all after culls).
        #[arg(long, default_value_t = 0)] budget: usize,
        /// Held-out split period, identical to eval-split-every (0 = none).
        #[arg(long, default_value_t = 8)] eval_every: usize,
        /// Drop this fraction of the blurriest training views.
        #[arg(long, default_value_t = 0.05)] blur_drop_frac: f64,
        /// Also drop any view with blur < this * median blur (0 = off).
        #[arg(long, default_value_t = 0.5)] blur_rel_thresh: f64,
        /// Parked-duplicate cull: translations closer than this (m) ...
        #[arg(long, default_value_t = 0.02)] dup_eps_t: f64,
        /// ... and look directions closer than this (radians) collapse to
        /// one cluster; the sharpest frame wins.
        #[arg(long, default_value_t = 0.01)] dup_eps_r: f64,
        /// Weight of view direction (rad-ish units per metre) in the
        /// coverage metric.
        #[arg(long, default_value_t = 0.5)] dir_weight: f64,
        /// Decode width for blur scoring.
        #[arg(long, default_value_t = 160)] blur_res: u32,
        /// Sharpness engine: auto | rust | vision. `vision` = vImage
        /// Laplacian (Accelerate) on macOS builds with --features vision.
        #[arg(long, default_value = "auto")] engine: String,
    },
    /// Temporal-median transient masks for training frames.
    Masks {
        #[arg(long)] in_dir: PathBuf,
        /// transforms.json to mask (defaults to <in_dir>/transforms.json).
        #[arg(long)] transforms: Option<PathBuf>,
        /// Pose-nearest neighbours for the median stack.
        #[arg(long, default_value_t = 5)] k: usize,
        /// Working width for mask computation.
        #[arg(long, default_value_t = 320)] res: u32,
        /// Per-channel residual threshold for "transient" (0-255).
        #[arg(long, default_value_t = 28)] thresh: u8,
        /// Dilation radius (px at working res) around transient pixels.
        #[arg(long, default_value_t = 2)] dilate: u32,
        /// Mask engine: auto | temporal | vision. `vision` = Apple
        /// VNGeneratePersonSegmentationRequest (balanced → ANE) on macOS
        /// builds with --features vision; `auto` picks it when available.
        #[arg(long, default_value = "auto")] engine: String,
    },
    /// Per-view SE3 pose refinement via direct photometric alignment:
    /// LiDAR-seeded anchors (ply rgb if present, else LiDAR depth
    /// projected into a pose-neighbour frame to steal its colours) give
    /// shared 3D points; each view's pose is Gauss-Newton-refined to
    /// minimise |I_i(proj p) - colour(p)| with Huber weights. Absorbs
    /// ARKit-style drift. Writes refined transforms.json.
    PoseRefine {
        #[arg(long)] in_dir: PathBuf,
        /// LiDAR seed (e.g. init.ply).
        #[arg(long)] ply: PathBuf,
        /// transforms.json to refine (defaults to <in_dir>/transforms.json).
        #[arg(long)] transforms: Option<PathBuf>,
        /// Output transforms.json (defaults to in-place update).
        #[arg(long)] out_transforms: Option<PathBuf>,
        /// Pose-neighbours used as colour sources when the ply has no rgb.
        #[arg(long, default_value_t = 3)] k: usize,
        /// Working decode width for photometric sampling.
        #[arg(long, default_value_t = 320)] res: u32,
        /// Gauss-Newton rounds.
        #[arg(long, default_value_t = 8)] iters: u32,
        /// Huber delta (0-255 luma units).
        #[arg(long, default_value_t = 16.0)] huber: f64,
        /// Max anchors per view.
        #[arg(long, default_value_t = 4000)] max_anchors: usize,
        /// Reject pose updates whose final robust cost isn't lower than
        /// the original pose's.
        #[arg(long, default_value_t = true)] guard: bool,
    },
    /// Per-view affine gain/bias normalization of TRAIN images toward the
    /// dataset-wide tone. Eval images are copied unchanged by default;
    /// --eval-half-fit fits the affine on the top half only and applies it
    /// to the whole eval image (honest "appearance-fair" metric variant).
    /// --mode picks ONE appearance strategy (they must not double-apply):
    ///   offline = rewrite train images (this command);
    ///   learned = copy frames unchanged, mark views_meta.json for the
    ///             in-trainer affine bank (`brush-cli --appearance-lr`);
    ///   none    = copy frames unchanged, no compensation.
    Exposure {
        #[arg(long)] in_dir: PathBuf,
        #[arg(long)] out_dir: PathBuf,
        #[arg(long, default_value = "offline")] mode: String,
        #[arg(long, default_value_t = false)] eval_half_fit: bool,
        /// JPEG quality for rewritten train images.
        #[arg(long, default_value_t = 95)] jpeg_quality: u8,
    },
}

// ---------- shared helpers ----------

struct Frame {
    /// index in the frames array
    idx: usize,
    file_path: String,
    pos: [f64; 3],
    fwd: [f64; 3],
}

fn load_transforms(path: &Path) -> Result<(Value, Vec<Frame>)> {
    let text = fs::read_to_string(path).context("read transforms.json")?;
    let json: Value = serde_json::from_str(&text).context("parse transforms.json")?;
    let frames = json["frames"]
        .as_array()
        .context("transforms.json: no frames array")?;
    let mut out = Vec::with_capacity(frames.len());
    for (idx, f) in frames.iter().enumerate() {
        let file_path = f["file_path"].as_str().context("frame file_path")?.to_string();
        let m = f["transform_matrix"]
            .as_array()
            .context("frame transform_matrix")?;
        let get = |r: usize, c: usize| m[r][c].as_f64().unwrap_or(0.0);
        // world position = column 3; look dir = -Z column of the rotation.
        let pos = [get(0, 3), get(1, 3), get(2, 3)];
        let fwd = [-get(0, 2), -get(1, 2), -get(2, 2)];
        out.push(Frame { idx, file_path, pos, fwd });
    }
    Ok((json, out))
}

/// Decode an image at ~`req_w` px wide using jpeg IDCT scaling when possible.
fn decode_small(path: &Path, req_w: u32) -> Result<(u32, u32, Vec<u8>)> {
    let bytes = fs::read(path)?;
    if path
        .extension()
        .is_some_and(|e| e.eq_ignore_ascii_case("jpg") || e.eq_ignore_ascii_case("jpeg"))
    {
        let mut dec = jpeg_decoder::Decoder::new(&bytes[..]);
        dec.read_info().context("jpeg read_info")?;
        let info = dec.info().context("jpeg info")?;
        let w = info.width as u32;
        let req_h = (req_w as u64 * info.height as u64 / w as u64) as u16;
        let (dw, dh) = dec.scale(req_w.min(u16::MAX as u32) as u16, req_h)?;
        let px = dec.decode().context("jpeg decode")?;
        Ok((dw as u32, dh as u32, px))
    } else {
        let img = image::load_from_memory(&bytes)?.to_rgb8();
        let (w, h) = img.dimensions();
        let nh = (req_w as u64 * h as u64 / w as u64).max(1) as u32;
        let img = image::imageops::resize(&img, req_w, nh, image::imageops::FilterType::Triangle);
        Ok((req_w, nh, img.into_raw()))
    }
}

/// Luminance plane (0-255) from interleaved RGB.
fn luma(w: usize, h: usize, rgb: &[u8]) -> Vec<u8> {
    (0..w * h)
        .map(|i| {
            let r = rgb[3 * i] as u32;
            let g = rgb[3 * i + 1] as u32;
            let b = rgb[3 * i + 2] as u32;
            ((r * 299 + g * 587 + b * 114) / 1000) as u8
        })
        .collect()
}

/// Variance of the Laplacian — standard blur score (bigger = sharper).
fn blur_score(w: usize, h: usize, y: &[u8]) -> f64 {
    if w < 3 || h < 3 {
        return f64::MAX;
    }
    let mut sum = 0f64;
    let mut sq = 0f64;
    let mut n = 0f64;
    for yy in 1..h - 1 {
        for xx in 1..w - 1 {
            let c = y[yy * w + xx] as f64;
            let lap = 4.0 * c
                - y[yy * w + xx - 1] as f64
                - y[yy * w + xx + 1] as f64
                - y[(yy - 1) * w + xx] as f64
                - y[(yy + 1) * w + xx] as f64;
            sum += lap;
            sq += lap * lap;
            n += 1.0;
        }
    }
    sq / n - (sum / n) * (sum / n)
}

fn angle_between(a: [f64; 3], b: [f64; 3]) -> f64 {
    let dot = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]).clamp(-1.0, 1.0);
    dot.acos()
}

fn dist(a: [f64; 3], b: [f64; 3]) -> f64 {
    ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt()
}

// ---------- select ----------

fn cmd_select(
    in_dir: &Path,
    out_dir: &Path,
    budget: usize,
    eval_every: usize,
    blur_drop_frac: f64,
    blur_rel_thresh: f64,
    dup_eps_t: f64,
    dup_eps_r: f64,
    dir_weight: f64,
    blur_res: u32,
    engine: &str,
) -> Result<()> {
    let (json, frames) = load_transforms(&in_dir.join("transforms.json"))?;
    let n = frames.len();

    let _use_vimage = want_vision(engine, "select")?;
    #[cfg(all(target_os = "macos", feature = "vision"))]
    let use_vimage = _use_vimage;
    // Blur scores for every frame (cheap 160px decode).
    let mut scores = vec![0f64; n];
    for f in frames.iter() {
        let p = in_dir.join(&f.file_path);
        match decode_small(&p, blur_res) {
            Ok((w, h, rgb)) => {
                let y = luma(w as usize, h as usize, &rgb);
                #[cfg(all(target_os = "macos", feature = "vision"))]
                if use_vimage {
                    scores[f.idx] =
                        vision_mac::sharpness_vimage(w as usize, h as usize, &y);
                    continue;
                }
                scores[f.idx] = blur_score(w as usize, h as usize, &y);
            }
            Err(e) => {
                eprintln!("warn: {}: {e:#}; keeping with worst score", f.file_path);
                scores[f.idx] = 0.0;
            }
        }
    }

    // Held-out split: identical membership to nerfstudio eval_split_every.
    let eval: Vec<usize> = if eval_every > 0 {
        (0..n).filter(|i| i % eval_every == 0).collect()
    } else {
        Vec::new()
    };
    let mut pool: Vec<usize> = (0..n).filter(|i| eval_every == 0 || i % eval_every != 0).collect();

    // Blur cull within the train pool.
    let mut sorted: Vec<f64> = pool.iter().map(|&i| scores[i]).collect();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median = sorted[sorted.len() / 2];
    let q_idx = ((pool.len() as f64) * blur_drop_frac) as usize;
    let frac_floor = if q_idx > 0 { sorted[q_idx - 1] } else { 0.0 };
    let floor = frac_floor.max(blur_rel_thresh * median);
    let before = pool.len();
    pool.retain(|&i| scores[i] >= floor || floor <= 0.0);
    eprintln!(
        "blur cull: {}/{} train views dropped (floor {:.1}, median {:.1})",
        before - pool.len(),
        before,
        floor,
        median
    );

    // Parked-duplicate cull: greedy clusters in pose space; sharpest wins.
    let mut taken = vec![false; n];
    let mut kept = Vec::with_capacity(pool.len());
    for &i in pool.iter() {
        if taken[i] {
            continue;
        }
        let mut cluster = vec![i];
        for &j in pool.iter() {
            if j != i
                && !taken[j]
                && dist(frames[i].pos, frames[j].pos) < dup_eps_t
                && angle_between(frames[i].fwd, frames[j].fwd) < dup_eps_r
            {
                cluster.push(j);
            }
        }
        let best = cluster
            .iter()
            .copied()
            .max_by(|a, b| scores[*a].partial_cmp(&scores[*b]).unwrap())
            .unwrap();
        for &c in &cluster {
            taken[c] = true;
        }
        kept.push(best);
    }
    eprintln!("dup cull: {} -> {} train views", pool.len(), kept.len());

    // Coverage-max subset: farthest-point sampling on [pos, dir_weight*fwd].
    if budget > 0 && kept.len() > budget {
        let pt = |i: usize| -> [f64; 6] {
            let f = &frames[i];
            [
                f.pos[0],
                f.pos[1],
                f.pos[2],
                dir_weight * f.fwd[0],
                dir_weight * f.fwd[1],
                dir_weight * f.fwd[2],
            ]
        };
        let mut chosen = vec![kept[0]];
        let mut dmin: Vec<f64> = kept.iter().map(|&i| d6(pt(i), pt(kept[0]))).collect();
        while chosen.len() < budget {
            let (arg, _) = kept
                .iter()
                .zip(dmin.iter())
                .enumerate()
                .max_by(|a, b| a.1 .1.partial_cmp(b.1 .1).unwrap())
                .unwrap();
            let next = kept[arg];
            chosen.push(next);
            for (k, d) in kept.iter().zip(dmin.iter_mut()) {
                let nd = d6(pt(*k), pt(next));
                if nd < *d {
                    *d = nd;
                }
            }
            dmin[arg] = 0.0;
        }
        kept = chosen;
    }
    kept.sort_unstable();
    eprintln!("coverage select: kept {} train views (budget {})", kept.len(), budget);

    write_capture(&json, &frames, in_dir, out_dir, &kept, &eval)
}

fn d6(a: [f64; 6], b: [f64; 6]) -> f64 {
    a.iter().zip(b.iter()).map(|(x, y)| (x - y).powi(2)).sum::<f64>().sqrt()
}

/// Emit a self-contained capture dir: transforms.json (train subset),
/// transforms_val.json (eval set) and images/ links (hardlink, fallback copy).
fn write_capture(
    json: &Value,
    frames: &[Frame],
    in_dir: &Path,
    out_dir: &Path,
    train_idx: &[usize],
    eval_idx: &[usize],
) -> Result<()> {
    fs::create_dir_all(out_dir.join("images"))?;
    let frames_arr = json["frames"].as_array().unwrap();

    let keep: Vec<usize> = train_idx.iter().chain(eval_idx.iter()).copied().collect();
    for &i in &keep {
        let src = in_dir.join(&frames[i].file_path);
        let name = Path::new(&frames[i].file_path)
            .file_name()
            .context("frame name")?;
        let dst = out_dir.join("images").join(name);
        if !dst.exists() && fs::hard_link(&src, &dst).is_err() {
            fs::copy(&src, &dst).context("copy frame")?;
        }
    }

    let emit = |indices: &[usize], dest: &Path| -> Result<()> {
        let mut obj = json.clone();
        let sel: Vec<Value> = indices
            .iter()
            .map(|&i| {
                let mut f = frames_arr[i].clone();
                let name = Path::new(&frames[i].file_path).file_name().unwrap();
                f["file_path"] = Value::String(format!("images/{}", name.to_string_lossy()));
                f
            })
            .collect();
        obj["frames"] = Value::Array(sel);
        fs::write(dest, serde_json::to_string_pretty(&obj)?)?;
        Ok(())
    };
    emit(train_idx, &out_dir.join("transforms.json"))?;
    if !eval_idx.is_empty() {
        emit(eval_idx, &out_dir.join("transforms_val.json"))?;
    }
    Ok(())
}

// ---------- masks ----------

/// True if the requested engine should use the macOS Vision path.
fn want_vision(engine: &str, what: &str) -> Result<bool> {
    match engine {
        "auto" => Ok(cfg!(all(target_os = "macos", feature = "vision"))),
        "vision" => {
            if cfg!(all(target_os = "macos", feature = "vision")) {
                Ok(true)
            } else {
                bail!("{what}: --engine vision needs a macOS build with --features vision")
            }
        }
        "temporal" | "rust" | "laplacian" => Ok(false),
        _ => bail!("unknown --engine '{engine}'"),
    }
}

fn cmd_masks(
    in_dir: &Path,
    transforms: Option<PathBuf>,
    k: usize,
    res: u32,
    thresh: u8,
    dilate: u32,
    engine: &str,
) -> Result<()> {
    let tpath = transforms.unwrap_or_else(|| in_dir.join("transforms.json"));
    let (mut json, frames) = load_transforms(&tpath)?;
    let n = frames.len();

    // Neighbour sets by pose distance (position + direction).
    let neigh: Vec<Vec<usize>> = frames
        .iter()
        .map(|f| {
            let mut ds: Vec<(f64, usize)> = frames
                .iter()
                .map(|g| {
                    (
                        dist(f.pos, g.pos) + angle_between(f.fwd, g.fwd) * 0.5,
                        g.idx,
                    )
                })
                .collect();
            ds.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
            ds.iter().take(k.max(2)).map(|&(_, i)| i).collect()
        })
        .collect();

    // Decode neighbours once each at working res.
    let mut planes: Vec<Option<(u32, u32, Vec<u8>)>> = vec![None; n];
    for (i, f) in frames.iter().enumerate() {
        planes[i] = decode_small(&in_dir.join(&f.file_path), res).ok();
    }

    let mask_dir = in_dir.join("masks").join("images");
    fs::create_dir_all(&mask_dir)?;

    // Vision engine: per-frame person segmentation (ANE). Produces the
    // identical mask layout — person pixels become transient (0).
    #[cfg(all(target_os = "macos", feature = "vision"))]
    if want_vision(engine, "masks")? {
        let mut patched = 0usize;
        for (i, f) in frames.iter().enumerate() {
            let Ok((w, h, conf)) =
                (unsafe { vision_mac::person_mask(&in_dir.join(&f.file_path)) })
            else {
                continue;
            };
            // conf > 128 ⇒ person ⇒ transient.
            let mut mask: Vec<u8> =
                conf.iter().map(|&c| if c > 128 { 0 } else { 255 }).collect();
            if dilate > 0 {
                let src = mask.clone();
                let r = dilate as usize;
                for yy in 0..h {
                    for xx in 0..w {
                        if src[yy * w + xx] == 0 {
                            for dy in yy.saturating_sub(r)..=(yy + r).min(h - 1) {
                                for dx in xx.saturating_sub(r)..=(xx + r).min(w - 1) {
                                    mask[dy * w + dx] = 0;
                                }
                            }
                        }
                    }
                }
            }
            let name = Path::new(&f.file_path)
                .file_stem()
                .context("frame stem")?
                .to_string_lossy()
                .to_string();
            image::GrayImage::from_raw(w as u32, h as u32, mask)
                .unwrap()
                .save(mask_dir.join(format!("{name}.png")))?;
            if let Some(fj) = json["frames"].as_array_mut().and_then(|a| a.get_mut(i)) {
                fj["mask_path"] =
                    Value::String(format!("masks/images/{name}.png"));
                patched += 1;
            }
        }
        fs::write(&tpath, serde_json::to_string_pretty(&json)?)?;
        eprintln!("masks: vision engine, {patched}/{n} frames patched");
        return Ok(());
    }
    if engine == "vision" {
        bail!("masks: --engine vision needs a macOS build with --features vision");
    }
    let _ = want_vision(engine, "masks")?;

    let frames_arr = json["frames"].as_array().unwrap().clone();
    let mut patched = 0usize;
    for (i, f) in frames.iter().enumerate() {
        let Some((w, h, ref rgb)) = planes[i] else {
            continue;
        };
        let (w, h) = (w as usize, h as usize);
        // Per-pixel temporal median over the neighbour stack.
        let mut med = vec![0u8; w * h * 3];
        let mut tmp = Vec::with_capacity(neigh[i].len());
        for px in 0..w * h * 3 {
            tmp.clear();
            for &j in &neigh[i] {
                if let Some((nw, nh, ref nrgb)) = planes[j] {
                    if (nw as usize, nh as usize) == (w, h) {
                        tmp.push(nrgb[px]);
                    }
                }
            }
            tmp.sort_unstable();
            med[px] = if tmp.is_empty() { rgb[px] } else { tmp[tmp.len() / 2] };
        }
        // Transient = |I - median| > thresh on any channel.
        let mut mask = vec![255u8; w * h];
        for px in 0..w * h {
            let d = (0..3).any(|c| {
                (rgb[3 * px + c] as i32 - med[3 * px + c] as i32).abs() > thresh as i32
            });
            if d {
                mask[px] = 0;
            }
        }
        // Dilate the dropped region.
        if dilate > 0 {
            let src = mask.clone();
            let r = dilate as usize;
            for yy in 0..h {
                for xx in 0..w {
                    if src[yy * w + xx] == 0 {
                        for dy in yy.saturating_sub(r)..=(yy + r).min(h - 1) {
                            for dx in xx.saturating_sub(r)..=(xx + r).min(w - 1) {
                                mask[dy * w + dx] = 0;
                            }
                        }
                    }
                }
            }
        }
        let name = Path::new(&f.file_path)
            .file_stem()
            .context("frame stem")?
            .to_string_lossy()
            .to_string();
        let img = image::GrayImage::from_raw(w as u32, h as u32, mask).unwrap();
        let mpath = mask_dir.join(format!("{name}.png"));
        img.save(&mpath)?;
        // Patch frame with mask_path (Brush nerfstudio loader consumes it).
        if let Some(fj) = json["frames"].as_array_mut().and_then(|a| a.get_mut(i)) {
            fj["mask_path"] =
                Value::String(format!("masks/images/{name}.png"));
            patched += 1;
        }
        let _ = &frames_arr;
    }
    fs::write(&tpath, serde_json::to_string_pretty(&json)?)?;
    eprintln!("masks: wrote {patched}/{n} masks to {}", mask_dir.display());
    Ok(())
}

// ---------- exposure ----------

/// Robust per-channel mean/std via 5-95 percentiles.
fn chan_stats(w: usize, h: usize, rgb: &[u8]) -> ([f64; 3], [f64; 3]) {
    let mut mean = [0f64; 3];
    let mut var = [0f64; 3];
    let n = (w * h) as f64;
    for px in 0..w * h {
        for c in 0..3 {
            let v = rgb[3 * px + c] as f64;
            mean[c] += v;
            var[c] += v * v;
        }
    }
    let mut sd = [0f64; 3];
    for c in 0..3 {
        mean[c] /= n;
        sd[c] = (var[c] / n - mean[c] * mean[c]).max(1.0).sqrt();
    }
    (mean, sd)
}

fn cmd_exposure(
    in_dir: &Path,
    out_dir: &Path,
    mode: &str,
    eval_half_fit: bool,
    q: u8,
) -> Result<()> {
    let (json, frames) = load_transforms(&in_dir.join("transforms.json"))?;
    let val_path = in_dir.join("transforms_val.json");
    let (vjson, vframes) = if val_path.exists() {
        load_transforms(&val_path)?
    } else {
        (json.clone(), Vec::new())
    };

    // Mode select: exactly one appearance strategy may be active.
    if mode != "offline" {
        if mode != "learned" && mode != "none" {
            bail!("--mode must be offline|learned|none");
        }
        // Pass frames through unchanged; the marker tells downstream
        // (bench_mac.sh / farsight-splat) which strategy was chosen.
        let link = |frames: &[Frame], json: &Value, dest: &Path| -> Result<()> {
            let arr = json["frames"].as_array().unwrap();
            for f in frames {
                let src = in_dir.join(&f.file_path);
                let name = Path::new(&f.file_path).file_name().context("frame name")?;
                let dst = out_dir.join("images").join(name);
                if !dst.exists() && fs::hard_link(&src, &dst).is_err() {
                    fs::copy(&src, &dst).context("copy frame")?;
                }
            }
            let mut obj = json.clone();
            let arr2: Vec<Value> = arr
                .iter()
                .map(|f| {
                    let mut f = f.clone();
                    let name = Path::new(f["file_path"].as_str().unwrap())
                        .file_name()
                        .unwrap();
                    f["file_path"] =
                        Value::String(format!("images/{}", name.to_string_lossy()));
                    f
                })
                .collect();
            obj["frames"] = Value::Array(arr2);
            fs::write(dest, serde_json::to_string_pretty(&obj)?)?;
            Ok(())
        };
        fs::create_dir_all(out_dir.join("images"))?;
        link(&frames, &json, &out_dir.join("transforms.json"))?;
        if !vframes.is_empty() {
            link(&vframes, &vjson, &out_dir.join("transforms_val.json"))?;
        }
        let meta = serde_json::json!({
            "appearance": mode,
            "appearance_lr": if mode == "learned" { 0.005 } else { 0.0 },
            "note": "exactly one appearance strategy per capture; learned = brush-cli --appearance-lr",
        });
        fs::write(
            out_dir.join("views_meta.json"),
            serde_json::to_string_pretty(&meta)?,
        )?;
        eprintln!("exposure: mode={mode}, {} frames passed through", frames.len());
        return Ok(());
    }

    // Reference tone = median of per-image channel means/stds.
    let mut stats = Vec::with_capacity(frames.len());
    for f in frames.iter() {
        let (w, h, rgb) = decode_small(&in_dir.join(&f.file_path), 320)?;
        stats.push(chan_stats(w as usize, h as usize, &rgb));
    }
    let mut refm = [0f64; 3];
    let mut refs = [0f64; 3];
    for c in 0..3 {
        let mut ms: Vec<f64> = stats.iter().map(|s| s.0[c]).collect();
        let mut ss: Vec<f64> = stats.iter().map(|s| s.1[c]).collect();
        ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
        ss.sort_by(|a, b| a.partial_cmp(b).unwrap());
        refm[c] = ms[ms.len() / 2];
        refs[c] = ss[ss.len() / 2];
    }

    fs::create_dir_all(out_dir.join("images"))?;

    let write = |fj: &mut Value, frame: &Frame, half_fit: bool| -> Result<()> {
        let src = in_dir.join(&frame.file_path);
        let bytes = fs::read(&src)?;
        let img = image::load_from_memory(&bytes)?.to_rgb8();
        let (w, h) = (img.width() as usize, img.height() as usize);
        let raw = img.as_raw();
        // Fit region: full image, or top half for the eval-fair variant.
        let fit_h = if half_fit { h / 2 } else { h };
        let (m, s) = chan_stats(w, fit_h, raw);
        let mut gain = [1f64; 3];
        let mut bias = [0f64; 3];
        for c in 0..3 {
            gain[c] = (refs[c] / s[c]).clamp(0.5, 2.0);
            bias[c] = refm[c] - gain[c] * m[c];
        }
        let mut out = vec![0u8; w * h * 3];
        for px in 0..w * h {
            for c in 0..3 {
                let v = raw[3 * px + c] as f64 * gain[c] + bias[c];
                out[3 * px + c] = v.round().clamp(0.0, 255.0) as u8;
            }
        }
        let name = Path::new(&frame.file_path).file_name().unwrap();
        let dst = out_dir.join("images").join(name);
        let f = fs::File::create(&dst)?;
        let mut wtr = std::io::BufWriter::new(f);
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut wtr, q).encode(
            &out,
            w as u32,
            h as u32,
            image::ExtendedColorType::Rgb8,
        )?;
        fj["file_path"] = Value::String(format!("images/{}", name.to_string_lossy()));
        Ok(())
    };

    let mut obj = json.clone();
    for (i, f) in frames.iter().enumerate() {
        write(&mut obj["frames"][i], f, false)?;
    }
    fs::write(out_dir.join("transforms.json"), serde_json::to_string_pretty(&obj)?)?;

    if !vframes.is_empty() {
        let mut vobj = vjson.clone();
        for (i, f) in vframes.iter().enumerate() {
            write(&mut vobj["frames"][i], f, eval_half_fit)?;
        }
        fs::write(
            out_dir.join("transforms_val.json"),
            serde_json::to_string_pretty(&vobj)?,
        )?;
    }
    let meta = serde_json::json!({
        "appearance": "offline",
        "appearance_lr": 0.0,
        "eval_half_fit": eval_half_fit,
        "note": "exactly one appearance strategy per capture; learned = brush-cli --appearance-lr",
    });
    fs::write(
        out_dir.join("views_meta.json"),
        serde_json::to_string_pretty(&meta)?,
    )?;
    Ok(())
}

// ---------- pose-refine ----------

use nalgebra::{Matrix3, Matrix4, Vector3, Vector6};

/// Ply vertex: position + optional rgb.
struct PlyPoint {
    p: [f64; 3],
    rgb: Option<[u8; 3]>,
}

/// Minimal ply loader: vertex element with x,y,z (+ optional red,green,
/// blue), ascii or binary_little_endian float32 vertices.
fn load_ply_points(path: &Path) -> Result<Vec<PlyPoint>> {
    let mut data = std::io::BufReader::new(fs::File::open(path)?);
    let mut line = String::new();
    use std::io::BufRead;
    let mut fmt_ascii = false;
    let mut fmt_bin = false;
    let mut n = 0usize;
    let mut props: Vec<String> = Vec::new();
    let mut in_vertex = false;
    loop {
        line.clear();
        if data.read_line(&mut line)? == 0 {
            bail!("ply: unexpected EOF in header");
        }
        let t = line.trim();
        if t.starts_with("format") {
            fmt_ascii = t.contains("ascii");
            fmt_bin = t.contains("binary_little_endian");
        } else if t.starts_with("element") {
            in_vertex = t.starts_with("element vertex");
            if in_vertex {
                n = t
                    .rsplit(' ')
                    .next()
                    .and_then(|s| s.parse().ok())
                    .context("ply vertex count")?;
            }
        } else if in_vertex && t.starts_with("property") {
            props.push(t.rsplit(' ').next().unwrap_or("").to_string());
        } else if t == "end_header" {
            break;
        }
    }
    let ix = props.iter().position(|p| p == "x");
    let iy = props.iter().position(|p| p == "y");
    let iz = props.iter().position(|p| p == "z");
    let ir = props.iter().position(|p| p == "red" || p == "r");
    let ig = props.iter().position(|p| p == "green" || p == "g");
    let ib = props.iter().position(|p| p == "blue" || p == "b");
    let (Some(ix), Some(iy), Some(iz)) = (ix, iy, iz) else {
        bail!("ply: no x/y/z vertex properties");
    };
    let has_rgb = ir.is_some() && ig.is_some() && ib.is_some();
    let mut out = Vec::with_capacity(n);
    if fmt_ascii {
        for _ in 0..n {
            line.clear();
            data.read_line(&mut line)?;
            let f: Vec<f64> = line
                .split_whitespace()
                .filter_map(|s| s.parse().ok())
                .collect();
            if f.len() <= iz.max(ix).max(iy) {
                continue;
            }
            let rgb = has_rgb.then(|| {
                [
                    f[ir.unwrap()] as u8,
                    f[ig.unwrap()] as u8,
                    f[ib.unwrap()] as u8,
                ]
            });
            out.push(PlyPoint {
                p: [f[ix], f[iy], f[iz]],
                rgb,
            });
        }
    } else if fmt_bin {
        use std::io::Read;
        let stride = props.len(); // assume all float32 except rgb u8?
        // Generic: parse each property type from header order.
        // Re-scan header for property types.
        // For simplicity: handle common layout [x,y,z,(nx,ny,nz,)red,green,blue] all float32 except u8 colours.
        // Fall back: read per-vertex using prop names — most LiDAR exports
        // use all-float32 anyway; treat rgb as float.
        let mut buf = vec![0u8; n * stride * 4];
        data.read_exact(&mut buf)?;
        for v in 0..n {
            let b = &buf[v * stride * 4..(v + 1) * stride * 4];
            let f = |i: usize| f32::from_le_bytes(b[i * 4..i * 4 + 4].try_into().unwrap()) as f64;
            out.push(PlyPoint {
                p: [f(ix), f(iy), f(iz)],
                rgb: has_rgb.then(|| {
                    [
                        f(ir.unwrap()) as u8,
                        f(ig.unwrap()) as u8,
                        f(ib.unwrap()) as u8,
                    ]
                }),
            });
        }
    } else {
        bail!("ply: unsupported format (ascii/binary_le only)");
    }
    Ok(out)
}

fn se3_exp(xi: &Vector6<f64>) -> Matrix4<f64> {
    let (w, v) = (xi.fixed_rows::<3>(3), xi.fixed_rows::<3>(0));
    let theta = w.norm();
    let wx = Matrix3::new(0.0, -w[2], w[1], w[2], 0.0, -w[0], -w[1], w[0], 0.0);
    let (r, vmat) = if theta < 1e-9 {
        (Matrix3::identity() + wx, Matrix3::identity() + wx / 2.0)
    } else {
        let a = wx / theta;
        let s = theta.sin();
        let c = theta.cos();
        (
            Matrix3::identity() + a * s + a * a * (1.0 - c),
            Matrix3::identity() + a * (1.0 - c) / theta + a * a * (theta - s) / theta,
        )
    };
    let mut m = Matrix4::identity();
    m.fixed_view_mut::<3, 3>(0, 0).copy_from(&r);
    m.fixed_view_mut::<3, 1>(0, 3).copy_from(&(vmat * v));
    m
}

/// Sample luma at (u,v) by bilinear interpolation; None outside.
fn sample_luma(y: &[u8], w: usize, h: usize, u: f64, v: f64) -> Option<f64> {
    let (x0, y0) = (u.floor() as i32, v.floor() as i32);
    if x0 < 0 || y0 < 0 || x0 + 1 >= w as i32 || y0 + 1 >= h as i32 {
        return None;
    }
    let (fx, fy) = (u - x0 as f64, v - y0 as f64);
    let g = |dx: i32, dy: i32| y[(y0 + dy) as usize * w + (x0 + dx) as usize] as f64;
    Some(
        g(0, 0) * (1.0 - fx) * (1.0 - fy)
            + g(1, 0) * fx * (1.0 - fy)
            + g(0, 1) * (1.0 - fx) * fy
            + g(1, 1) * fx * fy,
    )
}

/// Cam2world 4x4 from the nerfstudio matrix row-major, plus intrinsics.
fn frame_matrix(f: &Value) -> Option<Matrix4<f64>> {
    let m = f["transform_matrix"].as_array()?;
    let mut out = Matrix4::zeros();
    for r in 0..4 {
        for c in 0..4 {
            out[(r, c)] = m.get(r)?.get(c)?.as_f64()?;
        }
    }
    Some(out)
}

/// world2cam = inverse(cam2world) (rigid).
fn w2c(c2w: &Matrix4<f64>) -> Matrix4<f64> {
    let r = c2w.fixed_view::<3, 3>(0, 0).clone_owned();
    let t = c2w.fixed_view::<3, 1>(0, 3).clone_owned();
    let rt = r.transpose();
    let mut m = Matrix4::identity();
    m.fixed_view_mut::<3, 3>(0, 0).copy_from(&rt);
    m.fixed_view_mut::<3, 1>(0, 3).copy_from(&(-rt * t));
    m
}

fn cmd_pose_refine(
    in_dir: &Path,
    ply_path: &Path,
    transforms: Option<PathBuf>,
    out_transforms: Option<PathBuf>,
    k: usize,
    res: u32,
    iters: u32,
    huber: f64,
    max_anchors: usize,
    guard: bool,
) -> Result<()> {
    let tpath = transforms.unwrap_or_else(|| in_dir.join("transforms.json"));
    let mut json: Value = serde_json::from_str(&fs::read_to_string(&tpath)?)?;
    let farr = json["frames"].as_array().context("frames")?.clone();
    let n = farr.len();
    // Intrinsics (per-frame override → top-level fallback), hoisted
    // out of `json` so the frames array can be rewritten in place later.
    let tl = |key: &str| json[key].as_f64().unwrap_or(0.0);
    let (fl0, fy0, cx0, cy0, w0, h0) = (tl("fl_x"), tl("fl_y"), tl("cx"), tl("cy"), tl("w"), tl("h"));
    let intrs: Vec<[f64; 6]> = farr
        .iter()
        .map(|f| {
            let g = |k: &str, d: f64| f[k].as_f64().unwrap_or(d);
            [g("fl_x", fl0), g("fl_y", fy0), g("cx", cx0), g("cy", cy0), g("w", w0), g("h", h0)]
        })
        .collect();
    let c2w: Vec<Matrix4<f64>> = farr.iter().filter_map(frame_matrix).collect();
    if c2w.len() != n {
        bail!("transform_matrix missing on some frames");
    }
    let poses_t: Vec<Vector3<f64>> = c2w
        .iter()
        .map(|m| Vector3::new(m[(0, 3)], m[(1, 3)], m[(2, 3)]))
        .collect();

    // Decode working-res luma planes for all frames.
    let mut planes: Vec<Option<(usize, usize, Vec<u8>)>> = Vec::with_capacity(n);
    for f in farr.iter() {
        let p = f["file_path"].as_str().context("file_path")?;
        planes.push(
            decode_small(&in_dir.join(p), res)
                .ok()
                .map(|(w, h, rgb)| (w as usize, h as usize, luma(w as usize, h as usize, &rgb))),
        );
    }

    let pts = load_ply_points(ply_path)?;
    eprintln!("pose-refine: {} LiDAR points, {n} views", pts.len());

    // Anchor colours: ply rgb if present, else steal from the k pose-
    // nearest neighbour frames (LiDAR depth projected into the frame).
    let ply_rgb = pts.iter().any(|p| p.rgb.is_some());
    let mut anchors: Vec<(Vector3<f64>, f64)> = Vec::with_capacity(pts.len());
    if ply_rgb {
        for p in &pts {
            if let Some(rgb) = p.rgb {
                let y = (rgb[0] as f64 * 299.0 + rgb[1] as f64 * 587.0 + rgb[2] as f64 * 114.0)
                    / 1000.0;
                anchors.push((Vector3::from_column_slice(&p.p), y));
            }
        }
    } else {
        // For each frame j, project ply points into it → (world, luma_j).
        for (j, m) in c2w.iter().enumerate() {
            let Some((w, h, ref y)) = planes[j] else { continue };
            let wmat = w2c(m);
            let (fx, fy, cx, cy) = (
                intrs[j][0] * w as f64 / intrs[j][4],
                intrs[j][1] * h as f64 / intrs[j][5],
                intrs[j][2] * w as f64 / intrs[j][4],
                intrs[j][3] * h as f64 / intrs[j][5],
            );
            for p in &pts {
                let q4 = wmat * nalgebra::Vector4::new(p.p[0], p.p[1], p.p[2], 1.0);
                let q = Vector3::new(q4.x, q4.y, q4.z);
                if q.z <= 0.05 {
                    continue;
                }
                let (u, v) = (fx * q.x / q.z + cx, fy * q.y / q.z + cy);
                if let Some(yy) = sample_luma(y, w, h, u, v) {
                    anchors.push((Vector3::from_column_slice(&p.p), yy));
                }
            }
            if anchors.len() > max_anchors * k.max(1) {
                break;
            }
        }
        // Keep at most max_anchors*k anchors globally by thinning.
        let cap = max_anchors * k.max(1);
        if anchors.len() > cap {
            let stride = anchors.len() / cap + 1;
            anchors = anchors.iter().step_by(stride).copied().collect();
        }
    }
    eprintln!("pose-refine: {} anchors", anchors.len());
    if anchors.len() < 8 {
        eprintln!("pose-refine: too few anchors, skipping");
        fs::write(
            out_transforms.unwrap_or(tpath),
            serde_json::to_string_pretty(&json)?,
        )?;
        return Ok(());
    }

    // Neighbour sets by camera-centre distance.
    let neigh: Vec<Vec<usize>> = (0..n)
        .map(|i| {
            let mut ds: Vec<(f64, usize)> = (0..n)
                .map(|j| ((poses_t[i] - poses_t[j]).norm(), j))
                .collect();
            ds.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
            ds.iter().skip(1).take(k.max(1)).map(|&(_, j)| j).collect()
        })
        .collect();

    let mut refined = 0usize;
    let mut mean_move = 0f64;
    for i in 0..n {
        let Some((w, h, ref y)) = planes[i] else { continue };
        // Anchors visible to any of i's neighbours (or all if ply_rgb).
        let pool: &[(Vector3<f64>, f64)] = &anchors;
        let (fx, fy, cx, cy) = (
            intrs[i][0] * w as f64 / intrs[i][4],
            intrs[i][1] * h as f64 / intrs[i][5],
            intrs[i][2] * w as f64 / intrs[i][4],
            intrs[i][3] * h as f64 / intrs[i][5],
        );
        // Restrict to anchors that project inside view i's box.
        let wmat0 = w2c(&c2w[i]);
        let mut pts3: Vec<(Vector3<f64>, f64)> = Vec::new();
        let mut proj: Vec<(f64, f64, f64, f64)> = Vec::new(); // u,v + gx,gy? no
        for (p, col) in pool.iter().take(if ply_rgb { max_anchors * 4 } else { pool.len() }) {
            let q4 = wmat0 * nalgebra::Vector4::new(p.x, p.y, p.z, 1.0);
            let q = Vector3::new(q4.x, q4.y, q4.z);
            if q.z <= 0.05 {
                continue;
            }
            let (u, v) = (fx * q.x / q.z + cx, fy * q.y / q.z + cy);
            if u >= 1.0 && v >= 1.0 && u < (w as f64 - 1.0) && v < (h as f64 - 1.0) {
                pts3.push((*p, *col));
            }
        }
        if pts3.len() > max_anchors {
            let stride = pts3.len() / max_anchors + 1;
            pts3 = pts3.iter().step_by(stride).copied().collect();
        }
        if pts3.len() < 16 {
            continue;
        }
        let _ = &mut proj;
        let _ = &neigh;

        let cost = |wmat: &Matrix4<f64>| -> f64 {
            let mut c = 0.0;
            let mut m = 0usize;
            for (p, col) in pts3.iter() {
                let q4 = wmat * nalgebra::Vector4::new(p.x, p.y, p.z, 1.0);
                let q = Vector3::new(q4.x, q4.y, q4.z);
                if q.z <= 0.05 {
                    continue;
                }
                let (u, v) = (fx * q.x / q.z + cx, fy * q.y / q.z + cy);
                if let Some(s) = sample_luma(y, w, h, u, v) {
                    let r = (s - col).abs();
                    // Huber cost.
                    c += if r <= huber { 0.5 * r * r } else { huber * (r - 0.5 * huber) };
                    m += 1;
                }
            }
            if m == 0 { f64::MAX } else { c / m as f64 }
        };

        let mut wmat = wmat0;
        let c0 = cost(&wmat);
        if !c0.is_finite() {
            continue;
        }
        for _it in 0..iters {
            // Build normal equations JᵀWJ ξ = -JᵀWr (Huber weights).
            let mut hmat: nalgebra::Matrix6<f64> = nalgebra::Matrix6::zeros();
            let mut g: Vector6<f64> = Vector6::zeros();
            for (p, col) in pts3.iter() {
                let q4 = wmat * nalgebra::Vector4::new(p.x, p.y, p.z, 1.0);
                let q = Vector3::new(q4.x, q4.y, q4.z);
                if q.z <= 0.05 {
                    continue;
                }
                let (u, v) = (fx * q.x / q.z + cx, fy * q.y / q.z + cy);
                let Some(s) = sample_luma(y, w, h, u, v) else { continue };
                let r = s - col;
                let wt = if r.abs() <= huber { 1.0 } else { huber / r.abs().max(1e-6) };
                // image gradient at (u,v)
                let gx = (sample_luma(y, w, h, u + 1.0, v).unwrap_or(s)
                    - sample_luma(y, w, h, u - 1.0, v).unwrap_or(s))
                    / 2.0;
                let gy = (sample_luma(y, w, h, u, v + 1.0).unwrap_or(s)
                    - sample_luma(y, w, h, u, v - 1.0).unwrap_or(s))
                    / 2.0;
                // d(u,v)/dq
                let du = [fx / q.z, 0.0, -fx * q.x / (q.z * q.z)];
                let dv = [0.0, fy / q.z, -fy * q.y / (q.z * q.z)];
                // dq/dξ with ξ=[t(3) w(3)]: dq/dt=I, dq/dw=-[q]×
                let mut j = nalgebra::Matrix1x6::<f64>::zeros();
                for c in 0..3 {
                    j[c] = gx * du[c] + gy * dv[c];
                }
                let qv = Vector3::new(q.x, q.y, q.z);
                // -[q]× columns for the rotational part
                let neg_skew = Matrix3::new(
                    0.0, qv.z, -qv.y, -qv.z, 0.0, qv.x, qv.y, -qv.x, 0.0,
                );
                for c in 0..3 {
                    let colv = neg_skew.column(c);
                    j[3 + c] = gx * (du[0] * colv[0] + du[1] * colv[1] + du[2] * colv[2])
                        + gy * (dv[0] * colv[0] + dv[1] * colv[1] + dv[2] * colv[2]);
                }
                hmat += wt * (j.transpose() * j);
                g += wt * (j.transpose() * r);
            }
            // LM-ish damping.
            let mut damp = nalgebra::Matrix6::<f64>::identity() * 1e-3;
            damp[(3, 3)] = 1e-4;
            damp[(4, 4)] = 1e-4;
            damp[(5, 5)] = 1e-4;
            let Some(delta) = (hmat + damp).try_inverse() else { break };
            let xi = -(delta * g);
            if xi.norm() < 1e-8 {
                break;
            }
            let mut xi_c = xi;
            let tn = xi_c.fixed_rows::<3>(0).norm();
            if tn > 0.05 {
                xi_c.fixed_rows_mut::<3>(0).scale_mut(0.05 / tn);
            }
            let rn = xi_c.fixed_rows::<3>(3).norm();
            if rn > 0.02 {
                xi_c.fixed_rows_mut::<3>(3).scale_mut(0.02 / rn);
            }
            wmat = se3_exp(&xi_c) * wmat;
        }
        let c1 = cost(&wmat);
        if !guard || c1 < c0 {
            let new_c2w = wmat.try_inverse().unwrap_or(c2w[i]);
            let moved = (new_c2w.fixed_view::<3, 1>(0, 3) - c2w[i].fixed_view::<3, 1>(0, 3))
                .norm();
            mean_move += moved;
            refined += 1;
            let m = &mut json["frames"][i]["transform_matrix"];
            for r in 0..4 {
                for c in 0..4 {
                    m[r][c] = serde_json::json!(new_c2w[(r, c)]);
                }
            }
        }
    }
    eprintln!(
        "pose-refine: refined {refined}/{n} views, mean translation move {:.4} m",
        if refined > 0 { mean_move / refined as f64 } else { 0.0 }
    );
    fs::write(
        out_transforms.unwrap_or(tpath),
        serde_json::to_string_pretty(&json)?,
    )?;
    Ok(())
}

fn main() -> Result<()> {
    let args = Args::parse();
    match args.cmd {
        Cmd::Select {
            in_dir,
            out_dir,
            budget,
            eval_every,
            blur_drop_frac,
            blur_rel_thresh,
            dup_eps_t,
            dup_eps_r,
            dir_weight,
            blur_res,
            engine,
        } => cmd_select(
            &in_dir,
            &out_dir,
            budget,
            eval_every,
            blur_drop_frac,
            blur_rel_thresh,
            dup_eps_t,
            dup_eps_r,
            dir_weight,
            blur_res,
            &engine,
        ),
        Cmd::PoseRefine {
            in_dir,
            ply,
            transforms,
            out_transforms,
            k,
            res,
            iters,
            huber,
            max_anchors,
            guard,
        } => cmd_pose_refine(
            &in_dir,
            &ply,
            transforms,
            out_transforms,
            k,
            res,
            iters,
            huber,
            max_anchors,
            guard,
        ),
        Cmd::Masks {
            in_dir,
            transforms,
            k,
            res,
            thresh,
            dilate,
            engine,
        } => cmd_masks(&in_dir, transforms, k, res, thresh, dilate, &engine),
        Cmd::Exposure {
            in_dir,
            out_dir,
            mode,
            eval_half_fit,
            jpeg_quality,
        } => cmd_exposure(&in_dir, &out_dir, &mode, eval_half_fit, jpeg_quality),
    }
    .or_else(|e| -> Result<()> { bail!("{e:#}") })
}
