//! `farsight-splat synth` — a phone-capture-like synthetic room for headless
//! correctness + ablation work (real captures can't go on the public repo —
//! they show people).
//!
//! Emulates Dechante's run: ~150 frames of 640x480 phone JPEGs on a rover
//! drive (move / parked stretches / one >12 s gap), ARKit-like pose noise
//! (1-2 cm, ~0.5 deg) stored in transforms.json while renders use the TRUE
//! pose, per-frame auto-exposure variation, a few limited-tracking frames,
//! one truncated JPEG (the `early eof` case), and noisy holey LiDAR dots
//! written as init.ply + points.bin + dense.xyzrgb.

use std::f64::consts::PI;
use std::path::Path;

use anyhow::Result;
use serde::Serialize;

use crate::capture::{Frame, Transforms};
use crate::seed::{Dot, write_init_ply};

/// Deterministic RNG (splitmix64 + Box-Muller) — synth must be reproducible.
struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
    fn gauss(&mut self) -> f64 {
        let u = self.f64().max(1e-12);
        let v = self.f64();
        (-2.0 * u.ln()).sqrt() * (2.0 * PI * v).cos()
    }
    fn range(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.f64()
    }
}

#[derive(Clone, Debug)]
pub struct SynthOpts {
    pub frames: usize,      // ~150 default
    pub w: u32,
    pub h: u32,
    pub room: f64,          // half-size of the cubic room (m)
    pub dots: usize,
    pub seed: u64,
    pub pose_noise_m: f64,
    pub pose_noise_deg: f64,
    pub dot_noise_m: f64,
    pub exposure_jitter: f64,
    pub corrupt_frame: Option<usize>,
}

impl Default for SynthOpts {
    fn default() -> Self {
        Self {
            frames: 150,
            w: 640,
            h: 480,
            room: 3.0,
            dots: 80_000,
            seed: 7,
            pose_noise_m: 0.015,
            pose_noise_deg: 0.5,
            dot_noise_m: 0.02,
            exposure_jitter: 0.08,
            corrupt_frame: Some(77),
        }
    }
}

// ---- scene: cube room + 2 boxes + 1 sphere, flat-shaded, procedural detail ----

struct Hit {
    t: f64,
    n: [f64; 3],
    surf: Surf,
}
enum Surf {
    Wall { axis: usize, side: f64, u: f64, v: f64 },
    Box { p: [f64; 3], n: [f64; 3] },
    Sphere { n: [f64; 3] },
}

const BOXES: [[f64; 6]; 2] = [
    [-1.6, -3.0, 0.6, -0.6, -1.6, 1.2],  // table-ish box (x0,y0,z0,x1,y1,z1)
    [0.9, -3.0, -0.4, 1.9, -1.9, 0.6],   // chair-ish box
];
const SPHERE: ([f64; 3], f64) = ([-0.3, -2.2, -1.2], 0.55);

fn intersect(o: [f64; 3], d: [f64; 3], room: f64) -> Option<Hit> {
    let mut best = f64::INFINITY;
    let mut hit = None;
    // 6 walls
    for axis in 0..3 {
        for side in [-1.0, 1.0] {
            let plane = side * room;
            if d[axis].abs() < 1e-9 {
                continue;
            }
            let t = (plane - o[axis]) / d[axis];
            if t <= 0.0 || t >= best {
                continue;
            }
            let p = [o[0] + t * d[0], o[1] + t * d[1], o[2] + t * d[2]];
            let inb = |v: f64| v >= -room - 1e-6 && v <= room + 1e-6;
            if inb(p[0]) && inb(p[1]) && inb(p[2]) {
                let (u, v) = match axis {
                    0 => (p[2], p[1]),
                    1 => (p[0], p[2]),
                    _ => (p[0], p[1]),
                };
                let mut n = [0.0; 3];
                n[axis] = -side;
                best = t;
                hit = Some(Hit {
                    t,
                    n,
                    surf: Surf::Wall { axis, side, u, v },
                });
            }
        }
    }
    // boxes
    for b in &BOXES {
        let mut t0 = 0.0f64;
        let mut t1 = f64::INFINITY;
        let mut ax = 0usize;
        let mut sign = 1.0;
        let mut ok = true;
        for i in 0..3 {
            let (lo, hi) = (b[i], b[i + 3]);
            if d[i].abs() < 1e-9 {
                if o[i] < lo || o[i] > hi {
                    ok = false;
                    break;
                }
            } else {
                let mut ta = (lo - o[i]) / d[i];
                let mut tb = (hi - o[i]) / d[i];
                let mut s = -1.0;
                if ta > tb {
                    std::mem::swap(&mut ta, &mut tb);
                    s = 1.0;
                }
                if ta > t0 {
                    t0 = ta;
                    ax = i;
                    sign = s;
                }
                t1 = t1.min(tb);
            }
        }
        if ok && t0 < t1 && t0 > 0.0 && t0 < best {
            let mut n = [0.0; 3];
            n[ax] = sign;
            best = t0;
            hit = Some(Hit {
                t: t0,
                n,
                surf: Surf::Box {
                    p: [o[0] + t0 * d[0], o[1] + t0 * d[1], o[2] + t0 * d[2]],
                    n,
                },
            });
        }
    }
    // sphere
    let (c, r) = SPHERE;
    let oc = [o[0] - c[0], o[1] - c[1], o[2] - c[2]];
    let b = oc[0] * d[0] + oc[1] * d[1] + oc[2] * d[2];
    let cc = oc[0] * oc[0] + oc[1] * oc[1] + oc[2] * oc[2] - r * r;
    let disc = b * b - cc;
    if disc > 0.0 {
        let t = -b - disc.sqrt();
        if t > 0.0 && t < best {
            let p = [o[0] + t * d[0], o[1] + t * d[1], o[2] + t * d[2]];
            let n = [
                (p[0] - c[0]) / r,
                (p[1] - c[1]) / r,
                (p[2] - c[2]) / r,
            ];
            best = t;
            hit = Some(Hit {
                t,
                n,
                surf: Surf::Sphere { n },
            });
        }
    }
    hit.map(|mut h| {
        h.t = best;
        h
    })
}

fn hash01(x: f64, y: f64) -> f64 {
    let s = (x * 127.1 + y * 311.7).sin() * 43758.5453;
    s - s.floor()
}

fn shade(hit: &Hit, t: f64) -> [f64; 3] {
    let base = match &hit.surf {
        Surf::Wall { axis, side, u, v } => match (axis, side) {
            (1, s) if *s < 0.0 => {
                // floor: 0.5m checkerboard, two wood tones
                let chk = ((u * 2.0).floor() + (v * 2.0).floor()) as i64 % 2 == 0;
                if chk { [0.62, 0.50, 0.38] } else { [0.44, 0.34, 0.26] }
            }
            (1, _) => [0.92, 0.90, 0.86], // ceiling
            (0, s) if *s > 0.0 => {
                // +x wall: green with two framed posters
                poster(*u, *v, [0.30, 0.55, 0.38])
            }
            (0, _) => [0.72, 0.42, 0.30],
            (2, s) if *s > 0.0 => {
                // +z wall: blue with vertical paneling
                let stripe = 0.05 * ((u * 6.0).sin());
                [0.28 + stripe, 0.40 + stripe, 0.68 + stripe]
            }
            _ => [0.78, 0.70, 0.52],
        },
        Surf::Box { p, n } => {
            if n[1] > 0.9 {
                [0.55, 0.44, 0.32] // wood top
            } else {
                let _ = p;
                [0.42, 0.33, 0.25]
            }
        }
        Surf::Sphere { n } => {
            let _ = n;
            [0.75, 0.25, 0.22]
        }
    };
    // speckle for micro-contrast, lambert to a light dir, distance falloff
    let (u, v) = match &hit.surf {
        Surf::Wall { u, v, .. } => (*u, *v),
        Surf::Box { p, .. } => (p[0], p[2]),
        Surf::Sphere { n } => (n[0], n[1]),
    };
    let sp = 0.9 + 0.2 * hash01(u * 7.3, v * 7.3);
    let l = [0.4, 0.8, 0.45];
    let mut lam = hit.n[0] * l[0] + hit.n[1] * l[1] + hit.n[2] * l[2];
    if lam < 0.0 {
        lam = -lam * 0.6; // thin walls / backface light bounce
    }
    let att = 1.0 / (1.0 + 0.10 * t);
    let k = sp * (0.35 + 0.65 * lam) * att;
    [base[0] * k, base[1] * k, base[2] * k]
}

fn poster(u: f64, v: f64, wall: [f64; 3]) -> [f64; 3] {
    // Two framed rectangles on the wall
    for (cu, cv, col) in [(-1.2, 0.4, [0.85, 0.78, 0.60]), (0.9, 0.9, [0.62, 0.72, 0.82])] {
        if (u - cu).abs() < 0.55 && (v - cv).abs() < 0.4 {
            let border = (u - cu).abs() > 0.48 || (v - cv).abs() > 0.33;
            return if border { [0.25, 0.2, 0.16] } else { col };
        }
    }
    wall
}

// ---- cameras ----

struct Pose {
    m: [[f64; 4]; 4],
    t: f64,
    tracking: &'static str,
}

/// Rover trajectory: arc drive (~60% of frames), parked stretch (~25%), a
/// 15 s gap, then a short reverse drive — exercising drive-cut and parked
/// cull paths.
fn trajectory(o: &SynthOpts) -> Vec<Pose> {
    let mut rng = Rng(o.seed);
    let n_drive1 = (o.frames as f64 * 0.58) as usize;
    let n_parked = (o.frames as f64 * 0.25) as usize;
    let n_drive2 = o.frames - n_drive1 - n_parked;
    let mut poses = Vec::with_capacity(o.frames);
    let mut t = 0.0f64;
    let cam_pos = |s: f64| -> [f64; 3] {
        // Curved path through the room: enter along +x, curve toward -z.
        let a = -0.9 + s * 1.6;
        [
            1.9 * a.cos() + 0.2 * s,
            -1.0 + 0.15 * (s * 9.0).sin(),
            1.9 * a.sin() - 0.4,
        ]
    };
    let look = |p: [f64; 3], s: f64| -> [f64; 3] {
        // Gaze drifts ahead of travel + slight wander (driver looking around).
        let tgt = [
            0.4 * (s * 5.0 + 1.0).cos() - 0.3,
            -0.6 + 0.25 * (s * 7.0).sin(),
            0.4 * (s * 5.0 + 1.0).sin() - 0.2,
        ];
        let mut d = [tgt[0] - p[0], tgt[1] - p[1], tgt[2] - p[2]];
        let n = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2]).sqrt();
        for v in &mut d {
            *v /= n;
        }
        d
    };
    let c2w = |pos: [f64; 3], fwd: [f64; 3]| -> [[f64; 4]; 4] {
        let up = [0.0, 1.0, 0.0];
        let mut x = [
            up[1] * fwd[2] - up[2] * fwd[1],
            up[2] * fwd[0] - up[0] * fwd[2],
            up[0] * fwd[1] - up[1] * fwd[0],
        ];
        let xn = (x[0] * x[0] + x[1] * x[1] + x[2] * x[2]).sqrt();
        for v in &mut x {
            *v /= xn;
        }
        let y = [
            fwd[1] * x[2] - fwd[2] * x[1],
            fwd[2] * x[0] - fwd[0] * x[2],
            fwd[0] * x[1] - fwd[1] * x[0],
        ];
        [
            [x[0], y[0], -fwd[0], pos[0]],
            [x[1], y[1], -fwd[1], pos[1]],
            [x[2], y[2], -fwd[2], pos[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
    };
    for i in 0..n_drive1 {
        let s = i as f64 / (n_drive1.max(1) - 1) as f64;
        let p = cam_pos(s);
        poses.push(Pose {
            m: c2w(p, look(p, s)),
            t,
            tracking: if i % 41 == 40 { "limited" } else { "normal" },
        });
        t += rng.range(0.35, 0.5);
    }
    // parked stretch: same pose + mm-scale jitter (ghosts/parked cull fodder)
    let (s0, p0) = (1.0, cam_pos(1.0));
    for _ in 0..n_parked {
        let pj = [
            p0[0] + rng.gauss() * 0.002,
            p0[1] + rng.gauss() * 0.002,
            p0[2] + rng.gauss() * 0.002,
        ];
        let d = look(p0, s0);
        let dj = [
            d[0] + rng.gauss() * 0.001,
            d[1] + rng.gauss() * 0.001,
            d[2] + rng.gauss() * 0.001,
        ];
        poses.push(Pose {
            m: c2w(pj, dj),
            t,
            tracking: "normal",
        });
        t += rng.range(0.2, 0.3);
    }
    t += 15.0; // >12 s gap -> a second "drive"
    let p2 = [
        p0[0] + 0.7,
        p0[1] + 0.05,
        p0[2] - 0.5,
    ];
    for i in 0..n_drive2 {
        let s = i as f64 / (n_drive2.max(1) - 1) as f64;
        let p = [
            p2[0] - 1.2 * s,
            p2[1] + 0.1 * (s * 4.0).sin(),
            p2[2] - 0.8 * s,
        ];
        poses.push(Pose {
            m: c2w(p, look(p, 0.5 + 0.5 * s)),
            t,
            tracking: if i % 17 == 16 { "limited" } else { "normal" },
        });
        t += rng.range(0.35, 0.5);
    }
    poses
}

fn jitter_pose(m: &[[f64; 4]; 4], rng: &mut Rng, dm: f64, ddeg: f64) -> [[f64; 4]; 4] {
    // Apply small translation + small world-axis rotation to the c2w.
    let dr = ddeg.to_radians();
    let (rx, ry, rz) = (rng.gauss() * dr, rng.gauss() * dr, rng.gauss() * dr);
    let (sx, cx) = rx.sin_cos();
    let (sy, cy) = ry.sin_cos();
    let (sz, cz) = rz.sin_cos();
    let rotx = [[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]];
    let roty = [[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]];
    let rotz = [[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]];
    let matmul = |a: [[f64; 3]; 3], b: [[f64; 3]; 3]| -> [[f64; 3]; 3] {
        let mut o = [[0.0; 3]; 3];
        for i in 0..3 {
            for j in 0..3 {
                o[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
            }
        }
        o
    };
    let r = matmul(rotz, matmul(roty, rotx));
    let mut out = *m;
    for i in 0..3 {
        for j in 0..3 {
            out[i][j] = r[i][0] * m[0][j] + r[i][1] * m[1][j] + r[i][2] * m[2][j];
        }
    }
    for i in 0..3 {
        out[i][3] = m[i][3] + rng.gauss() * dm;
    }
    out
}

// ---- render ----

fn render_frame(
    o: &SynthOpts,
    m: &[[f64; 4]; 4],
    fx: f64,
    fy: f64,
    gain: f64,
) -> image::RgbImage {
    let (w, h) = (o.w as usize, o.h as usize);
    let cx = w as f64 / 2.0;
    let cy = h as f64 / 2.0;
    let mut img = image::RgbImage::new(o.w, o.h);
    for y in 0..h {
        for x in 0..w {
            let dc = [
                (x as f64 + 0.5 - cx) / fx,
                -(y as f64 + 0.5 - cy) / fy,
                -1.0,
            ];
            // rotate into world (rows of c2w rotation)
            let d = [
                m[0][0] * dc[0] + m[0][1] * dc[1] + m[0][2] * dc[2],
                m[1][0] * dc[0] + m[1][1] * dc[1] + m[1][2] * dc[2],
                m[2][0] * dc[0] + m[2][1] * dc[1] + m[2][2] * dc[2],
            ];
            let org = [m[0][3], m[1][3], m[2][3]];
            let rgb = match intersect(org, d, o.room) {
                Some(hit) => shade(&hit, hit.t),
                None => [0.02, 0.02, 0.03],
            };
            img.put_pixel(
                x as u32,
                y as u32,
                image::Rgb([
                    (rgb[0] * gain).clamp(0.0, 1.0).powf(1.0 / 2.2) as f64,
                    (rgb[1] * gain).clamp(0.0, 1.0).powf(1.0 / 2.2) as f64,
                    (rgb[2] * gain).clamp(0.0, 1.0).powf(1.0 / 2.2) as f64,
                ]
                .map(|v| (v * 255.0) as u8)),
            );
        }
    }
    img
}

// ---- dots ----

fn make_dots(o: &SynthOpts, poses: &[Pose]) -> Vec<Dot> {
    let mut rng = Rng(o.seed + 1);
    let mut dots = Vec::with_capacity(o.dots);
    // Weighted surface samplers: walls (60%), floor+ceiling (25%), objects.
    while dots.len() < o.dots {
        let r = rng.f64();
        let mut p = [0.0; 3];
        let mut hole = false;
        if r < 0.60 {
            // wall: pick axis + side, uniform in the plane
            let axis = (rng.f64() * 3.0) as usize;
            let side = if rng.f64() < 0.5 { -1.0 } else { 1.0 };
            p = [
                rng.range(-o.room, o.room),
                rng.range(-o.room, o.room),
                rng.range(-o.room, o.room),
            ];
            p[axis] = side * o.room;
            // LiDAR hole: nothing on the +z wall's upper-left quadrant
            if axis == 2 && side > 0.0 && p[0] < 0.0 && p[1] > 0.0 {
                hole = true;
            }
        } else if r < 0.85 {
            p = [
                rng.range(-o.room, o.room),
                if rng.f64() < 0.5 { -o.room } else { o.room },
                rng.range(-o.room, o.room),
            ];
        } else {
            // on a box / sphere surface
            if rng.f64() < 0.7 {
                let b = BOXES[(rng.f64() * BOXES.len() as f64) as usize];
                let face = (rng.f64() * 3.0) as usize;
                let side = if rng.f64() < 0.5 { 0 } else { 3 };
                p = [
                    rng.range(b[0], b[3]),
                    rng.range(b[1], b[4]),
                    rng.range(b[2], b[5]),
                ];
                p[face] = b[face + side];
            } else {
                let (c, rad) = SPHERE;
                let u = rng.f64() * 2.0 - 1.0;
                let a = rng.f64() * 2.0 * PI;
                let s = (1.0 - u * u).sqrt();
                p = [c[0] + rad * s * a.cos(), c[1] + rad * u, c[2] + rad * s * a.sin()];
            }
        }
        // random dropout for sparse-cover realism
        if hole || rng.f64() < 0.06 {
            continue;
        }
        for i in 0..3 {
            p[i] += rng.gauss() * o.dot_noise_m;
        }
        // color: nearest camera surface color, slightly noisy
        let rgb = [
            (rng.f64() * 90.0 + 130.0) as u8,
            (rng.f64() * 90.0 + 110.0) as u8,
            (rng.f64() * 90.0 + 90.0) as u8,
        ];
        dots.push(Dot {
            p: p.map(|v| v as f32),
            rgb,
        });
    }
    let _ = poses;
    dots
}

#[derive(Serialize)]
pub struct SynthReport {
    pub dir: String,
    pub frames: usize,
    pub images: usize,
    pub dots: usize,
    pub transforms: String,
}

/// Generate the capture. `farsight-splat synth <dir>`.
pub fn synth(out: &Path, o: &SynthOpts) -> Result<SynthReport> {
    let mut rng = Rng(o.seed);
    let img_dir = out.join("images");
    std::fs::create_dir_all(&img_dir)?;

    let poses = trajectory(o);
    let fovx = 1.05f64; // ~60 deg horizontal
    let fx = o.w as f64 / (2.0 * (fovx / 2.0).tan());
    let fy = fx;
    let mut frames = Vec::with_capacity(poses.len());
    for (i, pose) in poses.iter().enumerate() {
        let name = format!("images/frame{i:05}.jpg");
        // per-frame auto exposure
        let gain = 1.0 + rng.gauss() * o.exposure_jitter;
        // slight ARKit drift: bias grows linearly through the capture
        let drift = i as f64 / poses.len() as f64 * 0.03;
        let mut noisy = jitter_pose(&pose.m, &mut rng, o.pose_noise_m, o.pose_noise_deg);
        noisy[1][3] += drift;
        let img = render_frame(o, &pose.m, fx, fy, gain);
        let path = img_dir.join(format!("frame{i:05}.jpg"));
        let mut buf = std::io::Cursor::new(Vec::new());
        img.write_to(&mut buf, image::ImageFormat::Jpeg)?;
        let mut bytes = buf.into_inner();
        if o.corrupt_frame == Some(i) {
            bytes.truncate(bytes.len() * 55 / 100); // 'early eof'
        }
        std::fs::write(&path, bytes)?;
        let mut f = Frame {
            file_path: name,
            transform_matrix: noisy.iter().map(|r| r.to_vec()).collect(),
            t_capture: Some(pose.t),
            tracking: Some(pose.tracking.to_string()),
            extra: Default::default(),
        };
        f.extra.insert("fl_x".into(), fx.into());
        f.extra.insert("fl_y".into(), fy.into());
        f.extra.insert("w".into(), (o.w as f64).into());
        f.extra.insert("h".into(), (o.h as f64).into());
        frames.push(f);
    }

    let dots = make_dots(o, &poses);
    write_init_ply(&out.join("init.ply"), &dots)?;
    // dot side files for the seed-from-dots path
    let mut bin = (dots.len() as u32).to_le_bytes().to_vec();
    for d in &dots {
        for v in d.p {
            bin.extend_from_slice(&v.to_le_bytes());
        }
        bin.extend_from_slice(&d.rgb);
        bin.push(0u8);
    }
    std::fs::write(out.join("points.bin"), &bin)?;
    std::fs::write(out.join("dense.xyzrgb"), &bin[4..])?;

    let t = Transforms {
        camera_angle_x: Some(fovx),
        w: Some(o.w as f64),
        h: Some(o.h as f64),
        ply_file_path: Some("init.ply".to_string()),
        frames,
        ..Default::default()
    };
    std::fs::write(out.join("transforms.json"), serde_json::to_string_pretty(&t)?)?;

    Ok(SynthReport {
        dir: out.display().to_string(),
        frames: poses.len(),
        images: poses.len(),
        dots: dots.len(),
        transforms: out.join("transforms.json").display().to_string(),
    })
}
