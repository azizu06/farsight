//! Capture-guided colour and visibility carving. Renders the measured cloud
//! into each capture camera as a depth buffer, then:
//! * carves fill points that contradict observed free space (a fill in front
//!   of a measured surface along a camera ray would have been seen);
//! * samples fill colours directly from the posed images where visible
//!   (vastly better than neighbour smearing);
//! * falls back to inverse-distance neighbour colour otherwise.

use crate::core::*;
use crate::spatial::Grid;
use rayon::prelude::*;

/// Measured-cloud depth buffer for one camera.
pub struct DepthView {
    pub w: usize,
    pub h: usize,
    /// Minimum measured depth per pixel; 0 = no measured hit.
    pub z: Vec<f32>,
}

impl DepthView {
    /// Nearest measured depth in the 3x3 pixel neighbourhood; 0 if none.
    #[inline]
    fn min_near(&self, u: f32, v: f32) -> f32 {
        let (mut i0, mut i1) = (u.floor() as i32 - 1, u.floor() as i32 + 1);
        let (mut j0, mut j1) = (v.floor() as i32 - 1, v.floor() as i32 + 1);
        i0 = i0.max(0);
        j0 = j0.max(0);
        i1 = i1.min(self.w as i32 - 1);
        j1 = j1.min(self.h as i32 - 1);
        let mut m = f32::MAX;
        for j in j0..=j1 {
            for i in i0..=i1 {
                let z = self.z[(j as usize) * self.w + i as usize];
                if z > 0.0 && z < m {
                    m = z;
                }
            }
        }
        if m == f32::MAX {
            0.0
        } else {
            m
        }
    }
}

/// Render measured points into every camera's depth buffer.
pub fn render_depths(measured_pos: &[V3], capture: &Capture) -> Vec<DepthView> {
    capture
        .cameras
        .par_iter()
        .map(|cam| {
            let (w, h) = (cam.width as usize, cam.height as usize);
            let mut z = vec![0.0f32; w * h];
            for p in measured_pos {
                if let Some((u, v, d)) = cam.project(*p) {
                    if u >= 0.0 && v >= 0.0 && u < cam.width as f32 - 1.0 && v < cam.height as f32 - 1.0 {
                        let i = v as usize * w + u as usize;
                        if z[i] == 0.0 || d < z[i] {
                            z[i] = d;
                        }
                    }
                }
            }
            DepthView { w, h, z }
        })
        .collect()
}

/// Reject fill points that violate observed free space: any camera that sees a
/// measured surface clearly *behind* the candidate proves the candidate floats
/// mid-air. `margin` is the depth tolerance in metres.
pub fn carve(pos: &[V3], capture: &Capture, depths: &[DepthView], margin: f32) -> Vec<bool> {
    pos.par_iter()
        .map(|p| {
            for (cam, dv) in capture.cameras.iter().zip(depths) {
                if let Some((u, v, z)) = cam.project(*p) {
                    if u < 1.0 || v < 1.0 || u >= dv.w as f32 - 1.0 || v >= dv.h as f32 - 1.0 {
                        continue;
                    }
                    let mz = dv.min_near(u, v);
                    if mz > 0.0 && z < mz - margin * (0.5 + 0.5 * z / 5.0) {
                        return false;
                    }
                }
            }
            true
        })
        .collect()
}

/// Sample fill colours; returns the colours plus (from_image, from_idw) counts.
pub fn paint_counted(
    pos: &[V3],
    nrm: &[V3],
    capture: Option<&Capture>,
    depths: &[DepthView],
    margin: f32,
    idw_grid: &Grid,
    idw_col: &[[u8; 3]],
) -> (Vec<[u8; 3]>, usize, usize) {
    let out: Vec<([u8; 3], bool)> = pos
        .par_iter()
        .enumerate()
        .map(|(i, p)| {
            if let Some(cap) = capture {
                let mut acc = [0.0f64; 3];
                let mut aw = 0.0f64;
                for (ci, cam) in cap.cameras.iter().enumerate() {
                    let Some((u, v, z)) = cam.project(*p) else { continue };
                    if u < 1.0 || v < 1.0 || u >= cam.width as f32 - 1.0 || v >= cam.height as f32 - 1.0 {
                        continue;
                    }
                    let mz = depths[ci].min_near(u, v);
                    if mz > 0.0 && z > mz + margin * (0.5 + 0.5 * z / 5.0) {
                        continue;
                    }
                    let ray = cam.ray(u, v);
                    let facing = (-dot(ray, nrm[i])).max(0.15);
                    let w = (facing / (0.5 + z * z)) as f64;
                    let c = cap.images[ci].sample(u, v);
                    for k in 0..3 {
                        acc[k] += c[k] as f64 * w;
                    }
                    aw += w;
                }
                if aw > 0.0 {
                    return (
                        [
                            (acc[0] / aw).round() as u8,
                            (acc[1] / aw).round() as u8,
                            (acc[2] / aw).round() as u8,
                        ],
                        true,
                    );
                }
            }
            let mut knn: Vec<(u32, f32)> = Vec::new();
            idw_grid.knn(*p, 8, 0.2, &mut knn);
            if knn.is_empty() {
                return ([180, 180, 180], false);
            }
            let mut acc = [0.0f64; 3];
            let mut aw = 0.0f64;
            for &(j, d2) in &knn {
                let w = 1.0 / (d2 as f64 + 1e-8);
                let c = idw_col[j as usize];
                for k in 0..3 {
                    acc[k] += c[k] as f64 * w;
                }
                aw += w;
            }
            (
                [
                    (acc[0] / aw).round() as u8,
                    (acc[1] / aw).round() as u8,
                    (acc[2] / aw).round() as u8,
                ],
                false,
            )
        })
        .collect();
    let from_img = out.iter().filter(|o| o.1).count();
    let from_idw = out.len() - from_img;
    (out.into_iter().map(|o| o.0).collect(), from_img, from_idw)
}
