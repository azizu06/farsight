//! `mesh.bin` (`/map/mesh.bin` mapper + `/geometry/mesh.bin` ARKit assembler):
//! u32 vertex count, u32 scalar index count, `vcount` 16-byte vertex records
//! (f32 x,y,z + u8 r,g,b + pad), then `icount` u32 indices.
//! Imported as a cloud: the vertices, plus optional triangle area-weighted
//! samples (`IoOptions::mesh_sample_density`, samples/m², 0 = vertices only).
use crate::core::{cross, length, sub, PointCloud, Rng};
use anyhow::{bail, Result};

use super::points::{decode_records, RECORD_SIZE};

/// Parsed mesh.bin content.
pub struct Bin {
    pub pos: Vec<[f32; 3]>,
    pub col: Vec<[u8; 3]>,
    pub idx: Vec<u32>,
}

/// Structural check: exact size, index count divisible by 3, indices in range.
pub fn looks_like(bytes: &[u8]) -> bool {
    if bytes.len() < 8 {
        return false;
    }
    let v = u32::from_le_bytes(bytes[0..4].try_into().unwrap()) as usize;
    let i = u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
    if !i.is_multiple_of(3) || bytes.len() != 8 + v * RECORD_SIZE + i * 4 {
        return false;
    }
    let idx = &bytes[8 + v * RECORD_SIZE..];
    idx.chunks_exact(4).all(|b| (u32::from_le_bytes(b.try_into().unwrap()) as usize) < v)
}

/// Strict parse.
pub fn read(bytes: &[u8]) -> Result<Bin> {
    if bytes.len() < 8 {
        bail!("truncated mesh.bin header");
    }
    let v = u32::from_le_bytes(bytes[0..4].try_into().unwrap()) as usize;
    let i = u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
    if !i.is_multiple_of(3) || bytes.len() != 8 + v * RECORD_SIZE + i * 4 {
        bail!("mesh.bin size mismatch ({v} vertices, {i} indices)");
    }
    let mut mesh = Bin {
        pos: Vec::with_capacity(v),
        col: Vec::with_capacity(v),
        idx: Vec::with_capacity(i),
    };
    decode_records(&bytes[8..8 + v * RECORD_SIZE], &mut mesh.pos, &mut mesh.col);
    for b in bytes[8 + v * RECORD_SIZE..].chunks_exact(4) {
        let ix = u32::from_le_bytes(b.try_into().unwrap());
        if ix as usize >= v {
            bail!("mesh.bin index {ix} out of range ({v} vertices)");
        }
        mesh.idx.push(ix);
    }
    Ok(mesh)
}

/// Triangle area in m².
#[inline]
fn tri_area(a: [f32; 3], b: [f32; 3], c: [f32; 3]) -> f32 {
    0.5 * length(cross(sub(b, a), sub(c, a)))
}

/// `density` samples/m² (rounded per triangle, deterministic per input).
/// Colours are barycentric mixes of the vertex colours.
pub fn sample_faces(pos: &[[f32; 3]], col: &[[u8; 3]], idx: &[u32], density: f32) -> PointCloud {
    let mut out = PointCloud::default();
    if density <= 0.0 {
        return out;
    }
    for (t, tri) in idx.chunks_exact(3).enumerate() {
        let (a, b, c) = (tri[0] as usize, tri[1] as usize, tri[2] as usize);
        if a >= pos.len() || b >= pos.len() || c >= pos.len() {
            continue;
        }
        let (pa, pb, pc) = (pos[a], pos[b], pos[c]);
        let n = (tri_area(pa, pb, pc) * density).round() as usize;
        if n == 0 {
            continue;
        }
        let grey = [200, 200, 200];
        let (ca, cb, cc) = (
            col.get(a).copied().unwrap_or(grey),
            col.get(b).copied().unwrap_or(grey),
            col.get(c).copied().unwrap_or(grey),
        );
        let mut rng = Rng::new(0x9e37_79b9_7f4a_7c15u64.wrapping_mul(t as u64 + 1));
        for _ in 0..n {
            // Uniform barycentric via sqrt trick.
            let r1 = rng.f32().sqrt();
            let (u, v) = (1.0 - r1, r1 * (1.0 - rng.f32()));
            let w = 1.0 - u - v;
            out.pos.push([
                pa[0] * u + pb[0] * v + pc[0] * w,
                pa[1] * u + pb[1] * v + pc[1] * w,
                pa[2] * u + pb[2] * v + pc[2] * w,
            ]);
            out.col.push([
                (ca[0] as f32 * u + cb[0] as f32 * v + cc[0] as f32 * w + 0.5) as u8,
                (ca[1] as f32 * u + cb[1] as f32 * v + cc[1] as f32 * w + 0.5) as u8,
                (ca[2] as f32 * u + cb[2] as f32 * v + cc[2] as f32 * w + 0.5) as u8,
            ]);
        }
    }
    out
}

/// mesh.bin -> cloud (vertices + optional face samples).
pub fn try_read(bytes: &[u8], opts: &super::IoOptions) -> Result<Option<PointCloud>> {
    if !looks_like(bytes) {
        return Ok(None);
    }
    let mesh = read(bytes)?;
    let mut cloud = PointCloud { pos: mesh.pos, col: mesh.col, ..Default::default() };
    if opts.mesh_sample_density > 0.0 {
        let extra = sample_faces(&cloud.pos, &cloud.col, &mesh.idx, opts.mesh_sample_density);
        cloud.pos.extend(extra.pos);
        cloud.col.extend(extra.col);
    }
    Ok(Some(cloud))
}
