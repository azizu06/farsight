//! Mesh post-processing. OWNER: W5.
//! Pipeline (in place): weld -> degenerate/duplicate-face drop -> non-manifold
//! vertex-fan splitting -> consistent winding (+ closed-component outward flip)
//! -> small-component prune -> small boundary-hole ear-clip fill -> bilateral
//! normal-filter smoothing (boundary pinned) -> point-to-plane resnap to the
//! measured cloud -> vertex normals -> colour IDW + support transfer ->
//! meshopt QEM decimation (colour-aware, border-locked) + GPU vertex reorder.
//! `lod_chain` additionally derives lower-detail LODs for the viewer.

use crate::core::*;
use crate::spatial::Grid;
use crate::Stats;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct MeshOptions {
    /// Final triangle budget (0 = keep all).
    pub max_triangles: usize,
    /// Bilateral-normal smoothing iterations.
    pub smooth_iters: usize,
    /// Vertex welding tolerance (m).
    pub weld_eps: f32,
    /// Drop triangles thinner than this area (m^2).
    pub min_tri_area: f32,
    /// Drop disconnected components under this area (m^2) ...
    pub min_component_area: f32,
    /// ... or under this triangle count.
    pub min_component_tris: usize,
    /// Fill boundary loops with perimeter <= this (m) ...
    pub hole_max_perimeter: f32,
    /// ... and at most this many vertices.
    pub hole_max_verts: usize,
    /// Bilateral spatial sigma, in units of mean edge length.
    pub smooth_sigma_spatial: f32,
    /// Bilateral range sigma in normal space (~0.3-0.6).
    pub smooth_sigma_range: f32,
    /// Vertex update strength per smoothing iteration (0-1).
    pub smooth_strength: f32,
    /// Point-to-plane snap search radius (m).
    pub resnap_radius: f32,
    /// Snap strength 0..1.
    pub resnap_strength: f32,
    /// IDW neighbour count for colour transfer.
    pub color_k: usize,
    /// Colour search radius (m).
    pub color_radius: f32,
    /// Support lookup bound (m); reported support is clamped to this.
    pub support_radius: f32,
    /// Lock boundary vertices during decimation.
    pub decimate_lock_border: bool,
    /// QEM weight per colour channel (0 = ignore colour, ~0.5 typical).
    pub decimate_color_weight: f32,
    /// meshopt target_error (fraction of mesh extent); 1.0 = budget-only.
    pub decimate_error: f32,
}

impl Default for MeshOptions {
    fn default() -> Self {
        MeshOptions {
            max_triangles: 1_500_000,
            smooth_iters: 4,
            weld_eps: 2e-5,
            min_tri_area: 1e-7,
            min_component_area: 2e-4,
            min_component_tris: 8,
            hole_max_perimeter: 0.6,
            hole_max_verts: 96,
            smooth_sigma_spatial: 1.5,
            smooth_sigma_range: 0.45,
            smooth_strength: 1.0,
            resnap_radius: 0.06,
            resnap_strength: 0.65,
            color_k: 6,
            color_radius: 0.08,
            support_radius: 1.5,
            decimate_lock_border: true,
            decimate_color_weight: 0.5,
            decimate_error: 1.0,
        }
    }
}

// ---------------- internal helpers ----------------

#[inline]
fn tri_area(p0: V3, p1: V3, p2: V3) -> f32 {
    0.5 * length(cross(sub(p1, p0), sub(p2, p0)))
}

#[inline]
fn tri_normal(p0: V3, p1: V3, p2: V3) -> V3 {
    normalize(cross(sub(p1, p0), sub(p2, p0)))
}

#[inline]
fn edge_key(a: u32, b: u32) -> u64 {
    let (lo, hi) = if a < b { (a, b) } else { (b, a) };
    ((lo as u64) << 32) | hi as u64
}

/// vertex -> incident faces (CSR).
fn vertex_faces(nv: usize, idx: &[u32]) -> (Vec<u32>, Vec<u32>) {
    let mut off = vec![0u32; nv + 1];
    for &v in idx {
        off[v as usize + 1] += 1;
    }
    for i in 0..nv {
        off[i + 1] += off[i];
    }
    let mut items = vec![0u32; idx.len()];
    let mut cur = off.clone();
    for (f, t) in idx.chunks_exact(3).enumerate() {
        for &v in t {
            items[cur[v as usize] as usize] = f as u32;
            cur[v as usize] += 1;
        }
    }
    (off, items)
}

/// Sorted (canonical edge key, packed directed-edge id = 3f+k).
fn build_edges(idx: &[u32]) -> Vec<(u64, u32)> {
    let mut e = Vec::with_capacity(idx.len());
    for (f, t) in idx.chunks_exact(3).enumerate() {
        for k in 0..3 {
            e.push((edge_key(t[k], t[(k + 1) % 3]), (3 * f + k) as u32));
        }
    }
    e.par_sort_unstable();
    e
}

/// For each directed edge id (3f+k): the partner directed edge id across the
/// same canonical edge when the edge has exactly 2 incident faces, else u32::MAX.
fn twin_table(idx: &[u32]) -> Vec<u32> {
    let edges = build_edges(idx);
    let mut twin = vec![u32::MAX; idx.len()];
    let mut i = 0;
    while i < edges.len() {
        let mut j = i + 1;
        while j < edges.len() && edges[j].0 == edges[i].0 {
            j += 1;
        }
        if j - i == 2 {
            let (e0, e1) = (edges[i].1 as usize, edges[i + 1].1 as usize);
            twin[e0] = e1 as u32;
            twin[e1] = e0 as u32;
        }
        i = j;
    }
    twin
}

/// Boundary directed-edge id (3f+k) per canonical edge with exactly 1 face.
fn boundary_edges(idx: &[u32]) -> Vec<u32> {
    let edges = build_edges(idx);
    let mut out = Vec::new();
    let mut i = 0;
    while i < edges.len() {
        let mut j = i + 1;
        while j < edges.len() && edges[j].0 == edges[i].0 {
            j += 1;
        }
        if j - i == 1 {
            out.push(edges[i].1);
        }
        i = j;
    }
    out
}

fn boundary_vertex_mask(nv: usize, idx: &[u32]) -> Vec<bool> {
    let mut mask = vec![false; nv];
    for &e in &boundary_edges(idx) {
        let f = e as usize / 3;
        let k = e as usize % 3;
        mask[idx[3 * f + k] as usize] = true;
        mask[idx[3 * f + (k + 1) % 3] as usize] = true;
    }
    mask
}

/// Keep faces where `keep[f]`; drop unreferenced vertices; fix all arrays.
fn compact(mesh: &mut Mesh, keep: &[bool]) {
    let mut idx = Vec::with_capacity(mesh.idx.len());
    for (f, t) in mesh.idx.chunks_exact(3).enumerate() {
        if keep[f] {
            idx.extend_from_slice(t);
        }
    }
    mesh.idx = idx;
    compact_vertices(mesh);
}

/// Drop vertices not referenced by idx; remap idx; align attribute arrays.
fn compact_vertices(mesh: &mut Mesh) {
    let n = mesh.pos.len();
    let mut remap = vec![u32::MAX; n];
    for &v in &mesh.idx {
        remap[v as usize] = 0;
    }
    let mut next = 0u32;
    for r in remap.iter_mut() {
        if *r != u32::MAX {
            *r = next;
            next += 1;
        }
    }
    let mut pos = Vec::with_capacity(next as usize);
    let mut nrm = Vec::with_capacity(next as usize);
    let mut col = Vec::with_capacity(next as usize);
    let mut uv = Vec::with_capacity(next as usize);
    let mut sup = Vec::with_capacity(next as usize);
    let (has_n, has_c, has_u, has_s) = (!mesh.nrm.is_empty(), !mesh.col.is_empty(), !mesh.uv.is_empty(), !mesh.support.is_empty());
    for i in 0..n {
        if remap[i] != u32::MAX {
            pos.push(mesh.pos[i]);
            if has_n {
                nrm.push(mesh.nrm[i]);
            }
            if has_c {
                col.push(mesh.col[i]);
            }
            if has_u {
                uv.push(mesh.uv[i]);
            }
            if has_s {
                sup.push(mesh.support[i]);
            }
        }
    }
    for v in mesh.idx.iter_mut() {
        *v = remap[*v as usize];
    }
    mesh.pos = pos;
    mesh.nrm = nrm;
    mesh.col = col;
    mesh.uv = uv;
    mesh.support = sup;
}

// ---------------- topology passes ----------------

/// Weld vertices within `eps` (27-cell neighbour search); keeps first-seen attrs.
fn weld(mesh: &mut Mesh, eps: f32) -> usize {
    let n = mesh.pos.len();
    if n == 0 {
        return 0;
    }
    let eps2 = eps * eps;
    let inv = 1.0 / eps.max(1e-9);
    let cell = |p: V3| ((p[0] * inv).floor() as i32, (p[1] * inv).floor() as i32, (p[2] * inv).floor() as i32);
    let mut map: FxHashMap<(i32, i32, i32), Vec<u32>> = FxHashMap::default();
    let mut canon = vec![0u32; n];
    for i in 0..n {
        let p = mesh.pos[i];
        let c = cell(p);
        let mut found = u32::MAX;
        'cells: for dx in -1..=1 {
            for dy in -1..=1 {
                for dz in -1..=1 {
                    if let Some(list) = map.get(&(c.0 + dx, c.1 + dy, c.2 + dz)) {
                        for &j in list {
                            if dist2(mesh.pos[j as usize], p) <= eps2 {
                                found = canon[j as usize];
                                break 'cells;
                            }
                        }
                    }
                }
            }
        }
        if found == u32::MAX {
            found = i as u32;
            map.entry(c).or_default().push(i as u32);
        }
        canon[i] = found;
    }
    for v in mesh.idx.iter_mut() {
        *v = canon[*v as usize];
    }
    compact_vertices(mesh);
    n - mesh.pos.len()
}

/// Drop degenerate slivers, tris with repeated indices and exact duplicate faces.
fn drop_degenerate(mesh: &mut Mesh, min_area: f32) -> usize {
    let nf = mesh.triangles();
    let mut seen: FxHashSet<(u32, u32, u32)> = FxHashSet::default();
    let mut keep = vec![true; nf];
    let mut removed = 0;
    for (f, t) in mesh.idx.chunks_exact(3).enumerate() {
        let (a, b, c) = (t[0], t[1], t[2]);
        let mut s = [a, b, c];
        s.sort_unstable();
        let dup = a == b || b == c || a == c || !seen.insert((s[0], s[1], s[2]));
        if dup || tri_area(mesh.pos[a as usize], mesh.pos[b as usize], mesh.pos[c as usize]) < min_area {
            keep[f] = false;
            removed += 1;
        }
    }
    compact(mesh, &keep);
    removed
}

/// Split vertices whose incident faces form >1 fan (bowtie / edge non-manifold).
/// After this every edge has <=2 incident faces. Returns number of new verts.
fn split_nonmanifold_vertices(mesh: &mut Mesh) -> usize {
    fn find(p: &mut [u32], x: u32) -> u32 {
        let mut x = x;
        while p[x as usize] != x {
            p[x as usize] = p[p[x as usize] as usize];
            x = p[x as usize];
        }
        x
    }
    let n = mesh.pos.len();
    let (off, items) = vertex_faces(n, &mesh.idx);
    let twin = twin_table(&mesh.idx);
    let edges = build_edges(&mesh.idx);
    // canonical edge key -> (start, len) in `edges`, only for groups >2 faces
    let mut big: FxHashMap<u64, (u32, u32)> = FxHashMap::default();
    let mut i = 0;
    while i < edges.len() {
        let mut j = i + 1;
        while j < edges.len() && edges[j].0 == edges[i].0 {
            j += 1;
        }
        if j - i > 2 {
            big.insert(edges[i].0, (i as u32, (j - i) as u32));
        }
        i = j;
    }
    // source vertex for each appended duplicate (extra[0] -> vert n)
    let mut extra: Vec<u32> = Vec::new();
    let idx = &mut mesh.idx;
    for v in 0..n as u32 {
        let fs = &items[off[v as usize] as usize..off[v as usize + 1] as usize];
        if fs.len() < 2 {
            continue;
        }
        let mut parent: Vec<u32> = (0..fs.len() as u32).collect();
        for (fi, &f) in fs.iter().enumerate() {
            let base = 3 * f as usize;
            let t = [idx[base], idx[base + 1], idx[base + 2]];
            let k = t.iter().position(|&x| x == v).unwrap();
            // f's two directed edges incident to v: (v->next)=edge k, (prev->v)=edge (k+2)%3
            for e in [base + k, base + (k + 2) % 3] {
                let key = edge_key(idx[e / 3 * 3 + e % 3], idx[e / 3 * 3 + (e % 3 + 1) % 3]);
                if let Some(&(s, l)) = big.get(&key) {
                    for q in &edges[s as usize..(s + l) as usize] {
                        let g = q.1 / 3;
                        if g != f {
                            if let Some(gi) = fs.iter().position(|&x| x == g) {
                                let (x, y) = (find(&mut parent, fi as u32), find(&mut parent, gi as u32));
                                if x != y {
                                    parent[x as usize] = y;
                                }
                            }
                        }
                    }
                } else {
                    let te = twin[e];
                    if te == u32::MAX {
                        continue;
                    }
                    let g = te / 3;
                    if let Some(gi) = fs.iter().position(|&x| x == g) {
                        let (x, y) = (find(&mut parent, fi as u32), find(&mut parent, gi as u32));
                        if x != y {
                            parent[x as usize] = y;
                        }
                    }
                }
            }
        }
        let mut roots: Vec<u32> = (0..fs.len() as u32).map(|i| find(&mut parent, i)).collect();
        roots.sort_unstable();
        roots.dedup();
        if roots.len() <= 1 {
            continue;
        }
        // first fan keeps v; every other fan gets a fresh duplicate vertex
        let first = roots[0];
        let mut fan_vert: Vec<(u32, u32)> = Vec::with_capacity(roots.len() - 1);
        for (fi, &f) in fs.iter().enumerate() {
            let r = find(&mut parent, fi as u32);
            if r == first {
                continue;
            }
            let nv_id = match fan_vert.iter().find(|(x, _)| *x == r) {
                Some(&(_, id)) => id,
                None => {
                    let id = n as u32 + extra.len() as u32;
                    extra.push(v);
                    fan_vert.push((r, id));
                    id
                }
            };
            for x in &mut idx[base_of(f)..base_of(f) + 3] {
                if *x == v {
                    *x = nv_id;
                }
            }
        }
    }
    // append duplicated verts and copy their attributes from the source vertex
    for &src in &extra {
        mesh.pos.push(mesh.pos[src as usize]);
        if !mesh.nrm.is_empty() {
            mesh.nrm.push(mesh.nrm[src as usize]);
        }
        if !mesh.col.is_empty() {
            mesh.col.push(mesh.col[src as usize]);
        }
        if !mesh.uv.is_empty() {
            mesh.uv.push(mesh.uv[src as usize]);
        }
        if !mesh.support.is_empty() {
            mesh.support.push(mesh.support[src as usize]);
        }
    }
    extra.len()
}

#[inline]
fn base_of(f: u32) -> usize {
    3 * f as usize
}

/// Consistent winding via BFS over twin edges; closed components get flipped
/// outward by signed volume. Returns (flips, adjacency conflicts).
fn orient_faces(mesh: &mut Mesh) -> (usize, usize) {
    let nf = mesh.triangles();
    if nf == 0 {
        return (0, 0);
    }
    let twin = twin_table(&mesh.idx);
    let mut flip = vec![false; nf];
    let mut comp = vec![u32::MAX; nf];
    let mut visited = vec![false; nf];
    let mut conflicts = 0usize;
    let mut queue: Vec<u32> = Vec::new();
    let mut ncomp = 0u32;
    for seed in 0..nf as u32 {
        if visited[seed as usize] {
            continue;
        }
        visited[seed as usize] = true;
        comp[seed as usize] = ncomp;
        queue.push(seed);
        while let Some(f) = queue.pop() {
            let f = f as usize;
            let tf = [mesh.idx[3 * f], mesh.idx[3 * f + 1], mesh.idx[3 * f + 2]];
            for k in 0..3 {
                let e = 3 * f + k;
                let te = twin[e];
                if te == u32::MAX {
                    continue;
                }
                let g = (te / 3) as usize;
                let tg = [mesh.idx[3 * g], mesh.idx[3 * g + 1], mesh.idx[3 * g + 2]];
                // f traverses its edge k as tf[k]->tf[k+1]; g must traverse opposite
                let same_dir = tg[te as usize % 3] == tf[k] && tg[(te as usize % 3 + 1) % 3] == tf[(k + 1) % 3];
                let want_flip = flip[f] ^ same_dir;
                if !visited[g] {
                    visited[g] = true;
                    flip[g] = want_flip;
                    comp[g] = ncomp;
                    queue.push(g as u32);
                } else if flip[g] != want_flip {
                    conflicts += 1;
                }
            }
        }
        ncomp += 1;
    }
    // per-component closed check + signed volume -> outward flip
    let mut area_vol: Vec<(f64, f64, bool)> = vec![(0.0, 0.0, true); ncomp as usize];
    // boundary count per component
    for e in 0..nf {
        let _ = e;
    }
    for f in 0..nf {
        let c = comp[f] as usize;
        let t = [mesh.idx[3 * f] as usize, mesh.idx[3 * f + 1] as usize, mesh.idx[3 * f + 2] as usize];
        let (p0, p1, p2) = (mesh.pos[t[0]], mesh.pos[t[1]], mesh.pos[t[2]]);
        let signed = dot(p0, cross(p1, p2)) as f64 / 6.0;
        // apply current flip mentally: flipped faces invert volume sign
        let s = if flip[f] { -signed } else { signed };
        area_vol[c].0 += tri_area(p0, p1, p2) as f64;
        area_vol[c].1 += s;
        for k in 0..3 {
            if twin[3 * f + k] == u32::MAX {
                area_vol[c].2 = false;
            }
        }
    }
    let mut flips = 0usize;
    for f in 0..nf {
        let c = comp[f] as usize;
        let out_flip = area_vol[c].2 && area_vol[c].1 < 0.0;
        if flip[f] ^ out_flip {
            mesh.idx.swap(3 * f + 1, 3 * f + 2);
            flips += 1;
        }
    }
    (flips, conflicts)
}

/// Remove disconnected components under area or tri-count thresholds.
fn drop_small_components(mesh: &mut Mesh, min_area: f32, min_tris: usize) -> usize {
    let nf = mesh.triangles();
    if nf == 0 {
        return 0;
    }
    let twin = twin_table(&mesh.idx);
    let mut comp = vec![u32::MAX; nf];
    let mut queue = Vec::new();
    let mut ncomp = 0u32;
    for seed in 0..nf as u32 {
        if comp[seed as usize] != u32::MAX {
            continue;
        }
        comp[seed as usize] = ncomp;
        queue.push(seed);
        while let Some(f) = queue.pop() {
            for k in 0..3 {
                let te = twin[3 * f as usize + k];
                if te != u32::MAX {
                    let g = (te / 3) as usize;
                    if comp[g] == u32::MAX {
                        comp[g] = ncomp;
                        queue.push(g as u32);
                    }
                }
            }
        }
        ncomp += 1;
    }
    let mut area = vec![0.0f64; ncomp as usize];
    let mut cnt = vec![0usize; ncomp as usize];
    for f in 0..nf {
        let t = [mesh.idx[3 * f] as usize, mesh.idx[3 * f + 1] as usize, mesh.idx[3 * f + 2] as usize];
        area[comp[f] as usize] += tri_area(mesh.pos[t[0]], mesh.pos[t[1]], mesh.pos[t[2]]) as f64;
        cnt[comp[f] as usize] += 1;
    }
    let mut keep = vec![true; nf];
    let mut removed = 0;
    for f in 0..nf {
        let c = comp[f] as usize;
        if (area[c] as f32) < min_area || cnt[c] < min_tris {
            keep[f] = false;
            removed += 1;
        }
    }
    compact(mesh, &keep);
    removed
}

/// Trace closed boundary loops: vertex -> outgoing boundary directed edge.
fn boundary_loops(idx: &[u32]) -> Vec<Vec<u32>> {
    let bedges = boundary_edges(idx);
    // outgoing[vertex] = list of (to_vertex, edge_id)
    let mut out: FxHashMap<u32, Vec<(u32, u32)>> = FxHashMap::default();
    for &e in &bedges {
        let (f, k) = (e as usize / 3, e as usize % 3);
        let (a, b) = (idx[3 * f + k], idx[3 * f + (k + 1) % 3]);
        out.entry(a).or_default().push((b, e));
    }
    let mut used: FxHashSet<u32> = FxHashSet::default();
    let mut loops = Vec::new();
    for &e0 in &bedges {
        if used.contains(&e0) {
            continue;
        }
        let (f0, k0) = (e0 as usize / 3, e0 as usize % 3);
        let start = idx[3 * f0 + k0];
        let mut cur = idx[3 * f0 + (k0 + 1) % 3];
        used.insert(e0);
        let mut verts = vec![start];
        let mut ok = true;
        while cur != start {
            verts.push(cur);
            let next_edge = out.get(&cur).and_then(|l| l.iter().find(|(_, e)| !used.contains(e)).map(|x| x.1));
            match next_edge {
                Some(e) => {
                    used.insert(e);
                    let (f, k) = (e as usize / 3, e as usize % 3);
                    cur = idx[3 * f + (k + 1) % 3];
                }
                None => {
                    ok = false;
                    break;
                }
            }
        }
        if ok && verts.len() >= 3 {
            loops.push(verts);
        }
    }
    loops
}

/// Fill small boundary loops with a greedy ear-clip triangulation; degenerate
/// leftovers fall back to a centroid fan. Returns (loops_filled, tris_added).
fn fill_holes(mesh: &mut Mesh, max_perim: f32, max_verts: usize) -> (usize, usize) {
    let loops = boundary_loops(&mesh.idx);
    let mut filled = 0usize;
    let mut added = 0usize;
    for verts in loops {
        if verts.len() > max_verts {
            continue;
        }
        let mut perim = 0.0;
        for i in 0..verts.len() {
            perim += length(sub(mesh.pos[verts[(i + 1) % verts.len()] as usize], mesh.pos[verts[i] as usize]));
        }
        if perim > max_perim {
            continue;
        }
        // Newell polygon normal
        let mut pn = [0.0f32; 3];
        for i in 0..verts.len() {
            let a = mesh.pos[verts[i] as usize];
            let b = mesh.pos[verts[(i + 1) % verts.len()] as usize];
            pn[0] += (a[1] - b[1]) * (a[2] + b[2]);
            pn[1] += (a[2] - b[2]) * (a[0] + b[0]);
            pn[2] += (a[0] - b[0]) * (a[1] + b[1]);
        }
        let pn = normalize(pn);
        // ear clipping on the loop polygon
        let mut ring: Vec<u32> = verts.clone();
        let mut tris: Vec<[u32; 3]> = Vec::with_capacity(ring.len());
        let mut guard = 0;
        while ring.len() > 3 && guard < ring.len() * ring.len() {
            guard += 1;
            let m = ring.len();
            let mut best: Option<(f32, usize)> = None;
            for i in 0..m {
                let (a, b, c) = (ring[(i + m - 1) % m], ring[i], ring[(i + 1) % m]);
                let (pa, pb, pc) = (mesh.pos[a as usize], mesh.pos[b as usize], mesh.pos[c as usize]);
                let n = cross(sub(pb, pa), sub(pc, pa));
                let nl = length(n);
                if nl < 1e-12 || dot(n, pn) <= 0.0 {
                    continue; // reflex ear
                }
                let w = 0.5 * nl - 1e-3 * dot(scale(n, 1.0 / nl), pn); // area - small planar bonus
                if best.map_or(true, |(bw, _)| w < bw) {
                    best = Some((w, i));
                }
            }
            match best {
                Some((_, i)) => {
                    let m = ring.len();
                    tris.push([ring[(i + m - 1) % m], ring[i], ring[(i + 1) % m]]);
                    ring.remove(i);
                }
                None => break,
            }
        }
        if ring.len() == 3 {
            tris.push([ring[0], ring[1], ring[2]]);
        } else {
            // fallback: centroid fan on remaining ring
            let c = {
                let mut c = [0.0f32; 3];
                for &v in &ring {
                    c = add(c, mesh.pos[v as usize]);
                }
                scale(c, 1.0 / ring.len() as f32)
            };
            let nv = mesh.pos.len() as u32;
            mesh.pos.push(c);
            if !mesh.nrm.is_empty() {
                mesh.nrm.push(pn);
            }
            if !mesh.col.is_empty() {
                mesh.col.push([128, 128, 128]);
            }
            if !mesh.uv.is_empty() {
                mesh.uv.push([0.0, 0.0]);
            }
            if !mesh.support.is_empty() {
                mesh.support.push(f32::MAX);
            }
            for i in 0..ring.len() {
                tris.push([ring[i], ring[(i + 1) % ring.len()], nv]);
            }
        }
        // ensure fill orientation matches loop (ear tris may flip vs pn on non-planar rings)
        for t in &tris {
            let (a, b, c) = (mesh.pos[t[0] as usize], mesh.pos[t[1] as usize], mesh.pos[t[2] as usize]);
            if dot(cross(sub(b, a), sub(c, a)), pn) < 0.0 {
                mesh.idx.extend_from_slice(&[t[0], t[2], t[1]]);
            } else {
                mesh.idx.extend_from_slice(t);
            }
            added += 1;
        }
        filled += 1;
    }
    (filled, added)
}

// ---------------- geometry passes ----------------

/// Angle-weighted vertex normals from faces.
pub fn compute_vertex_normals(mesh: &mut Mesh) {
    let n = mesh.pos.len();
    let mut nrm = vec![[0.0f32; 3]; n];
    for t in mesh.idx.chunks_exact(3) {
        let (a, b, c) = (t[0] as usize, t[1] as usize, t[2] as usize);
        let (p0, p1, p2) = (mesh.pos[a], mesh.pos[b], mesh.pos[c]);
        let fn_ = cross(sub(p1, p0), sub(p2, p0));
        for (v, u, w) in [(a, p1, p2), (b, p2, p0), (c, p0, p1)] {
            let d1 = normalize(sub(u, mesh.pos[v]));
            let d2 = normalize(sub(w, mesh.pos[v]));
            let ang = dot(d1, d2).clamp(-1.0, 1.0).acos();
            nrm[v] = add(nrm[v], scale(fn_, ang));
        }
    }
    mesh.nrm = nrm.par_iter().map(|&x| normalize(x)).collect();
}

/// Bilateral normal-filter smoothing (Zheng et al.): filter face normals
/// bilaterally over vertex-incident neighbourhoods, then update vertices toward
/// the filtered face planes. Boundary vertices are pinned.
fn bilateral_smooth(mesh: &mut Mesh, opts: &MeshOptions) {
    let nf = mesh.triangles();
    if nf == 0 || opts.smooth_iters == 0 {
        return;
    }
    let pin = boundary_vertex_mask(mesh.pos.len(), &mesh.idx);
    for _ in 0..opts.smooth_iters {
        let (off, items) = vertex_faces(mesh.pos.len(), &mesh.idx);
        let (mut fnrm, mut fcent, mut farea) = (vec![[0.0f32; 3]; nf], vec![[0.0f32; 3]; nf], vec![0.0f32; nf]);
        fnrm.par_iter_mut().zip(fcent.par_iter_mut()).zip(farea.par_iter_mut()).enumerate().for_each(|(f, ((n, c), a))| {
            let (p0, p1, p2) = (mesh.pos[mesh.idx[3 * f] as usize], mesh.pos[mesh.idx[3 * f + 1] as usize], mesh.pos[mesh.idx[3 * f + 2] as usize]);
            *n = tri_normal(p0, p1, p2);
            *c = scale(add(add(p0, p1), p2), 1.0 / 3.0);
            *a = tri_area(p0, p1, p2);
        });
        // mean edge length for spatial sigma
        let edge_sum: f64 = (0..nf)
            .into_par_iter()
            .map(|f| {
                let t = [mesh.idx[3 * f] as usize, mesh.idx[3 * f + 1] as usize, mesh.idx[3 * f + 2] as usize];
                (length(sub(mesh.pos[t[1]], mesh.pos[t[0]])) + length(sub(mesh.pos[t[2]], mesh.pos[t[1]])) + length(sub(mesh.pos[t[0]], mesh.pos[t[2]]))) as f64
            })
            .sum();
        let mean_edge = (edge_sum / (3 * nf) as f64) as f32;
        let ss2 = 2.0 * (opts.smooth_sigma_spatial * mean_edge).powi(2);
        let sr2 = 2.0 * opts.smooth_sigma_range.powi(2);
        // filtered face normals
        let filtered: Vec<V3> = (0..nf)
            .into_par_iter()
            .map(|f| {
                // neighbour faces = union of incident faces at f's vertices
                let mut neigh: [u32; 96] = [0; 96];
                let mut nn = 0usize;
                for k in 0..3 {
                    let v = mesh.idx[3 * f + k] as usize;
                    for &g in &items[off[v] as usize..off[v + 1] as usize] {
                        if nn < neigh.len() && !neigh[..nn].contains(&g) {
                            neigh[nn] = g;
                            nn += 1;
                        }
                    }
                }
                let nf_n = fnrm[f];
                let cf = fcent[f];
                let mut acc = [0.0f32; 3];
                let mut wsum = 0.0f32;
                for &g in &neigh[..nn] {
                    let d2 = dist2(fcent[g as usize], cf);
                    let nd = dot(fnrm[g as usize], nf_n).clamp(-1.0, 1.0);
                    let w = farea[g as usize] * (-d2 / ss2.max(1e-12)).exp() * (-(1.0 - nd).powi(2) / sr2).exp();
                    acc = add(acc, scale(fnrm[g as usize], w));
                    wsum += w;
                }
                if wsum > 0.0 {
                    normalize(acc)
                } else {
                    nf_n
                }
            })
            .collect();
        // vertex update
        mesh.pos.par_iter_mut().enumerate().for_each(|(v, p)| {
            if pin[v] {
                return;
            }
            let fs = &items[off[v] as usize..off[v + 1] as usize];
            if fs.is_empty() {
                return;
            }
            let mut d = [0.0f32; 3];
            for &f in fs {
                let n = filtered[f as usize];
                d = add(d, scale(n, dot(n, sub(fcent[f as usize], *p))));
            }
            *p = add(*p, scale(d, opts.smooth_strength / fs.len() as f32));
        });
    }
}

/// Point-to-plane snap toward measured cloud points within `resnap_radius`.
fn resnap(mesh: &mut Mesh, grid: &Grid, cloud: &PointCloud, opts: &MeshOptions) -> usize {
    let moved = std::sync::atomic::AtomicUsize::new(0);
    let has_n = cloud.has_normals();
    mesh.pos.par_iter_mut().for_each(|p| {
        if let Some((i, d2)) = grid.nearest(*p, opts.resnap_radius) {
            let q = grid.points()[i as usize];
            let d = d2.sqrt();
            let w = opts.resnap_strength * (1.0 - d / opts.resnap_radius).max(0.0);
            if w <= 0.0 {
                return;
            }
            if has_n {
                let n = cloud.nrm[i as usize];
                let s = dot(sub(*p, q), n);
                *p = sub(*p, scale(n, s * w));
            } else {
                *p = lerp(*p, q, w * 0.5);
            }
            moved.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    });
    moved.load(std::sync::atomic::Ordering::Relaxed)
}

/// IDW colour transfer + per-vertex support (distance to nearest *measured* sample).
fn transfer_color_support(mesh: &mut Mesh, color_grid: Option<&Grid>, support_grid: Option<&Grid>, cloud: &PointCloud, opts: &MeshOptions) {
    // support: distance to nearest measured point
    if let Some(g) = support_grid {
        mesh.support = mesh
            .pos
            .par_iter()
            .map(|p| g.nearest(*p, opts.support_radius).map(|(_, d2)| d2.sqrt()).unwrap_or(opts.support_radius))
            .collect();
    }
    // colour: IDW over knn nearest cloud points (linear space)
    if let Some(g) = color_grid {
        if !cloud.col.is_empty() {
            let k = opts.color_k.max(1);
            mesh.col = mesh
                .pos
                .par_iter()
                .map_init(Vec::new, |out, p| {
                    g.knn(*p, k, opts.color_radius, out);
                    if out.is_empty() {
                        g.knn(*p, 1, opts.support_radius, out);
                    }
                    if out.is_empty() {
                        return [128, 128, 128];
                    }
                    let mut acc = [0.0f64; 3];
                    let mut wsum = 0.0f64;
                    for &(i, d2) in out.iter() {
                        let w = 1.0 / (d2 as f64 + 1e-8);
                        let c = cloud.col[i as usize];
                        for ch in 0..3 {
                            acc[ch] += w * srgb_to_linear(c[ch]) as f64;
                        }
                        wsum += w;
                    }
                    [
                        linear_to_srgb((acc[0] / wsum) as f32),
                        linear_to_srgb((acc[1] / wsum) as f32),
                        linear_to_srgb((acc[2] / wsum) as f32),
                    ]
                })
                .collect();
        }
    }
}

/// QEM decimation via meshopt + GPU vertex-cache/overdraw/fetch reorder.
fn decimate(mesh: &mut Mesh, opts: &MeshOptions) -> serde_json::Value {
    let before = mesh.triangles();
    if opts.max_triangles == 0 || before <= opts.max_triangles {
        return serde_json::json!({ "skipped": true });
    }
    let nv = mesh.pos.len();
    // lock boundary verts if requested
    let locks: Vec<bool> = if opts.decimate_lock_border {
        boundary_vertex_mask(nv, &mesh.idx)
    } else {
        vec![false; nv]
    };
    // colour attributes (linear) for seam preservation
    let attrs: Vec<f32> = if !mesh.col.is_empty() && opts.decimate_color_weight > 0.0 {
        mesh.col.iter().flat_map(|c| [srgb_to_linear(c[0]), srgb_to_linear(c[1]), srgb_to_linear(c[2])]).collect()
    } else {
        vec![0.0; nv * 3]
    };
    let weights = [opts.decimate_color_weight; 3];
    let pos_bytes: &[u8] = bytemuck::cast_slice(&mesh.pos);
    let adapter = match meshopt::VertexDataAdapter::new(pos_bytes, 12, 0) {
        Ok(a) => a,
        Err(_) => return serde_json::json!({ "error": "vertex adapter" }),
    };
    let mut err = 0.0f32;
    let mut idx = meshopt::simplify_with_attributes_and_locks(
        &mesh.idx,
        &adapter,
        &attrs,
        &weights,
        12,
        &locks,
        opts.max_triangles * 3,
        opts.decimate_error,
        meshopt::SimplifyOptions::empty(),
        Some(&mut err),
    );
    // GPU reorder: cache -> overdraw -> fetch (fetch also compacts unused verts)
    meshopt::optimize_vertex_cache_in_place(&mut idx, nv);
    meshopt::optimize_overdraw_in_place(&mut idx, &adapter, 1.05);
    // pack all vertex attributes into one stream so fetch reorder carries them
    #[derive(Clone, Default)]
    struct Vtx {
        pos: V3,
        nrm: V3,
        col: [u8; 3],
        uv: [f32; 2],
        sup: f32,
    }
    let (has_n, has_c, has_u, has_s) = (!mesh.nrm.is_empty(), !mesh.col.is_empty(), !mesh.uv.is_empty(), !mesh.support.is_empty());
    let mut vtx: Vec<Vtx> = (0..nv)
        .map(|i| Vtx {
            pos: mesh.pos[i],
            nrm: if has_n { mesh.nrm[i] } else { [0.0; 3] },
            col: if has_c { mesh.col[i] } else { [0; 3] },
            uv: if has_u { mesh.uv[i] } else { [0.0; 2] },
            sup: if has_s { mesh.support[i] } else { f32::MAX },
        })
        .collect();
    let new_nv = meshopt::optimize_vertex_fetch_in_place(&mut idx, &mut vtx);
    vtx.truncate(new_nv);
    mesh.idx = idx;
    mesh.pos = vtx.iter().map(|v| v.pos).collect();
    mesh.nrm = vtx.iter().map(|v| v.nrm).collect();
    mesh.col = vtx.iter().map(|v| v.col).collect();
    if has_u {
        mesh.uv = vtx.iter().map(|v| v.uv).collect();
    }
    mesh.support = vtx.iter().map(|v| v.sup).collect();
    serde_json::json!({
        "tris_before": before, "tris_after": mesh.triangles(),
        "verts_before": nv, "verts_after": mesh.pos.len(), "error": err,
    })
}

/// Additive helper: derive a LOD chain `[ratio -> Mesh]` for the viewer.
/// Ratios are fractions of the input triangle count, descending.
pub fn lod_chain(mesh: &Mesh, ratios: &[f32], opts: &MeshOptions) -> Vec<Mesh> {
    ratios
        .iter()
        .map(|&r| {
            let mut m = mesh.clone();
            let mut o = opts.clone();
            o.max_triangles = ((mesh.triangles() as f32) * r) as usize;
            o.decimate_lock_border = true;
            decimate(&mut m, &o);
            compute_vertex_normals(&mut m);
            m
        })
        .collect()
}

// ---------------- entry ----------------

pub fn run(mesh: &mut Mesh, cloud: &PointCloud, opts: &MeshOptions) -> Stats {
    let t0 = Instant::now();
    let mut s = serde_json::json!({});
    macro_rules! ms {
        ($name:expr, $t:expr) => {
            s[$name] = serde_json::json!(($t.elapsed().as_secs_f64() * 1e3).round() as u64)
        };
    }

    let t = Instant::now();
    let welded = weld(mesh, opts.weld_eps);
    ms!("weld_ms", t);
    s["welded"] = welded.into();

    let t = Instant::now();
    let degenerate = drop_degenerate(mesh, opts.min_tri_area);
    ms!("degenerate_ms", t);
    s["degenerate_dropped"] = degenerate.into();

    let t = Instant::now();
    let splits = split_nonmanifold_vertices(mesh);
    ms!("nonmanifold_ms", t);
    s["vertex_splits"] = splits.into();

    let t = Instant::now();
    let (flips, conflicts) = orient_faces(mesh);
    ms!("orient_ms", t);
    s["winding_flips"] = flips.into();
    s["winding_conflicts"] = conflicts.into();

    let t = Instant::now();
    let comps = drop_small_components(mesh, opts.min_component_area, opts.min_component_tris);
    ms!("components_ms", t);
    s["component_tris_dropped"] = comps.into();

    let t = Instant::now();
    let (loops, filled) = fill_holes(mesh, opts.hole_max_perimeter, opts.hole_max_verts);
    ms!("holefill_ms", t);
    s["loops_filled"] = loops.into();
    s["fill_tris"] = filled.into();

    let t = Instant::now();
    compute_vertex_normals(mesh);
    bilateral_smooth(mesh, opts);
    ms!("smooth_ms", t);

    // measured-only grids for resnap/support; full cloud for colour fallback
    let t = Instant::now();
    let measured: Vec<V3> = if cloud.flags.is_empty() {
        cloud.pos.clone()
    } else {
        cloud.pos.iter().zip(&cloud.flags).filter(|(_, f)| **f & FLAG_INTERPOLATED == 0).map(|(p, _)| *p).collect()
    };
    let mg = if measured.is_empty() { None } else { Some(Grid::build(&measured, opts.resnap_radius.max(0.02))) };
    let sg = if measured.is_empty() { None } else { Some(Grid::build(&measured, 0.15)) };
    let cg = if cloud.col.is_empty() { None } else { Some(Grid::build(&cloud.pos, opts.color_radius.max(0.02))) };
    ms!("grids_ms", t);

    let t = Instant::now();
    let snapped = mg.as_ref().map(|g| resnap(mesh, g, cloud, opts)).unwrap_or(0);
    ms!("resnap_ms", t);
    s["resnapped"] = snapped.into();

    let t = Instant::now();
    compute_vertex_normals(mesh);
    transfer_color_support(mesh, cg.as_ref(), sg.as_ref(), cloud, opts);
    ms!("transfer_ms", t);

    let t = Instant::now();
    let d = decimate(mesh, opts);
    ms!("decimate_ms", t);
    s["decimate"] = d;

    let t = Instant::now();
    let post = drop_degenerate(mesh, opts.min_tri_area);
    compute_vertex_normals(mesh);
    ms!("final_ms", t);
    s["post_degenerate"] = post.into();

    s["total_ms"] = serde_json::json!((t0.elapsed().as_secs_f64() * 1e3).round() as u64);
    s["vertices"] = mesh.pos.len().into();
    s["triangles"] = mesh.triangles().into();
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quad() -> Mesh {
        // two triangles forming a square in z=0
        Mesh {
            pos: vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
            idx: vec![0, 1, 2, 0, 2, 3],
            ..Default::default()
        }
    }

    #[test]
    fn weld_merges_close_vertices() {
        let mut m = quad();
        m.pos.push([1e-6, 0.0, 0.0]);
        m.idx = vec![0, 1, 2, 4, 2, 3];
        weld(&mut m, 1e-4);
        assert_eq!(m.pos.len(), 4);
        assert_eq!(m.triangles(), 2);
    }

    #[test]
    fn degenerate_removed() {
        let mut m = quad();
        m.idx.extend_from_slice(&[0, 0, 1]); // zero area
        m.idx.extend_from_slice(&[0, 1, 2]); // duplicate face
        drop_degenerate(&mut m, 1e-7);
        assert_eq!(m.triangles(), 2);
    }

    #[test]
    fn bowtie_vertex_splits() {
        // two triangles sharing only a vertex
        let mut m = Mesh {
            pos: vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, -1.0, 0.0]],
            idx: vec![0, 1, 2, 0, 4, 3],
            ..Default::default()
        };
        let splits = split_nonmanifold_vertices(&mut m);
        assert_eq!(splits, 1);
        assert_eq!(m.pos.len(), 6);
        m.validate();
    }

    #[test]
    fn winding_bfs_flips_inconsistent() {
        let mut m = quad();
        m.idx = vec![0, 1, 2, 0, 3, 2]; // second tri reversed
        let (flips, _) = orient_faces(&mut m);
        assert!(flips >= 1);
        // the shared edge is now traversed in opposite directions
        let twin = twin_table(&m.idx);
        let mut paired = 0;
        for e in 0..6 {
            let te = twin[e];
            if te == u32::MAX {
                continue;
            }
            paired += 1;
            let (f, k) = (e / 3, e % 3);
            let (g, kg) = (te as usize / 3, te as usize % 3);
            let a = m.idx[3 * f + k];
            let b = m.idx[3 * f + (k + 1) % 3];
            let c = m.idx[3 * g + kg];
            let d = m.idx[3 * g + (kg + 1) % 3];
            assert_eq!((a, b), (d, c));
        }
        assert_eq!(paired, 2);
    }

    #[test]
    fn small_component_pruned() {
        let mut m = quad();
        // add tiny far triangle
        let base = m.pos.len() as u32;
        m.pos.extend_from_slice(&[[10.0, 0.0, 0.0], [10.001, 0.0, 0.0], [10.0, 0.001, 0.0]]);
        m.idx.extend_from_slice(&[base, base + 1, base + 2]);
        let removed = drop_small_components(&mut m, 1e-3, 2);
        assert_eq!(removed, 1);
        assert_eq!(m.triangles(), 2);
        assert_eq!(m.pos.len(), 4);
    }

    #[test]
    fn hole_fill_closes_square_gap() {
        // 3x3 grid of quads with centre quad removed -> 1 boundary loop of 8 verts
        let mut pos = Vec::new();
        for y in 0..4 {
            for x in 0..4 {
                pos.push([x as f32, 0.0, y as f32]);
            }
        }
        let mut idx = Vec::new();
        for y in 0..3 {
            for x in 0..3 {
                if x == 1 && y == 1 {
                    continue;
                }
                let (a, b, c, d) = (y * 4 + x, y * 4 + x + 1, (y + 1) * 4 + x + 1, (y + 1) * 4 + x);
                idx.extend_from_slice(&[a as u32, b as u32, c as u32, a as u32, c as u32, d as u32]);
            }
        }
        let mut m = Mesh { pos, idx, ..Default::default() };
        orient_faces(&mut m);
        let (loops, tris) = fill_holes(&mut m, 10.0, 16);
        assert_eq!(loops, 1);
        assert!(tris >= 2); // 4-vertex hole -> 2 ear triangles
        m.validate();
        // inner hole gone: only the outer 12 border edges remain
        assert_eq!(boundary_edges(&m.idx).len(), 12);
    }

    #[test]
    fn smooth_preserves_boundary() {
        let mut m = quad();
        let before = m.pos.clone();
        bilateral_smooth(&mut m, &MeshOptions { smooth_iters: 3, ..Default::default() });
        assert_eq!(m.pos, before); // all verts are boundary -> pinned
    }

    #[test]
    fn resnap_pulls_toward_cloud() {
        let mut m = quad();
        m.pos[0][2] = 0.05;
        let cloud = PointCloud { pos: vec![[0.0, 0.0, 0.0]], nrm: vec![[0.0, 0.0, 1.0]], ..Default::default() };
        let g = Grid::build(&cloud.pos, 0.1);
        let moved = resnap(&mut m, &g, &cloud, &MeshOptions::default());
        assert_eq!(moved, 1);
        assert!(m.pos[0][2].abs() < 0.05);
    }

    #[test]
    fn decimate_reduces_and_reorders() {
        // 8x8 grid -> decimate to few tris
        let mut pos = Vec::new();
        for y in 0..9 {
            for x in 0..9 {
                pos.push([x as f32 * 0.1, (x as f32 * 0.1) * (y as f32 * 0.1), y as f32 * 0.1]);
            }
        }
        let mut idx = Vec::new();
        for y in 0..8 {
            for x in 0..8 {
                let (a, b, c, d) = (y * 9 + x, y * 9 + x + 1, (y + 1) * 9 + x + 1, (y + 1) * 9 + x);
                idx.extend_from_slice(&[a as u32, b as u32, c as u32, a as u32, c as u32, d as u32]);
            }
        }
        let mut m = Mesh { pos, idx, ..Default::default() };
        let before = m.triangles();
        let st = decimate(&mut m, &MeshOptions { max_triangles: 20, ..Default::default() });
        assert!(m.triangles() <= 40, "{st}"); // border-locked floor is fine
        assert!(m.triangles() < before);
        m.validate();
        assert!(m.pos.iter().all(|p| p.iter().all(|x| x.is_finite())));
    }

    #[test]
    fn lod_chain_sizes_descend() {
        let mut m = Mesh {
            pos: vec![],
            idx: vec![],
            ..Default::default()
        };
        for y in 0..9 {
            for x in 0..9 {
                m.pos.push([x as f32 * 0.1, 0.0, y as f32 * 0.1]);
            }
        }
        for y in 0..8 {
            for x in 0..8 {
                let (a, b, c, d) = (y * 9 + x, y * 9 + x + 1, (y + 1) * 9 + x + 1, (y + 1) * 9 + x);
                m.idx.extend_from_slice(&[a as u32, b as u32, c as u32, a as u32, c as u32, d as u32]);
            }
        }
        let lods = lod_chain(&m, &[0.5, 0.1], &MeshOptions::default());
        assert_eq!(lods.len(), 2);
        assert!(lods[0].triangles() <= m.triangles() / 2 + 8);
        assert!(lods[1].triangles() <= lods[0].triangles() + 4);
    }

    #[test]
    fn normals_are_unit_and_upward() {
        let mut m = quad();
        compute_vertex_normals(&mut m);
        assert!(m.nrm.iter().all(|n| (length(*n) - 1.0).abs() < 1e-5 && n[2] > 0.9));
    }
}
