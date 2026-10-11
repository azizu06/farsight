//! Synthetic ground-truth room + simulated rover RGB-D scan. OWNER: orchestrator.
//! Gives every stage a reproducible test bed with known geometry, sensor noise,
//! 10 mm quantisation like /dense, occlusion shadows and deliberate unobserved
//! holes (so interpolation quality is measurable), plus posed RGB images in
//! the same nerfstudio layout as jetson/mapping/run_capture.py.

use crate::core::*;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub enum Prim {
    /// Interior of an axis-aligned room (surface = walls/floor/ceiling).
    Room { min: V3, max: V3 },
    Box { center: V3, half: V3 },
    Sphere { center: V3, radius: f32 },
    /// Vertical cylinder standing on `base`.
    Cylinder { base: V3, radius: f32, height: f32 },
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub enum Material {
    Solid([f32; 3]),
    /// Checker on the two axes orthogonal to the dominant normal.
    Checker([f32; 3], [f32; 3], f32),
    /// Wood-like planks along X.
    Planks([f32; 3], f32),
    /// Horizontal stripes (wallpaper).
    Stripes([f32; 3], [f32; 3], f32),
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Scene {
    pub prims: Vec<(Prim, Material)>,
    pub light: V3,
    /// World-space spheres whose interior is never measured by the depth sensor
    /// (images still see them). Used to score hole filling.
    pub holes: Vec<(V3, f32)>,
}

fn sd_box(p: V3, c: V3, h: V3) -> f32 {
    let q = [(p[0] - c[0]).abs() - h[0], (p[1] - c[1]).abs() - h[1], (p[2] - c[2]).abs() - h[2]];
    let o = length([q[0].max(0.0), q[1].max(0.0), q[2].max(0.0)]);
    o + q[0].max(q[1]).max(q[2]).min(0.0)
}

impl Prim {
    pub fn sdf(&self, p: V3) -> f32 {
        match *self {
            Prim::Room { min, max } => -sd_box(p, scale(add(min, max), 0.5), scale(sub(max, min), 0.5)),
            Prim::Box { center, half } => sd_box(p, center, half),
            Prim::Sphere { center, radius } => length(sub(p, center)) - radius,
            Prim::Cylinder { base, radius, height } => {
                let d = [(p[0] - base[0]).hypot(p[2] - base[2]) - radius, (p[1] - base[1] - height * 0.5).abs() - height * 0.5];
                d[0].max(d[1]).min(0.0) + d[0].max(0.0).hypot(d[1].max(0.0))
            }
        }
    }
}

fn hash3(x: i32, y: i32, z: i32) -> f32 {
    let mut h = (x as u32).wrapping_mul(73856093) ^ (y as u32).wrapping_mul(19349663) ^ (z as u32).wrapping_mul(83492791);
    h ^= h >> 13; h = h.wrapping_mul(0x5bd1e995); h ^= h >> 15;
    (h & 0xffff) as f32 / 65535.0
}

impl Material {
    fn albedo(&self, p: V3, n: V3) -> [f32; 3] {
        match *self {
            Material::Solid(c) => c,
            Material::Checker(a, b, s) => {
                let ax = if n[1].abs() > 0.7 { (p[0], p[2]) } else if n[0].abs() > 0.7 { (p[2], p[1]) } else { (p[0], p[1]) };
                if ((ax.0 / s).floor() as i32 + (ax.1 / s).floor() as i32) & 1 == 0 { a } else { b }
            }
            Material::Planks(base, w) => {
                let row = (p[2] / w).floor() as i32;
                let off = hash3(row, 0, 7) * 1.7;
                let seg = ((p[0] + off) / 1.2).floor() as i32;
                let tone = 0.8 + 0.35 * hash3(row, seg, 1);
                let grain = 0.92 + 0.08 * ((p[0] * 37.0 + hash3(row, seg, 2) * 6.0).sin());
                let edge = if (p[2] / w).fract() < 0.04 { 0.55 } else { 1.0 };
                scale(base, tone * grain * edge)
            }
            Material::Stripes(a, b, period) => if (p[1] / period).fract() < 0.5 { a } else { b },
        }
    }
}

impl Scene {
    /// Furnished 6 x 2.8 x 5 m room, rover-scale.
    pub fn room() -> Self {
        Scene {
            prims: vec![
                (Prim::Room { min: [-3.0, 0.0, -2.5], max: [3.0, 2.8, 2.5] }, Material::Planks([0.55, 0.36, 0.2], 0.18)),
                (Prim::Box { center: [0.6, 0.38, -0.6], half: [0.7, 0.38, 0.45] }, Material::Checker([0.75, 0.73, 0.68], [0.3, 0.32, 0.36], 0.15)),
                (Prim::Box { center: [-2.2, 0.45, 1.2], half: [0.55, 0.45, 1.0] }, Material::Solid([0.2, 0.35, 0.6])),
                (Prim::Sphere { center: [0.8, 0.98, -0.5], radius: 0.22 }, Material::Solid([0.85, 0.2, 0.15])),
                (Prim::Cylinder { base: [2.2, 0.0, 1.6], radius: 0.18, height: 1.6 }, Material::Stripes([0.9, 0.85, 0.5], [0.3, 0.25, 0.2], 0.2)),
                (Prim::Box { center: [2.95, 1.4, -1.0], half: [0.05, 0.6, 0.9] }, Material::Checker([0.9, 0.9, 0.9], [0.1, 0.1, 0.1], 0.3)),
            ],
            light: [0.0, 2.6, 0.0],
            holes: vec![([-3.0, 1.5, -0.8], 0.45), ([1.5, 0.0, 1.2], 0.4), ([0.0, 1.6, 2.5], 0.5), ([-2.2, 0.9, 1.2], 0.3)],
        }
    }

    pub fn sdf(&self, p: V3) -> (f32, usize) {
        let mut best = (f32::INFINITY, 0);
        for (i, (prim, _)) in self.prims.iter().enumerate() {
            let d = prim.sdf(p);
            if d < best.0 { best = (d, i); }
        }
        best
    }

    pub fn normal(&self, p: V3) -> V3 {
        let e = 1e-4;
        let f = |q: V3| self.sdf(q).0;
        normalize([
            f([p[0] + e, p[1], p[2]]) - f([p[0] - e, p[1], p[2]]),
            f([p[0], p[1] + e, p[2]]) - f([p[0], p[1] - e, p[2]]),
            f([p[0], p[1], p[2] + e]) - f([p[0], p[1], p[2] - e]),
        ])
    }

    /// Sphere-traced ray hit: (distance, primitive id).
    pub fn raycast(&self, o: V3, d: V3, tmax: f32) -> Option<(f32, usize)> {
        let mut t = 1e-3;
        for _ in 0..256 {
            let (s, id) = self.sdf(add(o, scale(d, t)));
            if s.abs() < 2e-5 * (1.0 + t) { return Some((t, id)); }
            t += s.max(1e-5);
            if t > tmax { return None; }
        }
        None
    }

    /// Lit linear colour seen at surface point p with normal n (camera-captured look).
    pub fn shade(&self, p: V3, n: V3, id: usize) -> [f32; 3] {
        let a = self.prims[id].1.albedo(p, n);
        let l = sub(self.light, p);
        let dl = length(l);
        let ld = scale(l, 1.0 / dl);
        let mut lambert = dot(n, ld).max(0.0);
        if lambert > 0.0 {
            if let Some((t, _)) = self.raycast(add(p, scale(n, 2e-3)), ld, dl) { if t < dl - 0.05 { lambert *= 0.25; } }
        }
        let e = 0.35 + 1.6 * lambert / (1.0 + 0.15 * dl * dl);
        [a[0] * e, a[1] * e, a[2] * e]
    }

    pub fn in_hole(&self, p: V3) -> bool { self.holes.iter().any(|(c, r)| dist2(*c, p) < r * r) }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ScanOptions {
    pub seed: u64,
    /// Depth samples per camera = depth_w x depth_h.
    pub depth_w: u32,
    pub depth_h: u32,
    /// RGB capture image size.
    pub image_w: u32,
    pub image_h: u32,
    pub hfov_deg: f32,
    pub max_depth: f32,
    /// Depth noise sigma = noise_a + noise_b * d^2 (metres).
    pub noise_a: f32,
    pub noise_b: f32,
    /// Quantise to the /dense 10 mm cell grid (one sample per cell).
    pub quantize_10mm: bool,
    /// Rover stops and yaw steps per stop.
    pub yaw_steps: usize,
}
impl Default for ScanOptions {
    fn default() -> Self {
        ScanOptions { seed: 7, depth_w: 192, depth_h: 144, image_w: 640, image_h: 480, hfov_deg: 68.0, max_depth: 5.0,
            noise_a: 0.002, noise_b: 0.0015, quantize_10mm: true, yaw_steps: 8 }
    }
}

/// Rover-height (0.45 m phone mount) camera path: three stops, full yaw sweep, slight tilt.
pub fn rover_path(opts: &ScanOptions) -> Vec<Camera> {
    let stops = [[-0.8f32, 0.45, 0.6], [1.2, 0.45, 0.9], [-0.5, 0.45, -1.6]];
    let mut cams = Vec::new();
    for (s, eye) in stops.iter().enumerate() {
        for k in 0..opts.yaw_steps {
            let yaw = (k as f32 + 0.37 * s as f32) / opts.yaw_steps as f32 * std::f32::consts::TAU;
            let pitch: f32 = if k % 2 == 0 { 0.18 } else { -0.05 };
            let dir = [yaw.cos() * pitch.cos(), pitch.sin(), yaw.sin() * pitch.cos()];
            cams.push(Camera::look_at(*eye, add(*eye, dir), opts.image_w, opts.image_h, opts.hfov_deg));
        }
    }
    cams
}

pub struct Scan {
    pub cloud: PointCloud,
    pub capture: Capture,
    /// Noise-free, hole-free samples of every surface visible from the path
    /// (the completeness reference).
    pub truth: Vec<V3>,
}

fn rescale(cam: &Camera, w: u32, h: u32) -> Camera {
    let (sx, sy) = (w as f32 / cam.width as f32, h as f32 / cam.height as f32);
    Camera { width: w, height: h, fx: cam.fx * sx, fy: cam.fy * sy, cx: cam.cx * sx, cy: cam.cy * sy, c2w: cam.c2w }
}

/// Simulate the scan. Deterministic for a given seed.
pub fn simulate(scene: &Scene, opts: &ScanOptions) -> Scan {
    let cams = rover_path(opts);
    let per_cam: Vec<(Vec<V3>, Vec<[u8; 3]>, Vec<V3>, Image)> = cams.par_iter().enumerate().map(|(ci, cam)| {
        let mut rng = Rng::new(opts.seed ^ (ci as u64).wrapping_mul(0x9e3779b97f4a7c15));
        let dc = rescale(cam, opts.depth_w, opts.depth_h);
        let (mut pos, mut col, mut truth) = (Vec::new(), Vec::new(), Vec::new());
        for v in 0..dc.height { for u in 0..dc.width {
            let (fu, fv) = (u as f32 + rng.f32(), v as f32 + rng.f32());
            let d = dc.ray(fu, fv);
            let Some((t, id)) = scene.raycast(dc.position(), d, opts.max_depth) else { continue };
            let p = add(dc.position(), scale(d, t));
            truth.push(p);
            if scene.in_hole(p) { continue; }
            let n = scene.normal(p);
            let graze = -dot(n, d);
            if graze < 0.12 { continue; } // grazing dropout like real ToF/LiDAR
            let sigma = opts.noise_a + opts.noise_b * t * t;
            let tn = t + rng.normal() * sigma / graze.max(0.3);
            let mut c = scene.shade(p, n, id);
            for k in 0..3 { c[k] *= 1.0 + 0.04 * rng.normal(); }
            pos.push(add(dc.position(), scale(d, tn)));
            col.push([linear_to_srgb(c[0]), linear_to_srgb(c[1]), linear_to_srgb(c[2])]);
        }}
        let mut img = Image::new(cam.width, cam.height);
        for v in 0..cam.height { for u in 0..cam.width {
            let d = cam.ray(u as f32 + 0.5, v as f32 + 0.5);
            let c = match scene.raycast(cam.position(), d, 20.0) {
                Some((t, id)) => { let p = add(cam.position(), scale(d, t)); scene.shade(p, scene.normal(p), id) }
                None => [0.0; 3],
            };
            img.put(u, v, [linear_to_srgb(c[0]), linear_to_srgb(c[1]), linear_to_srgb(c[2])]);
        }}
        (pos, col, truth, img)
    }).collect();

    let mut cloud = PointCloud::default();
    let mut truth = Vec::new();
    let mut capture = Capture { cameras: cams.clone(), images: Vec::new() };
    for (p, c, t, img) in per_cam { cloud.pos.extend(p); cloud.col.extend(c); truth.extend(t); capture.images.push(img); }
    if opts.quantize_10mm {
        let mut seen = rustc_hash::FxHashSet::default();
        let mut keep = vec![false; cloud.len()];
        for (i, p) in cloud.pos.iter_mut().enumerate() {
            let mm = [(p[0] * 1000.0).round() as i32, (p[1] * 1000.0).round() as i32, (p[2] * 1000.0).round() as i32];
            *p = [mm[0] as f32 / 1000.0, mm[1] as f32 / 1000.0, mm[2] as f32 / 1000.0];
            keep[i] = seen.insert((mm[0].div_euclid(10), mm[1].div_euclid(10), mm[2].div_euclid(10)));
        }
        cloud.retain_mask(&keep);
    }
    Scan { cloud, capture, truth }
}

/// Write a capture in the jetson run_capture.py layout (transforms.json + images/NNNNNN.jpg).
pub fn write_capture(dir: &Path, capture: &Capture) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir.join("images"))?;
    let mut frames = Vec::new();
    for (i, (cam, img)) in capture.cameras.iter().zip(&capture.images).enumerate() {
        let rel = format!("images/{i:06}.jpg");
        image::RgbImage::from_raw(img.width, img.height, img.rgb.clone())
            .ok_or_else(|| anyhow::anyhow!("bad image"))?
            .save_with_format(dir.join(&rel), image::ImageFormat::Jpeg)?;
        frames.push(serde_json::json!({
            "file_path": rel, "fl_x": cam.fx, "fl_y": cam.fy, "cx": cam.cx, "cy": cam.cy,
            "w": cam.width, "h": cam.height, "transform_matrix": cam.c2w, "frame_id": i, "tracking": "normal",
        }));
    }
    let doc = serde_json::json!({ "camera_model": "OPENCV", "k1": 0.0, "k2": 0.0, "p1": 0.0, "p2": 0.0, "frames": frames });
    std::fs::write(dir.join("transforms.json"), serde_json::to_vec_pretty(&doc)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn small_scan_has_holes_and_truth() {
        let scene = Scene::room();
        let opts = ScanOptions { depth_w: 48, depth_h: 36, image_w: 64, image_h: 48, yaw_steps: 4, ..Default::default() };
        let scan = simulate(&scene, &opts);
        assert!(scan.cloud.len() > 1000);
        assert!(scan.truth.len() > scan.cloud.len());
        assert!(scan.cloud.pos.iter().all(|p| !scene.in_hole(*p) || scene.holes.iter().any(|(c, r)| length(sub(*p, *c)) > r - 0.05)));
        assert_eq!(scan.capture.images.len(), scan.capture.cameras.len());
    }
}
