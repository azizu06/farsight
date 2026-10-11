//! Ingest. OWNER: W1. Loads every farsight point source into `PointCloud`
//! (+Y up metres) and posed captures into `Capture`.
//!
//! Sources, auto-detected by magic/layout (extension only as a hint):
//! - `points.bin` / `points.delta`: u32 LE count + 16-byte records
//!   (f32 x,y,z metres + u8 r,g,b + pad) — `jetson/mapping/geometry.py`.
//! - dense packets (`/map/dense.bin`, `/dense/chunk.bin`, `/dense/delta.bin`
//!   pages): u32 LE JSON length + metadata + 16-byte `<iiiBBBB` records
//!   (i32 x,y,z millimetres, u8 r,g,b, u8 op: 1 upsert, 0 tombstone).
//! - `mesh.bin` (`/map/mesh.bin`, `/geometry/mesh.bin`): u32 vertex count,
//!   u32 scalar index count, 16-byte vertex records, u32 indices.
//! - PLY: ascii + binary little/big endian, arbitrary property sets.
//! - `.xyz` / `.txt`: one `x y z [r g b]` sample per line.
//! - live `http(s)://host:port`: dense delta pull, manifest+chunks, or the
//!   coarse map fallbacks (see `live.rs`).
//!
//! Knobs live in `IoOptions` (env `FARSIGHT_IO` as a JSON patch; e.g.
//! `FARSIGHT_IO='{"capture_downscale":2}'`). Signatures kept stable for
//! main.rs: `load_source`, `load_points`, `load_capture` use `options()`.

pub mod capture;
pub mod dense;
pub mod live;
pub mod meshbin;
pub mod ply;
pub mod points;
pub mod xyz;

use crate::core::{Capture, PointCloud};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;

/// Ingest knobs. Defaults are the right thing for every preset; override via
/// the `FARSIGHT_IO` env var (JSON object merged over these fields).
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct IoOptions {
    /// Extra area-weighted samples per square metre synthesised when a
    /// triangle mesh (mesh.bin, or PLY faces with `ply_sample_faces`) is
    /// imported as a cloud. 0 = mesh vertices only.
    pub mesh_sample_density: f32,
    /// Whether triangle faces found in a PLY file are sampled into the cloud.
    pub ply_sample_faces: bool,
    /// Integer downscale factor applied to capture images and intrinsics.
    pub capture_downscale: u32,
    /// Bound on dense delta pages pulled from a live source.
    pub max_dense_pages: usize,
    /// Per-request timeout (seconds) for live HTTP sources.
    pub http_timeout_s: f32,
    /// Maximum bytes read for a single HTTP response body (MiB).
    pub http_max_mib: usize,
}

impl Default for IoOptions {
    fn default() -> Self {
        IoOptions {
            mesh_sample_density: 0.0,
            ply_sample_faces: true,
            capture_downscale: 1,
            max_dense_pages: 4096,
            http_timeout_s: 20.0,
            http_max_mib: 1024,
        }
    }
}

/// Active options: defaults merged with the `FARSIGHT_IO` env JSON patch.
pub fn options() -> IoOptions {
    let mut opts = IoOptions::default();
    if let Ok(raw) = std::env::var("FARSIGHT_IO") {
        match serde_json::from_str::<serde_json::Value>(&raw) {
            Ok(patch) => {
                if let Ok(mut base) = serde_json::to_value(&opts) {
                    merge(&mut base, &patch);
                    if let Ok(patched) = serde_json::from_value(base) {
                        opts = patched;
                    }
                }
            }
            Err(e) => eprintln!("[io] ignoring malformed FARSIGHT_IO: {e}"),
        }
    }
    opts
}

fn merge(a: &mut serde_json::Value, b: &serde_json::Value) {
    match (a, b) {
        (serde_json::Value::Object(a), serde_json::Value::Object(b)) => {
            for (k, v) in b {
                merge(a.entry(k.clone()).or_insert(serde_json::Value::Null), v);
            }
        }
        (a, b) => *a = b.clone(),
    }
}

/// Decode a raw body of any supported point format into a cloud.
/// `hint` is an optional file name used only for error messages/logging.
pub fn read_cloud(bytes: &[u8], hint: &str, opts: &IoOptions) -> Result<PointCloud> {
    if bytes.starts_with(b"ply") {
        return ply::read_points_opts(bytes, opts).context("PLY decode");
    }
    if let Some(cloud) = dense::try_read_cloud(bytes).context("dense packet decode")? {
        return Ok(cloud);
    }
    if let Some(cloud) = meshbin::try_read(bytes, opts).context("mesh.bin decode")? {
        return Ok(cloud);
    }
    if let Some(cloud) = points::try_read(bytes).context("points.bin decode")? {
        return Ok(cloud);
    }
    if let Some(cloud) = xyz::try_read(bytes).context("xyz text decode")? {
        return Ok(cloud);
    }
    bail!("unrecognised point source {hint} (not PLY, dense packet, mesh.bin, points.bin or xyz text)")
}

/// Decompress a body by content coding or file extension/magic.
/// `coding` is an HTTP Content-Encoding value; `name` a file name hint.
pub fn decompress(bytes: &[u8], coding: &str, name: &str) -> Result<Vec<u8>> {
    use std::io::Read;
    match coding {
        "" | "identity" => {
            if bytes.starts_with(b"\x1f\x8b") {
                return decompress(bytes, "gzip", name);
            }
            if name.ends_with(".br") {
                return decompress(bytes, "br", name);
            }
            Ok(bytes.to_vec())
        }
        "gzip" | "x-gzip" => {
            let mut out = Vec::with_capacity(bytes.len() * 4);
            flate2::read::GzDecoder::new(bytes).read_to_end(&mut out).context("gzip decode")?;
            Ok(out)
        }
        "br" => {
            let mut out = Vec::with_capacity(bytes.len() * 4);
            brotli::Decompressor::new(bytes, 4096).read_to_end(&mut out).context("brotli decode")?;
            Ok(out)
        }
        other => bail!("unsupported content encoding {other}"),
    }
}

/// Load a point cloud from a file path (format sniffed from content).
pub fn load_points(path: &Path) -> Result<PointCloud> {
    load_points_opts(path, &options())
}

/// Load a point cloud from a file path with explicit options.
pub fn load_points_opts(path: &Path, opts: &IoOptions) -> Result<PointCloud> {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
    let raw = std::fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let bytes = decompress(&raw, "", &name)?;
    read_cloud(&bytes, &name, opts).with_context(|| format!("{}", path.display()))
}

/// Load from a path or an http(s) URL of a farsight server/relay.
pub fn load_source(source: &str) -> Result<PointCloud> {
    load_source_opts(source, &options())
}

/// Load from a path or an http(s) URL with explicit options.
pub fn load_source_opts(source: &str, opts: &IoOptions) -> Result<PointCloud> {
    if source.starts_with("http://") || source.starts_with("https://") {
        return live::pull(source, opts);
    }
    let file = source.strip_prefix("file://").unwrap_or(source);
    load_points_opts(Path::new(file), opts)
}

/// Load a nerfstudio capture directory (transforms.json + images/).
pub fn load_capture(dir: &Path) -> Result<Capture> {
    load_capture_opts(dir, &options())
}

/// Load a nerfstudio capture directory with explicit options.
pub fn load_capture_opts(dir: &Path, opts: &IoOptions) -> Result<Capture> {
    capture::load(dir, opts)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn points_bin(pts: &[[f32; 3]], cols: &[[u8; 3]]) -> Vec<u8> {
        let mut out = (pts.len() as u32).to_le_bytes().to_vec();
        for (p, c) in pts.iter().zip(cols) {
            for v in p {
                out.extend_from_slice(&v.to_le_bytes());
            }
            out.extend_from_slice(c);
            out.push(0);
        }
        out
    }

    #[test]
    fn points_bin_roundtrip() {
        let body = points_bin(&[[1.5, -0.25, 3.0], [0.0, 0.0, -2.0]], &[[255, 0, 1], [2, 255, 128]]);
        let pc = read_cloud(&body, "points.bin", &IoOptions::default()).unwrap();
        assert_eq!(pc.len(), 2);
        assert_eq!(pc.pos[0], [1.5, -0.25, 3.0]);
        assert_eq!(pc.col[1], [2, 255, 128]);
    }

    #[test]
    fn ply_le_and_ascii() {
        let le = b"ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n".to_vec();
        let mut body = le;
        for (p, c) in [([1f32, 2.0, 3.0], [9u8, 8, 7]), ([-4f32, 5.0, -6.0], [6u8, 5, 4])] {
            for v in p {
                body.extend_from_slice(&v.to_le_bytes());
            }
            body.extend_from_slice(&c);
        }
        let pc = ply::read_points(&body).unwrap();
        assert_eq!(pc.len(), 2);
        assert_eq!(pc.pos[1], [-4.0, 5.0, -6.0]);
        assert_eq!(pc.col[0], [9, 8, 7]);

        let ascii = b"ply\nformat ascii 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nproperty float confidence\nend_header\n1 2 3 10 20 30 0.5\n4 5 6 40 50 60 0.9\n";
        let pc = ply::read_points(ascii).unwrap();
        assert_eq!(pc.len(), 2);
        assert_eq!(pc.col[1], [40, 50, 60]);
        assert!((pc.weight[0] - 0.5).abs() < 1e-6);
    }

    #[test]
    fn dense_packet_roundtrip() {
        let meta = br#"{"format":"xyz_mm_rgb8_op","epoch":"e","cursor":3,"reset":false,"snapshot":false,"more":false,"anchor":3,"points":2,"voxel_m":0.01,"ttl_s":30}"#;
        let mut body = (meta.len() as u32).to_le_bytes().to_vec();
        body.extend_from_slice(meta);
        let rec = |x: i32, y: i32, z: i32, rgb: [u8; 3], op: u8| {
            let mut r = Vec::new();
            r.extend_from_slice(&x.to_le_bytes());
            r.extend_from_slice(&y.to_le_bytes());
            r.extend_from_slice(&z.to_le_bytes());
            r.extend_from_slice(&rgb);
            r.push(op);
            r
        };
        body.extend_from_slice(&rec(1500, -20, 3005, [1, 2, 3], 1));
        body.extend_from_slice(&rec(11, 0, 0, [9, 9, 9], 1));
        body.extend_from_slice(&rec(10, 0, 0, [0, 0, 0], 0)); // tombstone kills cell (1,0,0)
        let pc = dense::read_cloud(&body).unwrap();
        assert_eq!(pc.len(), 1);
        assert!((pc.pos[0][0] - 1.5).abs() < 1e-6 && (pc.pos[0][2] - 3.005).abs() < 1e-6);
    }

    #[test]
    fn mesh_bin_detect() {
        // 3 verts, 1 triangle.
        let mut body = 3u32.to_le_bytes().to_vec();
        body.extend_from_slice(&3u32.to_le_bytes());
        for (p, c) in [([0f32, 0.0, 0.0], [255u8, 0, 0]), ([1f32, 0.0, 0.0], [0u8, 255, 0]), ([0f32, 1.0, 0.0], [0u8, 0, 255])] {
            for v in p {
                body.extend_from_slice(&v.to_le_bytes());
            }
            body.extend_from_slice(&c);
            body.push(0);
        }
        for i in [0u32, 1, 2] {
            body.extend_from_slice(&i.to_le_bytes());
        }
        let pc = read_cloud(&body, "mesh.bin", &IoOptions::default()).unwrap();
        assert_eq!(pc.len(), 3);
        let dense_opts = IoOptions { mesh_sample_density: 40.0, ..Default::default() };
        let pc2 = read_cloud(&body, "mesh.bin", &dense_opts).unwrap();
        assert!(pc2.len() > 3); // area 0.5 m² * 40 = 20 extra samples
    }

    #[test]
    fn export_ply_roundtrip() {
        use crate::core::{Mesh, PointCloud};
        let cloud = PointCloud {
            pos: vec![[0.1, 0.2, 0.3], [-1.0, 2.0, -3.0], [4.0, 5.0, 6.0]],
            col: vec![[10, 20, 30], [40, 50, 60], [70, 80, 90]],
            ..Default::default()
        };
        let body = crate::export::ply::points(&cloud);
        let pc = ply::read_points(&body).unwrap();
        assert_eq!(pc.len(), 3);
        assert_eq!(pc.pos[1], [-1.0, 2.0, -3.0]);
        assert_eq!(pc.col[2], [70, 80, 90]);

        let mesh = Mesh {
            pos: cloud.pos.clone(),
            col: cloud.col.clone(),
            idx: vec![0, 1, 2],
            ..Default::default()
        };
        let body = crate::export::ply::mesh(&mesh);
        let opts = IoOptions { ply_sample_faces: true, mesh_sample_density: 20.0, ..Default::default() };
        let pc = read_cloud(&body, "export.ply", &opts).unwrap();
        assert!(pc.len() >= 3, "vertices + face samples, got {}", pc.len());
    }

    #[test]
    fn capture_parity() {
        use crate::core::{Camera, Capture, Image};
        let dir = std::env::temp_dir().join(format!("farsight-io-test-{}-cap", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let img = |v: u8| Image { width: 8, height: 6, rgb: vec![v; 8 * 6 * 3] };
        let cap = Capture {
            cameras: vec![
                Camera::look_at([0.0, 0.5, 1.0], [0.0, 0.0, 0.0], 8, 6, 60.0),
                Camera::look_at([1.0, 0.5, 0.0], [0.0, 0.0, 0.0], 8, 6, 60.0),
            ],
            images: vec![img(40), img(200)],
        };
        crate::synth::write_capture(&dir, &cap).unwrap();
        let got = load_capture(&dir).unwrap();
        assert_eq!(got.cameras.len(), 2);
        assert_eq!(got.images.len(), 2);
        for (a, b) in cap.cameras.iter().zip(&got.cameras) {
            assert!((a.fx - b.fx).abs() < 1e-3 && (a.cx - b.cx).abs() < 1e-3);
            assert!((a.c2w[0][3] - b.c2w[0][3]).abs() < 1e-5 && (a.c2w[2][2] - b.c2w[2][2]).abs() < 1e-5);
        }
        // JPEG is lossy on flat fills but stays close to the source value.
        assert!((got.images[0].rgb[0] as i32 - 40).abs() < 30);
        assert!((got.images[1].rgb[100] as i32 - 200).abs() < 30);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
