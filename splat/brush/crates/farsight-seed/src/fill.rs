//! Stretch goal: synthesize splats in photo-visible regions the dots never
//! reached. For each fill view: complete the sparse min-depth buffer by
//! multi-round nearest-valid diffusion (coarse, then unproject a sparse
//! pixel grid), giving each synthesized point the photo colour and a
//! camera-facing normal. Voxel-merged into the measured cloud afterwards.

use crate::views::SeedView;
use glam::Vec3;

#[derive(Clone, Debug)]
pub struct FillConfig {
    /// Fill views = every Nth view (usually same as colour stride).
    pub stride: usize,
    /// Complete depth at this buffer resolution (image px per buffer px).
    pub ds: f32,
    /// Synthesize at most one splat per `px_step` buffer pixels.
    pub px_step: u32,
    /// Diffusion rounds for depth completion.
    pub rounds: u32,
    /// Cap on total synthesized points.
    pub max_fill: usize,
    /// Ignore pixels whose filled depth is nearer than this (safety vs sky).
    pub min_depth: f32,
    /// Synthesized splat scale (metres; ~ the pixel's world footprint).
    pub scale: f32,
}

impl Default for FillConfig {
    fn default() -> Self {
        FillConfig {
            stride: 4,
            ds: 8.0,
            px_step: 4,
            rounds: 12,
            max_fill: 120_000,
            min_depth: 0.15,
            scale: 0.02,
        }
    }
}

pub struct FillPoint {
    pub pos: Vec3,
    pub rgb: [u8; 3],
    /// Depth of the filled pixel — lets emit keep scale proportional to
    /// pixel footprint instead of a global constant.
    pub depth: f32,
    /// Unit normal pointing back at the synthesizing camera.
    pub normal: Vec3,
}

/// Nearest-valid diffusion fill of a sparse depth buffer (in-place).
/// `z`: f32 depths (INFINITY = unknown). Returns filled buffer.
fn diffuse_fill(z: &mut [f32], w: usize, h: usize, rounds: u32) {
    for _ in 0..rounds {
        let src = z.to_vec();
        let mut any = false;
        for y in 0..h {
            for x in 0..w {
                let i = y * w + x;
                if src[i].is_finite() {
                    continue;
                }
                let mut m = f32::INFINITY;
                for dy in -1i32..=1 {
                    for dx in -1i32..=1 {
                        let (xx, yy) = (x as i32 + dx, y as i32 + dy);
                        if xx >= 0 && yy >= 0 && xx < w as i32 && yy < h as i32 {
                            m = m.min(src[(yy as usize * w + xx as usize) as usize]);
                        }
                    }
                }
                if m.is_finite() {
                    z[i] = m;
                    any = true;
                }
            }
        }
        if !any {
            break;
        }
    }
}

/// Build a sparse depth buffer for `view` from `pts` at `ds` px-per-cell.
fn sparse_buf(view: &SeedView, pts: &[Vec3], ds: f32) -> (usize, usize, Vec<f32>) {
    let cam = &view.camera;
    let w = ((cam.width as f32 / ds).ceil() as usize).max(8);
    let h = ((cam.height as f32 / ds).ceil() as usize).max(8);
    let mut z = vec![f32::INFINITY; w * h];
    for &p in pts {
        if let Some((u, v, d)) = cam.project(p) {
            let (x, y) = ((u / ds) as i32, (v / ds) as i32);
            if x >= 0 && y >= 0 && x < w as i32 && y < h as i32 {
                let i = y as usize * w + x as usize;
                if d < z[i] {
                    z[i] = d;
                }
            }
        }
    }
    (w, h, z)
}

/// Bilinear image sample (duplicated from colour.rs to keep files independent).
#[inline]
fn sample_img(img: &image::RgbImage, u: f32, v: f32) -> [f32; 3] {
    let (w, h) = (img.width() as f32, img.height() as f32);
    let x = (u - 0.5).clamp(0.0, w - 1.0);
    let y = (v - 0.5).clamp(0.0, h - 1.0);
    let (x0, y0) = (x.floor() as u32, y.floor() as u32);
    let (x1, y1) = ((x0 + 1).min(img.width() - 1), (y0 + 1).min(img.height() - 1));
    let (fx, fy) = (x - x0 as f32, y - y0 as f32);
    let (a, b, c, d) = (
        img.get_pixel(x0, y0),
        img.get_pixel(x1, y0),
        img.get_pixel(x0, y1),
        img.get_pixel(x1, y1),
    );
    let mut out = [0.0f32; 3];
    for k in 0..3 {
        let top = a[k] as f32 * (1.0 - fx) + b[k] as f32 * fx;
        let bot = c[k] as f32 * (1.0 - fx) + d[k] as f32 * fx;
        out[k] = top * (1.0 - fy) + bot * fy;
    }
    out
}

/// Synthesize fill points for `views` (strided) over the measured `pts`.
/// Only pixels whose buffer cell had NO dot coverage get a candidate; depth
/// comes from the completed buffer. Returns world-space candidates.
pub fn synthesize(pts: &[Vec3], views: &[SeedView], cfg: &FillConfig) -> Vec<FillPoint> {
    let mut out: Vec<FillPoint> = Vec::new();
    let budget = cfg.max_fill;
    for (vi, view) in views.iter().enumerate() {
        if vi % cfg.stride.max(1) != 0 {
            continue;
        }
        let Some(img) = &view.image else { continue };
        let cam = &view.camera;
        let (w, h, mut z) = sparse_buf(view, pts, cfg.ds);
        // record which cells were empty BEFORE fill (only they get splats)
        let empty: Vec<bool> = z.iter().map(|v| !v.is_finite()).collect();
        diffuse_fill(&mut z, w, h, cfg.rounds);
        let step = cfg.px_step.max(1) as usize;
        let sx = img.width() as f32 / cam.width as f32;
        let sy = img.height() as f32 / cam.height as f32;
        let mut cand: Vec<FillPoint> = Vec::new();
        for by in (0..h).step_by(step) {
            for bx in (0..w).step_by(step) {
                let i = by * w + bx;
                if !empty[i] {
                    continue;
                }
                let d = z[i];
                if !d.is_finite() || d < cfg.min_depth {
                    continue;
                }
                let u = (bx as f32 + 0.5) * cfg.ds;
                let v = (by as f32 + 0.5) * cfg.ds;
                let p = cam.position() + cam.ray(u, v) * d;
                let c = sample_img(img, u * sx, v * sy);
                cand.push(FillPoint {
                    pos: p,
                    rgb: [c[0] as u8, c[1] as u8, c[2] as u8],
                    depth: d,
                    normal: (cam.position() - p).normalize_or_zero(),
                });
            }
        }
        out.append(&mut cand);
        if out.len() >= budget {
            out.truncate(budget);
            break;
        }
    }
    out
}
