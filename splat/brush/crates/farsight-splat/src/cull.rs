//! Frame selection: port of `find_drives.py`, `cull.py` plus corrupt-image
//! dropping (the `IO error ... early eof` JPEGs Brush rejects on load).

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::capture::{Capture, Frame};

/// Motion mask per find_drives.py: frame i counts as "moving" when the
/// position change from i-1 exceeds 1 cm OR the yaw change exceeds 2 deg.
/// Frame 0 always counts as moving (it starts the first drive).
pub fn motion_flags(frames: &[Frame]) -> Vec<bool> {
    let mut out = Vec::with_capacity(frames.len());
    let yaws: Vec<f64> = frames
        .iter()
        .map(|f| {
            let m = &f.transform_matrix;
            (-m[0][2]).atan2(-m[2][2])
        })
        .collect();
    let yaws = unwrap(&yaws);
    for i in 0..frames.len() {
        if i == 0 {
            out.push(true);
            continue;
        }
        let (a, b) = (frames[i - 1].pos(), frames[i].pos());
        let d = ((b[0] - a[0]).powi(2) + (b[1] - a[1]).powi(2) + (b[2] - a[2]).powi(2)).sqrt();
        let dyaw = (yaws[i] - yaws[i - 1]).abs().to_degrees();
        out.push(d > 0.01 || dyaw > 2.0);
    }
    out
}

fn unwrap(yaws: &[f64]) -> Vec<f64> {
    let mut out = Vec::with_capacity(yaws.len());
    let mut off = 0.0;
    for (i, &y) in yaws.iter().enumerate() {
        if i > 0 {
            let mut d = y + off - out[i - 1];
            while d > std::f64::consts::PI {
                off -= 2.0 * std::f64::consts::PI;
                d -= 2.0 * std::f64::consts::PI;
            }
            while d < -std::f64::consts::PI {
                off += 2.0 * std::f64::consts::PI;
                d += 2.0 * std::f64::consts::PI;
            }
        }
        out.push(y + off);
    }
    out
}

/// A contiguous drive: motion with pauses under `gap_s` seconds.
#[derive(Clone, Debug, Serialize)]
pub struct Drive {
    pub start: usize,
    pub end: usize, // inclusive
    pub frames: usize,
    pub seconds: f64,
    pub path_m: f64,
    pub moving: usize,
}

/// Group moving frames into drives (find_drives.py: new drive when the gap
/// between consecutive moving frames exceeds 12 s of capture time).
pub fn find_drives(frames: &[Frame], gap_s: f64) -> Vec<Drive> {
    let mov = motion_flags(frames);
    let idx: Vec<usize> = (0..frames.len()).filter(|&i| mov[i]).collect();
    if idx.is_empty() {
        return vec![];
    }
    let mut spans: Vec<(usize, usize)> = vec![];
    let (mut s, mut p) = (idx[0], idx[0]);
    for &i in &idx[1..] {
        if frames[i].t(i) - frames[p].t(p) > gap_s {
            spans.push((s, p));
            s = i;
        }
        p = i;
    }
    spans.push((s, p));
    spans
        .into_iter()
        .map(|(a, b)| {
            let mut path = 0.0;
            for w in frames[a..=b].windows(2) {
                let (p, q) = (w[0].pos(), w[1].pos());
                path += ((q[0] - p[0]).powi(2)
                    + (q[1] - p[1]).powi(2)
                    + (q[2] - p[2]).powi(2))
                .sqrt();
            }
            Drive {
                start: a,
                end: b,
                frames: b - a + 1,
                seconds: frames[b].t(b) - frames[a].t(a),
                path_m: path,
                moving: mov[a..=b].iter().filter(|&&m| m).count(),
            }
        })
        .collect()
}

/// Grayscale Laplacian-variance sharpness (cull.py): cv2.Laplacian(CV_64F)
/// 3x3 kernel [0 1 0; 1 -4 1; 0 1 0], variance over the interior pixels.
/// `None` when the file will not decode.
pub fn sharpness(path: &Path) -> Option<f64> {
    let img = image::ImageReader::open(path).ok()?.decode().ok()?;
    let g = img.to_luma8();
    let (w, h) = (g.width() as usize, g.height() as usize);
    if w < 3 || h < 3 {
        return Some(0.0);
    }
    let px = g.as_raw();
    let mut sum = 0.0f64;
    let mut sq = 0.0f64;
    let mut n = 0u64;
    for y in 1..h - 1 {
        for x in 1..w - 1 {
            let l = px[y * w + x - 1] as f64
                + px[y * w + x + 1] as f64
                + px[(y - 1) * w + x] as f64
                + px[(y + 1) * w + x] as f64
                - 4.0 * px[y * w + x] as f64;
            sum += l;
            sq += l * l;
            n += 1;
        }
    }
    let mean = sum / n as f64;
    Some(sq / n as f64 - mean * mean)
}

/// True when the file fails to decode at all (Brush's loader rejects these;
/// cull drops them up front instead).
pub fn is_undecodable(path: &Path) -> bool {
    image::ImageReader::open(path)
        .ok()
        .and_then(|r| r.decode().ok())
        .is_none()
}

/// Angle (deg) between two rotation matrices, and position delta (m).
fn pose_delta(a: &Frame, b: &Frame) -> (f64, f64) {
    let (ra, rb) = (a.rot(), b.rot());
    // trace(R_a^T R_b)
    let mut tr = 0.0;
    for i in 0..3 {
        for j in 0..3 {
            tr += ra[j][i] * rb[j][i];
        }
    }
    let ang = ((tr - 1.0) / 2.0).clamp(-1.0, 1.0).acos().to_degrees();
    let (pa, pb) = (a.pos(), b.pos());
    let d = ((pb[0] - pa[0]).powi(2) + (pb[1] - pa[1]).powi(2) + (pb[2] - pa[2]).powi(2)).sqrt();
    (d, ang)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CullReport {
    pub input: usize,
    pub kept: usize,
    pub dropped_tracking: usize,
    pub dropped_corrupt: usize,
    pub dropped_parked: usize,
    pub dropped_blur: usize,
    pub blur_cutoff: f64,
}

/// Port of cull.py: drop non-normal tracking + undecodable files, group parked
/// stretches (within 5 mm / 0.5 deg of the group's first frame), keep the
/// sharpest of each group, then drop the bottom 10 % by sharpness.
pub fn cull(cap: &Capture) -> (Vec<Frame>, CullReport) {
    let mut rep = CullReport {
        input: cap.transforms.frames.len(),
        ..Default::default()
    };
    let sharp: Vec<Option<f64>> = cap
        .transforms
        .frames
        .iter()
        .map(|f| sharpness(&cap.image_path(f)))
        .collect();

    // Parked groups over surviving frames only.
    let mut groups: Vec<Vec<(f64, &Frame)>> = vec![];
    let mut start: Option<&Frame> = None;
    for (f, sv) in cap.transforms.frames.iter().zip(&sharp) {
        if f.tracking.as_deref().unwrap_or("normal") != "normal" {
            rep.dropped_tracking += 1;
            continue;
        }
        let Some(sv) = sv else {
            rep.dropped_corrupt += 1;
            continue;
        };
        if let Some(s0) = start {
            let (d, ang) = pose_delta(s0, f);
            if d < 0.005 && ang < 0.5 {
                groups.last_mut().expect("non-empty").push((*sv, f));
                continue;
            }
        }
        groups.push(vec![(*sv, f)]);
        start = Some(f);
    }
    let best: Vec<(f64, &Frame)> = groups
        .iter()
        .map(|g| {
            g.iter()
                .copied()
                .max_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
                .expect("non-empty group")
        })
        .collect();
    rep.dropped_parked = groups.iter().map(|g| g.len() - 1).sum();

    let mut sorted: Vec<f64> = best.iter().map(|b| b.0).collect();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let cut = percentile(&sorted, 10.0);
    rep.blur_cutoff = cut;
    let keep: Vec<Frame> = best
        .iter()
        .filter(|(s, _)| *s >= cut)
        .map(|(_, f)| (*f).clone())
        .collect();
    rep.dropped_blur = best.len() - keep.len();
    rep.kept = keep.len();
    (keep, rep)
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    // numpy 'linear' interpolation, matching np.percentile defaults.
    let rank = p / 100.0 * (sorted.len() - 1) as f64;
    let lo = rank.floor() as usize;
    let hi = (lo + 1).min(sorted.len() - 1);
    sorted[lo] + (sorted[hi] - sorted[lo]) * (rank - lo as f64)
}
