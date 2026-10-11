//! Local PCA over kNN: normal (smallest eigenvector), eigenvalues, and mean
//! neighbour spacing per point. Normals are oriented toward the nearest
//! viewpoint. Ported from farsight-compiler normals.rs (PCA core only — graph
//! propagation replaced by view-orientation, which is always available here).

use crate::grid::Grid;
use glam::Vec3;
use rayon::prelude::*;

/// Eigenvalues (descending) of symmetric 3x3 packed [xx xy xz yy yz zz].
/// Closed-form trigonometric method (same as farsight-compiler::normals).
pub(crate) fn sym3_eigvals(c: [f32; 6]) -> [f32; 3] {
    let (a, b, cc, d, e, f) = (c[0], c[1], c[2], c[3], c[4], c[5]);
    let p1 = b * b + cc * cc + e * e;
    if p1 < 1e-20 {
        let mut v = [a, d, f];
        v.sort_by(|x, y| y.total_cmp(x));
        return v;
    }
    let q = (a + d + f) / 3.0;
    let p2 = (a - q) * (a - q) + (d - q) * (d - q) + (f - q) * (f - q) + 2.0 * p1;
    let p = (p2 / 6.0).sqrt();
    let b11 = (a - q) / p;
    let b22 = (d - q) / p;
    let b33 = (f - q) / p;
    let b12 = b / p;
    let b13 = cc / p;
    let b23 = e / p;
    let det_b = b11 * (b22 * b33 - b23 * b23) - b12 * (b12 * b33 - b13 * b23)
        + b13 * (b12 * b23 - b13 * b22);
    let r = (det_b / 2.0).clamp(-1.0, 1.0);
    let phi = r.acos() / 3.0;
    let l0 = q + 2.0 * p * phi.cos();
    let l2 = q + 2.0 * p * (phi + 2.0 * std::f32::consts::PI / 3.0).cos();
    let l1 = 3.0 * q - l0 - l2;
    [l0, l1, l2]
}

/// Smallest eigenvector + eigenvalues (descending) of packed symmetric 3x3.
fn sym3_min_eigvec(c: [f32; 6]) -> (Vec3, [f32; 3]) {
    let ev = sym3_eigvals(c);
    let l = ev[2];
    let r0 = [c[0] - l, c[1], c[2]];
    let r1 = [c[1], c[3] - l, c[4]];
    let r2 = [c[2], c[4], c[5] - l];
    let cross = |u: [f32; 3], v: [f32; 3]| -> Vec3 {
        Vec3::new(u[0], u[1], u[2]).cross(Vec3::new(v[0], v[1], v[2]))
    };
    let c01 = cross(r0, r1);
    let c02 = cross(r0, r2);
    let c12 = cross(r1, r2);
    let (s01, s02, s12) = (c01.length_squared(), c02.length_squared(), c12.length_squared());
    let n = if s01 >= s02 && s01 >= s12 {
        c01
    } else if s02 >= s12 {
        c02
    } else {
        c12
    };
    let n = if n.length_squared() > 1e-24 {
        n.normalize()
    } else {
        Vec3::Y
    };
    (n, ev)
}

/// All three eigenvectors (columns: e0 e1 e2 matching descending eigenvalues)
/// via cyclic-Jacobi — needed when we want the full tangent frame, not just
/// the normal. `m` is row-major symmetric.
fn sym3_eigvecs(m: [[f32; 3]; 3]) -> ([[f32; 3]; 3], [f32; 3]) {
    let mut a = m;
    let mut v = [[0.0f32; 3]; 3];
    for i in 0..3 {
        v[i][i] = 1.0;
    }
    for _ in 0..24 {
        // largest off-diagonal
        let (mut p, mut q, mut mx) = (0usize, 1usize, 0.0f32);
        for i in 0..3 {
            for j in (i + 1)..3 {
                if a[i][j].abs() > mx {
                    mx = a[i][j].abs();
                    p = i;
                    q = j;
                }
            }
        }
        if mx < 1e-12 {
            break;
        }
        let app = a[p][p];
        let aqq = a[q][q];
        let apq = a[p][q];
        let theta = 0.5 * (aqq - app).atan2(apq);
        let (s, c) = theta.sin_cos();
        for k in 0..3 {
            let (akp, akq) = (a[k][p], a[k][q]);
            a[k][p] = c * akp - s * akq;
            a[k][q] = s * akp + c * akq;
        }
        for k in 0..3 {
            let (apk, aqk) = (a[p][k], a[q][k]);
            a[p][k] = c * apk - s * aqk;
            a[q][k] = s * apk + c * aqk;
        }
        for k in 0..3 {
            let (vkp, vkq) = (v[k][p], v[k][q]);
            v[k][p] = c * vkp - s * vkq;
            v[k][q] = s * vkp + c * vkq;
        }
    }
    let ev = [a[0][0], a[1][1], a[2][2]];
    // sort descending, permuting eigenvector columns to match
    let mut order = [0usize, 1, 2];
    order.sort_by(|&i, &j| ev[j].total_cmp(&ev[i]));
    let mut out_v = [[0.0f32; 3]; 3];
    let mut out_e = [0.0f32; 3];
    for (new, &old) in order.iter().enumerate() {
        for r in 0..3 {
            out_v[r][new] = v[r][old];
        }
        out_e[new] = ev[old];
    }
    (out_v, out_e)
}

pub struct LocalFrame {
    /// Unit normal (smallest-eigenvalue direction), oriented toward views.
    pub normal: Vec3,
    /// Tangent axes t0, t1 (largest two eigenvectors), then normal — columns
    /// of a rotation matrix whose Z axis is the normal. Used as the splat
    /// frame so scale_2 is the thin direction.
    pub rot_cols: [[f32; 3]; 3],
    /// Eigenvalues descending (l0 >= l1 >= l2).
    pub eig: [f32; 3],
    /// Mean distance to the k neighbours (local sample spacing, metres).
    pub spacing: f32,
}

#[derive(Clone, Debug)]
pub struct PcaConfig {
    /// Neighbourhood size.
    pub k: usize,
    /// Down-weight far neighbours in the covariance.
    pub robust: bool,
}

impl Default for PcaConfig {
    fn default() -> Self {
        PcaConfig { k: 16, robust: true }
    }
}

/// PCA frames for every point. `views` = camera positions used to orient
/// normals (nearest view wins). Returns per-point LocalFrame + median spacing.
pub fn frames(pts: &[Vec3], cfg: &PcaConfig, views: &[Vec3], spacing_hint: f32) -> (Vec<LocalFrame>, f32) {
    let n = pts.len();
    if n == 0 {
        return (vec![], spacing_hint);
    }
    let cell = (spacing_hint * 2.0).max(0.008);
    let grid = Grid::build(pts, cell);
    let k = cfg.k.max(4);
    let max_r = spacing_hint * 12.0;
    let (idx, d2) = grid.knn_batch_d2(pts, k + 1, max_r);

    let out: Vec<LocalFrame> = (0..n)
        .into_par_iter()
        .map(|i| {
            let row_i = &idx[i * (k + 1)..(i + 1) * (k + 1)];
            let row_d = &d2[i * (k + 1)..(i + 1) * (k + 1)];
            // weighted covariance around the neighbour mean
            let mut dmax = 0.0f32;
            let mut cnt = 0usize;
            let mut sd_sum = 0.0f32;
            for u in 0..=k {
                let (j, d) = (row_i[u], row_d[u]);
                if j == u32::MAX || j as usize == i || !d.is_finite() {
                    continue;
                }
                if d > dmax {
                    dmax = d;
                }
                sd_sum += d.sqrt();
                cnt += 1;
            }
            let spacing_i = if cnt > 0 { sd_sum / cnt as f32 } else { cell };
            let h2 = if cfg.robust && dmax > 0.0 { dmax * 0.5 } else { f32::INFINITY };
            let mut m = Vec3::ZERO;
            let mut sw = 0.0f32;
            let mut ws = Vec::with_capacity(cnt);
            for u in 0..=k {
                let (j, d) = (row_i[u], row_d[u]);
                if j == u32::MAX || j as usize == i || !d.is_finite() {
                    continue;
                }
                let wj = (-d / h2).exp();
                ws.push((j, wj));
                sw += wj;
                m += pts[j as usize] * wj;
            }
            if ws.len() < 3 || sw <= 0.0 {
                return LocalFrame {
                    normal: Vec3::Y,
                    rot_cols: [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                    eig: [0.0; 3],
                    spacing: spacing_i,
                };
            }
            m /= sw;
            let mut c = [0.0f32; 6];
            for &(j, wj) in &ws {
                let d = pts[j as usize] - m;
                c[0] += wj * d.x * d.x;
                c[1] += wj * d.x * d.y;
                c[2] += wj * d.x * d.z;
                c[3] += wj * d.y * d.y;
                c[4] += wj * d.y * d.z;
                c[5] += wj * d.z * d.z;
            }
            for v in c.iter_mut() {
                *v /= sw;
            }
            // full eigendecomposition for the tangent frame
            let cov = [
                [c[0], c[1], c[2]],
                [c[1], c[3], c[4]],
                [c[2], c[4], c[5]],
            ];
            let (v, ev) = sym3_eigvecs(cov);
            let mut normal = Vec3::new(v[0][2], v[1][2], v[2][2]);
            if normal.length_squared() < 0.5 {
                normal = sym3_min_eigvec(c).0;
            }
            // orient toward the nearest view position
            if !views.is_empty() {
                let p = pts[i];
                let mut best = views[0];
                let mut bd = f32::INFINITY;
                for &vv in views {
                    let d = vv.distance_squared(p);
                    if d < bd {
                        bd = d;
                        best = vv;
                    }
                }
                if normal.dot(best - p) < 0.0 {
                    normal = -normal;
                }
            }
            // keep rot_cols' z-column aligned with the (possibly flipped) normal
            let mut cols = v;
            let zc = Vec3::new(cols[0][2], cols[1][2], cols[2][2]);
            if zc.dot(normal) < 0.0 {
                // flip z column and re-orthogonalise x against it
                for r in 0..3 {
                    cols[r][2] = -cols[r][2];
                }
            }
            let t0 = Vec3::new(cols[0][0], cols[1][0], cols[2][0]);
            let t1 = normal.cross(t0).normalize_or_zero();
            let t0o = t1.cross(normal).normalize_or_zero();
            let rot_cols = [[t0o.x, t1.x, normal.x], [t0o.y, t1.y, normal.y], [t0o.z, t1.z, normal.z]];
            LocalFrame {
                normal,
                rot_cols,
                eig: ev,
                spacing: spacing_i,
            }
        })
        .collect();
    let mut spacings: Vec<f32> = out.iter().map(|f| f.spacing).collect();
    spacings.sort_by(f32::total_cmp);
    (out, spacings[spacings.len() / 2])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::Rng;

    #[test]
    fn flat_patch_normal_is_z() {
        let mut rng = Rng::new(5);
        let pts: Vec<Vec3> = (0..400)
            .map(|_| {
                Vec3::new(
                    rng.f32() * 2.0 - 1.0,
                    rng.f32() * 2.0 - 1.0,
                    rng.f32() * 0.002,
                )
            })
            .collect();
        let views = vec![Vec3::new(0.0, 0.0, 3.0)];
        let (fr, _) = frames(&pts, &PcaConfig::default(), &views, 0.05);
        let mid = &fr[200];
        assert!(mid.normal.z > 0.95, "{:?}", mid.normal);
        assert!(mid.eig[2] < mid.eig[0] * 0.01);
    }
}
