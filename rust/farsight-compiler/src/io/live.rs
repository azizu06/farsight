//! Live pull from a farsight-server or relay origin (`http(s)://host:port`).
//! Preference order (richest measured detail first):
//!   1. `/dense/delta.bin` — Mac retained cloud, paged journal/snapshot.
//!   2. `/dense/manifest` + `/dense/chunk.bin?id=_&lod=0` — Mac view chunks.
//!   3. `/map/dense.bin` — Jetson dense producer, same delta paging wire.
//!   4. `/map/points.bin` — legacy coarse map.
//!   5. `/map/mesh.bin` + `/geometry/mesh.bin` — assembled ARKit mesh.
//!   6. `/map/export.ply` — coarse PLY.
//!
//! Responses may be brotli/gzip encoded; we advertise both.
use crate::core::PointCloud;
use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::io::Read;
use std::time::Duration;

use super::{dense, meshbin, ply, points, IoOptions};

struct Fetch {
    timeout: Duration,
    max_bytes: usize,
}

impl Fetch {
    fn get(&self, url: &str) -> Result<Vec<u8>> {
        let resp = ureq::get(url)
            .set("Accept-Encoding", "br, gzip")
            .timeout(self.timeout)
            .call()
            .map_err(|e| anyhow::anyhow!("GET {url}: {e}"))?;
        let coding = resp.header("Content-Encoding").unwrap_or("").to_string();
        let mut body = Vec::new();
        resp.into_reader()
            .take((self.max_bytes + 1) as u64)
            .read_to_end(&mut body)
            .with_context(|| format!("GET {url}: read"))?;
        if body.len() > self.max_bytes {
            bail!("GET {url}: response exceeds {} MiB", self.max_bytes / (1 << 20));
        }
        super::decompress(&body, &coding, url)
    }
}

fn join(base: &str, path: &str) -> String {
    format!("{}{}", base.trim_end_matches('/'), path)
}

/// Page a delta endpoint until `more` is false, applying upserts/tombstones.
/// `route` is `/dense/delta.bin` or `/map/dense.bin` (same wire contract).
fn pull_delta(f: &Fetch, base: &str, route: &str, source: &str, opts: &IoOptions) -> Result<PointCloud> {
    let mut acc = dense::Accumulator::new();
    let mut after: i64 = 0;
    let mut snapshot = false;
    let mut anchor: Option<i64> = None;
    let mut epoch: Option<String> = None;
    let mut pages = 0usize;
    loop {
        if pages >= opts.max_dense_pages {
            bail!("dense pull exceeded {} pages", opts.max_dense_pages);
        }
        let mut url = format!("{}?after={after}&limit=8192&source={source}", join(base, route));
        if snapshot {
            url.push_str("&snapshot=true");
        }
        if let Some(a) = anchor {
            url.push_str(&format!("&anchor={a}"));
        }
        if let Some(e) = &epoch {
            url.push_str(&format!("&epoch={e}"));
        }
        let body = f.get(&url)?;
        let (meta, data) = dense::unpack(&body).with_context(|| format!("{route} page {pages}"))?;
        if meta.get("reset").and_then(Value::as_bool).unwrap_or(false) {
            acc.clear();
        }
        acc.apply(data);
        after = meta.get("cursor").and_then(Value::as_i64).unwrap_or(after);
        snapshot = meta.get("snapshot").and_then(Value::as_bool).unwrap_or(false);
        if let Some(a) = meta.get("anchor").and_then(Value::as_i64) {
            anchor = Some(a);
        }
        if let Some(e) = meta.get("epoch").and_then(Value::as_str) {
            epoch = Some(e.to_string());
        }
        pages += 1;
        if !meta.get("more").and_then(Value::as_bool).unwrap_or(false) {
            break;
        }
    }
    let cloud = acc.into_cloud();
    if cloud.is_empty() {
        bail!("{route}: empty cloud");
    }
    Ok(cloud)
}

/// Manifest + per-chunk pull. The manifest selects up to 32 nearest chunks to
/// a position; probe a small grid to cover the whole retained cloud.
fn pull_chunks(f: &Fetch, base: &str, source: &str, _opts: &IoOptions) -> Result<PointCloud> {
    let src_q = if source == "rgbd" { "" } else { "&source=geometry" };
    let probes = [
        (0.0, 0.0, 0.0),
        (10.0, 0.0, 10.0),
        (-10.0, 0.0, -10.0),
        (10.0, 0.0, -10.0),
        (-10.0, 0.0, 10.0),
        (0.0, 5.0, 0.0),
    ];
    let mut ids: Vec<String> = Vec::new();
    for (x, y, z) in probes {
        let url = format!("{}/dense/manifest?x={x}&y={y}&z={z}{src_q}", base.trim_end_matches('/'));
        let Ok(body) = f.get(&url) else { continue };
        let Ok(meta) = serde_json::from_slice::<Value>(&body) else { continue };
        if let Some(chunks) = meta.get("chunks").and_then(Value::as_array) {
            for c in chunks {
                if let Some(id) = c.get("id").and_then(Value::as_str) {
                    if !ids.iter().any(|i| i == id) {
                        ids.push(id.to_string());
                    }
                }
            }
        }
    }
    if ids.is_empty() {
        bail!("dense manifest: no chunks");
    }
    let mut acc = dense::Accumulator::new();
    let mut got = 0usize;
    for id in &ids {
        let url = format!("{}/dense/chunk.bin?id={id}&lod=0{src_q}", base.trim_end_matches('/'));
        let Ok(body) = f.get(&url) else { continue };
        if let Ok((_, data)) = dense::unpack(&body) {
            acc.apply(data);
            got += 1;
        }
    }
    if got == 0 {
        bail!("dense chunks: no readable chunks");
    }
    let cloud = acc.into_cloud();
    if cloud.is_empty() {
        bail!("dense chunks: empty cloud");
    }
    Ok(cloud)
}

/// Full live pull: walk the preference chain, first success wins.
pub fn pull(base: &str, opts: &IoOptions) -> Result<PointCloud> {
    let f = Fetch {
        timeout: Duration::from_secs_f64((opts.http_timeout_s as f64).clamp(1.0, 120.0)),
        max_bytes: opts.http_max_mib * (1 << 20),
    };
    let mut errors = Vec::new();
    let mut try_step = |name: &str, step: &mut dyn FnMut() -> Result<PointCloud>| -> Option<PointCloud> {
        match step() {
            Ok(cloud) => {
                eprintln!("[io] live {base}: {name} -> {} points", cloud.len());
                Some(cloud)
            }
            Err(e) => {
                errors.push(format!("{name}: {e:#}"));
                None
            }
        }
    };
    if let Some(c) = try_step("/dense/delta.bin", &mut || pull_delta(&f, base, "/dense/delta.bin", "rgbd", opts)) {
        return Ok(c);
    }
    if let Some(c) = try_step("/dense/manifest+chunks", &mut || pull_chunks(&f, base, "rgbd", opts)) {
        return Ok(c);
    }
    if let Some(c) = try_step("/map/dense.bin", &mut || pull_delta(&f, base, "/map/dense.bin", "rgbd", opts)) {
        return Ok(c);
    }
    // Geometry lane (native ARKit) as an explicit source where rgbd is empty.
    if let Some(c) = try_step("/dense/delta.bin?source=geometry", &mut || pull_delta(&f, base, "/dense/delta.bin", "geometry", opts)) {
        return Ok(c);
    }
    for (name, path) in [
        ("/map/points.bin", "/map/points.bin"),
        ("/map/points.delta?after=0", "/map/points.delta?after=0"),
        ("/map/mesh.bin", "/map/mesh.bin"),
        ("/geometry/mesh.bin", "/geometry/mesh.bin"),
        ("/map/export.ply", "/map/export.ply"),
    ] {
        if let Some(c) = try_step(name, &mut || {
            let body = f.get(&join(base, path))?;
            match path {
                "/map/points.bin" | "/map/points.delta?after=0" => points::read_points(&body),
                "/map/export.ply" => ply::read_points_opts(&body, opts),
                _ => {
                    let m = meshbin::read(&body)?;
                    let mut cloud = PointCloud { pos: m.pos, col: m.col, ..Default::default() };
                    let extra = meshbin::sample_faces(&cloud.pos, &cloud.col, &m.idx, opts.mesh_sample_density);
                    cloud.pos.extend(extra.pos);
                    cloud.col.extend(extra.col);
                    Ok(cloud)
                }
            }
        }) {
            return Ok(c);
        }
    }
    bail!("live pull from {base} failed — {}", errors.join(" | "))
}
