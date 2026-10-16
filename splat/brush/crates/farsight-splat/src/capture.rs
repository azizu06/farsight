//! Nerfstudio `transforms.json` I/O for farsight rover captures.
//!
//! A capture dir looks like:
//!   <dir>/transforms.json   scene camera + frames[] (+ optional ply_file_path)
//!   <dir>/images/*.jpg      phone photos
//!   <dir>/init.ply          optional LiDAR-dot seed
//!   <dir>/points.bin        optional jetson map dots (u32 count + 16B records)
//!   <dir>/dense.xyzrgb      optional dense dots (16B records, no header)
//!
//! Frame-level extras (`t_capture`, `tracking`, distortion coeffs, ...) and
//! scene-level extras round-trip through `extra` maps so nothing the run
//! saver wrote is lost on rewrite.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Frame {
    pub file_path: String,
    /// 4x4 camera-to-world, OpenGL convention (rows).
    pub transform_matrix: Vec<Vec<f64>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub t_capture: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tracking: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Transforms {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub camera_angle_x: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub camera_angle_y: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fl_x: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fl_y: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cx: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cy: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub w: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub h: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub camera_model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ply_file_path: Option<String>,
    #[serde(default)]
    pub frames: Vec<Frame>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

/// A capture directory: the parsed transforms plus the dir it lives in.
pub struct Capture {
    pub dir: PathBuf,
    pub transforms: Transforms,
}

impl Frame {
    /// Camera position (translation column of c2w).
    pub fn pos(&self) -> [f64; 3] {
        [
            self.transform_matrix[0][3],
            self.transform_matrix[1][3],
            self.transform_matrix[2][3],
        ]
    }

    /// Capture timestamp, defaulting to a 2.5 fps index like find_drives.py.
    pub fn t(&self, index: usize) -> f64 {
        self.t_capture.unwrap_or(index as f64 / 2.5)
    }

    /// Rotation block of the c2w matrix, row-major 3x3.
    pub fn rot(&self) -> [[f64; 3]; 3] {
        let m = &self.transform_matrix;
        [
            [m[0][0], m[0][1], m[0][2]],
            [m[1][0], m[1][1], m[1][2]],
            [m[2][0], m[2][1], m[2][2]],
        ]
    }
}

impl Capture {
    pub fn load(dir: &Path) -> Result<Self> {
        let path = find_transforms(dir)?;
        let text = std::fs::read_to_string(&path)
            .with_context(|| format!("reading {}", path.display()))?;
        let transforms: Transforms = serde_json::from_str(&text)
            .with_context(|| format!("parsing {}", path.display()))?;
        Ok(Self {
            dir: dir.to_path_buf(),
            transforms,
        })
    }

    pub fn save(&self, dir: &Path) -> Result<()> {
        std::fs::create_dir_all(dir)?;
        let text = serde_json::to_string_pretty(&self.transforms)?;
        std::fs::write(dir.join("transforms.json"), text)?;
        Ok(())
    }

    /// Absolute path of a frame's image on disk (may not exist).
    pub fn image_path(&self, frame: &Frame) -> PathBuf {
        self.dir.join(&frame.file_path)
    }

    /// Copy the capture to `out` keeping only `keep` frames (stable order),
    /// hardlinking images where possible. Returns the number of images copied.
    pub fn write_subset(&self, out: &Path, keep: &[Frame]) -> Result<usize> {
        let mut copied = 0usize;
        for f in keep {
            let src = self.image_path(f);
            let dst = out.join(&f.file_path);
            if let Some(parent) = dst.parent() {
                std::fs::create_dir_all(parent)?;
            }
            if !dst.exists() {
                match std::fs::hard_link(&src, &dst) {
                    Ok(()) => {}
                    // Cross-device or unsupported filesystems: plain copy.
                    Err(_) => {
                        std::fs::copy(&src, &dst)?;
                    }
                }
            }
            copied += 1;
        }
        let mut t = self.transforms.clone();
        t.frames = keep.to_vec();
        let text = serde_json::to_string_pretty(&t)?;
        std::fs::write(out.join("transforms.json"), text)?;
        // Aux dot-cloud files ride along (T5's farsight-seed and `--seed raw`
        // both read them from the prepared dataset dir).
        for aux in [
            "init.ply",
            "points.bin",
            "dense.xyzrgb",
            "sparse.ply",
            "depth.bin",
        ] {
            let src = self.dir.join(aux);
            let dst = out.join(aux);
            if src.exists() && !dst.exists() {
                let _ = std::fs::hard_link(&src, &dst).or_else(|_| std::fs::copy(&src, &dst).map(|_| ()));
            }
        }
        Ok(copied)
    }
}

/// Locate the transforms file the same way the Brush loader does: the only
/// json, else anything ending `transforms.json`, else `transforms_train.json`.
pub fn find_transforms(dir: &Path) -> Result<PathBuf> {
    let jsons: Vec<PathBuf> = std::fs::read_dir(dir)
        .with_context(|| format!("reading dir {}", dir.display()))?
        .filter_map(|e| {
            let p = e.ok()?.path();
            (p.extension().is_some_and(|x| x == "json")).then_some(p)
        })
        .collect();
    if jsons.len() == 1 {
        return Ok(jsons[0].clone());
    }
    for p in &jsons {
        if p.file_name().is_some_and(|n| n == "transforms.json") {
            return Ok(p.clone());
        }
    }
    for p in &jsons {
        if p
            .file_name()
            .is_some_and(|n| n.to_string_lossy().ends_with("transforms_train.json"))
        {
            return Ok(p.clone());
        }
    }
    bail!("no transforms.json under {}", dir.display())
}
