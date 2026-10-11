//! Held-out evaluation on current splats — renders each eval view through
//! `brush_train::eval::eval_stats` (the same code Brush's own eval uses), so
//! numbers are identical whether training finished its schedule or stopped at
//! a wall-clock deadline.

use std::path::Path;

use anyhow::{Context, Result};
use brush_render::camera::Camera;
use brush_render::kernels::camera_model::CameraModel;
use brush_render::gaussian_splats::Splats;
use brush_render::AlphaMode;
use glam::{Mat4, Quat, Vec2, Vec3};

use crate::capture::{Capture, Frame};
use crate::score::ScoreReport;

/// OpenGL/Blender c2w -> brush pose (+X right, +Y down, +Z forward).
/// Mirrors `brush_dataset::formats::opengl_c2w_to_pose` (private there).
pub fn opengl_c2w_to_pose(mut c2w: Mat4) -> (Vec3, Quat) {
    c2w.y_axis *= -1.0;
    c2w.z_axis *= -1.0;
    let (_, rotation, translation) = c2w.to_scale_rotation_translation();
    (translation, rotation)
}

fn fov_to_focal(fov: f64, pixels: u32) -> f64 {
    (pixels as f64) / (2.0 * (fov / 2.0).tan())
}
fn focal_to_fov(focal: f64, pixels: u32) -> f64 {
    2.0 * ((pixels as f64) / (2.0 * focal)).atan()
}

/// Build a brush `Camera` from a capture frame the way the nerfstudio loader
/// does: fl_x/fl_y or camera_angle_x/_y, w/h, optional cx/cy.
pub fn camera_of(cap: &Capture, frame: &Frame) -> Result<Camera> {
    let t = &cap.transforms;
    let f = &frame.extra;
    let num = |k: &str| f.get(k).and_then(|v| v.as_f64());
    let (w, h) = match (num("w").or(t.w), num("h").or(t.h)) {
        (Some(w), Some(h)) => (w as u32, h as u32),
        _ => {
            let img = image::ImageReader::open(cap.image_path(frame))?.decode()?;
            (img.width(), img.height())
        }
    };
    let camx = num("camera_angle_x").or(t.camera_angle_x);
    let camy = num("camera_angle_y").or(t.camera_angle_y);
    let flx = num("fl_x").or(t.fl_x);
    let fly = num("fl_y").or(t.fl_y);
    let model = CameraModel::Pinhole;
    let fovx = camx.or_else(|| flx.map(|f| focal_to_fov(f, w)));
    let fovy = camy.or_else(|| fly.map(|f| focal_to_fov(f, h)));
    let (fx, fy) = match (fovx, fovy) {
        (Some(x), Some(y)) => (x, y),
        (Some(x), None) => (x, focal_to_fov(fov_to_focal(x, w), h)),
        (None, Some(y)) => (focal_to_fov(fov_to_focal(y, h), w), y),
        (None, None) => anyhow::bail!("frame {}: no focal length", frame.file_path),
    };
    let cuv = Vec2::new(
        num("cx").or(t.cx).map_or(0.5, |c| (c / w as f64) as f32),
        num("cy").or(t.cy).map_or(0.5, |c| (c / h as f64) as f32),
    );
    let cols: Vec<f32> = frame
        .transform_matrix
        .iter()
        .flatten()
        .map(|v| *v as f32)
        .collect();
    if cols.len() != 16 {
        anyhow::bail!("frame {}: transform_matrix not 4x4", frame.file_path);
    }
    let m = Mat4::from_cols_slice(&cols).transpose();
    let (pos, rot) = opengl_c2w_to_pose(m);
    Ok(Camera::new(pos, rot, fx, fy, cuv, model))
}

/// Index of the eval views (every `eval_split_every`-th kept frame — the same
/// split brush-dataset applies on load).
pub fn eval_frames<'a>(cap: &'a Capture, split_every: usize) -> Vec<&'a Frame> {
    cap.transforms
        .frames
        .iter()
        .enumerate()
        .filter(|(i, _)| i % split_every == 0)
        .map(|(_, f)| f)
        .collect()
}

/// Render + score eval views from `splats`, writing renders under
/// `<out>/eval_<iter>/` mirroring Brush's layout (`<rel image path>.png`).
/// Returns the ScoreReport plus Brush's avg PSNR/SSIM.
pub async fn eval_current(
    splats: Splats,
    cap: &Capture,
    eval_views: &[&Frame],
    iter: u32,
    out: &Path,
    device: &burn::tensor::Device,
) -> Result<(ScoreReport, f32, f32)> {
    let mut pairs = Vec::new();
    let mut psnr_sum = 0.0f32;
    let mut ssim_sum = 0.0f32;
    let eval_dir = out.join(format!("eval_{iter}"));
    for f in eval_views {
        let img = image::ImageReader::open(cap.image_path(f))
            .context("eval frame open")?
            .decode()
            .context("eval frame decode")?;
        let cam = camera_of(cap, f)?;
        let sample = brush_train::eval::eval_stats(
            splats.clone(),
            &cam,
            img.clone(),
            AlphaMode::Masked,
            device,
        )
        .await?;
        let p = sample.psnr.clone().into_scalar_async::<f32>().await?;
        let s = sample.ssim.clone().into_scalar_async::<f32>().await?;
        psnr_sum += p;
        ssim_sum += s;
        let path = eval_dir.join(&f.file_path).with_extension("png");
        if let Some(par) = path.parent() {
            tokio::fs::create_dir_all(par).await?;
        }
        sample.save_to_disk(&path).await?;
        pairs.push(crate::score::ViewScore {
            view: f.file_path.clone(),
            psnr: p as f64,
        });
    }
    let n = pairs.len().max(1) as f32;
    let mut rep = crate::score::report_of_public(pairs);
    rep.ssim_mean = Some((ssim_sum / n) as f64);
    Ok((rep, psnr_sum / n, ssim_sum / n))
}
