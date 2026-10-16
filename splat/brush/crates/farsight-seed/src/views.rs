//! Capture-side inputs: pinhole cameras from a nerfstudio transforms.json,
//! the init.ply dot cloud, and the source photos.

use anyhow::{Context, Result};
use glam::Vec3;
use serde::Deserialize;
use std::path::{Path, PathBuf};

/// Pinhole camera, nerfstudio/ARKit convention: camera +X right, +Y up,
/// looks down -Z. `c2w` is row-major camera-to-world (metres).
#[derive(Clone, Debug)]
pub struct SeedCamera {
    pub width: u32,
    pub height: u32,
    pub fx: f32,
    pub fy: f32,
    pub cx: f32,
    pub cy: f32,
    pub c2w: [[f32; 4]; 4],
}

impl SeedCamera {
    pub fn position(&self) -> Vec3 {
        Vec3::new(self.c2w[0][3], self.c2w[1][3], self.c2w[2][3])
    }
    /// World point -> (pixel u, pixel v, depth along -Z). None behind camera.
    /// (0,0) = top-left image corner.
    #[inline]
    pub fn project(&self, p: Vec3) -> Option<(f32, f32, f32)> {
        let m = &self.c2w;
        let d = p - self.position();
        // inverse rotation = transpose of the rotation part
        let x = m[0][0] * d.x + m[1][0] * d.y + m[2][0] * d.z;
        let y = m[0][1] * d.x + m[1][1] * d.y + m[2][1] * d.z;
        let z = m[0][2] * d.x + m[1][2] * d.y + m[2][2] * d.z;
        let depth = -z;
        if depth <= 1e-4 {
            return None;
        }
        Some((self.cx + self.fx * x / depth, self.cy - self.fy * y / depth, depth))
    }
    /// World-space unit ray through pixel (u, v).
    #[inline]
    pub fn ray(&self, u: f32, v: f32) -> Vec3 {
        let m = &self.c2w;
        let x = (u - self.cx) / self.fx;
        let y = -(v - self.cy) / self.fy;
        Vec3::new(
            m[0][0] * x + m[0][1] * y - m[0][2],
            m[1][0] * x + m[1][1] * y - m[1][2],
            m[2][0] * x + m[2][1] * y - m[2][2],
        )
        .normalize_or_zero()
    }
}

/// A posed photo. `image` is decoded lazily by the CLI; the seed pass only
/// needs it when colour sampling.
#[derive(Clone)]
pub struct SeedView {
    pub camera: SeedCamera,
    /// Original frame index in transforms.json (for depth-file naming).
    pub frame_index: usize,
    /// Path of the frame relative to the capture dir.
    pub file_path: String,
    pub image: Option<image::RgbImage>,
}

#[derive(Deserialize)]
struct JsonScene {
    camera_angle_x: Option<f64>,
    camera_angle_y: Option<f64>,
    fl_x: Option<f64>,
    fl_y: Option<f64>,
    cx: Option<f64>,
    cy: Option<f64>,
    w: Option<f64>,
    h: Option<f64>,
    ply_file_path: Option<String>,
    frames: Vec<JsonFrame>,
}

#[derive(Deserialize)]
struct JsonFrame {
    fl_x: Option<f64>,
    fl_y: Option<f64>,
    camera_angle_x: Option<f64>,
    camera_angle_y: Option<f64>,
    cx: Option<f64>,
    cy: Option<f64>,
    w: Option<f64>,
    h: Option<f64>,
    transform_matrix: Vec<Vec<f32>>,
    file_path: String,
}

pub struct NerfCapture {
    pub views: Vec<SeedView>,
    /// ply_file_path from transforms.json (relative), if set.
    pub ply_file: Option<String>,
    /// The parsed transforms doc (for patched re-emit by the CLI).
    pub raw: serde_json::Value,
}

/// Read a nerfstudio capture dir (transforms.json). Images are NOT decoded
/// here — call `load_images` with the views you actually need.
pub fn read_capture(dir: &Path) -> Result<NerfCapture> {
    let tf_path = dir.join("transforms.json");
    let text = std::fs::read_to_string(&tf_path)
        .with_context(|| format!("reading {}", tf_path.display()))?;
    let raw: serde_json::Value = serde_json::from_str(&text).context("transforms.json parse")?;
    let scene: JsonScene = serde_json::from_value(raw.clone()).context("transforms.json schema")?;
    let mut views = Vec::with_capacity(scene.frames.len());
    for (i, f) in scene.frames.iter().enumerate() {
        let flat: Vec<f32> = f.transform_matrix.iter().flatten().copied().collect();
        if flat.len() != 16 {
            anyhow::bail!("frame {} transform_matrix has {} elements", f.file_path, flat.len());
        }
        // transforms.json stores row-major c2w
        let c2w: [[f32; 4]; 4] = [
            [flat[0], flat[1], flat[2], flat[3]],
            [flat[4], flat[5], flat[6], flat[7]],
            [flat[8], flat[9], flat[10], flat[11]],
            [flat[12], flat[13], flat[14], flat[15]],
        ];
        let w = f.w.or(scene.w).unwrap_or(0.0) as u32;
        let h = f.h.or(scene.h).unwrap_or(0.0) as u32;
        let fx = f
            .fl_x
            .or(scene.fl_x)
            .or_else(|| {
                f.camera_angle_x
                    .or(scene.camera_angle_x)
                    .map(|a| 0.5 * w as f64 / (0.5 * a).tan())
            })
            .context("frame has no focal length")? as f32;
        let fy = f
            .fl_y
            .or(scene.fl_y)
            .or_else(|| {
                f.camera_angle_y
                    .or(scene.camera_angle_y)
                    .map(|a| 0.5 * h as f64 / (0.5 * a).tan())
            })
            .unwrap_or(fx as f64) as f32;
        let cx = f.cx.or(scene.cx).unwrap_or(w as f64 * 0.5) as f32;
        let cy = f.cy.or(scene.cy).unwrap_or(h as f64 * 0.5) as f32;
        views.push(SeedView {
            camera: SeedCamera { width: w, height: h, fx, fy, cx, cy, c2w },
            frame_index: i,
            file_path: f.file_path.clone(),
            image: None,
        });
    }
    Ok(NerfCapture {
        views,
        ply_file: scene.ply_file_path,
        raw,
    })
}

/// Decode photos for `views[want]` in place (skips missing files, warns).
pub fn load_images(dir: &Path, views: &mut [SeedView], want: &[usize]) -> usize {
    let mut loaded = 0;
    for &i in want {
        let Some(v) = views.get_mut(i) else { continue };
        let p = dir.join(&v.file_path);
        match image::open(&p) {
            Ok(img) => {
                let rgb = img.to_rgb8();
                // trust the actual decode size if the header was wrong/missing
                if v.camera.width == 0 {
                    v.camera.width = rgb.width();
                    v.camera.height = rgb.height();
                }
                v.image = Some(rgb);
                loaded += 1;
            }
            Err(e) => eprintln!("farsight-seed: skipping {p:?}: {e}"),
        }
    }
    loaded
}

/// Read a binary-little-endian PLY vertex list: x,y,z float + optional rgb
/// uchar (+ gaussian fields, ignored). Returns (positions, Option<colours>).
pub fn read_dot_ply(path: &Path) -> Result<(Vec<Vec3>, Option<Vec<[u8; 3]>>)> {
    let bytes = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let end = bytes
        .windows(b"end_header\n".len())
        .position(|w| w == b"end_header\n")
        .context("no end_header")?
        + b"end_header\n".len();
    let head = std::str::from_utf8(&bytes[..end])?;
    let n: usize = head
        .lines()
        .find_map(|l| {
            l.strip_prefix("element vertex ")
                .and_then(|v| v.split_whitespace().next().and_then(|s| s.parse().ok()))
        })
        .context("no vertex count")?;
    let props: Vec<&str> = head
        .lines()
        .filter_map(|l| l.strip_prefix("property ").map(|r| r.split_whitespace().last().unwrap_or("")))
        .collect();
    // property types: we only support float (4B) and uchar (1B) rows.
    let mut offset = vec![0usize; props.len()];
    let mut stride = 0usize;
    let mut type_of: Vec<u8> = Vec::with_capacity(props.len()); // 0=f32, 1=u8
    for (i, l) in head.lines().filter(|l| l.starts_with("property ")).enumerate() {
        let t = l.split_whitespace().nth(1).unwrap_or("float");
        let (sz, ty) = match t {
            "uchar" | "uint8" | "char" => (1usize, 1u8),
            "ushort" | "uint16" => (2, 2),
            "float" | "float32" => (4, 0),
            "double" | "float64" => (8, 3),
            "int" | "uint" | "int32" | "uint32" => (4, 4),
            _ => (4, 0),
        };
        offset[i] = stride;
        stride += sz;
        type_of.push(ty);
    }
    let idx = |name: &str| props.iter().position(|p| *p == name);
    let (ix, iy, iz) = (
        idx("x").context("ply has no x")?,
        idx("y").context("ply has no y")?,
        idx("z").context("ply has no z")?,
    );
    let (ir, ig, ib) = (
        idx("red").or_else(|| idx("r")),
        idx("green").or_else(|| idx("g")),
        idx("blue").or_else(|| idx("b")),
    );
    let f32at = |base: usize, pi: usize| -> f32 {
        let o = base + offset[pi];
        match type_of[pi] {
            1 => bytes[o] as f32,
            2 => u16::from_le_bytes(bytes[o..o + 2].try_into().unwrap()) as f32,
            3 => f64::from_le_bytes(bytes[o..o + 8].try_into().unwrap()) as f32,
            4 => i32::from_le_bytes(bytes[o..o + 4].try_into().unwrap()) as f32,
            _ => f32::from_le_bytes(bytes[o..o + 4].try_into().unwrap()),
        }
    };
    let u8at = |base: usize, pi: usize| -> u8 {
        let o = base + offset[pi];
        match type_of[pi] {
            0 => (f32::from_le_bytes(bytes[o..o + 4].try_into().unwrap()) * 255.0)
                .clamp(0.0, 255.0) as u8,
            _ => bytes[o],
        }
    };
    let mut pos = Vec::with_capacity(n);
    let has_col = ir.is_some() && ig.is_some() && ib.is_some();
    let mut col = has_col.then(|| Vec::with_capacity(n));
    for i in 0..n {
        let base = end + i * stride;
        if base + stride > bytes.len() {
            break;
        }
        pos.push(Vec3::new(f32at(base, ix), f32at(base, iy), f32at(base, iz)));
        if let Some(c) = &mut col {
            c.push([u8at(base, ir.unwrap()), u8at(base, ig.unwrap()), u8at(base, ib.unwrap())]);
        }
    }
    Ok((pos, col))
}

/// Where the capture's dot cloud lives (init.ply or ply_file_path).
pub fn dots_path(dir: &Path, cap: &NerfCapture) -> PathBuf {
    match &cap.ply_file {
        Some(p) => dir.join(p),
        None => dir.join("init.ply"),
    }
}
