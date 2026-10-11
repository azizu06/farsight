//! Texture baking internals: per-camera min-depth occlusion buffers, per-chart
//! candidate view selection, weighted top-k image blending with per-image gain
//! compensation, a cloud-colour splat fallback, and coarse-to-fine inpainting
//! of texels no view (or point) covers.

use crate::core::*;
use crate::spatial::Grid;
use crate::texture::atlas::{decode, Atlas, Chart};
use rayon::prelude::*;
use rustc_hash::FxHashMap;
use std::sync::atomic::{AtomicU32, Ordering};

/// Reduced-resolution min-depth buffer for one capture view: whether a world
/// point is visible to that camera (i.e. not behind closer geometry).
pub struct DepthBuf {
    pub w: u32,
    pub h: u32,
    /// image pixels per buffer pixel (>1 = downsampled buffer)
    pub ds: f32,
    pub z: Vec<f32>,
}

impl DepthBuf {
    /// True when buffer depth at (u,v) is closer than `d - eps`.
    #[inline]
    pub fn occluded(&self, u: f32, v: f32, d: f32, eps: f32) -> bool {
        let x = (u / self.ds) as u32;
        let y = (v / self.ds) as u32;
        if x >= self.w || y >= self.h {
            return true;
        }
        self.z[(y * self.w + x) as usize] < d - eps
    }
}

/// One min-depth buffer per camera: splat mesh verts plus every `cloud_stride`-th
/// cloud point (the dense cloud plugs the gaps a decimated mesh would leave;
/// striding keeps this linear in views). `dilate` 3x3-min passes close
/// residual pinholes between sparse splats.
/// GPU-shaped kernel: pure SoA — positions in, per-view atomic-min buffer out.
pub fn depth_buffers(capture: &Capture, mesh: &Mesh, cloud: &PointCloud, scale: f32, dilate: u32, cloud_stride: usize) -> Vec<DepthBuf> {
    let stride = cloud_stride.max(1);
    capture
        .cameras
        .par_iter()
        .map(|cam| {
            let w = ((cam.width as f32 / scale).ceil() as u32).max(8);
            let h = ((cam.height as f32 / scale).ceil() as u32).max(8);
            let z: Vec<AtomicU32> = (0..w * h).map(|_| AtomicU32::new(u32::MAX)).collect();
            let splat = |pts: &[V3], step: usize| {
                pts.par_chunks(step).for_each(|chunk| {
                    let p = chunk[0];
                    if let Some((u, v, d)) = cam.project(p) {
                        let (x, y) = ((u / scale) as i32, (v / scale) as i32);
                        if x >= 0 && y >= 0 && x < w as i32 && y < h as i32 {
                            // positive f32 bit patterns order like the floats
                            z[(y as u32 * w + x as u32) as usize].fetch_min(d.to_bits(), Ordering::Relaxed);
                        }
                    }
                });
            };
            splat(&mesh.pos, 1);
            splat(&cloud.pos, stride);
            let mut zb: Vec<f32> = z.iter().map(|a| f32::from_bits(a.load(Ordering::Relaxed))).collect();
            // 3x3 min-filter dilation closes single-pixel splat holes
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
            DepthBuf { w, h, ds: scale, z: zb }
        })
        .collect()
}

/// Per-chart shortlist of capture views, best first: front-facing views that
/// resolve at least ~`prefer_px` pixels per atlas texel.
pub fn candidates(c: &Chart, mesh: &Mesh, cams: &[Camera], keep: usize, texel: f32, prefer_px: f32) -> Vec<(u32, f32)> {
    let mut cen = [0.0f32; 3];
    for &v in &c.verts {
        cen = add(cen, mesh.pos[v as usize]);
    }
    cen = scale(cen, 1.0 / c.verts.len().max(1) as f32);
    let mut scored: Vec<(u32, f32)> = cams
        .iter()
        .enumerate()
        .filter_map(|(i, cam)| {
            let to = sub(cam.position(), cen);
            let d = length(to);
            if d < 1e-3 {
                return None;
            }
            let facing = dot(c.normal, scale(to, 1.0 / d));
            if facing <= 0.02 {
                return None; // camera behind the chart plane
            }
            let res = cam.fx * texel / d; // pixels per texel at centroid depth
            let score = facing * (res / prefer_px).min(1.0);
            if score <= 0.0 {
                None
            } else {
                Some((i as u32, score))
            }
        })
        .collect();
    scored.sort_by(|a, b| b.1.total_cmp(&a.1));
    scored.truncate(keep.min(16));
    scored
}

const MAX_CAND: usize = 16;

/// One accepted view contribution inside `eval_capture`.
struct ViewEval {
    /// blended sRGB colour 0..255
    rgb: [f32; 3],
    /// dominant camera (or -1) and its raw (pre-gain) sample
    dom: i32,
    dom_src: [f32; 3],
}

/// Weighted multi-view colour at surface point `p` (normal `n`), sRGB 0..255.
#[inline]
fn eval_capture(
    p: V3,
    n: V3,
    cand: &[(u32, f32)],
    capture: &Capture,
    depths: &[DepthBuf],
    gains: &[[f32; 3]],
    blend_k: usize,
    eps: f32,
    prefer_px: f32,
) -> (ViewEval, f32) {
    let mut w = [0f32; MAX_CAND];
    let mut uu = [0f32; MAX_CAND];
    let mut vv = [0f32; MAX_CAND];
    let mut cc = [0u32; MAX_CAND];
    let mut m = 0usize;
    for &(ci, score) in cand.iter().take(MAX_CAND) {
        let cam = &capture.cameras[ci as usize];
        let img = &capture.images[ci as usize];
        let Some((u, v, d)) = cam.project(p) else { continue };
        if u < 1.0 || v < 1.0 || u > img.width as f32 - 1.0 || v > img.height as f32 - 1.0 {
            continue;
        }
        let dir = normalize(sub(p, cam.position()));
        let facing = -dot(n, dir);
        if facing <= 0.0 {
            continue;
        }
        if depths[ci as usize].occluded(u, v, d, eps) {
            continue;
        }
        // weight: chart score * per-texel facing * resolution adequacy * edge feather
        let res = cam.fx * prefer_px / d; // px per texel at this depth
        let edge = u.min(v).min(img.width as f32 - u).min(img.height as f32 - v).clamp(0.0, 8.0) / 8.0;
        w[m] = score * facing.sqrt() * res.min(1.0) * edge;
        uu[m] = u;
        vv[m] = v;
        cc[m] = ci;
        m += 1;
        if m == MAX_CAND {
            break;
        }
    }
    let mut sum = [0f32; 3];
    let mut wsum = 0f32;
    let mut ev = ViewEval { rgb: [0.0; 3], dom: -1, dom_src: [0.0; 3] };
    for _ in 0..blend_k.min(m) {
        let mut bi = -1i32;
        for j in 0..m {
            if w[j] > 0.0 && (bi < 0 || w[j] > w[bi as usize]) {
                bi = j as i32;
            }
        }
        if bi < 0 {
            break;
        }
        let j = bi as usize;
        let ci = cc[j] as usize;
        let s = capture.images[ci].sample(uu[j], vv[j]);
        if ev.dom < 0 {
            ev.dom = ci as i32;
            ev.dom_src = s;
        }
        let g = gains.get(ci).copied().unwrap_or([1.0; 3]);
        for k in 0..3 {
            sum[k] += w[j] * g[k] * s[k];
        }
        wsum += w[j];
        w[j] = -1.0;
    }
    if wsum <= 1e-6 {
        return (ev, 0.0);
    }
    ev.rgb = [sum[0] / wsum, sum[1] / wsum, sum[2] / wsum];
    (ev, wsum)
}

/// Inverse-distance weighted cloud colour at `q` (sRGB 0..255).
#[inline]
pub fn idw(grid: &Grid, cloud: &PointCloud, q: V3, r: f32, k: usize, scratch: &mut Vec<(u32, f32)>) -> Option<[f32; 3]> {
    if cloud.col.is_empty() {
        return None;
    }
    grid.knn(q, k, r, scratch);
    if scratch.is_empty() {
        return None;
    }
    let (mut sum, mut wsum) = ([0f32; 3], 0f32);
    for &(i, d2) in scratch.iter() {
        let w = cloud.weight(i as usize) / (d2 + 1e-8);
        let c = cloud.col[i as usize];
        for ch in 0..3 {
            sum[ch] += w * c[ch] as f32;
        }
        wsum += w;
    }
    if wsum <= 1e-9 {
        None
    } else {
        Some([sum[0] / wsum, sum[1] / wsum, sum[2] / wsum])
    }
}

/// Per-chart render target: rgb bytes + per-texel "set" mask (1 = baked).
pub struct ChartOut {
    pub rgb: Vec<u8>,
    pub set: Vec<u8>,
    /// dominant-view texel count per camera (stats)
    pub dom: FxHashMap<u32, u64>,
    pub covered: u64,
    /// per-camera (sum src, sum target) pairs for the gain solve
    pub gain_acc: FxHashMap<u32, ([f64; 3], [f64; 3])>,
}

/// Shared bake inputs. Everything texel evaluation needs, indexed by chart.
pub struct BakeCtx<'a> {
    pub mesh: &'a Mesh,
    pub cloud: &'a PointCloud,
    pub capture: Option<&'a Capture>,
    pub face_nrm: &'a [V3],
    pub depths: Vec<DepthBuf>,
    pub cands: Vec<Vec<(u32, f32)>>,
    pub gains: Vec<[f32; 3]>,
    pub grid: Option<Grid>,
    pub splat_r: f32,
    pub splat_k: usize,
    pub blend_k: usize,
    pub eps: f32,
    pub prefer_px: f32,
    /// texel evaluation stride (1 = every texel; >1 = subsampled gain pass)
    pub stride: usize,
    /// (strength 0..1, tint colour sRGB 0..255) applied above `tint_support_m`
    pub tint: Option<(f32, [f32; 3])>,
    pub tint_support_m: f32,
}

/// Barycentric mix of the mesh's own vertex colours at texel `v` — the
/// last-resort fill so no covered texel stays black even when neither a
/// capture view nor a nearby cloud point covers it.
#[inline]
pub fn vert_col(mesh: &Mesh, v: u64) -> Option<[f32; 3]> {
    if mesh.col.len() != mesh.pos.len() {
        return None;
    }
    let (f, w0, w1, w2) = decode(v);
    let (a, b, c) = (
        mesh.idx[3 * f as usize] as usize,
        mesh.idx[3 * f as usize + 1] as usize,
        mesh.idx[3 * f as usize + 2] as usize,
    );
    let mut o = [0f32; 3];
    for k in 0..3 {
        o[k] = mesh.col[a][k] as f32 * w0 + mesh.col[b][k] as f32 * w1 + mesh.col[c][k] as f32 * w2;
    }
    Some(o)
}

/// World position / interpolated normal / interpolated support of a texel.
#[inline]
fn texel_point(mesh: &Mesh, face_nrm: &[V3], v: u64) -> (V3, V3, f32) {
    let (f, w0, w1, w2) = decode(v);
    let (a, b, c) = (
        mesh.idx[3 * f as usize] as usize,
        mesh.idx[3 * f as usize + 1] as usize,
        mesh.idx[3 * f as usize + 2] as usize,
    );
    let p = add(add(scale(mesh.pos[a], w0), scale(mesh.pos[b], w1)), scale(mesh.pos[c], w2));
    let n = if mesh.nrm.len() == mesh.pos.len() {
        normalize(add(add(scale(mesh.nrm[a], w0), scale(mesh.nrm[b], w1)), scale(mesh.nrm[c], w2)))
    } else {
        face_nrm[f as usize]
    };
    let s = if mesh.support.len() == mesh.pos.len() {
        mesh.support[a] * w0 + mesh.support[b] * w1 + mesh.support[c] * w2
    } else {
        0.0
    };
    (p, n, s)
}

/// Bake one chart into its own pixel buffer.
pub fn bake_chart(ctx: &BakeCtx, atlas: &Atlas, ci: usize, cov: &[u64]) -> ChartOut {
    let c = &atlas.charts[ci];
    let mut out = ChartOut {
        rgb: vec![0; (c.w * c.h * 3) as usize],
        set: vec![0; (c.w * c.h) as usize],
        dom: FxHashMap::default(),
        covered: 0,
        gain_acc: FxHashMap::default(),
    };
    let cand = ctx.cands.get(ci).map(|v| v.as_slice()).unwrap_or(&[]);
    let stride = ctx.stride.max(1);
    let mut knn_scratch: Vec<(u32, f32)> = Vec::with_capacity(ctx.splat_k);
    for ty in (0..c.h).step_by(stride) {
        for tx in (0..c.w).step_by(stride) {
            let ti = (ty * c.w + tx) as usize;
            let v = cov[ti];
            if v == u64::MAX {
                continue;
            }
            let (p, n, sup) = texel_point(ctx.mesh, ctx.face_nrm, v);
            let mut rgb: Option<[f32; 3]> = None;
            if let (Some(cap), true) = (ctx.capture, !cand.is_empty()) {
                let (ev, wsum) = eval_capture(p, n, cand, cap, &ctx.depths, &ctx.gains, ctx.blend_k, ctx.eps, ctx.prefer_px);
                if wsum > 0.0 {
                    rgb = Some(ev.rgb);
                    if ev.dom >= 0 {
                        *out.dom.entry(ev.dom as u32).or_default() += 1;
                        if stride > 1 {
                            let e = out.gain_acc.entry(ev.dom as u32).or_default();
                            for k in 0..3 {
                                e.0[k] += ev.dom_src[k] as f64;
                                e.1[k] += ev.rgb[k] as f64;
                            }
                        }
                    }
                }
            }
            if rgb.is_none() {
                if let Some(g) = &ctx.grid {
                    rgb = idw(g, ctx.cloud, p, ctx.splat_r, ctx.splat_k, &mut knn_scratch);
                }
            }
            if rgb.is_none() {
                rgb = vert_col(ctx.mesh, v);
            }
            if let Some(mut rgb) = rgb {
                if let (true, Some((strength, tc))) = (sup > ctx.tint_support_m, ctx.tint) {
                    for k in 0..3 {
                        rgb[k] = rgb[k] * (1.0 - strength) + tc[k] * strength;
                    }
                }
                out.rgb[3 * ti] = rgb[0].clamp(0.0, 255.0) as u8;
                out.rgb[3 * ti + 1] = rgb[1].clamp(0.0, 255.0) as u8;
                out.rgb[3 * ti + 2] = rgb[2].clamp(0.0, 255.0) as u8;
                out.set[ti] = 1;
                out.covered += 1;
            }
        }
    }
    out
}

/// Per-image exposure gains from the estimation pass:
/// g_i = clamp(sum target / sum src) per channel, a robust mean-ratio solve.
pub fn solve_gains(outs: &[ChartOut], n_cams: usize) -> Vec<[f32; 3]> {
    let mut sums: Vec<([f64; 3], [f64; 3])> = vec![([0.0; 3], [0.0; 3]); n_cams];
    for o in outs {
        for (&ci, &(src, tgt)) in &o.gain_acc {
            for k in 0..3 {
                sums[ci as usize].0[k] += src[k];
                sums[ci as usize].1[k] += tgt[k];
            }
        }
    }
    sums.iter()
        .map(|(s, t)| {
            let mut g = [1.0f32; 3];
            for k in 0..3 {
                if s[k] > 1.0 && t[k] > 0.0 {
                    g[k] = (t[k] / s[k]).clamp(0.5, 2.0) as f32;
                }
            }
            g
        })
        .collect()
}

/// Fill unset texels inside `rects` (chart gutter-boxes, non-overlapping) by
/// multi-source 4-neighbour colour propagation: every round fills texels
/// adjacent to set ones with their mean, until nothing is left unset.
/// Band-limited work: cost ~ chart rects, not the whole atlas.
struct RawMut(*mut u8);
unsafe impl Send for RawMut {}
unsafe impl Sync for RawMut {}
impl RawMut {
    // SAFETY: callers guarantee disjoint access (see fill_rects).
    #[inline]
    fn rd(&self, i: usize) -> u8 {
        unsafe { *self.0.add(i) }
    }
    #[inline]
    fn wr(&self, i: usize, v: u8) {
        unsafe { *self.0.add(i) = v }
    }
}

pub fn fill_rects(img: &mut Image, set: &mut [u8], rects: &[(u32, u32, u32, u32)]) {
    // SAFETY: shelf-packed gutter boxes are disjoint, so each rect's pixel
    // range is exclusively owned by one parallel task.
    let rgb = RawMut(img.rgb.as_mut_ptr());
    let setp = RawMut(set.as_mut_ptr());
    rects.par_iter().for_each(|&(rx, ry, rw, rh)| {
        let (aw, ah) = (img.width as i64, img.height as i64);
        let (x0, y0) = (rx as i64, ry as i64);
        let (x1, y1) = (((rx + rw) as i64).min(aw), ((ry + rh) as i64).min(ah));
        for _ in 0..(rw + rh).max(2) {
            let mut filled_any = false;
            for y in y0.max(0)..y1 {
                for x in x0.max(0)..x1 {
                    let i = (y * aw + x) as usize;
                    if setp.rd(i) != 0 {
                        continue;
                    }
                    let (mut s, mut n) = ([0f32; 3], 0f32);
                    for (dx, dy) in [(-1i64, 0), (1, 0), (0, -1), (0, 1)] {
                        let (xx, yy) = (x + dx, y + dy);
                        if xx >= x0 && yy >= y0 && xx < x1 && yy < y1 {
                            let j = (yy * aw + xx) as usize;
                            if setp.rd(j) != 0 {
                                s[0] += rgb.rd(3 * j) as f32;
                                s[1] += rgb.rd(3 * j + 1) as f32;
                                s[2] += rgb.rd(3 * j + 2) as f32;
                                n += 1.0;
                            }
                        }
                    }
                    if n > 0.0 {
                        rgb.wr(3 * i, (s[0] / n) as u8);
                        rgb.wr(3 * i + 1, (s[1] / n) as u8);
                        rgb.wr(3 * i + 2, (s[2] / n) as u8);
                        setp.wr(i, 1);
                        filled_any = true;
                    }
                }
            }
            if !filled_any {
                break;
            }
        }
    });
}

/// Fill unset texels (set[] == 0) by coarse-to-fine diffusion: average covered
/// texels per `ds`-cell, diffuse the coarse grid, bilinear-sample it back.
/// Cheap and smooth — the right look for regions no camera ever saw.
pub fn inpaint(img: &mut Image, set: &[u8], ds: u32) {
    let (w, h) = (img.width as usize, img.height as usize);
    let ds = ds.max(2) as usize;
    let cw = w.div_ceil(ds);
    let ch = h.div_ceil(ds);
    let mut csum = vec![[0f32; 4]; cw * ch]; // rgb + count
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            if set[i] == 0 {
                continue;
            }
            let c = &mut csum[(y / ds) * cw + (x / ds)];
            c[0] += img.rgb[3 * i] as f32;
            c[1] += img.rgb[3 * i + 1] as f32;
            c[2] += img.rgb[3 * i + 2] as f32;
            c[3] += 1.0;
        }
    }
    // global fallback colour for coarse cells with nothing nearby
    let mut gmean = [0f32; 3];
    let mut gn = 0f32;
    for c in &csum {
        gmean[0] += c[0];
        gmean[1] += c[1];
        gmean[2] += c[2];
        gn += c[3];
    }
    if gn > 0.0 {
        for k in 0..3 {
            gmean[k] /= gn;
        }
    }
    let mut cells: Vec<[f32; 3]> = csum
        .iter()
        .map(|c| if c[3] > 0.0 { [c[0] / c[3], c[1] / c[3], c[2] / c[3]] } else { [f32::NAN; 3] })
        .collect();
    // diffuse empty coarse cells until all are set (bounded by grid extent)
    let mut todo: Vec<usize> = (0..cells.len()).filter(|&i| cells[i][0].is_nan()).collect();
    let mut rounds = 0;
    while !todo.is_empty() && rounds < cw + ch {
        rounds += 1;
        let prev = cells.clone();
        todo.retain(|&i| {
            let (cx, cy) = (i % cw, i / cw);
            let (mut s, mut n) = ([0f32; 3], 0f32);
            for (dx, dy) in [(-1i32, 0), (1, 0), (0, -1), (0, 1)] {
                let (x, y) = (cx as i32 + dx, cy as i32 + dy);
                if x >= 0 && y >= 0 && x < cw as i32 && y < ch as i32 {
                    let p = prev[y as usize * cw + x as usize];
                    if !p[0].is_nan() {
                        for k in 0..3 {
                            s[k] += p[k];
                        }
                        n += 1.0;
                    }
                }
            }
            if n > 0.0 {
                cells[i] = [s[0] / n, s[1] / n, s[2] / n];
                false
            } else {
                true
            }
        });
    }
    for i in todo {
        cells[i] = gmean;
    }
    // bilinear-sample the coarse image into every unset texel
    let lut_x: Vec<f32> = (0..w).map(|x| (x as f32 + 0.5) / ds as f32 - 0.5).collect();
    let lut_y: Vec<f32> = (0..h).map(|y| (y as f32 + 0.5) / ds as f32 - 0.5).collect();
    let cidx = |fx: f32, fy: f32| -> [f32; 3] {
        let x = fx.clamp(0.0, cw as f32 - 1.0);
        let y = fy.clamp(0.0, ch as f32 - 1.0);
        let (x0, y0) = (x.floor() as usize, y.floor() as usize);
        let (x1, y1) = ((x0 + 1).min(cw - 1), (y0 + 1).min(ch - 1));
        let (ax, ay) = (x - x0 as f32, y - y0 as f32);
        let (a, b, c, d) = (cells[y0 * cw + x0], cells[y0 * cw + x1], cells[y1 * cw + x0], cells[y1 * cw + x1]);
        let mut o = [0f32; 3];
        for k in 0..3 {
            o[k] = (a[k] * (1.0 - ax) + b[k] * ax) * (1.0 - ay) + (c[k] * (1.0 - ax) + d[k] * ax) * ay;
        }
        o
    };
    let mut filled = Vec::with_capacity(w * h / 8 + 16);
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            if set[i] == 0 {
                let c = cidx(lut_x[x], lut_y[y]);
                filled.push((i, c));
            }
        }
    }
    for (i, c) in filled {
        img.rgb[3 * i] = c[0].clamp(0.0, 255.0) as u8;
        img.rgb[3 * i + 1] = c[1].clamp(0.0, 255.0) as u8;
        img.rgb[3 * i + 2] = c[2].clamp(0.0, 255.0) as u8;
    }
}

/// DC seam-leveling across chart boundaries (multiband-lite): texels on a
/// mesh-space seam are re-averaged with the same world point sampled through
/// every neighbouring chart, so two charts meeting at a wall corner can't
/// show a hard colour discontinuity from different view blends.
/// Runs on the immutable baked image; corrections are applied afterwards.
pub fn seam_level(img: &Image, set: &[u8], atlas: &Atlas, mesh: &Mesh, cov: &[Vec<u64>]) -> Vec<(usize, [u8; 3])> {
    // vertex -> charts (CSR)
    let mut deg = vec![0u32; mesh.pos.len()];
    for c in &atlas.charts {
        for &v in &c.verts {
            deg[v as usize] += 1;
        }
    }
    let mut off = vec![0u64; mesh.pos.len() + 1];
    for i in 0..mesh.pos.len() {
        off[i + 1] = off[i] + deg[i] as u64;
    }
    let mut vchart = vec![0u32; off[mesh.pos.len()] as usize];
    let mut cur = off[..mesh.pos.len()].to_vec();
    for (ci, c) in atlas.charts.iter().enumerate() {
        for &v in &c.verts {
            let s = cur[v as usize] as usize;
            vchart[s] = ci as u32;
            cur[v as usize] += 1;
        }
    }
    let sample = |c: &Chart, p: V3| -> Option<[f32; 3]> {
        let u = dot(p, c.eu);
        let v = dot(p, c.ev);
        let tx = (u - c.omin[0]) / atlas.texel_m;
        let ty = (v - c.omin[1]) / atlas.texel_m;
        if tx < 0.0 || ty < 0.0 || tx > c.w as f32 - 1.0 || ty > c.h as f32 - 1.0 {
            return None;
        }
        let ax = c.x as f32 + tx;
        let ay = c.y as f32 + ty;
        let (ix, iy) = (ax as i64, ay as i64);
        if ix < 0 || iy < 0 || ix >= img.width as i64 || iy >= img.height as i64 {
            return None;
        }
        if set[(iy * img.width as i64 + ix) as usize] == 0 {
            return None;
        }
        Some(img.sample(ax, ay))
    };
    let mut out = Vec::new();
    for (ci, c) in atlas.charts.iter().enumerate() {
        for ty in 0..c.h {
            for tx in 0..c.w {
                let ti = (ty * c.w + tx) as usize;
                let v = cov[ci][ti];
                if v == u64::MAX {
                    continue;
                }
                let f = v as u32;
                // boundary = any of the tri's verts lives in >1 chart
                let (a, b, d) = (mesh.idx[3 * f as usize], mesh.idx[3 * f as usize + 1], mesh.idx[3 * f as usize + 2]);
                if deg[a as usize] < 2 && deg[b as usize] < 2 && deg[d as usize] < 2 {
                    continue;
                }
                let (_, w0, w1, w2) = decode(v);
                let p = add(add(scale(mesh.pos[a as usize], w0), scale(mesh.pos[b as usize], w1)), scale(mesh.pos[d as usize], w2));
                let mut acc = [0f32; 3];
                let mut n = 0f32;
                for k in [a, b, d] {
                    let (s, e) = (off[k as usize] as usize, off[k as usize + 1] as usize);
                    for &oj in &vchart[s..e] {
                        if oj as usize != ci {
                            if let Some(smp) = sample(&atlas.charts[oj as usize], p) {
                                acc = add(acc, smp);
                                n += 1.0;
                            }
                        }
                    }
                }
                if n > 0.0 {
                    let own_i = ((c.y + ty) * atlas.w + c.x + tx) as usize;
                    let own = [img.rgb[3 * own_i] as f32, img.rgb[3 * own_i + 1] as f32, img.rgb[3 * own_i + 2] as f32];
                    let mean = scale(add(acc, own), 1.0 / (n + 1.0));
                    out.push((own_i, [mean[0] as u8, mean[1] as u8, mean[2] as u8]));
                }
            }
        }
    }
    out
}
