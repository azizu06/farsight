//! Watertight-where-justified surface reconstruction. OWNER: W4.
//!
//! Sparse-voxel implicit surface from oriented points, marching-cubes-style
//! polygonisation, support trimming, per-vertex normals/colours/support.
//!
//! Pipeline inside this stage:
//!   1. mark: cells containing points, dilated `band_vox` shells (narrow band)
//!      plus `dilate_vox` extra shells into unobserved space (hole bridging).
//!   2. observe: per band cell a weighted point-to-plane signed-distance
//!      estimate, colour and `support` seed (distance to nearest *measured*
//!      contributor).
//!   3. smooth: Jacobi screened-Poisson-style iterations propagate the implicit
//!      function through dilated cells and denoise observed cells; support and
//!      colour propagate alongside.
//!   4. polygonise: per-cube face-walk contour extraction (equivalent to
//!      marching cubes with the asymptotic decider; table-free). Vertices are
//!      welded globally by shared grid-edge keys.
//!   5. trim: drop triangles whose mean vertex support exceeds `trim_m`.
//!
//! Everything is deterministic: all order-dependent structures are built by
//! index-ordered parallel collects; hash maps are used for lookup only.

use crate::core::*;
use crate::spatial::Grid;
use crate::Stats;
use rayon::prelude::*;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct SurfaceOptions {
    /// Implicit grid voxel size (metres).
    pub voxel: f32,
    /// Remove surface farther than this from any *measured* sample (metres).
    pub trim_m: f32,
    /// Narrow-band half-thickness in voxels: points contribute to cells within
    /// this distance and observed samples are evaluated inside it.
    pub band_vox: f32,
    /// Extra dilation shells (voxels) beyond the band into unobserved space;
    /// the implicit function is Laplacian-filled there (small-gap bridging).
    pub dilate_vox: usize,
    /// Screened-Poisson smoothing iterations over the whole cell set.
    pub smooth_iters: usize,
    /// Screening strength: 0 keeps observations verbatim, higher smooths more.
    pub smooth_lambda: f32,
    /// How far (metres) a fill region can extend before it counts as a
    /// one-sided extrapolation skirt. Fill inside observed gaps (bracketed
    /// by band cells on two sides within this reach) is kept; unbracketed
    /// extrapolation is clamped to solid so no phantom shells form outside
    /// the measured set.
    pub hole_reach: f32,
}

impl Default for SurfaceOptions {
    fn default() -> Self {
        SurfaceOptions { voxel: 0.02, trim_m: 0.5, band_vox: 2.0, dilate_vox: 6, smooth_iters: 14, smooth_lambda: 0.6, hole_reach: 0.55 }
    }
}

// ---------------------------------------------------------------------------
// Sparse cell field
// ---------------------------------------------------------------------------

const MISSING: u32 = u32::MAX;

#[inline]
fn cell_key(i: i32, j: i32, k: i32) -> u64 {
    // 20 bits per axis (offset by 2^19); bits 0-1 free for edge/vertex tag.
    (((i + (1 << 19)) as u64 & 0xFFFFF) << 42)
        | (((j + (1 << 19)) as u64 & 0xFFFFF) << 22)
        | (((k + (1 << 19)) as u64 & 0xFFFFF) << 2)
}

#[inline]
fn key_coords(key: u64) -> (i32, i32, i32) {
    (
        ((key >> 42) & 0xFFFFF) as i32 - (1 << 19),
        ((key >> 22) & 0xFFFFF) as i32 - (1 << 19),
        ((key >> 2) & 0xFFFFF) as i32 - (1 << 19),
    )
}

/// Neighbour order: +x, -x, +y, -y, +z, -z.
const NEIGHBORS: [(i32, i32, i32); 6] = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)];

/// Sparse implicit-function field over cell centres.
struct Field {
    /// Cell keys in insertion order (occupied, then dilation rounds).
    keys: Vec<u64>,
    idx: FxHashMap<u64, u32>,
    /// Current implicit value (positive = free-space side).
    val: Vec<f32>,
    /// Observed point-to-plane value (band cells only).
    obs: Vec<f32>,
    /// Observation weight.
    w: Vec<f32>,
    /// Distance to nearest measured sample (seeded on band cells, propagated).
    sup: Vec<f32>,
    /// Weighted-average colour (linear-ish sRGB space kept as f32 of 0-255).
    col: Vec<[f32; 3]>,
    /// Neighbour indices [+x,-x,+y,-y,+z,-z] built once.
    nbr: Vec<[u32; 6]>,
    /// Per-cell implicit gradient (central differences).
    grad: Vec<V3>,
}

impl Field {
    fn insert(&mut self, key: u64, val: f32, obs: f32, w: f32, sup: f32, col: [f32; 3]) -> u32 {
        let i = self.keys.len() as u32;
        self.keys.push(key);
        self.idx.insert(key, i);
        self.val.push(val);
        self.obs.push(obs);
        self.w.push(w);
        self.sup.push(sup);
        self.col.push(col);
        i
    }

    #[inline]
    fn get(&self, i: i32, j: i32, k: i32) -> u32 {
        self.idx.get(&cell_key(i, j, k)).copied().unwrap_or(MISSING)
    }

    fn build_neighbors(&mut self) {
        let n = self.keys.len();
        self.nbr = vec![[MISSING; 6]; n];
        let (idx, keys) = (&self.idx, &self.keys);
        self.nbr.par_iter_mut().enumerate().for_each(|(i, nb)| {
            let (x, y, z) = key_coords(keys[i]);
            for (d, &(dx, dy, dz)) in NEIGHBORS.iter().enumerate() {
                nb[d] = idx.get(&cell_key(x + dx, y + dy, z + dz)).copied().unwrap_or(MISSING);
            }
        });
    }
}

// ---------------------------------------------------------------------------
// Normals fallback (until W2's normals land upstream)
// ---------------------------------------------------------------------------

/// Jacobi eigendecomposition of a symmetric 3x3 matrix. Returns eigenvalues and
/// eigenvectors (as columns of `v`). Deterministic.
#[allow(clippy::needless_range_loop)]
fn sym_eigen3(a: [[f32; 3]; 3]) -> ([f32; 3], [[f32; 3]; 3]) {
    let mut m = a;
    let mut v = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
    for _ in 0..24 {
        // largest off-diagonal
        let (mut p, mut q, mut mx) = (0usize, 1usize, 0.0f32);
        for i in 0..3 {
            for j in i + 1..3 {
                if m[i][j].abs() > mx {
                    mx = m[i][j].abs();
                    p = i;
                    q = j;
                }
            }
        }
        if mx < 1e-10 {
            break;
        }
        let app = m[p][p];
        let aqq = m[q][q];
        let apq = m[p][q];
        let theta = 0.5 * (aqq - app) / apq;
        let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
        let c = 1.0 / (t * t + 1.0).sqrt();
        let s = t * c;
        for i in 0..3 {
            let (mip, miq) = (m[i][p], m[i][q]);
            m[i][p] = c * mip - s * miq;
            m[i][q] = s * mip + c * miq;
        }
        for j in 0..3 {
            let (mpj, mqj) = (m[p][j], m[q][j]);
            m[p][j] = c * mpj - s * mqj;
            m[q][j] = s * mpj + c * mqj;
        }
        for i in 0..3 {
            let (vip, viq) = (v[i][p], v[i][q]);
            v[i][p] = c * vip - s * viq;
            v[i][q] = s * vip + c * viq;
        }
    }
    ([m[0][0], m[1][1], m[2][2]], v)
}

/// Local-PCA normals oriented toward the cloud centroid. Fallback only; W2's
/// oriented normals take precedence when present on the cloud.
fn fallback_normals(cloud: &PointCloud, grid: &Grid, k: usize, max_r: f32) -> Vec<V3> {
    let b = cloud.bounds();
    let ctr = b.center();
    let pts = grid.points();
    cloud
        .pos
        .par_iter()
        .map_init(Vec::new, |scratch, &p| {
            scratch.clear();
            grid.knn(p, k.max(4), max_r, scratch);
            let mut mean = [0.0f32; 3];
            for &(i, _) in scratch.iter() {
                mean = add(mean, pts[i as usize]);
            }
            let inv = 1.0 / scratch.len().max(1) as f32;
            mean = scale(mean, inv);
            let mut c = [[0.0f32; 3]; 3];
            for &(i, _) in scratch.iter() {
                let d = sub(pts[i as usize], mean);
                for r in 0..3 {
                    for s in 0..3 {
                        c[r][s] += d[r] * d[s];
                    }
                }
            }
            let (eval, evec) = sym_eigen3(c);
            let mut lo = 0usize;
            for i in 1..3 {
                if eval[i] < eval[lo] {
                    lo = i;
                }
            }
            let mut n = [evec[0][lo], evec[1][lo], evec[2][lo]];
            if dot(n, n) < 1e-8 || scratch.len() < 3 {
                n = normalize(sub(ctr, p));
            }
            // Orient toward the cloud centroid (room-interior convention).
            if dot(n, sub(ctr, p)) < 0.0 {
                n = scale(n, -1.0);
            }
            normalize(n)
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Cell marking + dilation
// ---------------------------------------------------------------------------

/// Breadth-first dilation of the occupied-cell set. Returns `(keys, rounds)`
/// where `rounds[i]` is the BFS shell the cell was added at: 0 means occupied,
/// up to `band_rounds` is the narrow band receiving real observations, and
/// beyond that is extrapolated fill (Laplacian-bridged).
/// Keys are emitted in non-decreasing round order, so every fill cell's BFS
/// parent has a smaller index, making round-wise initialisation well-defined.
fn mark_cells(cloud: &PointCloud, voxel: f32, _band_rounds: usize, total_rounds: usize) -> (Vec<u64>, Vec<u8>) {
    let inv = 1.0 / voxel;
    let mut occ: Vec<u64> = cloud
        .pos
        .par_iter()
        .map(|p| cell_key((p[0] * inv).floor() as i32, (p[1] * inv).floor() as i32, (p[2] * inv).floor() as i32))
        .collect();
    occ.par_sort_unstable();
    occ.dedup();

    let mut keys: Vec<u64> = occ.clone();
    let mut rounds: Vec<u8> = vec![0u8; occ.len()];
    let mut in_set: rustc_hash::FxHashSet<u64> = occ.iter().copied().collect();
    let mut frontier = occ;
    for r in 1..=total_rounds {
        let mut next: Vec<u64> = frontier
            .par_iter()
            .flat_map_iter(|&k| {
                let (i, j, l) = key_coords(k);
                NEIGHBORS.iter().map(move |&(dx, dy, dz)| cell_key(i + dx, j + dy, l + dz))
            })
            .collect();
        next.par_sort_unstable();
        next.dedup();
        next.retain(|k| !in_set.contains(k));
        if next.is_empty() {
            break;
        }
        keys.extend_from_slice(&next);
        rounds.extend(std::iter::repeat_n(r as u8, next.len()));
        in_set.extend(next.iter().copied());
        frontier = next;
    }
    (keys, rounds)
}

/// Bracket test: a cell is "bracketed" if, along at least one axis, observed
/// band cells exist within `reach` cells in BOTH directions — i.e. it lies in
/// an observed gap (hole, crack) rather than in free space outside the measured
/// set. Line-sweep per axis over cells sorted into (line, offset) order.
fn bracket_mask(keys: &[u64], rounds: &[u8], band_rounds: usize, reach: i32) -> Vec<bool> {
    const OFF: i32 = 1 << 19;
    let n = keys.len();
    let is_band = |i: usize| (rounds[i] as usize) <= band_rounds;
    let brack: Vec<std::sync::atomic::AtomicU8> =
        (0..n).map(|i| std::sync::atomic::AtomicU8::new(is_band(i) as u8)).collect();
    let bk = &brack;
    for axis in 0..3usize {
        // sort cell indices into (other-coords, axis-coord) order
        let mut order: Vec<u32> = (0..n as u32).collect();
        order.par_sort_unstable_by_key(|&ix| {
            let (i, j, k) = key_coords(keys[ix as usize]);
            let (a, b, c) = match axis {
                0 => (j, k, i),
                1 => (i, k, j),
                _ => (i, j, k),
            };
            (((a + OFF) as u64) << 44) | (((b + OFF) as u64) << 24) | (((c + OFF) as u64) << 4)
        });
        // find line spans (runs of equal (a,b))
        let mut spans: Vec<(usize, usize)> = Vec::new();
        let mut s = 0usize;
        let lab = |ix: u32| {
            let (i, j, k) = key_coords(keys[ix as usize]);
            match axis {
                0 => (j, k, i),
                1 => (i, k, j),
                _ => (i, j, k),
            }
        };
        for e in 1..n {
            let (a0, b0, _) = lab(order[e - 1]);
            let (a1, b1, _) = lab(order[e]);
            if (a0, b0) != (a1, b1) {
                spans.push((s, e));
                s = e;
            }
        }
        spans.push((s, n));
        // per span: for each fill cell check nearest band cell before/after
        spans.par_iter().for_each(|&(lo, hi)| {
            // collect band positions
            let mut band_pos: Vec<i32> = Vec::with_capacity(hi - lo);
            for &e in &order[lo..hi] {
                if is_band(e as usize) {
                    band_pos.push(lab(e).2);
                }
            }
            if band_pos.len() < 2 {
                return;
            }
            for &e in &order[lo..hi] {
                let ix = e as usize;
                if is_band(ix) || bk[ix].load(std::sync::atomic::Ordering::Relaxed) != 0 {
                    continue;
                }
                let c = lab(e).2;
                let p = band_pos.partition_point(|&b| b < c);
                let before = p > 0 && c - band_pos[p - 1] <= reach;
                let after = p < band_pos.len() && band_pos[p] - c <= reach;
                if before && after {
                    bk[ix].store(1, std::sync::atomic::Ordering::Relaxed);
                }
            }
        });
    }
    brack
        .into_iter()
        .map(|b| b.into_inner() != 0)
        .collect()
}

// ---------------------------------------------------------------------------
// Observations
// ---------------------------------------------------------------------------

struct Obs {
    val: f32,
    w: f32,
    sup: f32,
    col: [f32; 3],
}

/// Weighted point-to-plane implicit estimate for one cell centre.
fn observe(
    center: V3,
    grid: &Grid,
    cloud: &PointCloud,
    nrm: &[V3],
    dens: &[f32],
    r: f32,
    scratch: &mut Vec<u32>,
) -> Obs {
    grid.radius(center, r, scratch);
    let mut wsum = 0.0f32;
    let mut vsum = 0.0f32;
    let mut msum = 0.0f32;
    let mut csum = [0.0f32; 3];
    let mut sup = f32::INFINITY;
    let inv_r = 1.0 / r;
    for &i in scratch.iter() {
        let p = cloud.pos[i as usize];
        let d = sub(center, p);
        let t = length(d) * inv_r;
        if t >= 1.0 {
            continue;
        }
        let pw = cloud.weight(i as usize) * dens[i as usize];
        let w = pw * (1.0 - t * t) * (1.0 - t * t);
        if w <= 0.0 {
            continue;
        }
        let v = dot(d, nrm[i as usize]);
        vsum += w * v;
        msum += w * v.abs();
        wsum += w;
        if !cloud.col.is_empty() {
            let c = cloud.col[i as usize];
            for k in 0..3 {
                csum[k] += w * c[k] as f32;
            }
        }
        if cloud.flag(i as usize) & FLAG_INTERPOLATED == 0 {
            sup = sup.min(length(d));
        }
    }
    if wsum <= 0.0 {
        return Obs { val: 0.0, w: 0.0, sup, col: [0.0; 3] };
    }
    // Sign consensus: |signed sum| / magnitude sum in [0,1]. Contributors with
    // disagreeing normals cancel — those cells carry unreliable sign, so we
    // down-weight (or drop) them and let the smooth field erase the pocket
    // instead of growing a parallel membrane off the true surface.
    let consensus = vsum.abs() / (msum + 1e-9);
    if consensus < 0.30 {
        return Obs { val: 0.0, w: 0.0, sup, col: [0.0; 3] };
    }
    let inv = 1.0 / wsum;
    Obs { val: vsum * inv, w: wsum * consensus, sup, col: [csum[0] * inv, csum[1] * inv, csum[2] * inv] }
}

// ---------------------------------------------------------------------------
// Polygonisation (face-walk marching cubes, asymptotic decider)
// ---------------------------------------------------------------------------

/// Cube edges as (axis, corner_a, corner_b). Corner index bit0=x, bit1=y, bit2=z.
/// Edge id = axis*4 + off, off bit0 = lower of the other two axes, bit1 = higher.
const CUBE_EDGES: [(u8, u8); 12] = [
    (0, 1), (2, 3), (4, 5), (6, 7), // x-edges (off = y + 2z)
    (0, 2), (1, 3), (4, 6), (5, 7), // y-edges (off = x + 2z)
    (0, 4), (1, 5), (2, 6), (3, 7), // z-edges (off = x + 2y)
];

/// For each face (axis*2+side): the 4 edges lying on it.
/// Face order: x=0 face, x=1 face, y=0, y=1, z=0, z=1.
const FACE_EDGES: [[u8; 4]; 6] = [
    [4, 6, 8, 10], // x=0: y-edges off{0,2}, z-edges off{0,2}
    [5, 7, 9, 11], // x=1
    [0, 2, 8, 9],  // y=0
    [1, 3, 10, 11], // y=1
    [0, 1, 4, 5],  // z=0
    [2, 3, 6, 7],  // z=1
];

/// For each face (same order): its 4 corners.
const FACE_CORNERS: [[u8; 4]; 6] = [
    [0, 2, 4, 6],
    [1, 3, 5, 7],
    [0, 1, 4, 5],
    [2, 3, 6, 7],
    [0, 1, 2, 3],
    [4, 5, 6, 7],
];

/// The cube edge along `axis` that touches corner `c`.
#[inline]
fn edge_touching(c: u8, axis: usize) -> u8 {
    let b = |bit: usize| (c >> bit) & 1;
    let off = match axis {
        0 => b(1) + 2 * b(2),
        1 => b(0) + 2 * b(2),
        _ => b(0) + 2 * b(1),
    };
    axis as u8 * 4 + off
}

/// Canonical grid-edge key for a cube edge: the shared edge between the two
/// corner cells, tagged with its axis in bits 0-1 (cell keys keep those = 0).
#[inline]
fn edge_key(cell_a_key: u64, axis: u8) -> u64 {
    cell_a_key | axis as u64
}

/// A fan-centre vertex emitted by one cube (not shared with neighbours).
struct Centroid {
    key: u64,
    pos: V3,
    nrm: V3,
    col: [f32; 3],
    sup: f32,
}

/// Extract one cube's triangles. `corner` = the 8 corner cell indices
/// (u32, must all be < MISSING), `corner_key` = the 8 corner cell keys.
/// Emits (keyA,keyB,keyC) vertex-key triples and centroid vertex records.
#[allow(clippy::too_many_arguments)]
fn polygonise_cube(
    field: &Field,
    brack: &[bool],
    corner: &[u32; 8],
    corner_key: &[u64; 8],
    min_cell_key: u64,
    voxel: f32,
    tris: &mut Vec<(u64, u64, u64)>,
    centroids: &mut Vec<Centroid>,
) {
    let mut s = [0.0f32; 8];
    let mut inside = [false; 8];
    let mut n_in = 0usize;
    for i in 0..8 {
        s[i] = field.val[corner[i] as usize];
        inside[i] = s[i] < 0.0;
        n_in += inside[i] as usize;
    }
    if n_in == 0 || n_in == 8 {
        return;
    }

    // Crossing edges -> vertex keys + interpolated positions/normals.
    let mut ekey = [0u64; 12];
    let mut epos = [[0.0f32; 3]; 12];
    let mut ecol = [[0.0f32; 3]; 12];
    let mut esup = [0.0f32; 12];
    let mut enrm = [[0.0f32; 3]; 12];
    let mut has_vert = [false; 12];
    // eanch[e] = the edge's two endpoint cells are both bracketed: its
    // vertex sits on observation-anchored surface, not on an extrapolated
    // skirt boundary.
    let mut eanch = [false; 12];
    for (e, &(a, b)) in CUBE_EDGES.iter().enumerate() {
        if inside[a as usize] == inside[b as usize] {
            continue;
        }
        has_vert[e] = true;
        eanch[e] = brack[corner[a as usize] as usize] && brack[corner[b as usize] as usize];
        let (ia, ib) = (corner[a as usize] as usize, corner[b as usize] as usize);
        let (sa, sb) = (s[a as usize], s[b as usize]);
        let t = (sa / (sa - sb)).clamp(0.0, 1.0);
        let (ka, kb) = (key_coords(corner_key[a as usize]), key_coords(corner_key[b as usize]));
        let axis = (e / 4) as u8;
        // canonical grid edge = the lower-index corner cell + axis tag
        ekey[e] = edge_key(corner_key[a as usize], axis);
        let (pa, pb) = (
            [ka.0 as f32 + 0.5, ka.1 as f32 + 0.5, ka.2 as f32 + 0.5],
            [kb.0 as f32 + 0.5, kb.1 as f32 + 0.5, kb.2 as f32 + 0.5],
        );
        epos[e] = lerp(pa, pb, t);
        ecol[e] = lerp(field.col[ia], field.col[ib], t);
        esup[e] = field.sup[ia] + (field.sup[ib] - field.sup[ia]) * t;
        enrm[e] = normalize(lerp(field.grad[ia], field.grad[ib], t));
    }

    // Build segments from face connectivity (with asymptotic decider).
    // adjacency[e] = up to 2 neighbouring edge-vertices.
    let mut adj: [[u8; 2]; 12] = [[0xFF; 2]; 12];
    let mut link = |a: u8, b: u8| {
        for (x, y) in [(a, b), (b, a)] {
            let slot = &mut adj[x as usize];
            if slot[0] == 0xFF {
                slot[0] = y;
            } else if slot[0] != y {
                slot[1] = y;
            }
        }
    };
    for f in 0..6 {
        let mut cross = [0u8; 4];
        let mut nc = 0usize;
        for &e in FACE_EDGES[f].iter() {
            if has_vert[e as usize] {
                cross[nc] = e;
                nc += 1;
            }
        }
        if nc == 2 {
            link(cross[0], cross[1]);
        } else if nc == 4 {
            // ambiguous: pair around corners whose sign differs from the
            // face-centre sign (asymptotic decider).
            let center: f32 = FACE_CORNERS[f].iter().map(|&c| s[c as usize]).sum::<f32>();
            let center_in = center < 0.0;
            let axis = f / 2;
            let (a1, a2) = match axis {
                0 => (1usize, 2usize),
                1 => (0usize, 2usize),
                _ => (0usize, 1usize),
            };
            for &c in FACE_CORNERS[f].iter() {
                if inside[c as usize] != center_in {
                    link(edge_touching(c, a1), edge_touching(c, a2));
                }
            }
        }
    }

    // Walk closed loops of edge-vertices.
    let mut done = [false; 12];
    let mut loop_no = 0u64;
    for start in 0..12usize {
        if !has_vert[start] || done[start] {
            continue;
        }
        let mut loop_edges: Vec<u8> = Vec::with_capacity(12);
        let (mut cur, mut prev) = (start as u8, 0xFFu8);
        loop {
            loop_edges.push(cur);
            done[cur as usize] = true;
            let [n0, n1] = adj[cur as usize];
            let nxt = if n0 != 0xFF && n0 != prev { n0 } else { n1 };
            if nxt == 0xFF || nxt == start as u8 {
                break;
            }
            prev = cur;
            cur = nxt;
            if loop_edges.len() > 12 {
                break; // malformed; bail out of this loop
            }
        }
        if loop_edges.len() < 3 {
            continue;
        }

        // Orientation: Newell normal vs mean interpolated SDF gradient.
        let mut newell = [0.0f32; 3];
        let mut gn = [0.0f32; 3];
        for (i, &e) in loop_edges.iter().enumerate() {
            let p = epos[e as usize];
            let q = epos[loop_edges[(i + 1) % loop_edges.len()] as usize];
            newell[0] += (p[1] - q[1]) * (p[2] + q[2]);
            newell[1] += (p[2] - q[2]) * (p[0] + q[0]);
            newell[2] += (p[0] - q[0]) * (p[1] + q[1]);
            gn = add(gn, enrm[e as usize]);
        }
        if dot(newell, gn) < 0.0 {
            loop_edges.reverse();
        }

        // Centroid vertex (unique per cube AND per loop: tag 3 in bits 0-1,
        // loop index in bits 62-63 — a cube can yield several disjoint loops).
        let ckey = min_cell_key | 3u64 | (loop_no << 62);
        loop_no += 1;
        let mut cp = [0.0f32; 3];
        let mut cc = [0.0f32; 3];
        let mut cn = [0.0f32; 3];
        let mut cs = 0.0f32;
        for &e in loop_edges.iter() {
            cp = add(cp, epos[e as usize]);
            cc = add(cc, ecol[e as usize]);
            cn = add(cn, enrm[e as usize]);
            cs += esup[e as usize];
        }
        let m = 1.0 / loop_edges.len() as f32;
        centroids.push(Centroid {
            key: ckey,
            pos: cp.map(|v| v * m * voxel), // epos is in cell units
            nrm: normalize(cn),
            col: cc.map(|v| v * m),
            sup: cs * m,
        });

        for i in 0..loop_edges.len() {
            let e0 = loop_edges[i];
            let e1 = loop_edges[(i + 1) % loop_edges.len()];
            // Pure-skirt fan tri: both edge vertices live on unbracketed
            // cells — drop it (rim closure keeps at least one anchored edge).
            if !eanch[e0 as usize] && !eanch[e1 as usize] {
                continue;
            }
            tris.push((ckey, ekey[e0 as usize], ekey[e1 as usize]));
        }
    }
}

#[cfg(test)]
fn debug_check_cube(
    field: &Field,
    corner: &[u32; 8],
    corner_key: &[u64; 8],
    cube_i: usize,
) {
    // every cube edge's canonical key must decode to the cell the edge belongs to
    for (e, &(a, b)) in CUBE_EDGES.iter().enumerate() {
        let axis = (e / 4) as u8;
        let ek = edge_key(corner_key[a as usize], axis);
        let (ei, ej, el) = key_coords(ek & !3u64);
        let (ci, cj, cl) = key_coords(corner_key[0]);
        let (ca, cb, cc) = key_coords(corner_key[a as usize]);
        let exp_i = ci + (a & 1) as i32;
        let exp_j = cj + ((a >> 1) & 1) as i32;
        let exp_l = cl + ((a >> 2) & 1) as i32;
        if (ei, ej, el) != (ca, cb, cc) || (ca, cb, cc) != (exp_i, exp_j, exp_l) {
            eprintln!(
                "cube {cube_i} edge {e} axis {axis}: corner {a}/{b} key->({ca},{cb},{cc}) expect ({exp_i},{exp_j},{exp_l}) ekey decodes ({ei},{ej},{el}) val {:?}",
                [field.val[corner[a as usize] as usize], field.val[corner[b as usize] as usize]]
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Main entry
// ---------------------------------------------------------------------------

pub fn reconstruct(cloud: &PointCloud, opts: &SurfaceOptions) -> (Mesh, Stats) {
    let t0 = std::time::Instant::now();
    let mesh = Mesh::default();
    if cloud.is_empty() {
        return (mesh, serde_json::json!({ "empty": true }));
    }
    let voxel = opts.voxel.max(1e-3);
    let band_rounds = opts.band_vox.max(1.0).round() as usize;
    let total_rounds = band_rounds + opts.dilate_vox;
    let obs_r = opts.band_vox.max(1.0) * voxel;

    // --- normals (fallback if upstream hasn't provided them) ---
    let grid_cell = obs_r.max(voxel * 1.5);
    let grid = Grid::build(&cloud.pos, grid_cell);
    // Per-point density weight: isolated points (noise, grazing-angle spray)
    // build spurious mini-surfaces — membranes offset from the real sheet.
    // Scale their observation weight by local support.
    let dens_r = (voxel * 2.0).max(0.03);
    let dens: Vec<f32> = cloud
        .pos
        .par_iter()
        .map_init(Vec::new, |scratch, &p| {
            scratch.clear();
            grid.radius(p, dens_r, scratch);
            let c = scratch.len() as f32 - 1.0;
            (c / 8.0).clamp(0.0, 1.0)
        })
        .collect();
    let nrm_fallback: Vec<V3>;
    let nrm: &[V3] = if cloud.has_normals() {
        &cloud.nrm
    } else {
        let est_r = (voxel * 4.0).max(obs_r);
        nrm_fallback = fallback_normals(cloud, &grid, 16, est_r);
        &nrm_fallback
    };

    // --- mark + dilate cells ---
    let (keys, rounds) = mark_cells(cloud, voxel, band_rounds, total_rounds);
    let n_band = rounds.iter().filter(|&&r| r as usize <= band_rounds).count();
    let reach = (opts.hole_reach / voxel).ceil() as i32;
    let brack = bracket_mask(&keys, &rounds, band_rounds, reach);
    let is_band: Vec<bool> = rounds.iter().map(|&r| r as usize <= band_rounds).collect();

    // --- observe band cells ---
    let mut field = Field {
        keys: Vec::with_capacity(keys.len()),
        idx: FxHashMap::with_capacity_and_hasher(keys.len(), Default::default()),
        val: Vec::with_capacity(keys.len()),
        obs: Vec::with_capacity(keys.len()),
        w: Vec::with_capacity(keys.len()),
        sup: Vec::with_capacity(keys.len()),
        col: Vec::with_capacity(keys.len()),
        nbr: Vec::new(),
        grad: Vec::new(),
    };
    let obs: Vec<Obs> = keys
        .par_iter()
        .zip(rounds.par_iter())
        .map_init(Vec::new, |scratch, (&key, &round)| {
            let (i, j, k) = key_coords(key);
            let center = [(i as f32 + 0.5) * voxel, (j as f32 + 0.5) * voxel, (k as f32 + 0.5) * voxel];
            if round as usize <= band_rounds {
                observe(center, &grid, cloud, nrm, &dens, obs_r, scratch)
            } else {
                Obs { val: 0.0, w: 0.0, sup: f32::INFINITY, col: [0.0; 3] }
            }
        })
        .collect();
    for (&key, o) in keys.iter().zip(obs) {
        field.insert(key, o.val, o.val, o.w, o.sup, o.col);
    }
    field.build_neighbors();
    let t_field = t0.elapsed().as_secs_f64() * 1e3;

    // --- round-wise fill initialisation: each extrapolated cell takes the mean
    // of strictly-earlier neighbours (its BFS parents), so the zero level is
    // extended into unobserved space in one pass rather than ~1 cell/iter.
    {
        let mut start = 0usize;
        let mut r = 0usize;
        let mut bounds: Vec<(usize, usize)> = Vec::new();
        for (i, &rd) in rounds.iter().enumerate() {
            if rd as usize != r {
                bounds.push((start, i));
                start = i;
                r = rd as usize;
            }
        }
        bounds.push((start, rounds.len()));
        for &(s, _e) in bounds.iter().skip(band_rounds + 1) {
            // cells in [s,e) read only indices < s (earlier rounds are final),
            // so split borrows are race-free.
            let (val_done, val_todo) = field.val.split_at_mut(s);
            let (sup_done, sup_todo) = field.sup.split_at_mut(s);
            let (col_done, col_todo) = field.col.split_at_mut(s);
            let nbr = &field.nbr;
            val_todo
                .par_iter_mut()
                .zip(sup_todo.par_iter_mut())
                .zip(col_todo.par_iter_mut())
                .enumerate()
                .for_each(|(ii, ((v, sp), c))| {
                    let i = s + ii;
                    let mut acc = 0.0f32;
                    let mut cnt = 0u32;
                    let mut smin = f32::INFINITY;
                    let mut cacc = [0.0f32; 3];
                    for &nb in nbr[i].iter() {
                        if nb != MISSING && (nb as usize) < s {
                            acc += val_done[nb as usize];
                            cacc = add(cacc, col_done[nb as usize]);
                            smin = smin.min(sup_done[nb as usize]);
                            cnt += 1;
                        }
                    }
                    if cnt > 0 {
                        *v = acc / cnt as f32;
                        *sp = smin + voxel;
                        *c = cacc.map(|x| x / cnt as f32);
                    }
                });
        }
    }

    // --- smoothing: Jacobi screened-Poisson + support propagation ---
    let lam = opts.smooth_lambda;
    let mut next: Vec<f32> = vec![0.0; field.keys.len()];
    let mut next_sup: Vec<f32> = vec![0.0; field.keys.len()];
    for _ in 0..opts.smooth_iters {
        next
            .par_iter_mut()
            .enumerate()
            .for_each(|(i, out)| {
                let mut acc = 0.0f32;
                let mut cnt = 0u32;
                for &nb in field.nbr[i].iter() {
                    if nb != MISSING {
                        acc += field.val[nb as usize];
                        cnt += 1;
                    }
                }
                let mean = if cnt > 0 { acc / cnt as f32 } else { field.val[i] };
                let w = field.w[i];
                *out = if w > 0.0 {
                    (w * field.obs[i] + lam * mean) / (w + lam)
                } else {
                    mean
                };
            });
        std::mem::swap(&mut field.val, &mut next);
        next_sup
            .par_iter_mut()
            .enumerate()
            .for_each(|(i, out)| {
                let mut s = field.sup[i];
                for &nb in field.nbr[i].iter() {
                    if nb != MISSING {
                        s = s.min(field.sup[nb as usize] + voxel);
                    }
                }
                *out = s;
            });
        std::mem::swap(&mut field.sup, &mut next_sup);
    }

    // --- gradients ---
    field.grad = field
        .keys
        .par_iter()
        .enumerate()
        .map(|(i, _)| {
            let nb = field.nbr[i];
            let mut g = [0.0f32; 3];
            for ax in 0..3 {
                let (lo, hi) = (nb[ax * 2 + 1], nb[ax * 2]);
                let a = if lo != MISSING { field.val[lo as usize] } else { field.val[i] };
                let b = if hi != MISSING { field.val[hi as usize] } else { field.val[i] };
                g[ax] = (b - a) / (2.0 * voxel);
            }
            g
        })
        .collect();
    let t_smooth = t0.elapsed().as_secs_f64() * 1e3;

    // --- enumerate active edges (sign-crossing, defined endpoints) ---
    let edge_keys: Vec<u64> = field
        .keys
        .par_iter()
        .enumerate()
        .flat_map_iter(|(i, &key)| {
            let mut out = [0u64; 3];
            let mut n = 0;
            for ax in 0..3u8 {
                let nb = field.nbr[i][ax as usize * 2]; // +axis neighbour
                if nb != MISSING && (field.val[i] < 0.0) != (field.val[nb as usize] < 0.0) {
                    out[n] = edge_key(key, ax);
                    n += 1;
                }
            }
            out[..n].to_vec()
        })
        .collect();
    let mut vmap: FxHashMap<u64, u32> = FxHashMap::with_capacity_and_hasher(edge_keys.len(), Default::default());
    for (i, &k) in edge_keys.iter().enumerate() {
        vmap.insert(k, i as u32);
    }
    let n_edge_verts = edge_keys.len();

    // --- edge-vertex attributes ---
    let verts: Vec<(V3, V3, [f32; 3], f32)> = edge_keys
        .par_iter()
        .map(|&ek| {
            let (i, j, k) = key_coords(ek & !3u64);
            let ax = (ek & 3) as usize;
            let a = field.get(i, j, k);
            let (bi, bj, bk) = match ax {
                0 => (i + 1, j, k),
                1 => (i, j + 1, k),
                _ => (i, j, k + 1),
            };
            let b = field.get(bi, bj, bk);
            let (ia, ib) = (a as usize, b as usize);
            let (sa, sb) = (field.val[ia], field.val[ib]);
            let t = (sa / (sa - sb)).clamp(0.0, 1.0);
            let pa = [(i as f32 + 0.5) * voxel, (j as f32 + 0.5) * voxel, (k as f32 + 0.5) * voxel];
            let pb = [(bi as f32 + 0.5) * voxel, (bj as f32 + 0.5) * voxel, (bk as f32 + 0.5) * voxel];
            let pos = lerp(pa, pb, t);
            let nrm = normalize(lerp(field.grad[ia], field.grad[ib], t));
            let col = lerp(field.col[ia], field.col[ib], t);
            let sup = field.sup[ia] + (field.sup[ib] - field.sup[ia]) * t;
            (pos, nrm, col, sup)
        })
        .collect();

    // --- cubes ---
    struct CubeOut {
        tris: Vec<(u64, u64, u64)>,
        centroids: Vec<Centroid>,
    }
    let outs: Vec<CubeOut> = field
        .keys
        .par_iter()
        .enumerate()
        .map(|(i, &key)| {
            let mut out = CubeOut { tris: Vec::new(), centroids: Vec::new() };
            // gather the 7 other corners via the neighbour table
            let i100 = field.nbr[i][0];
            let i010 = field.nbr[i][2];
            let i001 = field.nbr[i][4];
            if i100 == MISSING || i010 == MISSING || i001 == MISSING {
                return out;
            }
            let i110 = field.nbr[i100 as usize][2];
            let i101 = field.nbr[i100 as usize][4];
            let i011 = field.nbr[i010 as usize][4];
            if i110 == MISSING || i101 == MISSING || i011 == MISSING {
                return out;
            }
            let i111 = field.nbr[i110 as usize][4];
            if i111 == MISSING {
                return out;
            }
            let corner = [i as u32, i100, i010, i110, i001, i101, i011, i111];
            // Skip only cubes that are purely extrapolated with no observed
            // anchor: a cube inside one-sided fill produces a phantom skirt,
            // while cubes at the band rim are needed to close the surface.
            let has_unbr = corner.iter().any(|&c| !brack[c as usize]);
            let has_band = corner.iter().any(|&c| is_band[c as usize]);
            if has_unbr && !has_band {
                return out;
            }
            let corner_key = [
                key,
                field.keys[i100 as usize],
                field.keys[i010 as usize],
                field.keys[i110 as usize],
                field.keys[i001 as usize],
                field.keys[i101 as usize],
                field.keys[i011 as usize],
                field.keys[i111 as usize],
            ];
            polygonise_cube(&field, &brack, &corner, &corner_key, key, voxel, &mut out.tris, &mut out.centroids);
            #[cfg(test)]
            debug_check_cube(&field, &corner, &corner_key, i);
            out
        })
        .collect();

    // --- weld centroid vertices, remap triangle keys to indices ---
    let mut cids: FxHashMap<u64, u32> =
        FxHashMap::with_capacity_and_hasher(outs.iter().map(|o| o.centroids.len()).sum(), Default::default());
    let mut ncentroids = 0usize;
    for out in outs.iter() {
        for c in out.centroids.iter() {
            if let std::collections::hash_map::Entry::Vacant(e) = cids.entry(c.key) {
                e.insert((n_edge_verts + ncentroids) as u32);
                ncentroids += 1;
            }
        }
    }
    let tri_keys: Vec<(u64, u64, u64)> = outs.iter().flat_map(|o| o.tris.iter().copied()).collect();
    let tris_emitted = tri_keys.len();
    // remap vertex keys -> indices in parallel over the read-only maps
    let tri_idx: Vec<u32> = tri_keys
        .par_iter()
        .flat_map_iter(|&(a, b, c)| {
            let ia = *vmap.get(&a).unwrap_or_else(|| cids.get(&a).unwrap());
            let ib = *vmap.get(&b).unwrap_or_else(|| cids.get(&b).unwrap());
            let ic = *vmap.get(&c).unwrap_or_else(|| cids.get(&c).unwrap());
            [ia, ib, ic]
        })
        .collect();

    // --- assemble vertex arrays ---
    let nverts = n_edge_verts + ncentroids;
    let mut pos: Vec<V3> = Vec::with_capacity(nverts);
    let mut nrm: Vec<V3> = Vec::with_capacity(nverts);
    let mut col: Vec<[u8; 3]> = Vec::with_capacity(nverts);
    let mut sup: Vec<f32> = Vec::with_capacity(nverts);
    for (p, n, c, s) in verts.iter() {
        pos.push(*p);
        nrm.push(*n);
        col.push([c[0].clamp(0.0, 255.0) as u8, c[1].clamp(0.0, 255.0) as u8, c[2].clamp(0.0, 255.0) as u8]);
        sup.push(*s);
    }
    for out in outs.iter() {
        for c in out.centroids.iter() {
            pos.push(c.pos);
            nrm.push(c.nrm);
            col.push([
                c.col[0].clamp(0.0, 255.0) as u8,
                c.col[1].clamp(0.0, 255.0) as u8,
                c.col[2].clamp(0.0, 255.0) as u8,
            ]);
            sup.push(c.sup);
        }
    }

    // --- trim ---
    let trim = opts.trim_m;
    let mut kept = 0usize;
    let mut idx: Vec<u32> = Vec::with_capacity(tri_idx.len());
    for t in tri_idx.chunks_exact(3) {
        let s = (sup[t[0] as usize] + sup[t[1] as usize] + sup[t[2] as usize]) / 3.0;
        if s <= trim {
            idx.extend_from_slice(t);
            kept += 1;
        }
    }

    let mut mesh = Mesh { pos, nrm, col, uv: Vec::new(), support: sup, idx, texture: None };
    if cloud.col.is_empty() {
        mesh.col.clear();
    }
    mesh.validate();

    let stats = serde_json::json!({
        "cells": field.keys.len(),
        "band_cells": n_band,
        "edge_verts": n_edge_verts,
        "centroid_verts": ncentroids,
        "tris_emitted": tris_emitted,
        "tris_kept": kept,
        "ms_field": (t_field * 10.0).round() / 10.0,
        "ms_to_smooth_done": (t_smooth * 10.0).round() / 10.0,
        "ms_total": ((t0.elapsed().as_secs_f64() * 1e3) * 10.0).round() / 10.0,
        "fallback_normals": !cloud.has_normals(),
    });
    (mesh, stats)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sphere_cloud(r: f32, n: usize) -> PointCloud {
        let mut cloud = PointCloud::default();
        let mut rng = Rng::new(3);
        for _ in 0..n {
            // fibonacci-ish via rng, then normalise
            let v = normalize([rng.normal(), rng.normal(), rng.normal()]);
            let p = scale(v, r + 0.001 * rng.normal());
            cloud.pos.push(p);
            cloud.nrm.push(normalize(p));
            cloud.col.push([200, 100, 50]);
        }
        cloud
    }

    fn plane_cloud(nx: usize, nz: usize) -> PointCloud {
        let mut cloud = PointCloud::default();
        for i in 0..nx {
            for k in 0..nz {
                cloud.pos.push([i as f32 * 0.01, 0.0, k as f32 * 0.01]);
                cloud.nrm.push([0.0, 1.0, 0.0]);
            }
        }
        cloud
    }

    #[test]
    fn empty_cloud_gives_empty_mesh() {
        let (m, _) = reconstruct(&PointCloud::default(), &SurfaceOptions::default());
        assert_eq!(m.triangles(), 0);
    }

    #[test]
    fn sphere_is_closed_and_near_surface() {
        let cloud = sphere_cloud(0.3, 6000);
        let opts = SurfaceOptions { voxel: 0.02, dilate_vox: 2, ..Default::default() };
        let (m, _) = reconstruct(&cloud, &opts);
        assert!(m.triangles() > 500, "tris {}", m.triangles());
        // all verts near the sphere
        for p in m.pos.iter() {
            let d = (length(*p) - 0.3).abs();
            assert!(d < 0.03, "vert off surface: {d} at {p:?}");
        }
        // closed: every edge shared by exactly 2 tris
        let mut edges: FxHashMap<(u32, u32), u32> = FxHashMap::default();
        for t in m.idx.chunks_exact(3) {
            for e in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
                let k = if e.0 < e.1 { (e.0, e.1) } else { (e.1, e.0) };
                *edges.entry(k).or_default() += 1;
            }
        }
        let open = edges.values().filter(|&&c| c != 2).count();
        assert_eq!(open, 0, "{open} open edges");
    }

    #[test]
    fn plane_stays_flat_and_oriented() {
        let cloud = plane_cloud(40, 40);
        let opts = SurfaceOptions { voxel: 0.02, dilate_vox: 1, ..Default::default() };
        let (m, _) = reconstruct(&cloud, &opts);
        assert!(m.triangles() > 100);
        // vertices lie on cell edges: within ~1.5 voxels of the plane at the
        // patch rim, but ~all of them inside half a voxel near the sheet.
        let mut ys: Vec<f32> = m.pos.iter().map(|p| p[1].abs()).collect();
        ys.sort_by(|a, b| a.total_cmp(b));
        assert!(ys[ys.len() * 95 / 100] < 0.011, "p95 |y| = {}", ys[ys.len() * 95 / 100]);
        // normals point +Y (free-space side given our sdf sign convention)
        let up: f32 = m.nrm.iter().map(|n| n[1]).sum::<f32>() / m.nrm.len() as f32;
        assert!(up > 0.9, "mean up {up}");
    }

    #[test]
    fn bridging_across_small_gap() {
        // two coplanar wall patches separated by a 6cm gap: dilation should close it
        let mut cloud = PointCloud::default();
        for x in -50i32..=50 {
            for y in -30i32..=30 {
                if x.abs() <= 3 {
                    continue; // ~6 cm unobserved gap at x in [-0.03, 0.03]
                }
                cloud.pos.push([x as f32 * 0.01, y as f32 * 0.01, 0.0]);
                cloud.nrm.push([0.0, 0.0, 1.0]);
            }
        }
        let opts = SurfaceOptions { voxel: 0.01, band_vox: 2.0, dilate_vox: 8, ..Default::default() };
        let (m, _) = reconstruct(&cloud, &opts);
        // some triangle vertex should land inside the gap band |x|<0.03, |y|<0.3
        let covered = m.pos.iter().any(|p| p[0].abs() < 0.03 && p[1].abs() < 0.25 && p[2].abs() < 0.01);
        assert!(covered, "gap not bridged");
    }

    #[test]
    fn deterministic_output() {
        let cloud = sphere_cloud(0.3, 3000);
        let opts = SurfaceOptions { voxel: 0.02, dilate_vox: 2, smooth_iters: 3, ..Default::default() };
        let (a, _) = reconstruct(&cloud, &opts);
        let (b, _) = reconstruct(&cloud, &opts);
        assert_eq!(a.pos, b.pos);
        assert_eq!(a.idx, b.idx);
    }

    #[test]
    fn debug_field_slice() {
        let scene = crate::synth::Scene::room();
        let scan = crate::synth::simulate(&scene, &crate::synth::ScanOptions { seed: 7, ..Default::default() });
        let opts = SurfaceOptions::default();
        let voxel = opts.voxel;
        let band_rounds = opts.band_vox.max(1.0).round() as usize;
        let total_rounds = band_rounds + opts.dilate_vox;
        let obs_r = opts.band_vox.max(1.0) * voxel;
        let grid = Grid::build(&scan.cloud.pos, obs_r.max(voxel * 1.5));
        let nrm_fb = fallback_normals(&scan.cloud, &grid, 16, (voxel * 4.0).max(obs_r));
        let nrm: &[V3] = &nrm_fb;
        let (keys, rounds) = mark_cells(&scan.cloud, voxel, band_rounds, total_rounds);
        let mut field = Field {
            keys: Vec::new(), idx: FxHashMap::default(), val: Vec::new(), obs: Vec::new(), w: Vec::new(),
            sup: Vec::new(), col: Vec::new(), nbr: Vec::new(), grad: Vec::new(),
        };
        let obs: Vec<Obs> = keys
            .par_iter()
            .zip(rounds.par_iter())
            .map_init(Vec::new, |scratch, (&key, &round)| {
                let (i, j, k) = key_coords(key);
                let center = [(i as f32 + 0.5) * voxel, (j as f32 + 0.5) * voxel, (k as f32 + 0.5) * voxel];
                if round as usize <= band_rounds {
                    observe(center, &grid, &scan.cloud, nrm, &vec![1.0f32; scan.cloud.len()], obs_r, scratch)
                } else {
                    Obs { val: 0.0, w: 0.0, sup: f32::INFINITY, col: [0.0; 3] }
                }
            })
            .collect();
        for (&key, o) in keys.iter().zip(obs) {
            field.insert(key, o.val, o.val, o.w, o.sup, o.col);
        }
        // slice through the x=-3 wall at the membrane location y=0.35 z=-1.5
        let j = (0.35f32 / voxel) as i32;
        let k = (-1.5f32 / voxel) as i32;
        for i in -165..-135 {
            let ix = field.get(i, j, k);
            if ix == MISSING {
                eprintln!("x={:.3} --", i as f32 * voxel);
                continue;
            }
            let ix = ix as usize;
            eprintln!(
                "x={:.3} val={:+.4} w={:.3} r={} sup={:.3}",
                (i as f32 + 0.5) * voxel,
                field.val[ix],
                field.w[ix],
                rounds[ix],
                field.sup[ix]
            );
        }
    }

    #[test]
    fn debug_synth_repro() {
        let scene = crate::synth::Scene::room();
        let scan = crate::synth::simulate(&scene, &crate::synth::ScanOptions { seed: 7, ..Default::default() });
        let opts = SurfaceOptions::default();
        let (m, s) = reconstruct(&scan.cloud, &opts);
        eprintln!("stats {s}");
        let mut bad = 0;
        for (ti, t) in m.idx.chunks_exact(3).enumerate() {
            let mx = [
                (m.pos[t[1] as usize][0] - m.pos[t[0] as usize][0]).powi(2)
                    + (m.pos[t[1] as usize][1] - m.pos[t[0] as usize][1]).powi(2)
                    + (m.pos[t[1] as usize][2] - m.pos[t[0] as usize][2]).powi(2),
                (m.pos[t[2] as usize][0] - m.pos[t[1] as usize][0]).powi(2)
                    + (m.pos[t[2] as usize][1] - m.pos[t[1] as usize][1]).powi(2)
                    + (m.pos[t[2] as usize][2] - m.pos[t[1] as usize][2]).powi(2),
            ]
            .iter()
            .fold(0.0f32, |a, b| a.max(*b))
            .sqrt();
            if mx > 0.06 {
                if bad < 5 {
                    eprintln!("tri {ti} {:?} len {mx:.3} verts {:?}", t, [m.pos[t[0] as usize], m.pos[t[1] as usize], m.pos[t[2] as usize]]);
                }
                bad += 1;
            }
        }
        eprintln!("bad {bad} of {}", m.triangles());
        // support histogram of tris near the x=-2.89 membrane line
        let mut near = 0;
        let mut sup_hist = [0usize; 8];
        for t in m.idx.chunks_exact(3) {
            let cx = (m.pos[t[0] as usize][0] + m.pos[t[1] as usize][0] + m.pos[t[2] as usize][0]) / 3.0;
            if (cx + 2.89).abs() < 0.05 {
                near += 1;
                let s = (m.support[t[0] as usize] + m.support[t[1] as usize] + m.support[t[2] as usize]) / 3.0;
                let b = ((s / 0.03) as usize).min(7);
                sup_hist[b] += 1;
            }
        }
        eprintln!("membrane tris {near} sup bins {sup_hist:?}");
    }

    #[test]
    fn debug_edge_lengths_large() {
        // large-scale repro: all emitted triangles must have edges < ~3 voxels
        let mut cloud = PointCloud::default();
        for x in -200i32..=200 {
            for y in -100i32..=100 {
                cloud.pos.push([x as f32 * 0.01, y as f32 * 0.01, 0.0]);
                cloud.nrm.push([0.0, 0.0, 1.0]);
            }
        }
        let opts = SurfaceOptions { voxel: 0.01, dilate_vox: 2, smooth_iters: 3, ..Default::default() };
        let (m, s) = reconstruct(&cloud, &opts);
        eprintln!("stats {s}");
        let mut bad = 0;
        for (ti, t) in m.idx.chunks_exact(3).enumerate() {
            let e = [
                (m.pos[t[1] as usize][0] - m.pos[t[0] as usize][0]).powi(2)
                    + (m.pos[t[1] as usize][1] - m.pos[t[0] as usize][1]).powi(2)
                    + (m.pos[t[1] as usize][2] - m.pos[t[0] as usize][2]).powi(2),
                (m.pos[t[2] as usize][0] - m.pos[t[1] as usize][0]).powi(2)
                    + (m.pos[t[2] as usize][1] - m.pos[t[1] as usize][1]).powi(2)
                    + (m.pos[t[2] as usize][2] - m.pos[t[1] as usize][2]).powi(2),
            ];
            let mx = e[0].max(e[1]).sqrt();
            if mx > 0.04 {
                if bad < 5 {
                    eprintln!("tri {ti} {:?} len {mx} verts {:?}", t, [m.pos[t[0] as usize], m.pos[t[1] as usize], m.pos[t[2] as usize]]);
                }
                bad += 1;
            }
        }
        assert_eq!(bad, 0, "{bad} long tris");
    }

    #[test]
    fn dilation_does_not_grow_skirt() {
        // one-sided extrapolated fill (deep dilation off a lone sheet) must
        // never surface: it is not bracketed by observation on both sides
        let cloud = plane_cloud(30, 30);
        let opts = SurfaceOptions { voxel: 0.02, dilate_vox: 10, ..Default::default() };
        let (m, _) = reconstruct(&cloud, &opts);
        for t in m.idx.chunks_exact(3) {
            let cy = (m.pos[t[0] as usize][1] + m.pos[t[1] as usize][1] + m.pos[t[2] as usize][1]) / 3.0;
            assert!(cy.abs() < 0.06, "skirt tri at y {cy}");
        }
    }

    #[test]
    fn trim_removes_deep_fill() {
        // triangles whose mean support exceeds trim_m are dropped even when
        // they sit inside a bracketed gap
        let mut cloud = PointCloud::default();
        // two parallel sheets 30cm apart -> bracketed gap gets filled support
        for x in -50i32..=50 {
            for y in -50i32..=50 {
                cloud.pos.push([x as f32 * 0.01, y as f32 * 0.01, -0.15]);
                cloud.nrm.push([0.0, 0.0, 1.0]);
                cloud.pos.push([x as f32 * 0.01, y as f32 * 0.01, 0.15]);
                cloud.nrm.push([0.0, 0.0, -1.0]);
            }
        }
        let opts = SurfaceOptions { voxel: 0.02, dilate_vox: 8, trim_m: 0.10, ..Default::default() };
        let (m, s) = reconstruct(&cloud, &opts);
        let emitted = s["tris_emitted"].as_u64().unwrap() as usize;
        let kept = s["tris_kept"].as_u64().unwrap() as usize;
        for t in m.idx.chunks_exact(3) {
            let mean = (m.support[t[0] as usize] + m.support[t[1] as usize] + m.support[t[2] as usize]) / 3.0;
            assert!(mean <= 0.10 + 1e-6, "tri with mean support {mean} kept");
        }
        let _ = (emitted, kept);
    }
}
