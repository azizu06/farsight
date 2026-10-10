//! Calibration and projection. Internal coordinates are OpenCV x-right/y-down/z-forward.
//! Port of jetson/mapping/geometry.py.

/// Camera calibration in pixels at the depth-map resolution.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Calibration {
    pub width: usize,
    pub height: usize,
    pub fx: f64,
    pub fy: f64,
    pub cx: f64,
    pub cy: f64,
}

impl Calibration {
    pub fn new(width: usize, height: usize, fx: f64, fy: f64, cx: f64, cy: f64) -> Result<Self, String> {
        if width == 0 || height == 0 {
            return Err("Calibration dimensions must be positive".into());
        }
        if ![fx, fy, cx, cy].iter().all(|v| v.is_finite()) {
            return Err("Calibration must contain finite numbers".into());
        }
        if fx <= 0.0 || fy <= 0.0 {
            return Err("Focal lengths must be positive".into());
        }
        Ok(Self { width, height, fx, fy, cx, cy })
    }
}

/// 3x3 min/max neighbourhood filters matching cv2.erode/cv2.dilate with a 3x3
/// ones kernel and OpenCV's default border handling (erode: +inf, dilate: -inf),
/// i.e. borders use the clipped in-image neighbourhood.
fn minmax3x3(clean: &[f32], w: usize, h: usize) -> (Vec<f32>, Vec<f32>) {
    let mut lo = vec![f32::MAX; w * h];
    let mut hi = vec![f32::MIN; w * h];
    for y in 0..h {
        let y0 = y.saturating_sub(1);
        let y1 = (y + 1).min(h - 1);
        for x in 0..w {
            let x0 = x.saturating_sub(1);
            let x1 = (x + 1).min(w - 1);
            let mut mn = f32::MAX;
            let mut mx = f32::MIN;
            for yy in y0..=y1 {
                for xx in x0..=x1 {
                    let v = clean[yy * w + xx];
                    if v < mn {
                        mn = v;
                    }
                    if v > mx {
                        mx = v;
                    }
                }
            }
            lo[y * w + x] = mn;
            hi[y * w + x] = mx;
        }
    }
    (lo, hi)
}

/// Reject invalid/range-clipped depths and samples near depth discontinuities.
/// Port of valid_depth: clean = where(valid, d, 0); local_min = erode(clean),
/// local_max = dilate(clean); valid & (max - min <= edge_ratio * max(clean, near)).
pub fn valid_depth(depth: &[f32], w: usize, h: usize, near: f32, far: f32, edge_ratio: f32) -> Vec<bool> {
    let mut valid = vec![false; w * h];
    let mut clean = vec![0.0f32; w * h];
    for i in 0..w * h {
        let d = depth[i];
        let ok = d.is_finite() && d >= near && d <= far;
        valid[i] = ok;
        if ok {
            clean[i] = d;
        }
    }
    let (local_min, local_max) = minmax3x3(&clean, w, h);
    for i in 0..w * h {
        if valid[i] {
            let threshold = edge_ratio * clean[i].max(near);
            valid[i] = (local_max[i] - local_min[i]) <= threshold;
        }
    }
    valid
}

/// Point record: 3x float32 xyz, 3x uint8 rgb, 1 pad byte = 16 bytes.
pub const POINT_RECORD: usize = 16;

/// uint32 count followed by 16-byte XYZ/RGB records, little endian.
/// Port of encode_points.
pub fn encode_points(points: &[[f32; 3]], colors: &[[u8; 3]]) -> Vec<u8> {
    let n = points.len();
    let mut out = vec![0u8; 4 + n * POINT_RECORD];
    out[..4].copy_from_slice(&(n as u32).to_le_bytes());
    for ((p, c), rec) in points.iter().zip(colors.iter()).zip(out[4..].chunks_exact_mut(POINT_RECORD)) {
        rec[..4].copy_from_slice(&p[0].to_le_bytes());
        rec[4..8].copy_from_slice(&p[1].to_le_bytes());
        rec[8..12].copy_from_slice(&p[2].to_le_bytes());
        rec[12..15].copy_from_slice(c);
    }
    out
}

/// Binary PLY export, little endian. Port of export_ply.
pub fn export_ply(points: &[[f32; 3]], colors: &[[u8; 3]]) -> Vec<u8> {
    let header = format!(
        "ply\nformat binary_little_endian 1.0\nelement vertex {}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n",
        points.len()
    );
    let mut out = vec![0u8; header.len() + points.len() * 15];
    out[..header.len()].copy_from_slice(header.as_bytes());
    for ((p, c), rec) in points.iter().zip(colors.iter()).zip(out[header.len()..].chunks_exact_mut(15)) {
        rec[..4].copy_from_slice(&p[0].to_le_bytes());
        rec[4..8].copy_from_slice(&p[1].to_le_bytes());
        rec[8..12].copy_from_slice(&p[2].to_le_bytes());
        rec[12..15].copy_from_slice(c);
    }
    out
}

/// 4x4 rigid transform, row-major internally (wire is column-major).
pub type Mat4 = [[f64; 4]; 4];

pub const IDENTITY: Mat4 = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
];

/// Column-major 16 values -> row-major Mat4 with rigidity checks.
/// Port of rigid_transform.
pub fn rigid_transform(values: &[f64]) -> Result<Mat4, String> {
    if values.len() != 16 || !values.iter().all(|v| v.is_finite()) {
        return Err("Camera transform must contain 16 finite numbers".into());
    }
    let mut t = [[0.0f64; 4]; 4];
    for col in 0..4 {
        for row in 0..4 {
            t[row][col] = values[col * 4 + row];
        }
    }
    let bottom = [t[3][0], t[3][1], t[3][2], t[3][3]];
    let target = [0.0, 0.0, 0.0, 1.0];
    if !bottom.iter().zip(target.iter()).all(|(a, b)| (a - b).abs() <= 1e-4) {
        return Err("Camera transform must be rigid and right-handed".into());
    }
    // R^T R == I within atol 0.005
    for i in 0..3 {
        for j in 0..3 {
            let mut s = 0.0;
            for k in 0..3 {
                s += t[k][i] * t[k][j];
            }
            let expect = if i == j { 1.0 } else { 0.0 };
            if (s - expect).abs() > 0.005 + 1e-5 * expect {
                return Err("Camera transform must be rigid and right-handed".into());
            }
        }
    }
    // det(R) == 1 within atol 0.005
    let r = |i: usize, j: usize| t[i][j];
    let det = r(0, 0) * (r(1, 1) * r(2, 2) - r(1, 2) * r(2, 1))
        - r(0, 1) * (r(1, 0) * r(2, 2) - r(1, 2) * r(2, 0))
        + r(0, 2) * (r(1, 0) * r(2, 1) - r(1, 1) * r(2, 0));
    if (det - 1.0).abs() > 0.005 {
        return Err("Camera transform must be rigid and right-handed".into());
    }
    Ok(t)
}

/// Column-major flatten of a Mat4 (matches numpy flatten(order="F")).
pub fn flatten_f(t: &Mat4) -> [f64; 16] {
    let mut out = [0.0; 16];
    for col in 0..4 {
        for row in 0..4 {
            out[col * 4 + row] = t[row][col];
        }
    }
    out
}

/// world = R * local + t for a 3D point.
#[inline]
pub fn transform_point(t: &Mat4, local: [f64; 3]) -> [f64; 3] {
    [
        t[0][0] * local[0] + t[0][1] * local[1] + t[0][2] * local[2] + t[0][3],
        t[1][0] * local[0] + t[1][1] * local[1] + t[1][2] * local[2] + t[1][3],
        t[2][0] * local[0] + t[2][1] * local[1] + t[2][2] * local[2] + t[2][3],
    ]
}

/// Deterministic visual-wire decimation for points.bin records.
/// Stride-samples 16-byte records to at most `cap` vertices; `cap = 0`
/// disables. Mirrors the board-agreed visual export cap: the internal
/// map is untouched — only the served/exported payload is bounded.
pub fn decimate_points_bin(raw: &[u8], cap: usize) -> Vec<u8> {
    let n = raw
        .get(..4)
        .map(|b| u32::from_le_bytes(b.try_into().unwrap()) as usize)
        .unwrap_or(0);
    if cap == 0 || n <= cap {
        return raw.to_vec();
    }
    let mut out = Vec::with_capacity(4 + cap * 16);
    out.extend_from_slice(&(cap as u32).to_le_bytes());
    for i in 0..cap {
        let idx = i * n / cap;
        out.extend_from_slice(&raw[4 + idx * 16..4 + idx * 16 + 16]);
    }
    out
}

/// Deterministic visual-wire decimation for mesh.bin bytes.
/// Stride-samples triangles to at most `cap`, then compacts the vertex
/// buffer to referenced vertices only and remaps indices so the wire
/// format stays valid. `cap = 0` disables.
pub fn decimate_mesh_bin(raw: &[u8], cap: usize) -> Vec<u8> {
    let vcount = raw
        .get(..4)
        .map(|b| u32::from_le_bytes(b.try_into().unwrap()) as usize)
        .unwrap_or(0);
    let icount = raw
        .get(4..8)
        .map(|b| u32::from_le_bytes(b.try_into().unwrap()) as usize)
        .unwrap_or(0);
    let tris = icount / 3;
    if cap == 0 || tris <= cap {
        return raw.to_vec();
    }
    let verts = &raw[8..8 + vcount * 16];
    let indices = &raw[8 + vcount * 16..8 + vcount * 16 + icount * 4];
    // First: which triangle indices survive, and which vertices they use.
    let mut remap = std::collections::HashMap::with_capacity(cap * 3);
    let new_index = |old: u32, remap: &mut std::collections::HashMap<u32, u32>, count: &mut u32| -> u32 {
        *remap.entry(old).or_insert_with(|| {
            let v = *count;
            *count += 1;
            v
        })
    };
    let mut new_vcount = 0u32;
    let mut out_indices = Vec::with_capacity(cap * 12);
    let mut used_verts: Vec<u32> = Vec::with_capacity(cap * 3);
    for i in 0..cap {
        let t = i * tris / cap;
        for k in 0..3 {
            let off = (t * 3 + k) * 4;
            let old = u32::from_le_bytes(indices[off..off + 4].try_into().unwrap());
            let nv = new_index(old, &mut remap, &mut new_vcount);
            if nv as usize == used_verts.len() {
                used_verts.push(old);
            }
            out_indices.extend_from_slice(&nv.to_le_bytes());
        }
    }
    let mut out = Vec::with_capacity(8 + used_verts.len() * 16 + out_indices.len());
    out.extend_from_slice(&(used_verts.len() as u32).to_le_bytes());
    out.extend_from_slice(&((cap * 3) as u32).to_le_bytes());
    for &v in &used_verts {
        out.extend_from_slice(&verts[v as usize * 16..v as usize * 16 + 16]);
    }
    out.extend_from_slice(&out_indices);
    out
}

#[cfg(test)]
mod cap_tests {
    use super::*;

    fn points_bin(n: usize) -> Vec<u8> {
        let pts: Vec<[f32; 3]> = (0..n).map(|i| [i as f32, 0.0, 0.0]).collect();
        let colors: Vec<[u8; 3]> = vec![[1, 2, 3]; n];
        encode_points(&pts, &colors)
    }

    #[test]
    fn points_cap_stride_samples_and_preserves_records() {
        let raw = points_bin(100);
        let capped = decimate_points_bin(&raw, 10);
        assert_eq!(u32::from_le_bytes(capped[0..4].try_into().unwrap()), 10);
        assert_eq!(capped.len(), 4 + 10 * 16);
        // stride: i*100/10 -> indices 0,10,20...; record x-coord = original idx
        for i in 0..10 {
            let x = f32::from_le_bytes(capped[4 + i * 16..8 + i * 16].try_into().unwrap());
            assert_eq!(x, (i * 10) as f32);
        }
        // same input -> same output (deterministic / flicker-free)
        assert_eq!(capped, decimate_points_bin(&raw, 10));
        // under cap -> unchanged; cap 0 -> disabled
        assert_eq!(decimate_points_bin(&raw, 200), raw);
        assert_eq!(decimate_points_bin(&raw, 0), raw);
    }

    #[test]
    fn mesh_cap_keeps_wire_valid_and_remaps() {
        // two disjoint triangles sharing no vertices: 6 verts, 2 tris
        let mut raw = Vec::new();
        raw.extend_from_slice(&6u32.to_le_bytes());
        raw.extend_from_slice(&6u32.to_le_bytes());
        for v in 0..6u32 {
            for c in [v as f32, 0.0, 0.0] {
                raw.extend_from_slice(&c.to_le_bytes());
            }
            raw.extend_from_slice(&[9u8, 9, 9, 0]);
        }
        for i in [0u32, 1, 2, 3, 4, 5] {
            raw.extend_from_slice(&i.to_le_bytes());
        }
        let capped = decimate_mesh_bin(&raw, 1);
        let v = u32::from_le_bytes(capped[0..4].try_into().unwrap());
        let i = u32::from_le_bytes(capped[4..8].try_into().unwrap());
        assert_eq!((v, i), (3, 3));
        assert_eq!(capped.len(), 8 + 3 * 16 + 3 * 4);
        // all indices in range, remapped to compact verts (first tri selected)
        for k in 0..3 {
            let idx = u32::from_le_bytes(
                capped[8 + 3 * 16 + k * 4..12 + 3 * 16 + k * 4].try_into().unwrap());
            assert!(idx < v);
        }
        assert_eq!(capped, decimate_mesh_bin(&raw, 1));
        assert_eq!(decimate_mesh_bin(&raw, 5), raw);
        assert_eq!(decimate_mesh_bin(&raw, 0), raw);
    }
}
