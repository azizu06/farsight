//! PLY read (baseline: binary_little_endian float x,y,z + uchar red,green,blue only).
use crate::core::PointCloud;
use anyhow::{bail, Result};

pub fn read_points(bytes: &[u8]) -> Result<PointCloud> {
    let end = bytes.windows(11).position(|w| w == b"end_header\n").ok_or_else(|| anyhow::anyhow!("no end_header"))? + 11;
    let header = std::str::from_utf8(&bytes[..end])?;
    if !header.contains("binary_little_endian") { bail!("baseline reader needs binary_little_endian PLY"); }
    let mut n = 0usize;
    let mut props = Vec::new();
    let mut in_vertex = false;
    for line in header.lines() {
        let t: Vec<&str> = line.split_whitespace().collect();
        match t.as_slice() {
            ["element", "vertex", c] => { n = c.parse()?; in_vertex = true; }
            ["element", ..] => in_vertex = false,
            ["property", ty, name] if in_vertex => props.push((ty.to_string(), name.to_string())),
            _ => {}
        }
    }
    let want = [("float", "x"), ("float", "y"), ("float", "z"), ("uchar", "red"), ("uchar", "green"), ("uchar", "blue")];
    if props.len() != 6 || props.iter().zip(want).any(|((t, n), (wt, wn))| t != wt || n != wn) {
        bail!("baseline reader needs exactly float x y z uchar red green blue (W1 generalises)");
    }
    let body = &bytes[end..];
    if body.len() < n * 15 { bail!("truncated PLY"); }
    let mut pc = PointCloud { pos: Vec::with_capacity(n), col: Vec::with_capacity(n), ..Default::default() };
    for r in body[..n * 15].chunks_exact(15) {
        let f = |o: usize| f32::from_le_bytes([r[o], r[o + 1], r[o + 2], r[o + 3]]);
        pc.pos.push([f(0), f(4), f(8)]);
        pc.col.push([r[12], r[13], r[14]]);
    }
    Ok(pc)
}
