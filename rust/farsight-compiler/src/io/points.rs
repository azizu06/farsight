//! `/map/points.bin` and `/map/points.delta`: u32 LE count, then `count`
//! 16-byte records — f32 x,y,z (metres) + u8 r,g,b + pad. Same layout is the
//! vertex block of `mesh.bin` (`jetson/mapping/geometry.py POINT_DTYPE`).
use crate::core::PointCloud;
use anyhow::{bail, Result};

pub const RECORD_SIZE: usize = 16;

/// Decode a 16-byte XYZ/RGB record block (no count header) into parallel
/// position/colour vectors. Used for points.bin bodies and mesh vertices.
pub fn decode_records(records: &[u8], pos: &mut Vec<[f32; 3]>, col: &mut Vec<[u8; 3]>) {
    pos.reserve(records.len() / RECORD_SIZE);
    col.reserve(records.len() / RECORD_SIZE);
    for r in records.chunks_exact(RECORD_SIZE) {
        pos.push([
            f32::from_le_bytes(r[0..4].try_into().unwrap()),
            f32::from_le_bytes(r[4..8].try_into().unwrap()),
            f32::from_le_bytes(r[8..12].try_into().unwrap()),
        ]);
        col.push([r[12], r[13], r[14]]);
    }
}

/// Parse the `points.bin`/`points.delta` body. Returns None when the layout
/// does not match (u32 count + exactly count*16 bytes), so callers can try
/// other formats.
pub fn try_read(bytes: &[u8]) -> Result<Option<PointCloud>> {
    if bytes.len() < 4 {
        return Ok(None);
    }
    let n = u32::from_le_bytes(bytes[0..4].try_into().unwrap()) as usize;
    if bytes.len() != 4 + n * RECORD_SIZE {
        return Ok(None);
    }
    let mut pc = PointCloud { pos: Vec::with_capacity(n), col: Vec::with_capacity(n), ..Default::default() };
    decode_records(&bytes[4..], &mut pc.pos, &mut pc.col);
    Ok(Some(pc))
}

/// Strict parse (fails instead of returning None).
pub fn read_points(bytes: &[u8]) -> Result<PointCloud> {
    match try_read(bytes)? {
        Some(pc) => Ok(pc),
        None => bail!("not a points.bin body (u32 count + 16-byte records)"),
    }
}
