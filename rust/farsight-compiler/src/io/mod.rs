//! Ingest. OWNER: W1. Loads every farsight point source into `PointCloud`
//! (+Y up metres) and posed captures into `Capture`.
//! Baseline supports only binary-little-endian PLY with float xyz + uchar rgb.

pub mod ply;

use crate::core::{Capture, PointCloud};
use anyhow::{bail, Result};
use std::path::Path;

/// Load a point cloud from a file path (format sniffed from content/extension).
pub fn load_points(path: &Path) -> Result<PointCloud> {
    let bytes = std::fs::read(path)?;
    if bytes.starts_with(b"ply") { return ply::read_points(&bytes); }
    bail!("unsupported point source {} (W1: points.bin, dense.bin pages, mesh.bin, ascii PLY, xyz)", path.display())
}

/// Load from a path or an http(s) URL of a farsight server/relay.
pub fn load_source(source: &str) -> Result<PointCloud> {
    if source.starts_with("http://") || source.starts_with("https://") {
        bail!("live fetch not implemented yet (W1)");
    }
    load_points(Path::new(source))
}

/// Load a nerfstudio capture directory (transforms.json + images/).
pub fn load_capture(dir: &Path) -> Result<Capture> {
    bail!("capture loading not implemented yet (W1): {}", dir.display())
}
