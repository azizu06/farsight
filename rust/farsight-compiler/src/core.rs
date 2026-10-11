//! Shared types for every compiler stage. Owned by the orchestrator: change only
//! by additive fields so parallel stage owners keep compiling.

pub type V3 = [f32; 3];

/// Bit flags on points / vertices.
pub const FLAG_INTERPOLATED: u8 = 1; // synthesized by interpolation, not measured

/// Canonical point cloud. Parallel arrays; optional arrays are either empty or
/// exactly `pos.len()` long. Colours are sRGB bytes as on the farsight wire.
/// World frame: metres, +Y up (ARKit / farsight map convention after ingest).
#[derive(Clone, Debug, Default)]
pub struct PointCloud {
    pub pos: Vec<V3>,
    pub col: Vec<[u8; 3]>,
    /// Unit normals (empty until normals::estimate).
    pub nrm: Vec<V3>,
    /// Per-point confidence/weight (empty = all 1.0).
    pub weight: Vec<f32>,
    /// Local sample spacing / surfel radius in metres (empty until computed).
    pub radius: Vec<f32>,
    /// FLAG_* bits (empty = all measured).
    pub flags: Vec<u8>,
}

impl PointCloud {
    pub fn len(&self) -> usize { self.pos.len() }
    pub fn is_empty(&self) -> bool { self.pos.is_empty() }
    pub fn has_normals(&self) -> bool { !self.nrm.is_empty() && self.nrm.len() == self.pos.len() }
    pub fn bounds(&self) -> Aabb { Aabb::from_points(&self.pos) }
    pub fn weight(&self, i: usize) -> f32 { self.weight.get(i).copied().unwrap_or(1.0) }
    pub fn flag(&self, i: usize) -> u8 { self.flags.get(i).copied().unwrap_or(0) }
    /// Panics if optional arrays have inconsistent lengths. Call after every stage.
    pub fn validate(&self) {
        let n = self.pos.len();
        assert!(self.col.is_empty() || self.col.len() == n, "col len");
        assert!(self.nrm.is_empty() || self.nrm.len() == n, "nrm len");
        assert!(self.weight.is_empty() || self.weight.len() == n, "weight len");
        assert!(self.radius.is_empty() || self.radius.len() == n, "radius len");
        assert!(self.flags.is_empty() || self.flags.len() == n, "flags len");
    }
    /// Keep the points where `keep[i]`, compacting all parallel arrays.
    pub fn retain_mask(&mut self, keep: &[bool]) {
        fn r<T: Copy>(v: &mut Vec<T>, keep: &[bool]) {
            if v.is_empty() { return; }
            let mut j = 0;
            for i in 0..v.len() { if keep[i] { v[j] = v[i]; j += 1; } }
            v.truncate(j);
        }
        r(&mut self.pos, keep); r(&mut self.col, keep); r(&mut self.nrm, keep);
        r(&mut self.weight, keep); r(&mut self.radius, keep); r(&mut self.flags, keep);
    }
    pub fn interpolated_count(&self) -> usize {
        self.flags.iter().filter(|f| **f & FLAG_INTERPOLATED != 0).count()
    }
}

/// Indexed triangle mesh. Optional arrays empty or `pos.len()` long.
#[derive(Clone, Debug, Default)]
pub struct Mesh {
    pub pos: Vec<V3>,
    pub nrm: Vec<V3>,
    /// sRGB vertex colours.
    pub col: Vec<[u8; 3]>,
    /// Texture coordinates into `Mesh::texture` (empty = untextured).
    pub uv: Vec<[f32; 2]>,
    /// Per-vertex distance (m) to the nearest *measured* sample: 0 = measured
    /// support, large = interpolated/hallucinated fill. Empty = unknown.
    pub support: Vec<f32>,
    pub idx: Vec<u32>,
    pub texture: Option<Image>,
}

impl Mesh {
    pub fn triangles(&self) -> usize { self.idx.len() / 3 }
    pub fn validate(&self) {
        let n = self.pos.len();
        assert!(self.idx.len() % 3 == 0, "idx not triangles");
        assert!(self.idx.iter().all(|&i| (i as usize) < n), "idx out of range");
        assert!(self.nrm.is_empty() || self.nrm.len() == n, "nrm len");
        assert!(self.col.is_empty() || self.col.len() == n, "col len");
        assert!(self.uv.is_empty() || self.uv.len() == n, "uv len");
        assert!(self.support.is_empty() || self.support.len() == n, "support len");
    }
}

/// 8-bit RGB image, row-major, top row first.
#[derive(Clone, Debug, Default)]
pub struct Image {
    pub width: u32,
    pub height: u32,
    pub rgb: Vec<u8>,
}

impl Image {
    pub fn new(width: u32, height: u32) -> Self {
        Image { width, height, rgb: vec![0; (width * height * 3) as usize] }
    }
    #[inline]
    pub fn get(&self, x: u32, y: u32) -> [u8; 3] {
        let o = ((y * self.width + x) * 3) as usize;
        [self.rgb[o], self.rgb[o + 1], self.rgb[o + 2]]
    }
    #[inline]
    pub fn put(&mut self, x: u32, y: u32, c: [u8; 3]) {
        let o = ((y * self.width + x) * 3) as usize;
        self.rgb[o..o + 3].copy_from_slice(&c);
    }
    /// Bilinear sample at pixel-centre coordinates (u,v in pixels), sRGB space.
    pub fn sample(&self, u: f32, v: f32) -> [f32; 3] {
        let x = (u - 0.5).clamp(0.0, self.width as f32 - 1.0);
        let y = (v - 0.5).clamp(0.0, self.height as f32 - 1.0);
        let (x0, y0) = (x.floor() as u32, y.floor() as u32);
        let (x1, y1) = ((x0 + 1).min(self.width - 1), (y0 + 1).min(self.height - 1));
        let (fx, fy) = (x - x0 as f32, y - y0 as f32);
        let mut out = [0.0; 3];
        let (a, b, c, d) = (self.get(x0, y0), self.get(x1, y0), self.get(x0, y1), self.get(x1, y1));
        for k in 0..3 {
            let top = a[k] as f32 * (1.0 - fx) + b[k] as f32 * fx;
            let bot = c[k] as f32 * (1.0 - fx) + d[k] as f32 * fx;
            out[k] = top * (1.0 - fy) + bot * fy;
        }
        out
    }
}

/// Pinhole camera, nerfstudio/ARKit convention: camera +X right, +Y up, looks
/// down -Z. `c2w` is row-major camera-to-world (metres).
#[derive(Clone, Debug)]
pub struct Camera {
    pub width: u32,
    pub height: u32,
    pub fx: f32,
    pub fy: f32,
    pub cx: f32,
    pub cy: f32,
    pub c2w: [[f32; 4]; 4],
}

impl Camera {
    pub fn position(&self) -> V3 { [self.c2w[0][3], self.c2w[1][3], self.c2w[2][3]] }
    /// World point -> (pixel u, pixel v, depth along -Z). None if behind camera.
    /// Pixel coordinates: u right, v down, (0,0) = top-left corner of the image.
    pub fn project(&self, p: V3) -> Option<(f32, f32, f32)> {
        let m = &self.c2w;
        let d = sub(p, self.position());
        // inverse rotation = transpose
        let x = m[0][0] * d[0] + m[1][0] * d[1] + m[2][0] * d[2];
        let y = m[0][1] * d[0] + m[1][1] * d[1] + m[2][1] * d[2];
        let z = m[0][2] * d[0] + m[1][2] * d[1] + m[2][2] * d[2];
        let depth = -z;
        if depth <= 1e-4 { return None; }
        Some((self.cx + self.fx * x / depth, self.cy - self.fy * y / depth, depth))
    }
    /// World-space unit ray direction through pixel (u, v).
    pub fn ray(&self, u: f32, v: f32) -> V3 {
        let x = (u - self.cx) / self.fx;
        let y = -(v - self.cy) / self.fy;
        let m = &self.c2w;
        let d = [
            m[0][0] * x + m[0][1] * y - m[0][2],
            m[1][0] * x + m[1][1] * y - m[1][2],
            m[2][0] * x + m[2][1] * y - m[2][2],
        ];
        normalize(d)
    }
    /// Camera looking from `eye` at `target` with world +Y up.
    pub fn look_at(eye: V3, target: V3, width: u32, height: u32, hfov_deg: f32) -> Self {
        let f = normalize(sub(target, eye));
        let mut r = cross(f, [0.0, 1.0, 0.0]);
        if dot(r, r) < 1e-8 { r = [1.0, 0.0, 0.0]; }
        let r = normalize(r);
        let u = cross(r, f);
        let fx = 0.5 * width as f32 / (0.5 * hfov_deg.to_radians()).tan();
        Camera {
            width, height, fx, fy: fx, cx: width as f32 * 0.5, cy: height as f32 * 0.5,
            c2w: [
                [r[0], u[0], -f[0], eye[0]],
                [r[1], u[1], -f[1], eye[1]],
                [r[2], u[2], -f[2], eye[2]],
                [0.0, 0.0, 0.0, 1.0],
            ],
        }
    }
}

/// Posed image set (nerfstudio transforms.json + images/). Images are loaded
/// eagerly by io::load_capture; `images[i]` matches `cameras[i]`.
#[derive(Clone, Debug, Default)]
pub struct Capture {
    pub cameras: Vec<Camera>,
    pub images: Vec<Image>,
}

#[derive(Clone, Copy, Debug)]
pub struct Aabb {
    pub min: V3,
    pub max: V3,
}

impl Aabb {
    pub fn empty() -> Self { Aabb { min: [f32::INFINITY; 3], max: [f32::NEG_INFINITY; 3] } }
    pub fn from_points(p: &[V3]) -> Self {
        let mut b = Aabb::empty();
        for q in p { b.grow(*q); }
        b
    }
    pub fn grow(&mut self, p: V3) {
        for k in 0..3 { self.min[k] = self.min[k].min(p[k]); self.max[k] = self.max[k].max(p[k]); }
    }
    pub fn is_empty(&self) -> bool { self.min[0] > self.max[0] }
    pub fn size(&self) -> V3 { sub(self.max, self.min) }
    pub fn center(&self) -> V3 { scale(add(self.min, self.max), 0.5) }
    pub fn diagonal(&self) -> f32 { if self.is_empty() { 0.0 } else { length(self.size()) } }
}

/// Small deterministic PRNG (PCG32) so stages and tests stay reproducible without a dep.
#[derive(Clone, Debug)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self { let mut r = Rng(seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407)); r.next_u32(); r }
    pub fn next_u32(&mut self) -> u32 {
        let old = self.0;
        self.0 = old.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let xorshifted = (((old >> 18) ^ old) >> 27) as u32;
        let rot = (old >> 59) as u32;
        xorshifted.rotate_right(rot)
    }
    /// Uniform in [0, 1).
    pub fn f32(&mut self) -> f32 { (self.next_u32() >> 8) as f32 * (1.0 / 16_777_216.0) }
    pub fn range(&mut self, lo: f32, hi: f32) -> f32 { lo + (hi - lo) * self.f32() }
    /// Standard normal (Box-Muller).
    pub fn normal(&mut self) -> f32 {
        let u1 = self.f32().max(1e-7);
        let u2 = self.f32();
        (-2.0 * u1.ln()).sqrt() * (std::f32::consts::TAU * u2).cos()
    }
}

#[inline] pub fn add(a: V3, b: V3) -> V3 { [a[0] + b[0], a[1] + b[1], a[2] + b[2]] }
#[inline] pub fn sub(a: V3, b: V3) -> V3 { [a[0] - b[0], a[1] - b[1], a[2] - b[2]] }
#[inline] pub fn scale(a: V3, s: f32) -> V3 { [a[0] * s, a[1] * s, a[2] * s] }
#[inline] pub fn dot(a: V3, b: V3) -> f32 { a[0] * b[0] + a[1] * b[1] + a[2] * b[2] }
#[inline] pub fn cross(a: V3, b: V3) -> V3 { [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]] }
#[inline] pub fn length(a: V3) -> f32 { dot(a, a).sqrt() }
#[inline] pub fn dist2(a: V3, b: V3) -> f32 { let d = sub(a, b); dot(d, d) }
#[inline] pub fn normalize(a: V3) -> V3 { let l = length(a); if l > 0.0 { scale(a, 1.0 / l) } else { a } }
#[inline] pub fn lerp(a: V3, b: V3, t: f32) -> V3 { add(a, scale(sub(b, a), t)) }

/// sRGB byte -> linear [0,1].
#[inline] pub fn srgb_to_linear(c: u8) -> f32 { let v = c as f32 / 255.0; if v <= 0.04045 { v / 12.92 } else { ((v + 0.055) / 1.055).powf(2.4) } }
/// Linear [0,1] -> sRGB byte.
#[inline] pub fn linear_to_srgb(v: f32) -> u8 { let v = v.clamp(0.0, 1.0); let s = if v <= 0.0031308 { v * 12.92 } else { 1.055 * v.powf(1.0 / 2.4) - 0.055 }; (s * 255.0 + 0.5) as u8 }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn camera_roundtrip() {
        let cam = Camera::look_at([1.0, 1.5, 2.0], [0.0, 1.0, 0.0], 320, 240, 70.0);
        let p = [0.3, 1.2, -0.4];
        let (u, v, d) = cam.project(p).unwrap();
        assert!(d > 0.0);
        let r = cam.ray(u, v);
        let back = add(cam.position(), scale(r, length(sub(p, cam.position()))));
        assert!(dist2(back, p) < 1e-6, "{back:?} vs {p:?}");
    }
    #[test]
    fn srgb_roundtrip() { for c in 0..=255u8 { assert_eq!(linear_to_srgb(srgb_to_linear(c)), c); } }
}
