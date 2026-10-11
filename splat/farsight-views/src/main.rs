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
    },
    /// Per-view affine gain/bias normalization of TRAIN images toward the
    /// dataset-wide tone. Eval images are copied unchanged by default;
    /// --eval-half-fit fits the affine on the top half only and applies it
    /// to the whole eval image (honest "appearance-fair" metric variant).
    Exposure {
        #[arg(long)] in_dir: PathBuf,
        #[arg(long)] out_dir: PathBuf,
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
) -> Result<()> {
    let (json, frames) = load_transforms(&in_dir.join("transforms.json"))?;
    let n = frames.len();

    // Blur scores for every frame (cheap 160px decode).
    let mut scores = vec![0f64; n];
    for f in frames.iter() {
        let p = in_dir.join(&f.file_path);
        match decode_small(&p, blur_res) {
            Ok((w, h, rgb)) => {
                let y = luma(w as usize, h as usize, &rgb);
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

fn cmd_masks(
    in_dir: &Path,
    transforms: Option<PathBuf>,
    k: usize,
    res: u32,
    thresh: u8,
    dilate: u32,
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

fn cmd_exposure(in_dir: &Path, out_dir: &Path, eval_half_fit: bool, q: u8) -> Result<()> {
    let (json, frames) = load_transforms(&in_dir.join("transforms.json"))?;
    let val_path = in_dir.join("transforms_val.json");
    let (vjson, vframes) = if val_path.exists() {
        load_transforms(&val_path)?
    } else {
        (json.clone(), Vec::new())
    };

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
        ),
        Cmd::Masks {
            in_dir,
            transforms,
            k,
            res,
            thresh,
            dilate,
        } => cmd_masks(&in_dir, transforms, k, res, thresh, dilate),
        Cmd::Exposure {
            in_dir,
            out_dir,
            eval_half_fit,
            jpeg_quality,
        } => cmd_exposure(&in_dir, &out_dir, eval_half_fit, jpeg_quality),
    }
    .or_else(|e| -> Result<()> { bail!("{e:#}") })
}
