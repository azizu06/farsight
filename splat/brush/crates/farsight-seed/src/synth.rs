//! Synthetic capture generator: an axis-aligned box "room" with two interior
//! occluder boxes, LiDAR-like dots (noise + holes), orbiting pinhole cameras
//! with ARKit-like pose jitter, and images rendered by analytic ray-casting
//! so depth and colour agree exactly with the scene. Used by the e2e test and
//! the `farsight-seed-synth` binary; T1's `farsight-splat synth` will supply
//! the shared ablation capture — this keeps farsight-seed self-verifiable.

use crate::core::Rng;
use crate::views::SeedCamera;
use glam::Vec3;
use image::{Rgb, RgbImage};

/// One axis-aligned box face set: min/max corners + per-face rgb.
pub struct Walled {
    pub lo: Vec3,
    pub hi: Vec3,
    /// face colours in +x,-x,+y,-y,+z,-z order
    pub col: [[u8; 3]; 6],
}

pub struct SynthScene {
    pub room: Walled,
    pub boxes: Vec<Walled>,
    pub fx: f32,
    pub fy: f32,
    pub w: u32,
    pub h: u32,
}

impl Default for SynthScene {
    fn default() -> Self {
        SynthScene {
            room: Walled {
                lo: Vec3::new(0.0, 0.0, 0.0),
                hi: Vec3::new(4.0, 2.8, 4.0),
                col: [
                    [150, 120, 90],  // +x wall
                    [90, 140, 160],  // -x wall
                    [170, 170, 170], // ceiling (+y)
                    [110, 90, 70],   // floor (-y)
                    [120, 150, 110], // +z wall
                    [160, 130, 140], // -z wall
                ],
            },
            boxes: vec![
                Walled {
                    lo: Vec3::new(1.4, 0.0, 1.4),
                    hi: Vec3::new(2.2, 1.1, 2.2),
                    col: [[200, 80, 60]; 6],
                },
                Walled {
                    lo: Vec3::new(2.8, 0.0, 2.6),
                    hi: Vec3::new(3.4, 0.7, 3.6),
                    col: [[60, 90, 200]; 6],
                },
            ],
            fx: 260.0,
            fy: 260.0,
            w: 320,
            h: 240,
        }
    }
}

/// Intersect ray o+t·r (t>0) with box walls; returns (t, face colour, face axis).
fn box_hit(lo: Vec3, hi: Vec3, o: Vec3, r: Vec3) -> Option<(f32, usize)> {
    let mut t_lo = 0.0f32;
    let mut t_hi = f32::INFINITY;
    let mut axis_lo = 0usize;
    for a in 0..3 {
        let (ro, rd) = (o[a], r[a]);
        if rd.abs() < 1e-9 {
            if ro < lo[a] || ro > hi[a] {
                return None;
            }
            continue;
        }
        let (mut t0, mut t1) = ((lo[a] - ro) / rd, (hi[a] - ro) / rd);
        if t0 > t1 {
            std::mem::swap(&mut t0, &mut t1);
        }
        if t0 > t_lo {
            t_lo = t0;
            axis_lo = a;
        }
        t_hi = t_hi.min(t1);
        if t_lo > t_hi {
            return None;
        }
    }
    (t_lo > 1e-4).then_some((t_lo, axis_lo))
}

/// Nearest hit over room interior + occluder boxes -> (t, base colour, axis, hit point).
fn scene_hit(s: &SynthScene, o: Vec3, r: Vec3) -> Option<(f32, [u8; 3], usize, Vec3)> {
    // room: ray exits the box — we want the first wall hit going outward
    let mut best: Option<(f32, [u8; 3], usize, Vec3)> = None;
    // room walls: treat each axis slab; the camera is inside so t_hi is the exit
    for a in 0..3 {
        for &face in &[0usize, 1] {
            let plane = if face == 0 { s.room.hi[a] } else { s.room.lo[a] };
            if r[a].abs() < 1e-9 {
                continue;
            }
            let t = (plane - o[a]) / r[a];
            if t <= 1e-4 {
                continue;
            }
            let p = o + r * t;
            let inside = (0..3).all(|b| {
                b == a || (p[b] >= s.room.lo[b] - 1e-4 && p[b] <= s.room.hi[b] + 1e-4)
            });
            if inside {
                // col order is +axis,-axis per axis
                let col = s.room.col[if face == 0 { a * 2 } else { a * 2 + 1 }];
                if best.is_none_or(|b| t < b.0) {
                    best = Some((t, col, a, p));
                }
            }
        }
    }
    for b in &s.boxes {
        if let Some((t, axis)) = box_hit(b.lo, b.hi, o, r) {
            let p = o + r * t;
            // face on near side (entry): determine sign by approach
            let col = b.col[axis * 2];
            if best.is_none_or(|bb| t < bb.0) {
                best = Some((t, col, axis, p));
            }
        }
    }
    best
}

/// Checkerboard tint on the two axes perpendicular to `axis`.
fn checker(p: Vec3, axis: usize, scale: f32, c: [u8; 3]) -> [u8; 3] {
    let (a, b) = match axis {
        0 => (p.y, p.z),
        1 => (p.x, p.z),
        _ => (p.x, p.y),
    };
    let even = ((a / scale).floor() as i32 + (b / scale).floor() as i32) & 1 == 0;
    let f = if even { 0.85 } else { 1.1 };
    [
        (c[0] as f32 * f).min(255.0) as u8,
        (c[1] as f32 * f).min(255.0) as u8,
        (c[2] as f32 * f).min(255.0) as u8,
    ]
}

/// Camera poses: `n` orbits at height ~1.1 m, yawing to cover the room, with
/// ARKit-like jitter (position ±1-2 cm, yaw ±0.5 deg).
pub fn cameras(s: &SynthScene, n: usize, rng: &mut Rng) -> Vec<SeedCamera> {
    let mut out = Vec::with_capacity(n);
    let cx = (s.room.lo.x + s.room.hi.x) * 0.5;
    let cz = (s.room.lo.z + s.room.hi.z) * 0.5;
    for i in 0..n {
        let t = i as f32 / n as f32;
        let ang = t * std::f32::consts::TAU;
        let rad = 1.35 + 0.15 * (t * 3.0).sin();
        let pos = Vec3::new(
            cx + rad * ang.cos() + rng.range(-0.015, 0.015),
            1.1 + 0.25 * (t * 5.0).sin() + rng.range(-0.015, 0.015),
            cz + rad * ang.sin() + rng.range(-0.015, 0.015),
        );
        // look at room centre + jitter + a per-camera yaw offset so coverage
        // spreads beyond the exact orbit look direction
        let yaw_off = rng.range(-0.45, 0.45);
        let yaw_jit = rng.range(-0.009, 0.009) + yaw_off; // ~0.5 deg + spread
        let fwd = (Vec3::new(cx, 1.15, cz) - pos).normalize();
        let fwd = Vec3::new(
            fwd.x * yaw_jit.cos() - fwd.z * yaw_jit.sin(),
            fwd.y,
            fwd.x * yaw_jit.sin() + fwd.z * yaw_jit.cos(),
        )
        .normalize();
        // camera looks down -Z: rotation columns are camera axes in world
        let right = fwd.cross(Vec3::Y).normalize_or_zero() * -1.0;
        let up = fwd.cross(right).normalize_or_zero() * -1.0;
        // c2w rows = world axes of camera basis (row-major): columns are basis vecs
        let c2w = [
            [right.x, up.x, -fwd.x, pos.x],
            [right.y, up.y, -fwd.y, pos.y],
            [right.z, up.z, -fwd.z, pos.z],
            [0.0, 0.0, 0.0, 1.0],
        ];
        out.push(SeedCamera {
            width: s.w,
            height: s.h,
            fx: s.fx,
            fy: s.fy,
            cx: s.w as f32 * 0.5,
            cy: s.h as f32 * 0.5,
            c2w,
        });
    }
    out
}

/// LiDAR-like dots: points on visible surfaces + noise + holes.
pub fn dots(s: &SynthScene, n: usize, rng: &mut Rng) -> (Vec<Vec3>, Vec<[u8; 3]>) {
    let mut pos = Vec::with_capacity(n);
    let mut col = Vec::with_capacity(n);
    let surf = |w: &Walled, rng: &mut Rng| -> (Vec3, usize) {
        // pick a face, sample a point on it
        let a = (rng.f32() * 3.0) as usize % 3;
        let face = rng.f32() < 0.5;
        let (u, v) = (rng.f32(), rng.f32());
        let mut p = Vec3::ZERO;
        for b in 0..3 {
            p[b] = if b == a {
                if face { w.hi[b] } else { w.lo[b] }
            } else {
                w.lo[b] + u * (w.hi[b] - w.lo[b])
            };
        }
        // second coord needs its own random — reuse v
        let b2 = (a + 1 + (rng.f32() * 2.0) as usize) % 3;
        if b2 != a {
            p[b2] = w.lo[b2] + v * (w.hi[b2] - w.lo[b2]);
        }
        (p, a * 2 + face as usize)
    };
    // holes: skip dots near two patch centres
    let holes = [Vec3::new(0.0, 1.4, 2.0), Vec3::new(2.0, 0.0, 4.0)];
    while pos.len() < n {
        let w = if rng.f32() < 0.85 {
            &s.room
        } else {
            &s.boxes[(rng.f32() * s.boxes.len() as f32) as usize % s.boxes.len()]
        };
        let (mut p, face) = surf(w, rng);
        if holes.iter().any(|h| (p - *h).length() < 0.35) {
            continue;
        }
        // 1-3 cm gaussian-ish noise (sum of two uniforms)
        for a in 0..3 {
            p[a] += (rng.f32() + rng.f32() - 1.0) * 0.02;
        }
        pos.push(p);
        col.push(w.col[face.min(5)]);
    }
    // 0.3% far-out noise points
    let n_noise = n / 300;
    for _ in 0..n_noise {
        pos.push(Vec3::new(
            rng.range(-6.0, 10.0),
            rng.range(-3.0, 6.0),
            rng.range(-6.0, 10.0),
        ));
        col.push([128, 128, 128]);
    }
    (pos, col)
}

/// Render one view: ray-cast each pixel, checker + per-frame exposure jitter.
pub fn render(s: &SynthScene, cam: &SeedCamera, exposure: f32, rng: &mut Rng) -> RgbImage {
    let mut img = RgbImage::new(cam.width, cam.height);
    for y in 0..cam.height {
        for x in 0..cam.width {
            let r = cam.ray(x as f32 + 0.5, y as f32 + 0.5);
            let px = match scene_hit(s, cam.position(), r) {
                Some((_t, c, axis, p)) => {
                    let c = checker(p, axis, 0.35, c);
                    let n = rng.range(-4.0, 4.0);
                    [
                        (c[0] as f32 * exposure + n).clamp(0.0, 255.0) as u8,
                        (c[1] as f32 * exposure + n).clamp(0.0, 255.0) as u8,
                        (c[2] as f32 * exposure + n).clamp(0.0, 255.0) as u8,
                    ]
                }
                None => [8, 8, 10],
            };
            img.put_pixel(x, y, Rgb(px));
        }
    }
    img
}

/// Write a dot cloud PLY (x,y,z + uchar rgb) — the format init.ply uses.
pub fn dots_ply(pos: &[Vec3], col: &[[u8; 3]]) -> Vec<u8> {
    let n = pos.len();
    let mut out = format!(
        "ply\nformat binary_little_endian 1.0\nelement vertex {n}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    )
    .into_bytes();
    out.reserve(n * 15);
    for i in 0..n {
        for c in [pos[i].x, pos[i].y, pos[i].z] {
            out.extend_from_slice(&c.to_le_bytes());
        }
        out.extend_from_slice(&col[i]);
    }
    out
}

/// nerfstudio transforms.json for the generated views.
pub fn transforms_json(s: &SynthScene, cams: &[SeedCamera], n_imgs: usize) -> serde_json::Value {
    let frames: Vec<_> = (0..n_imgs)
        .map(|i| {
            let c = &cams[i];
            let m = &c.c2w;
            serde_json::json!({
                "file_path": format!("images/{:04}.png", i),
                "transform_matrix": m,
            })
        })
        .collect();
    serde_json::json!({
        "fl_x": s.fx, "fl_y": s.fy,
        "cx": s.w as f32 * 0.5, "cy": s.h as f32 * 0.5,
        "w": s.w, "h": s.h,
        "ply_file_path": "init.ply",
        "frames": frames,
    })
}
