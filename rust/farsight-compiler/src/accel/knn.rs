//! Tiled brute-force kNN / radius queries over batches of query points.
//!
//! GPU path: one thread per query scans the whole point cloud in
//! workgroup-shared tiles, keeping a sorted top-K in registers — O(nq*np)
//! work, so use it for bounded batches (densify seeds, cell gathers), not as a
//! global index replacement (W2's `spatial` index remains the CPU structure).
//! CPU twin exists for parity tests and the no-adapter fallback.

#[cfg(any(target_os = "macos", feature = "gpu"))]
use super::imp::DevInner;
use rayon::prelude::*;

const WG: u32 = 64;
const TILE: u32 = 512;

fn wgsl(k: u32) -> String {
    let k = k.max(1).min(64);
    format!(
        r#"
const WG: u32 = {WG}u;
const TP: u32 = {TILE}u;
const K: u32 = {k}u;
struct Params {{ n: u32, m: u32, r2: f32, pad: u32 }};
@group(0) @binding(0) var<uniform> P: Params;
// flat xyz-interleaved input (vec3 storage stride is 16B, our data is 12B)
@group(0) @binding(1) var<storage, read> pts: array<f32>;
@group(0) @binding(2) var<storage, read> qs: array<f32>;
fn get3(a: u32, sel: u32) -> vec3<f32> {{
  if (sel == 1u) {{ return vec3(qs[a * 3u], qs[a * 3u + 1u], qs[a * 3u + 2u]); }}
  return vec3(pts[a * 3u], pts[a * 3u + 1u], pts[a * 3u + 2u]);
}}
@group(0) @binding(3) var<storage, read_write> out_idx: array<u32>;
@group(0) @binding(4) var<storage, read_write> out_d2: array<f32>;

var<workgroup> tile: array<vec3<f32>, TP>;

@compute @workgroup_size(WG)
fn main(@builtin(local_invocation_id) lid: vec3<u32>, @builtin(workgroup_id) wid: vec3<u32>) {{
  let qid = wid.x * WG + lid.x;
  var bd: array<f32, K>;
  var bi: array<u32, K>;
  for (var i = 0u; i < K; i++) {{ bd[i] = P.r2; bi[i] = 0xFFFFFFFFu; }}
  let q = select(vec3(0.0), get3(qid, 1u), qid < P.m);
  for (var base = 0u; base < P.n; base += TP) {{
    for (var t = lid.x; t < TP; t += WG) {{
      let p = base + t;
      tile[t] = select(vec3(0.0), get3(p, 0u), p < P.n);
    }}
    workgroupBarrier();
    let cnt = min(TP, P.n - base);
    for (var t = 0u; t < cnt; t++) {{
      if (qid >= P.m) {{ break; }}
      let d = tile[t] - q;
      let d2 = dot(d, d);
      if (d2 < bd[K - 1u]) {{
        var j = K - 1u;
        while (j > 0u && bd[j - 1u] > d2) {{ bd[j] = bd[j - 1u]; bi[j] = bi[j - 1u]; j--; }}
        bd[j] = d2; bi[j] = base + t;
      }}
    }}
    workgroupBarrier();
  }}
  if (qid < P.m) {{
    for (var i = 0u; i < K; i++) {{
      out_idx[qid * K + i] = bi[i];
      out_d2[qid * K + i] = bd[i];
    }}
  }}
}}
"#
    )
}

#[cfg(any(target_os = "macos", feature = "gpu"))]
fn knn_gpu_dev(
    dev: &DevInner,
    key: &str,
    pts: &[f32],
    queries: &[f32],
    k: usize,
    r2: f32,
) -> (Vec<u32>, Vec<f32>) {
    let m = (queries.len() / 3) as u32;
    let n = (pts.len() / 3) as u32;
    #[repr(C)]
    #[derive(bytemuck::Pod, bytemuck::Zeroable, Clone, Copy)]
    struct Params {
        n: u32,
        m: u32,
        r2: f32,
        pad: u32,
    }
    let pipe = dev.pipeline(key, &wgsl(k as u32), "main");
    let p_buf = dev.buf_uniform("params", &Params { n, m, r2, pad: 0 });
    let pts_b = dev.buf("pts", pts);
    let q_b = dev.buf("qs", queries);
    let idx_b = dev.buf_empty("out_idx", m as u64 * k as u64 * 4);
    let d2_b = dev.buf_empty("out_d2", m as u64 * k as u64 * 4);
    let bind = dev.bind(&pipe, &[&p_buf, &pts_b, &q_b, &idx_b, &d2_b]);
    let idx: Vec<u32> = dev.run_and_read(&pipe, &bind, m, WG, &idx_b, m as u64 * k as u64 * 4);
    let d2: Vec<f32> = dev.run_and_read(&pipe, &bind, m, WG, &d2_b, m as u64 * k as u64 * 4);
    (idx, d2)
}

/// k nearest points per query, sorted ascending by squared distance.
/// `pts`/`queries` are flat xyz-interleaved f32. Returns (indices, dist²),
/// each `m*k`; missing results are `u32::MAX` / `r2`.
/// Uses the GPU when available, else the rayon CPU twin.
pub fn knn(pts: &[f32], queries: &[f32], k: usize, radius: f32) -> (Vec<u32>, Vec<f32>) {
    let r2 = radius * radius;
    #[cfg(any(target_os = "macos", feature = "gpu"))]
    if let Some(g) = super::gpu() {
        if !queries.is_empty() && !pts.is_empty() {
            return knn_gpu_dev(&g.inner, "knn", pts, queries, k, r2);
        }
    }
    knn_cpu(pts, queries, k, r2)
}

/// CPU twin of [`knn`] (also the parity reference for the GPU kernel).
pub fn knn_cpu(pts: &[f32], queries: &[f32], k: usize, r2: f32) -> (Vec<u32>, Vec<f32>) {
    let m = queries.len() / 3;
    let n = pts.len() / 3;
    let k = k.max(1);
    let mut idx = vec![u32::MAX; m * k];
    let mut d2 = vec![r2; m * k];
    idx.par_chunks_mut(k)
        .zip(d2.par_chunks_mut(k))
        .enumerate()
        .for_each(|(qi, (bi, bd))| {
            let q = &queries[qi * 3..qi * 3 + 3];
            for p in 0..n {
                let dx = pts[p * 3] - q[0];
                let dy = pts[p * 3 + 1] - q[1];
                let dz = pts[p * 3 + 2] - q[2];
                let dd = dx * dx + dy * dy + dz * dz;
                if dd < bd[k - 1] {
                    let mut j = k - 1;
                    while j > 0 && bd[j - 1] > dd {
                        bd[j] = bd[j - 1];
                        bi[j] = bi[j - 1];
                        j -= 1;
                    }
                    bd[j] = dd;
                    bi[j] = p as u32;
                }
            }
        });
    (idx, d2)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn refcloud(n: usize) -> Vec<f32> {
        // deterministic pseudo-random scatter
        let mut v = Vec::with_capacity(n * 3);
        let mut s = 0x9E3779B9u64;
        for _ in 0..n {
            for _ in 0..3 {
                s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                v.push(((s >> 33) & 0xFFFFFF) as f32 / 0xFFFFFF as f32 * 10.0 - 5.0);
            }
        }
        v
    }

    #[test]
    fn cpu_finds_nearest() {
        let pts = vec![0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 2.0, 0.0, 0.0];
        let q = vec![0.1, 0.0, 0.0];
        let (idx, d2) = knn_cpu(&pts, &q, 2, 10.0);
        assert_eq!(idx[0], 0);
        assert_eq!(idx[1], 1);
        assert!(d2[0] < d2[1]);
    }

    #[test]
    fn gpu_matches_cpu() {
        let Some(g) = super::super::gpu() else { eprintln!("[skip] no gpu adapter"); return; };
        let pts = refcloud(4000);
        let q = refcloud(64);
        let (gi, gd) = knn_gpu_dev(&g.inner, "knn", &pts, &q, 8, 100.0);
        let (ci, cd) = knn_cpu(&pts, &q, 8, 100.0);
        assert_eq!(gi, ci);
        for (a, b) in gd.iter().zip(cd.iter()) {
            assert!((a - b).abs() < 1e-4, "{a} vs {b}");
        }
    }
}
