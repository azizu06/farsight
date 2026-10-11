//! Local surface fitting math: symmetric 3x3 eigendecomposition (cyclic
//! Jacobi), weighted PCA plane fits and tangent frames. All deterministic.

use crate::core::*;

/// Eigendecomposition of a symmetric 3x3 stored as [xx, xy, xz, yy, yz, zz].
/// Returns (eigenvalues ascending, eigenvectors as columns of a row-major 3x3).
pub fn eigen_symm3(c: [f32; 6]) -> ([f32; 3], [[f32; 3]; 3]) {
    let mut m = [
        [c[0], c[1], c[2]],
        [c[1], c[3], c[4]],
        [c[2], c[4], c[5]],
    ];
    let mut v = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
    for _ in 0..24 {
        // Largest off-diagonal element.
        let (mut p, mut q, mut mx) = (0usize, 1usize, 0.0f32);
        for i in 0..3 {
            for j in (i + 1)..3 {
                let a = m[i][j].abs();
                if a > mx {
                    mx = a;
                    p = i;
                    q = j;
                }
            }
        }
        if mx < 1e-14 {
            break;
        }
        let (app, aqq, apq) = (m[p][p], m[q][q], m[p][q]);
        let theta = 0.5 * (aqq - app) / apq;
        let t = if theta >= 0.0 {
            1.0 / (theta + (theta * theta + 1.0).sqrt())
        } else {
            -1.0 / (-theta + (theta * theta + 1.0).sqrt())
        };
        let cs = 1.0 / (t * t + 1.0).sqrt();
        let sn = t * cs;
        for k in 0..3 {
            let (mkp, mkq) = (m[k][p], m[k][q]);
            m[k][p] = cs * mkp - sn * mkq;
            m[k][q] = sn * mkp + cs * mkq;
        }
        for k in 0..3 {
            let (mpk, mqk) = (m[p][k], m[q][k]);
            m[p][k] = cs * mpk - sn * mqk;
            m[q][k] = sn * mpk + cs * mqk;
        }
        for k in 0..3 {
            let (vkp, vkq) = (v[k][p], v[k][q]);
            v[k][p] = cs * vkp - sn * vkq;
            v[k][q] = sn * vkp + cs * vkq;
        }
    }
    let mut order = [0usize, 1, 2];
    order.sort_by(|&a, &b| m[a][a].total_cmp(&m[b][b]));
    let vals = [m[order[0]][order[0]], m[order[1]][order[1]], m[order[2]][order[2]]];
    let mut vecs = [[0.0; 3]; 3];
    for (dst, &src) in order.iter().enumerate() {
        for k in 0..3 {
            vecs[dst][k] = v[k][src];
        }
    }
    (vals, vecs)
}

/// A weighted least-squares plane: centroid `c`, unit normal `n` (sign is
/// arbitrary — callers orient), root-mean-square residual `rms`.
#[derive(Clone, Copy, Debug)]
pub struct PlaneFit {
    pub c: V3,
    pub n: V3,
    pub rms: f32,
}

impl PlaneFit {
    /// Signed distance of `p` to the plane.
    #[inline]
    pub fn signed_dist(&self, p: V3) -> f32 {
        dot(sub(p, self.c), self.n)
    }
    /// Orthogonal projection of `p` onto the plane.
    #[inline]
    pub fn project(&self, p: V3) -> V3 {
        sub(p, scale(self.n, self.signed_dist(p)))
    }
}

/// Weighted PCA plane through `pts` (point, weight). Needs >= 3 points and
/// nonzero total weight. Returns None for degenerate (coincident) input.
pub fn fit_plane(pts: &[(V3, f32)]) -> Option<PlaneFit> {
    if pts.len() < 3 {
        return None;
    }
    let mut sw = 0.0f32;
    let mut c = [0.0f32; 3];
    for (p, w) in pts {
        sw += w;
        c = add(c, scale(*p, *w));
    }
    if sw <= 1e-12 {
        return None;
    }
    c = scale(c, 1.0 / sw);
    let mut cov = [0.0f32; 6];
    for (p, w) in pts {
        let d = sub(*p, c);
        cov[0] += w * d[0] * d[0];
        cov[1] += w * d[0] * d[1];
        cov[2] += w * d[0] * d[2];
        cov[3] += w * d[1] * d[1];
        cov[4] += w * d[1] * d[2];
        cov[5] += w * d[2] * d[2];
    }
    let (vals, vecs) = eigen_symm3(cov);
    let n = vecs[0];
    if dot(n, n) < 1e-12 {
        return None;
    }
    Some(PlaneFit {
        c,
        n: normalize(n),
        rms: (vals[0] / sw).max(0.0).sqrt(),
    })
}

/// Orthonormal tangent basis (u, v) perpendicular to unit `n`.
pub fn tangent_basis(n: V3) -> (V3, V3) {
    let a = if n[0].abs() <= n[1].abs() && n[0].abs() <= n[2].abs() {
        [1.0, 0.0, 0.0]
    } else if n[1].abs() <= n[2].abs() {
        [0.0, 1.0, 0.0]
    } else {
        [0.0, 0.0, 1.0]
    };
    let u = normalize(cross(n, a));
    let v = cross(n, u);
    (u, v)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plane_fit_recovers_normal() {
        // Plane z = 2 (normal +Z), points on a grid.
        let pts: Vec<(V3, f32)> = (0..10)
            .flat_map(|i| (0..10).map(move |j| ([i as f32 * 0.1, j as f32 * 0.1, 2.0], 1.0)))
            .collect();
        let f = fit_plane(&pts).unwrap();
        assert!(f.n[2].abs() > 0.999, "n={:?}", f.n);
        assert!(f.rms < 1e-6);
        let p = f.project([0.5, 0.5, 2.3]);
        assert!((p[2] - 2.0).abs() < 1e-5);
    }

    #[test]
    fn plane_fit_tilted() {
        // Plane x + z = 0 -> normal (1,0,1)/sqrt2.
        let n0 = normalize([1.0, 0.0, 1.0]);
        let pts: Vec<(V3, f32)> = (0..8)
            .flat_map(|i| (0..8).map(move |j| {
                let x = i as f32 * 0.1;
                let y = j as f32 * 0.1;
                ([x, y, -x], 1.0)
            }))
            .collect();
        let f = fit_plane(&pts).unwrap();
        assert!(dot(f.n, n0).abs() > 0.999, "n={:?}", f.n);
    }

    #[test]
    fn tangent_basis_orthonormal() {
        let n = normalize([0.3, -0.7, 0.6]);
        let (u, v) = tangent_basis(n);
        assert!((dot(u, n)).abs() < 1e-6 && (dot(v, n)).abs() < 1e-6);
        assert!((dot(u, v)).abs() < 1e-6);
    }
}
