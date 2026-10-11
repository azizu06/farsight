//! Hole filling from the learned per-view depth completion (W1 `learned`).
//! OWNER: W3 (densify).
//!
//! For every completed `DepthMap`, pixels the measured cloud never observed
//! (empty cells of the view z-buffer) are back-projected into world space —
//! but only where the completed depth agrees with the surface evidence around
//! it: smooth local continuation, confidence floor, and never inside space a
//! nearer measured surface already occupies. Points carry the depth-gradient
//! normal, the image colour, and FLAG_INTERPOLATED via the caller's assembly.

use crate::core::*;
use crate::learned::{self, DepthMap};
use crate::spatial::Grid;
use rayon::prelude::*;
use rustc_hash::FxHashSet;
use std::sync::atomic::{AtomicU32, Ordering};

/// Emitted fill plus rejection counters for the stats line.
#[derive(Default)]
pub struct DepthFill {
    pub pos: Vec<V3>,
    pub nrm: Vec<V3>,
    pub col: Vec<[u8; 3]>,
    pub support: Vec<f32>,
    pub weight: Vec<f32>,
    pub duplicate: usize,
}

/// Measured z-buffer for one view: nearest observed depth per pixel
/// (u32::MAX = empty). Mirrors learned::project_view, kept local so no other
/// module's internals are touched.
fn view_zbuf(cam: &Camera, cur: &PointCloud) -> Vec<AtomicU32> {
    let (w, h) = (cam.width as usize, cam.height as usize);
    let z: Vec<AtomicU32> = (0..w * h).map(|_| AtomicU32::new(u32::MAX)).collect();
    cur.pos.par_chunks(4096).for_each(|chunk| {
        for p in chunk {
            let Some((u, v, d)) = cam.project(*p) else {
                continue;
            };
            let (x, y) = (u.floor() as i32, v.floor() as i32);
            if x < 0 || y < 0 || x >= w as i32 || y >= h as i32 {
                continue;
            }
            z[y as usize * w + x as usize].fetch_min(d.to_bits(), Ordering::Relaxed);
        }
    });
    z
}

/// `min_conf` — confidence floor for hole-border pixels; pixels with no
/// measured neighbours at all need `min_conf_deep`. `reach` bounds distance
/// to the nearest measured point for emitted fills.
pub fn fill_from_depth(
    maps: &[DepthMap],
    capture: &crate::core::Capture,
    cur: &PointCloud,
    meas_grid: &Grid,
    occupied: &mut FxHashSet<(i32, i32, i32)>,
    ts: f32,
    reach: f32,
    min_conf: f32,
    min_conf_deep: f32,
) -> DepthFill {
    let mut out = DepthFill::default();
    let inv = 1.0 / ts;
    let new_points: Vec<(V3, V3, [u8; 3], f32, f32)> = maps
        .par_iter()
        .flat_map(|map| {
            let Some(cam) = capture.cameras.get(map.camera) else {
                return Vec::new();
            };
            let (w, h) = (map.width as usize, map.height as usize);
            let vz = view_zbuf(cam, cur);
            let nrms = learned::view_normals(map, cam);
            let img = capture.images.get(map.camera);
            let mut made = Vec::new();
            for y in 0..h {
                for x in 0..w {
                    let idx = y * w + x;
                    let z = map.depth[idx];
                    if z <= 0.0 {
                        continue;
                    }
                    if vz[idx].load(Ordering::Relaxed) != u32::MAX {
                        continue; // observed pixel — measured data owns it
                    }
                    let conf = map.confidence[idx];
                    // Neighbourhood evidence over the 3x3 ring.
                    let mut zn: Vec<f32> = Vec::with_capacity(8);
                    let mut covered = false;
                    for dy in -1i32..=1 {
                        for dx in -1i32..=1 {
                            if dx == 0 && dy == 0 {
                                continue;
                            }
                            let (nx, ny) = (x as i32 + dx, y as i32 + dy);
                            if nx < 0 || ny < 0 || nx >= w as i32 || ny >= h as i32 {
                                continue;
                            }
                            let mz = vz[ny as usize * w + nx as usize].load(Ordering::Relaxed);
                            if mz != u32::MAX {
                                let mz = f32::from_bits(mz);
                                zn.push(mz);
                                // A measured surface well in front of the
                                // completed depth: this pixel is occluded
                                // space, not a fillable hole.
                                if mz < z - (0.05 + 0.03 * z) {
                                    covered = true;
                                }
                            }
                        }
                    }
                    if covered {
                        continue;
                    }
                    if zn.is_empty() {
                        if conf < min_conf_deep {
                            continue;
                        }
                    } else {
                        if conf < min_conf {
                            continue;
                        }
                        zn.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
                        let med = zn[zn.len() / 2];
                        // Completed depth must continue the neighbouring
                        // measured surface, not jump to a different sheet.
                        if (z - med).abs() > 0.03 + 0.04 * z {
                            continue;
                        }
                    }
                    let p = learned::backproject(cam, x as f32 + 0.5, y as f32 + 0.5, z);
                    // Support must be a real gap: nearest measured point has
                    // to sit further than the emit spacing but within reach.
                    let Some((_, dn)) = meas_grid.nearest(p, reach) else {
                        continue;
                    };
                    if dn < ts * 1.2 {
                        continue;
                    }
                    let n = nrms.get(idx).copied().unwrap_or([0.0, 0.0, 1.0]);
                    let c = img
                        .map(|im| {
                            let px = ((x as f32 + 0.5) * im.width as f32 / w as f32) as u32;
                            let py = ((y as f32 + 0.5) * im.height as f32 / h as f32) as u32;
                            if px < im.width && py < im.height {
                                im.get(px, py)
                            } else {
                                [160, 160, 160]
                            }
                        })
                        .unwrap_or([160, 160, 160]);
                    made.push((p, n, c, dn, conf));
                }
            }
            made
        })
        .collect();
    for (p, n, c, dn, conf) in new_points {
        let key = (
            (p[0] * inv).floor() as i32,
            (p[1] * inv).floor() as i32,
            (p[2] * inv).floor() as i32,
        );
        if !occupied.insert(key) {
            out.duplicate += 1;
            continue;
        }
        out.pos.push(p);
        out.nrm.push(n);
        out.col.push(c);
        out.support.push(dn);
        out.weight.push((0.25 + 0.35 * conf).clamp(0.2, 0.6));
    }
    out
}
