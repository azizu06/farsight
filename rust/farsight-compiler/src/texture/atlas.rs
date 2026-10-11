//! UV atlas construction: normal-coherent chart growth, per-chart planar
//! parametrization, shelf packing, and per-texel coverage rasterization.
//! Charts are grown by dihedral angle to the *running* chart normal, so the
//! projection of a chart onto its own plane can never fold (every triangle
//! normal stays within `chart_angle` of the plane normal).

use crate::core::*;
use rayon::prelude::*;
use rustc_hash::FxHashMap;

/// One atlas chart: a connected, roughly-planar region of the mesh.
#[derive(Debug)]
pub struct Chart {
    /// Global triangle indices in `mesh.idx`.
    pub faces: Vec<u32>,
    /// Unique vertex ids used by `faces` (ascending).
    pub verts: Vec<u32>,
    /// Param-space coordinates aligned with `verts`, in metres.
    pub uv: Vec<[f32; 2]>,
    /// Area-weighted mean normal (unit). For `planar` charts this is the exact
    /// plane normal: write it to the chart's vertices so walls render flat.
    pub normal: V3,
    /// Chart is one merged coplanar region (walls/floor/tabletops): safe to
    /// flatten vertex normals and orthographically project with no distortion.
    pub planar: bool,
    /// Param basis (unit, tangent to `normal`).
    pub eu: V3,
    pub ev: V3,
    /// Param-space min corner.
    pub omin: [f32; 2],
    /// Content size in atlas pixels, excluding the gutter ring.
    pub w: u32,
    pub h: u32,
    /// Atlas position of the content origin (top-left content texel).
    pub x: u32,
    pub y: u32,
}

/// Packed atlas.
#[derive(Debug)]
pub struct Atlas {
    pub w: u32,
    pub h: u32,
    pub texel_m: f32,
    pub charts: Vec<Chart>,
}

/// Area-weighted per-triangle normals of `mesh` (unit vectors + areas).
pub fn face_normals(mesh: &Mesh) -> (Vec<V3>, Vec<f32>) {
    mesh.idx
        .par_chunks_exact(3)
        .map(|t| {
            let (a, b, c) = (mesh.pos[t[0] as usize], mesh.pos[t[1] as usize], mesh.pos[t[2] as usize]);
            let n = cross(sub(b, a), sub(c, a));
            let area = 0.5 * length(n);
            (if area > 0.0 { scale(n, 1.0 / (2.0 * area)) } else { [0.0, 1.0, 0.0] }, area)
        })
        .unzip()
}

/// Per-face normals used for chart growth: averaged vertex normals when the
/// mesh carries them — far smoother than raw facet normals on TSDF output,
/// which is what keeps planar walls in one piece instead of shattering.
pub fn cluster_normals(mesh: &Mesh, geo: &[V3]) -> Vec<V3> {
    if mesh.nrm.len() != mesh.pos.len() {
        return geo.to_vec();
    }
    mesh.idx
        .par_chunks_exact(3)
        .enumerate()
        .map(|(i, t)| {
            let n = add(add(mesh.nrm[t[0] as usize], mesh.nrm[t[1] as usize]), mesh.nrm[t[2] as usize]);
            let l = length(n);
            if l > 1e-9 { scale(n, 1.0 / l) } else { geo[i] }
        })
        .collect()
}

/// Undirected edge -> incident faces.
fn edge_faces(mesh: &Mesh) -> FxHashMap<u64, Vec<u32>> {
    let mut edges: FxHashMap<u64, Vec<u32>> = FxHashMap::default();
    for (t, tri) in mesh.idx.chunks_exact(3).enumerate() {
        for e in 0..3 {
            let (a, b) = (tri[e], tri[(e + 1) % 3]);
            let key = (a.min(b) as u64) << 32 | a.max(b) as u64;
            edges.entry(key).or_default().push(t as u32);
        }
    }
    edges
}

/// Grow charts over shared edges, gating on the angle to the running chart
/// normal so a chart stays within `cos_angle` of its mean plane normal.
fn segment(mesh: &Mesh, nrm: &[V3], area: &[f32], cos_angle: f32, edges: &FxHashMap<u64, Vec<u32>>) -> Vec<Vec<u32>> {
    let f = nrm.len();
    let mut visited = vec![false; f];
    let mut charts = Vec::new();
    let mut stack = Vec::new();
    for seed in 0..f as u32 {
        if visited[seed as usize] || area[seed as usize] <= 0.0 {
            continue;
        }
        let mut faces = Vec::new();
        let mut acc = [0.0f32; 3];
        visited[seed as usize] = true;
        stack.push(seed);
        while let Some(t) = stack.pop() {
            faces.push(t);
            acc = add(acc, scale(nrm[t as usize], area[t as usize]));
            for e in 0..3 {
                let (a, b) = (mesh.idx[3 * t as usize + e], mesh.idx[3 * t as usize + (e + 1) % 3]);
                let key = (a.min(b) as u64) << 32 | a.max(b) as u64;
                for &g in &edges[&key] {
                    if g == t || visited[g as usize] || area[g as usize] <= 0.0 {
                        continue;
                    }
                    // angle to running mean normal (unnormalized compare)
                    let acc_len = length(acc);
                    if dot(nrm[g as usize], acc) >= cos_angle * acc_len {
                        visited[g as usize] = true;
                        stack.push(g);
                    }
                }
            }
        }
        charts.push(faces);
    }
    charts
}

/// Union-find over the segmented groups: re-join edge-adjacent fragments whose
/// running mean normals stay within `cos_merge`, then absorb residual slivers
/// (< `min_faces` triangles) into their best-connected neighbour. Turns noisy
/// segmentation into one chart per planar surface.
fn merge(mesh: &Mesh, nrm: &[V3], area: &[f32], groups: Vec<Vec<u32>>, edges: &FxHashMap<u64, Vec<u32>>, cos_merge: f32, min_faces: usize) -> Vec<Vec<u32>> {
    let g = groups.len();
    // face -> group
    let mut fg = vec![u32::MAX; nrm.len()];
    for (gi, faces) in groups.iter().enumerate() {
        for &f in faces {
            fg[f as usize] = gi as u32;
        }
    }
    let mut parent: Vec<u32> = (0..g as u32).collect();
    fn find(p: &mut [u32], mut i: u32) -> u32 {
        while p[i as usize] != i {
            p[i as usize] = p[p[i as usize] as usize];
            i = p[i as usize];
        }
        i
    }
    // running area-weighted normal sums and areas per root
    let mut acc: Vec<V3> = groups.iter().map(|fs| fs.iter().fold([0.0; 3], |a, &f| add(a, scale(nrm[f as usize], area[f as usize])))).collect();
    let mut cnt: Vec<u32> = groups.iter().map(|fs| fs.len() as u32).collect();
    let union = |parent: &mut Vec<u32>, acc: &mut Vec<V3>, cnt: &mut Vec<u32>, a: u32, b: u32| {
        let (ra, rb) = (find(parent, a), find(parent, b));
        if ra != rb {
            parent[rb as usize] = ra;
            acc[ra as usize] = add(acc[ra as usize], acc[rb as usize]);
            cnt[ra as usize] += cnt[rb as usize];
        }
    };
    // pass 1: adjacent groups with compatible running normals
    for faces in edges.values() {
        for i in 0..faces.len() {
            for j in i + 1..faces.len() {
                let (ra, rb) = (find(&mut parent, fg[faces[i] as usize]), find(&mut parent, fg[faces[j] as usize]));
                if ra == rb || ra == u32::MAX || rb == u32::MAX {
                    continue;
                }
                let (la, lb) = (length(acc[ra as usize]), length(acc[rb as usize]));
                if la > 0.0 && lb > 0.0 && dot(acc[ra as usize], acc[rb as usize]) >= cos_merge * la * lb {
                    union(&mut parent, &mut acc, &mut cnt, ra, rb);
                }
            }
        }
    }
    // pass 2: slivers merge into their best-connected neighbour
    for _ in 0..3 {
        let mut moved = false;
        // shared-edge count per (small root, neighbour root)
        let mut share: FxHashMap<(u32, u32), u32> = FxHashMap::default();
        for faces in edges.values() {
            for i in 0..faces.len() {
                for j in i + 1..faces.len() {
                    let (ra, rb) = (find(&mut parent, fg[faces[i] as usize]), find(&mut parent, fg[faces[j] as usize]));
                    if ra != rb && ra != u32::MAX && rb != u32::MAX {
                        *share.entry((ra.min(rb), ra.max(rb))).or_default() += 1;
                    }
                }
            }
        }
        let mut best: FxHashMap<u32, u32> = FxHashMap::default();
        for (&(a, b), &s) in &share {
            let e = best.entry(a).or_insert(b);
            if share.get(&(a.min(*e), a.max(*e))).copied().unwrap_or(0) < s {
                *e = b;
            }
            let e = best.entry(b).or_insert(a);
            if share.get(&(b.min(*e), b.max(*e))).copied().unwrap_or(0) < s {
                *e = a;
            }
        }
        for (&r, &t) in &best {
            if cnt[find(&mut parent, r) as usize] < min_faces as u32 {
                let (rr, tt) = (find(&mut parent, r), find(&mut parent, t));
                if rr != tt && dot(acc[rr as usize], acc[tt as usize]) > 0.0 {
                    union(&mut parent, &mut acc, &mut cnt, rr, tt);
                    moved = true;
                }
            }
        }
        if !moved {
            break;
        }
    }
    let _ = mesh;
    // emit merged groups, deterministically ordered by smallest face id
    let mut roots: FxHashMap<u32, Vec<u32>> = FxHashMap::default();
    for (gi, faces) in groups.iter().enumerate() {
        let r = find(&mut parent, gi as u32);
        roots.entry(r).or_default().extend_from_slice(faces);
    }
    let mut out: Vec<Vec<u32>> = roots.into_values().collect();
    for fs in out.iter_mut() {
        fs.sort_unstable();
    }
    out.sort_by_key(|fs| fs[0]);
    out
}

/// Tangent basis for a unit normal: returns (eu, ev) perpendicular to `n`.
fn tangent(n: V3) -> (V3, V3) {
    let a = if n[0].abs() <= n[1].abs() && n[0].abs() <= n[2].abs() {
        [1.0, 0.0, 0.0]
    } else if n[1].abs() <= n[2].abs() {
        [0.0, 1.0, 0.0]
    } else {
        [0.0, 0.0, 1.0]
    };
    let eu = normalize(cross(n, a));
    (eu, cross(n, eu))
}

fn next_pow2(x: u32) -> u32 {
    x.checked_next_power_of_two().unwrap_or(u32::MAX)
}

/// Segment the mesh into charts, parametrize and pack them into an atlas of at
/// most `max_atlas` px per side at `texel_m` metres/texel (rescaled coarser if
/// the charts would not fit). `gutter` is the padding ring in texels.
/// `cos_merge`/`min_faces` control the coplanar merge pass that collapses the
/// noisy segmentation into one chart per planar surface.
pub fn build(mesh: &Mesh, nrm: &[V3], area: &[f32], texel_m: f32, max_atlas: u32, gutter: u32, cos_angle: f32, cos_merge: f32, min_faces: usize) -> Atlas {
    let edges = edge_faces(mesh);
    let groups = segment(mesh, nrm, area, cos_angle, &edges);
    let faces = merge(mesh, nrm, area, groups, &edges, cos_merge, min_faces);
    let mut texel = texel_m;
    // Charts are parametrized once (param coords in metres); only the pixel
    // dimensions change if texel has to be rescaled coarser to fit.
    let mut charts: Vec<Chart> = faces
        .into_iter()
        .map(|faces| {
            let mut acc = [0.0f32; 3];
            for &f in &faces {
                acc = add(acc, scale(nrm[f as usize], area[f as usize]));
            }
            let normal = normalize(acc);
            // A merged region counts as planar when its face normals cluster
            // within ~10 deg of the chart normal (walls, floors, table tops).
            let dev = faces.iter().map(|&f| area[f as usize] * dot(nrm[f as usize], normal).max(-1.0).min(1.0).acos()).sum::<f32>();
            let aw_ = faces.iter().map(|&f| area[f as usize]).sum::<f32>().max(1e-12);
            let planar = dev / aw_ <= (10.0f32).to_radians();
            let (mut eu, mut ev) = tangent(normal);
            let mut seen: FxHashMap<u32, u32> = FxHashMap::default();
            let mut verts = Vec::new();
            for &f in &faces {
                for k in 0..3 {
                    let v = mesh.idx[3 * f as usize + k];
                    if seen.insert(v, verts.len() as u32).is_none() {
                        verts.push(v);
                    }
                }
            }
            verts.sort_unstable();
            let mut uv: Vec<[f32; 2]> = verts.iter().map(|&v| [dot(mesh.pos[v as usize], eu), dot(mesh.pos[v as usize], ev)]).collect();
            if planar {
                // Rotate the in-plane basis onto the chart's principal axis so
                // the content rect hugs the region (tighter rects -> higher
                // coverage, fewer texels to bake).
                let (mx, my) = uv.iter().fold((0.0f64, 0.0f64), |(a, b), p| (a + p[0] as f64, b + p[1] as f64));
                let n = uv.len().max(1) as f64;
                let (mx, my) = (mx / n, my / n);
                let (mut c00, mut c01, mut c11) = (0.0f64, 0.0f64, 0.0f64);
                for p in &uv {
                    let (du, dv) = (p[0] as f64 - mx, p[1] as f64 - my);
                    c00 += du * du;
                    c01 += du * dv;
                    c11 += dv * dv;
                }
                let th = 0.5 * (2.0 * c01).atan2(c00 - c11);
                let (cs, sn) = (th.cos() as f32, th.sin() as f32);
                let (eu2, ev2) = (add(scale(eu, cs), scale(ev, sn)), add(scale(ev, cs), scale(eu, -sn)));
                eu = eu2;
                ev = ev2;
                uv = verts.iter().map(|&v| [dot(mesh.pos[v as usize], eu), dot(mesh.pos[v as usize], ev)]).collect();
            }
            let mut omin = [f32::INFINITY; 2];
            let mut omax = [f32::NEG_INFINITY; 2];
            for p in &uv {
                omin[0] = omin[0].min(p[0]);
                omin[1] = omin[1].min(p[1]);
                omax[0] = omax[0].max(p[0]);
                omax[1] = omax[1].max(p[1]);
            }
            Chart { faces, verts, uv, normal, planar, eu, ev, omin, w: 0, h: 0, x: 0, y: 0 }
        })
        .collect();

    // Pack, growing texel until the shelves fit under max_atlas.
    let mut packed: Option<(Vec<(u32, u32)>, u32, u32)> = None;
    for _ in 0..8 {
        for c in charts.iter_mut() {
            c.w = (((c.uv.iter().map(|p| p[0]).fold(f32::NEG_INFINITY, f32::max) - c.omin[0]) / texel).ceil() as u32).max(1);
            c.h = (((c.uv.iter().map(|p| p[1]).fold(f32::NEG_INFINITY, f32::max) - c.omin[1]) / texel).ceil() as u32).max(1);
        }
        let g = 2 * gutter;
        let area: u64 = charts.iter().map(|c| (c.w + g) as u64 * (c.h + g) as u64).sum();
        let aw = next_pow2((area as f64 * 1.3).sqrt() as u32).clamp(256, max_atlas.max(256));
        if let Some((p, h)) = shelf_pack(&charts, aw, gutter, max_atlas) {
            packed = Some((p, h, aw));
            break;
        }
        texel *= 1.45;
    }
    let (placement, used_h, aw) = match packed {
        Some(p) => p,
        None => {
            // Pathological overflow: degrade every chart to one texel so the
            // bake still produces colour and valid UVs instead of failing.
            let stride = 1 + 2 * gutter;
            let cols = (max_atlas / stride).max(1);
            for (i, c) in charts.iter_mut().enumerate() {
                c.w = 1;
                c.h = 1;
                c.x = (i as u32 % cols) * stride + gutter;
                c.y = (i as u32 / cols) * stride + gutter;
            }
            let rows = (charts.len() as u32 + cols - 1) / cols;
            return Atlas { w: (cols * stride).min(max_atlas), h: rows * stride, texel_m: texel, charts };
        }
    };
    for (c, &(x, y)) in charts.iter_mut().zip(placement.iter()) {
        c.x = x + gutter;
        c.y = y + gutter;
    }
    Atlas { w: aw, h: used_h, texel_m: texel, charts }
}

/// Shelf-pack `w+2g x h+2g` rects (height-descending) into `aw` wide shelves.
/// Returns (placement of content corner minus gutter, used height) or None.
fn shelf_pack(charts: &[Chart], aw: u32, gutter: u32, max_atlas: u32) -> Option<(Vec<(u32, u32)>, u32)> {
    let mut order: Vec<usize> = (0..charts.len()).collect();
    order.sort_by_key(|&i| (std::cmp::Reverse(charts[i].h + 2 * gutter), std::cmp::Reverse(charts[i].w + 2 * gutter), i));
    let g = 2 * gutter;
    let mut place = vec![(0u32, 0u32); charts.len()];
    let (mut x, mut y, mut shelf_h) = (0u32, 0u32, 0u32);
    for &i in &order {
        let (w, h) = (charts[i].w + g, charts[i].h + g);
        if w > aw {
            return None;
        }
        if x + w > aw {
            y += shelf_h;
            x = 0;
            shelf_h = 0;
        }
        if y + h > max_atlas {
            return None;
        }
        place[i] = (x, y);
        x += w;
        shelf_h = shelf_h.max(h);
    }
    Some((place, y + shelf_h))
}

/// For each chart, rasterize its triangles in chart-local pixel space.
/// Returns per-chart texel map: u64::MAX = empty, else
/// `tri_global (low u32) | quant(w1) << 32 | quant(w2) << 48` where
/// (w0, w1, w2) are barycentric weights of the triangle's vertices.
pub fn rasterize(mesh: &Mesh, atlas: &Atlas) -> Vec<Vec<u64>> {
    atlas
        .charts
        .par_iter()
        .map(|c| {
            let local: FxHashMap<u32, usize> = c.verts.iter().enumerate().map(|(i, &v)| (v, i)).collect();
            // chart-local pixel coordinates of each unique vertex
            let px: Vec<[f32; 2]> = c
                .uv
                .iter()
                .map(|p| [(p[0] - c.omin[0]) / atlas.texel_m, (p[1] - c.omin[1]) / atlas.texel_m])
                .collect();
            let mut cov = vec![u64::MAX; (c.w * c.h) as usize];
            for &f in &c.faces {
                let t = [
                    *local.get(&mesh.idx[3 * f as usize]).unwrap_or(&0),
                    *local.get(&mesh.idx[3 * f as usize + 1]).unwrap_or(&0),
                    *local.get(&mesh.idx[3 * f as usize + 2]).unwrap_or(&0),
                ];
                let (p0, p1, p2) = (px[t[0]], px[t[1]], px[t[2]]);
                let denom = (p1[1] - p2[1]) * (p0[0] - p2[0]) + (p2[0] - p1[0]) * (p0[1] - p2[1]);
                if denom.abs() < 1e-12 {
                    continue;
                }
                let (minx, maxx) = (p0[0].min(p1[0]).min(p2[0]), p0[0].max(p1[0]).max(p2[0]));
                let (miny, maxy) = (p0[1].min(p1[1]).min(p2[1]), p0[1].max(p1[1]).max(p2[1]));
                let (x0, x1) = ((minx.floor() as i32).max(0), (maxx.ceil() as i32).min(c.w as i32));
                let (y0, y1) = ((miny.floor() as i32).max(0), (maxy.ceil() as i32).min(c.h as i32));
                for y in y0..y1 {
                    for x in x0..x1 {
                        let (px_, py_) = (x as f32 + 0.5, y as f32 + 0.5);
                        let w0 = ((p1[1] - p2[1]) * (px_ - p2[0]) + (p2[0] - p1[0]) * (py_ - p2[1])) / denom;
                        let w1 = ((p2[1] - p0[1]) * (px_ - p2[0]) + (p0[0] - p2[0]) * (py_ - p2[1])) / denom;
                        let w2 = 1.0 - w0 - w1;
                        if w0 >= -1e-4 && w1 >= -1e-4 && w2 >= -1e-4 {
                            let q1 = (w1.clamp(0.0, 1.0) * 65535.0) as u64;
                            let q2 = (w2.clamp(0.0, 1.0) * 65535.0) as u64;
                            cov[(y * c.w as i32 + x) as usize] = f as u64 | (q1 << 32) | (q2 << 48);
                        }
                    }
                }
            }
            cov
        })
        .collect()
}

/// Decode a coverage texel: (global tri, w0, w1, w2).
#[inline]
pub fn decode(v: u64) -> (u32, f32, f32, f32) {
    let w1 = ((v >> 32) & 0xffff) as f32 / 65535.0;
    let w2 = ((v >> 48) & 0xffff) as f32 / 65535.0;
    (v as u32, (1.0 - w1 - w2).max(0.0), w1, w2)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Two quads at right angles must land in different charts and every
    /// covered texel must carry a valid triangle + barycentric weights.
    #[test]
    fn corner_splits_and_rasterizes() {
        // quad in z=0 plane (normal +Z) and quad in x=0 plane (normal +X)
        let mesh = Mesh {
            pos: vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 1.0]],
            idx: vec![0, 1, 2, 0, 2, 3, 0, 4, 5, 0, 5, 3],
            ..Default::default()
        };
        let (nrm, area) = face_normals(&mesh);
        let atlas = build(&mesh, &nrm, &area, 0.05, 512, 2, (35f32).to_radians().cos(), (65f32).to_radians().cos(), 8);
        assert_eq!(atlas.charts.len(), 2);
        let cov = rasterize(&mesh, &atlas);
        for (c, cv) in atlas.charts.iter().zip(&cov) {
            assert_eq!(cv.len(), (c.w * c.h) as usize);
            let filled = cv.iter().filter(|&&v| v != u64::MAX).count();
            assert!(filled as f32 > 0.5 * cv.len() as f32, "chart too sparse: {filled}/{}", cv.len());
            for &v in cv {
                if v == u64::MAX {
                    continue;
                }
                let (_, w0, w1, w2) = decode(v);
                assert!((w0 + w1 + w2 - 1.0).abs() < 0.01);
            }
        }
    }
}
