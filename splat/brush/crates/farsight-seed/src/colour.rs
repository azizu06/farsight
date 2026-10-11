//! Photo reprojection colour + sparse inverse-depth export, sharing one
//! projection pass per view:
//!   pass A — splat the point cloud into a coarse min-depth buffer;
//!   pass B — re-project every point, keep the unoccluded ones: collect an
//!            sRGB sample (for the robust-median colour) and append a sparse
//!            inverse-depth record for the depth-supervision export.
//! Depth-buffer occlusion logic follows farsight-compiler texture/bake.rs.

use crate::views::SeedView;
use glam::Vec3;
use rayon::prelude::*;
use std::sync::atomic::{AtomicU32, Ordering};

/// One sparse inverse-depth sample in a view: pixel (u, v) and 1/depth_m.
/// Serialized little-endian for the T2/T3 depth-supervision pipeline.
#[derive(Clone, Copy, Debug)]
pub struct DepthSample {
    pub u: f32,
    pub v: f32,
    pub inv_d: f32,
}

/// Sparse inverse depth for one training view.
pub struct SparseDepthView {
    /// Index into `views` of the capture (i.e. SeedView::frame_index).
    pub frame_index: usize,
    pub samples: Vec<DepthSample>,
}

#[derive(Clone, Debug)]
pub struct ColourConfig {
    /// Use every Nth view for colour sampling (0 = auto: ~96 views).
    pub colour_stride: usize,
    /// Max photo samples kept per point before we stop testing it (0 = all).
    pub max_samples: usize,
    /// Depth-buffer downsample factor (image px per buffer px). Keep at 1:
    /// coarser buffers + dilation bias the min depth low and mass-reject
    /// valid projections (measured: ds=4/dilate=2 accepts 42% of in-frame
    /// projections, ds=1/dilate=0 accepts 94% on ±2 cm dot noise).
    pub depth_buf_scale: f32,
    /// 3x3 min-dilation passes on the depth buffer (closes splat pinholes).
    pub dilate: u32,
    /// Occlusion epsilon in metres — a point is occluded when the buffer's
    /// min depth is < d - eps. Covers pose drift + dot noise (≈3σ of 2 cm).
    pub occ_eps: f32,
    /// Export sparse depth for every Nth view (0 = same as colour_stride).
    pub depth_stride: usize,
    /// Minimum samples a point needs before we trust its median colour.
    pub min_samples: usize,
}

impl Default for ColourConfig {
    fn default() -> Self {
        ColourConfig {
            colour_stride: 0,
            max_samples: 12,
            depth_buf_scale: 1.0,
            dilate: 1,
            occ_eps: 0.06,
            depth_stride: 0,
            min_samples: 1,
        }
    }
}

/// Coarse min-depth buffer for one view.
struct DepthBuf {
    w: u32,
    h: u32,
    ds: f32,
    z: Vec<f32>,
}

impl DepthBuf {
    #[inline]
    fn occluded(&self, u: f32, v: f32, d: f32, eps: f32) -> bool {
        let x = (u / self.ds) as u32;
        let y = (v / self.ds) as u32;
        if x >= self.w || y >= self.h {
            return true;
        }
        self.z[(y * self.w + x) as usize] < d - eps
    }
}

/// Splat `pts` into a min-depth buffer for `view` (downsampled by `ds`),
/// then `dilate` 3x3-min passes.
fn build_depth_buf(view: &SeedView, pts: &[Vec3], ds: f32, dilate: u32) -> DepthBuf {
    let cam = &view.camera;
    let w = ((cam.width as f32 / ds).ceil() as u32).max(8);
    let h = ((cam.height as f32 / ds).ceil() as u32).max(8);
    let z: Vec<AtomicU32> = (0..w * h).map(|_| AtomicU32::new(u32::MAX)).collect();
    pts.par_chunks(8192).for_each(|chunk| {
        for &p in chunk {
            if let Some((u, v, d)) = cam.project(p) {
                let (x, y) = ((u / ds) as i32, (v / ds) as i32);
                if x >= 0 && y >= 0 && x < w as i32 && y < h as i32 {
                    // positive f32 bit patterns order like the floats
                    z[(y as u32 * w + x as u32) as usize].fetch_min(d.to_bits(), Ordering::Relaxed);
                }
            }
        }
    });
    let mut zb: Vec<f32> = z.iter().map(|a| f32::from_bits(a.load(Ordering::Relaxed))).collect();
    for _ in 0..dilate {
        let src = zb.clone();
        for y in 0..h {
            for x in 0..w {
                let mut m = f32::INFINITY;
                for dy in -1i32..=1 {
                    for dx in -1i32..=1 {
                        let (xx, yy) = (x as i32 + dx, y as i32 + dy);
                        if xx >= 0 && yy >= 0 && xx < w as i32 && yy < h as i32 {
                            m = m.min(src[(yy as u32 * w + xx as u32) as usize]);
                        }
                    }
                }
                zb[(y * w + x) as usize] = m;
            }
        }
    }
    DepthBuf { w, h, ds, z: zb }
}

/// Bilinear sRGB sample of an RgbImage at pixel-centre coords (u, v).
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

/// Public wrapper of `sample_img` for the mono lane's per-point colour.
#[inline]
pub fn sample_img_pub(img: &image::RgbImage, u: f32, v: f32) -> [f32; 3] {
    sample_img(img, u, v)
}

/// Per-point result of the projection pass.
pub struct ColourResult {
    /// Median photo colour per point (sRGB 0..255); None when unobserved.
    pub colours: Vec<Option<[u8; 3]>>,
    /// How many accepted samples each point got (drives opacity).
    pub sample_counts: Vec<u16>,
    /// Sparse inverse depth per exported view.
    pub depth: Vec<SparseDepthView>,
    /// Per-view coverage stats: accepted projections / in-frame projections.
    pub view_stats: Vec<(usize, u32, u32)>,
}

/// Run the projection pass over `views` for `pts`.
/// `colour_views`: which views to take colour samples from (all get depth).
/// `depth_views`: which views to export sparse depth for.
pub fn project(
    pts: &[Vec3],
    views: &[SeedView],
    cfg: &ColourConfig,
    colour_stride: usize,
    depth_stride: usize,
) -> ColourResult {
    let n = pts.len();
    let max_s = cfg.max_samples.max(1);
    // per-point sample storage: fixed cap, u8 rgb
    let mut samples: Vec<Vec<[u8; 3]>> = vec![Vec::with_capacity(max_s.min(16)); n];
    let mut depth: Vec<SparseDepthView> = Vec::new();
    let mut view_stats: Vec<(usize, u32, u32)> = Vec::new();

    for (vi, view) in views.iter().enumerate() {
        let want_colour = vi % colour_stride == 0 && view.image.is_some();
        let want_depth = vi % depth_stride == 0;
        if !want_colour && !want_depth {
            continue;
        }
        let buf = build_depth_buf(view, pts, cfg.depth_buf_scale, cfg.dilate);
        let cam = &view.camera;
        let img = view.image.as_ref();
        let (img_w, img_h) = img.map_or((cam.width, cam.height), |i| (i.width(), i.height()));
        let eps = cfg.occ_eps;

        // pass B: project each point once; colour sample + depth record.
        let depth_out: Vec<DepthSample> = if want_depth {
            Vec::with_capacity(n / 8)
        } else {
            Vec::new()
        };
        let counters = (0..n)
            .into_par_iter()
            .map(|i| -> (Option<[u8; 3]>, Option<DepthSample>, u8) {
                let Some((u, v, d)) = cam.project(pts[i]) else {
                    return (None, None, 0);
                };
                if u < 0.5 || v < 0.5 || u >= img_w as f32 - 0.5 || v >= img_h as f32 - 0.5 {
                    return (None, None, 1); // in-frame miss counts only toward in-frame? no — treat as not projected
                }
                if buf.occluded(u, v, d, eps) {
                    return (None, None, 2);
                }
                let ds = DepthSample { u, v, inv_d: 1.0 / d };
                let rgb = if want_colour {
                    img.map(|im| {
                        let s = sample_img(im, u * im.width() as f32 / cam.width as f32, v * im.height() as f32 / cam.height as f32);
                        [s[0] as u8, s[1] as u8, s[2] as u8]
                    })
                } else {
                    None
                };
                (rgb, Some(ds), 3)
            })
            .collect::<Vec<_>>();

        let mut rec = if want_depth {
            Vec::with_capacity(n / 8)
        } else {
            Vec::new()
        };
        let (mut n_in, mut n_ok) = (0u32, 0u32);
        for (i, (rgb, ds, tag)) in counters.into_iter().enumerate() {
            match tag {
                3 => {
                    n_in += 1;
                    n_ok += 1;
                    if let (true, Some(c)) = (want_colour, rgb) {
                        if samples[i].len() < max_s {
                            samples[i].push(c);
                        }
                    }
                    if let (true, Some(s)) = (want_depth, ds) {
                        rec.push(s);
                    }
                }
                2 => n_in += 1,
                _ => {}
            }
        }
        if want_depth {
            depth.push(SparseDepthView {
                frame_index: view.frame_index,
                samples: rec,
            });
        }
        view_stats.push((vi, n_in, n_ok));
        let _ = depth_out;
    }

    let colours: Vec<Option<[u8; 3]>> = samples
        .par_iter()
        .map(|s| {
            if s.len() < cfg.min_samples.max(1) {
                return None;
            }
            let mut r: Vec<u8> = s.iter().map(|c| c[0]).collect();
            let mut g: Vec<u8> = s.iter().map(|c| c[1]).collect();
            let mut b: Vec<u8> = s.iter().map(|c| c[2]).collect();
            r.sort_unstable();
            g.sort_unstable();
            b.sort_unstable();
            Some([r[r.len() / 2], g[g.len() / 2], b[b.len() / 2]])
        })
        .collect();
    let sample_counts: Vec<u16> = samples.iter().map(|s| s.len().min(u16::MAX as usize) as u16).collect();
    ColourResult {
        colours,
        sample_counts,
        depth,
        view_stats,
    }
}

/// Auto colour stride: aim for ~`target` colour views.
pub fn auto_stride(n_views: usize, target: usize) -> usize {
    if n_views <= target {
        return 1;
    }
    (n_views / target).max(1)
}
