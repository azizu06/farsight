//! Plain-text point files (.xyz/.txt/.csv): one `x y z [r g b]` sample per
//! line, whitespace or comma separated, `#` comments. Optional colour columns
//! may be 0-255 ints or 0-1 floats (auto-detected from the column maximum).
use crate::core::PointCloud;
use anyhow::Result;

/// Parse an ascii xyz body; None when it does not look like one.
pub fn try_read(bytes: &[u8]) -> Result<Option<PointCloud>> {
    let text = match std::str::from_utf8(bytes) {
        Ok(t) => t,
        Err(_) => return Ok(None),
    };
    let mut rows: Vec<Vec<f32>> = Vec::new();
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        let fields: Vec<&str> = line.split([',', ' ', '\t']).filter(|s| !s.is_empty()).collect();
        if fields.len() < 3 || fields.len() == 4 || fields.len() == 5 {
            return Ok(None); // ambiguous column count: refuse to guess
        }
        let row: Option<Vec<f32>> = fields.iter().map(|f| f.parse::<f32>().ok()).collect();
        match row {
            Some(r) if r.iter().all(|v| v.is_finite() && v.abs() < 1e7) => rows.push(r),
            _ => return Ok(None),
        }
    }
    if rows.is_empty() {
        return Ok(None);
    }
    let has_col = rows[0].len() >= 6;
    let mut pc = PointCloud { pos: Vec::with_capacity(rows.len()), col: Vec::new(), ..Default::default() };
    if has_col {
        // 0-255 byte colours vs 0-1 floats: decide on the column max.
        let max = rows
            .iter()
            .flat_map(|r| r[3..6].iter().copied())
            .fold(0f32, f32::max);
        let scale = if max <= 1.0 { 255.0 } else { 1.0 };
        pc.col.reserve(rows.len());
        for r in &rows {
            pc.pos.push([r[0], r[1], r[2]]);
            pc.col.push([
                (r[3] * scale).clamp(0.0, 255.0) as u8,
                (r[4] * scale).clamp(0.0, 255.0) as u8,
                (r[5] * scale).clamp(0.0, 255.0) as u8,
            ]);
        }
    } else {
        for r in &rows {
            pc.pos.push([r[0], r[1], r[2]]);
        }
    }
    Ok(Some(pc))
}
