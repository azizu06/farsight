//! Small vector helpers + deterministic PRNG. Ported from farsight-compiler
//! (Apache-2.0, branch `compiler`, rust/farsight-compiler/src/core.rs).

use glam::Vec3;

#[inline]
pub fn clamp(v: f32, lo: f32, hi: f32) -> f32 {
    v.max(lo).min(hi)
}

/// Quaternion (w, x, y, z) rotating +Z onto unit vector `n`.
#[inline]
pub fn quat_from_z(n: Vec3) -> [f32; 4] {
    let z = n.z.clamp(-1.0, 1.0);
    if z < -0.999_99 {
        return [0.0, 1.0, 0.0, 0.0];
    }
    let c = Vec3::Z.cross(n);
    let w = 1.0 + z;
    let l = (w * w + c.length_squared()).sqrt();
    [w / l, c.x / l, c.y / l, c.z / l]
}

#[inline]
pub fn logit(p: f32) -> f32 {
    let p = p.clamp(1e-4, 1.0 - 1e-4);
    (p / (1.0 - p)).ln()
}

/// sRGB byte -> linear [0,1].
#[inline]
pub fn srgb_to_linear(c: u8) -> f32 {
    let v = c as f32 / 255.0;
    if v <= 0.04045 {
        v / 12.92
    } else {
        ((v + 0.055) / 1.055).powf(2.4)
    }
}

/// Linear [0,1] -> sRGB byte.
#[inline]
pub fn linear_to_srgb(v: f32) -> u8 {
    let v = v.clamp(0.0, 1.0);
    let s = if v <= 0.0031308 {
        v * 12.92
    } else {
        1.055 * v.powf(1.0 / 2.4) - 0.055
    };
    (s * 255.0 + 0.5) as u8
}

/// Small deterministic PRNG (PCG32) — reproducible without a dep.
#[derive(Clone, Debug)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        let mut r = Rng(
            seed.wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407),
        );
        r.next_u32();
        r
    }
    pub fn next_u32(&mut self) -> u32 {
        let old = self.0;
        self.0 = old
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let xorshifted = (((old >> 18) ^ old) >> 27) as u32;
        let rot = (old >> 59) as u32;
        xorshifted.rotate_right(rot)
    }
    /// Uniform in [0, 1).
    pub fn f32(&mut self) -> f32 {
        (self.next_u32() >> 8) as f32 * (1.0 / 16_777_216.0)
    }
    pub fn range(&mut self, lo: f32, hi: f32) -> f32 {
        lo + (hi - lo) * self.f32()
    }
}

/// Median of an unsorted slice (copies it).
pub fn median(v: &[f32]) -> f32 {
    if v.is_empty() {
        return 0.0;
    }
    let mut s = v.to_vec();
    s.sort_by(f32::total_cmp);
    s[s.len() / 2]
}
