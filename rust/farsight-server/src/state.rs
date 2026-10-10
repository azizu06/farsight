//! Single-session map state. Port of jetson/mapping/state.py.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde_json::{json, Value};

use crate::geometry::{encode_points, export_ply, flatten_f, Mat4, IDENTITY};
use crate::phone::{Identity, RgbdFrame};
use crate::surfaces::SurfaceMap;
use crate::voxel::VoxelMap;

/// OpenCV COLORMAP_TURBO equivalent (official turbo approximation polynomial).
fn turbo(v: u8) -> [u8; 3] {
    let x = v as f64 / 255.0;
    let x2 = x * x;
    let x3 = x2 * x;
    let x4 = x3 * x;
    let x5 = x4 * x;
    let r = 0.13572138 + 4.61539260 * x - 42.66032258 * x2 + 132.13108234 * x3 - 152.94239396 * x4 + 59.28637943 * x5;
    let g = 0.09140261 + 2.19418839 * x + 4.84296658 * x2 - 14.18503333 * x3 + 4.27729857 * x4 + 2.82956604 * x5;
    let b = 0.10667330 + 12.64194608 * x - 60.58204836 * x2 + 110.36276771 * x3 - 89.90310912 * x4 + 27.34824973 * x5;
    [
        (r * 255.0).clamp(0.0, 255.0).round() as u8,
        (g * 255.0).clamp(0.0, 255.0).round() as u8,
        (b * 255.0).clamp(0.0, 255.0).round() as u8,
    ]
}

fn monotonic() -> f64 {
    static T0: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    T0.get_or_init(Instant::now).elapsed().as_secs_f64()
}

pub use crate::geometry::rigid_transform;

type PoseIdentity = (i64, String, [f64; 16]);

pub struct Inner {
    pub cloud: VoxelMap,
    pub surfaces: SurfaceMap,
    pub connected: bool,
    pub identity: Option<Identity>,
    pub device: String,
    pub generation: u64,
    pub processing_generation: u64,
    pub paused: bool,
    pub last_received: f64,
    pub last_frame_received: f64,
    pub frame_id: i64,
    pub last_capture: f64,
    pub last_pose_capture: f64,
    pub pose: Mat4,
    pub tracking: String,
    pub frames: u64,
    pub rejected: u64,
    pub dropped: u64,
    pub revision: u64,
    pub fps: f64,
    pub processing_ms: f64,
    pub reason: Option<String>,
    pub jpeg: Option<Vec<u8>>,
    pub depth_jpeg: Option<Vec<u8>>,
    /// Bounded producer clock observations from the last processed frame
    /// (camera.capture_diagnostics; never a qualification decision).
    pub capture_diagnostics: serde_json::Map<String, Value>,
    pub depth_range: Option<[f64; 2]>,
    pub pose_history: VecDeque<(f64, PoseIdentity)>,
    pub scan: Option<Value>,
}

pub struct MapState {
    pub demo: bool,
    pub inner: Mutex<Inner>,
    /// Encoded packet cache keyed on (generation, frames, keyframe count).
    /// Compressible variants (zstd/br/gzip) are cached per map version like
    /// jetson/mapping/compress.py — no re-encoding per request.
    packets: Mutex<Option<PacketCache>>,
    /// Frame consumers (e.g. the nav seam) called with every accepted
    /// RGB-D frame, outside the map lock (state.frame_observers parity).
    pub frame_observers: Mutex<Vec<Arc<dyn Fn(&RgbdFrame) + Send + Sync>>>,
}

impl MapState {
    pub fn new(demo: bool) -> Self {
        let inner = Inner {
            cloud: VoxelMap::default(),
            surfaces: SurfaceMap::default(),
            connected: demo,
            identity: if demo { Some(("simulated-room".into(), 1)) } else { None },
            device: if demo { "Ray-cast room".into() } else { "No sensor".into() },
            generation: 0,
            processing_generation: 0,
            paused: false,
            last_received: 0.0,
            last_frame_received: 0.0,
            frame_id: -1,
            last_capture: -1.0,
            last_pose_capture: -1.0,
            pose: IDENTITY,
            tracking: "not_available".into(),
            frames: 0,
            rejected: 0,
            dropped: 0,
            revision: 0,
            fps: 0.0,
            processing_ms: 0.0,
            reason: Some("Waiting for iPhone RGB-D stream".into()),
            jpeg: None,
            depth_jpeg: None,
            capture_diagnostics: serde_json::Map::new(),
            depth_range: None,
            pose_history: VecDeque::new(),
            scan: None,
        };
        Self {
            demo,
            inner: Mutex::new(inner),
            packets: Mutex::new(None),
            frame_observers: Mutex::new(Vec::new()),
        }
    }

    pub fn start_session(&self, identity: Identity, device: String) {
        let mut s = self.inner.lock().unwrap();
        if s.identity.as_ref() != Some(&identity) {
            Self::clear_locked(&mut s);
            s.identity = Some(identity);
            s.pose = IDENTITY;
            s.jpeg = None;
            s.depth_jpeg = None;
            s.capture_diagnostics.clear();
            s.depth_range = None;
            s.last_received = 0.0;
            s.last_frame_received = 0.0;
            s.frame_id = -1;
        }
        s.connected = true;
        s.device = device;
        s.processing_generation += 1;
        s.last_capture = -1.0;
        s.last_pose_capture = -1.0;
        s.pose_history.clear();
        s.tracking = "not_available".into();
        s.reason = Some("Waiting for synchronized RGB-D frames".into());
    }

    fn clear_locked(s: &mut Inner) {
        s.cloud.clear();
        if s.scan.is_some() {
            s.cloud = VoxelMap::default(); // restore phone fusion's voxel size/budget
        }
        s.surfaces.clear();
        s.generation += 1;
        s.processing_generation += 1;
        s.revision += 1;
        s.frames = 0;
        s.scan = None;
    }

    pub fn clear(&self) {
        Self::clear_locked(&mut self.inner.lock().unwrap());
    }

    /// Replace, never stitch separate rover origins or mix with ARKit geometry.
    pub fn install_scan(&self, cloud: VoxelMap, metadata: Value) {
        let mut s = self.inner.lock().unwrap();
        Self::clear_locked(&mut s);
        s.cloud = cloud;
        s.scan = Some(metadata);
        s.paused = true;
        s.reason = Some("Camera-only snapshot retained; estimated metric depth".into());
    }

    pub fn observe_pose(&self, header: &Value, transform: &Mat4) -> Result<(), String> {
        Self::observe_pose_locked(&mut self.inner.lock().unwrap(), header, transform)
    }

    fn observe_pose_locked(s: &mut Inner, header: &Value, transform: &Mat4) -> Result<(), String> {
        let capture = header["t_capture"].as_f64().unwrap_or(-1.0);
        let identity: PoseIdentity = (
            header["frame_id"].as_i64().unwrap_or(-1),
            header["tracking"].as_str().unwrap_or("").to_string(),
            flatten_f(transform),
        );
        if let Some(previous) = s.pose_history.iter().find(|(c, _)| *c == capture) {
            if previous.1 != identity {
                return Err("Pose and RGB-D disagree about the same capture".into());
            }
        }
        s.pose_history.push_back((capture, identity));
        while s.pose_history.len() > 128 {
            s.pose_history.pop_front();
        }
        if capture <= s.last_pose_capture {
            return Ok(());
        }
        s.last_pose_capture = capture;
        s.last_received = monotonic();
        s.pose = *transform;
        s.tracking = header["tracking"].as_str().unwrap_or("not_available").to_string();
        Ok(())
    }

    /// Process one decoded RGB-D frame. Heavy reconstruction happens unlocked,
    /// matching the Python split-lock structure.
    pub fn process(&self, frame: &RgbdFrame) -> Result<(), String> {
        let started = Instant::now();
        // Mirrors camera_receipt: receipt time minus the producer's encode
        // delay, validated before the identity check like Python.
        let camera_receipt =
            crate::camera::adjusted_receipt(&frame.header, crate::state::monotonic())?;
        let generation;
        let may_fuse;
        {
            let mut s = self.inner.lock().unwrap();
            let frame_identity = (
                frame.header["session_id"].as_str().unwrap_or("").to_string(),
                frame.header["map_epoch"].as_u64().unwrap_or(0),
            );
            if !s.connected || s.identity.as_ref() != Some(&frame_identity) {
                return Ok(());
            }
            let capture = frame.header["t_capture"].as_f64().unwrap_or(-1.0);
            if capture <= s.last_capture {
                s.rejected += 1;
                return Ok(());
            }
            s.last_capture = capture;
            generation = s.processing_generation;
            Self::observe_pose_locked(&mut s, &frame.header, &frame.transform)?;
            s.jpeg = Some(frame.jpeg.clone());
            s.capture_diagnostics = crate::camera::capture_diagnostics(&frame.header);
            s.last_frame_received = camera_receipt;
            s.frame_id = frame.header["frame_id"].as_i64().unwrap_or(-1);
            // Depth heatmap (valid range 0.15..8m, turbo LUT, invalid black).
            let (w, h) = (frame.depth_width, frame.depth_height);
            let mut heat = vec![0u8; w * h * 3];
            let (mut dmin, mut dmax) = (f32::MAX, f32::MIN);
            let mut any = false;
            for i in 0..w * h {
                let d = frame.depth[i];
                let valid = d.is_finite() && d > 0.15 && d <= 8.0;
                if valid {
                    any = true;
                    dmin = dmin.min(d);
                    dmax = dmax.max(d);
                    let scaled = (d / 8.0 * 255.0).clamp(0.0, 255.0) as u8;
                    let [r, g, b] = turbo(255 - scaled);
                    heat[i * 3] = r;
                    heat[i * 3 + 1] = g;
                    heat[i * 3 + 2] = b;
                }
            }
            s.depth_range = if any {
                Some([(dmin as f64 * 100.0).round() / 100.0, (dmax as f64 * 100.0).round() / 100.0])
            } else {
                None
            };
            let mut jpeg_buf = Vec::new();
            let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg_buf, 80);
            image::ImageEncoder::write_image(
                enc,
                &heat,
                w as u32,
                h as u32,
                image::ExtendedColorType::Rgb8,
            )
            .expect("depth JPEG encode");
            s.depth_jpeg = Some(jpeg_buf);
            may_fuse = !s.paused
                && frame.header["tracking"].as_str() == Some("normal")
                && s.tracking == "normal";
            s.reason = if s.paused {
                Some("Fusion paused".into())
            } else if !may_fuse {
                Some("ARKit tracking limited; fusion frozen".into())
            } else {
                None
            };
        }
        // Frame consumers run outside the map lock, even when the frame is
        // too degraded to fuse (mapping/state.py parity).
        for observer in self.frame_observers.lock().unwrap().iter() {
            observer(frame);
        }
        if !may_fuse {
            return Ok(());
        }
        let (points, colors, mesh) = crate::phone::reconstruct(frame, 2, 1);
        let point_count = points.len();
        let mut s = self.inner.lock().unwrap();
        if generation != s.processing_generation
            || !s.connected
            || s.paused
            || s.tracking != "normal"
        {
            return Ok(());
        }
        s.cloud.integrate(&points, &colors);
        s.surfaces.integrate(mesh, &frame.transform);
        s.frames += 1;
        s.revision += 1;
        let duration = started.elapsed().as_secs_f64();
        s.processing_ms = (duration * 1000.0 * 10.0).round() / 10.0;
        s.fps = (1.0 / duration.max(1e-4) * 10.0).round() / 10.0;
        if point_count == 0 {
            s.reason = Some("No confident depth samples in this frame".into());
        }
        Ok(())
    }

    pub fn status(&self) -> Value {
        let s = self.inner.lock().unwrap();
        let age = if s.last_received != 0.0 { Some(monotonic() - s.last_received) } else { None };
        let fresh = s.connected && age.map(|a| a < 2.0).unwrap_or(false);
        let reason = if s.scan.is_some() {
            Some("Camera-only snapshot retained; estimated metric depth".into())
        } else if fresh {
            s.reason.clone()
        } else if !s.connected {
            Some("Sensor disconnected".into())
        } else {
            Some("Waiting for fresh sensor frames".into())
        };
        let pose: Mat4 = if s.scan.is_some() { IDENTITY } else { s.pose };
        let (session_id, map_epoch) = match &s.identity {
            Some((id, epoch)) => (Some(id.clone()), Some(*epoch)),
            None => (None, None),
        };
        json!({
            "type": "status",
            "source": if self.demo { "SIMULATED" } else if s.scan.is_some() { "CAMERA SCAN" } else { "IPHONE LIDAR" },
            "connected": s.connected,
            "fresh": fresh,
            "device": s.device,
            "session_id": session_id,
            "map_epoch": map_epoch,
            "generation": s.generation,
            "revision": s.revision,
            "mesh_revision": s.surfaces.revision,
            "paused": s.paused,
            "tracking": if fresh { s.tracking.clone() } else { "stale".into() },
            "points": s.cloud.cells.len(),
            "triangles": s.surfaces.count,
            "keyframes": s.surfaces.keyframes.len(),
            "frames": s.frames,
            "rejected": s.rejected,
            "dropped": s.dropped,
            "processing_ms": s.processing_ms,
            "processing_fps": s.fps,
            "age_ms": age.map(|a| (a * 1000.0).round() as i64),
            "position": [pose[0][3], pose[1][3], pose[2][3]],
            "transform": flatten_f(&pose).to_vec(),
            "scan": s.scan.clone().unwrap_or(Value::Null),
            "voxel_m": s.cloud.size,
            "depth_range": s.depth_range,
            "reason": reason,
            "camera": fresh,
            "detector_fps": 0,
            "rover": false,
            "estop": true,
        })
    }

    /// Pose heartbeats never refresh the camera's capture receipt time.
    pub fn capture_diagnostics(&self) -> serde_json::Map<String, Value> {
        self.inner.lock().unwrap().capture_diagnostics.clone()
    }

    pub fn camera_snapshot(&self) -> (Option<Vec<u8>>, f64, i64, &'static str) {
        let s = self.inner.lock().unwrap();
        (
            s.jpeg.clone(),
            s.last_frame_received,
            s.frame_id,
            if self.demo { "SIMULATED" } else { "IPHONE LIDAR" },
        )
    }

    /// Cache slot for one encoded resource with lazy compressed variants.
    fn packet_cached(&self) -> std::sync::MutexGuard<'_, Option<PacketCache>> {
        let s = self.inner.lock().unwrap();
        let key = (s.generation, s.frames, s.surfaces.keyframes.len());
        let mut cache = self.packets.lock().unwrap();
        let stale = cache.as_ref().map(|c| c.key != key).unwrap_or(true);
        if stale {
            // Snapshot under the lock, encode after releasing it — the
            // 16 B/point byte expansion must not stall frame fusion.
            let (points, colors) = s.cloud.snapshot();
            let mesh = s.surfaces.encode();
            drop(s);
            let pts = encode_points(&points, &colors);
            let ply = export_ply(&points, &colors);
            *cache = Some(PacketCache { key, pts: VariantCache::new(pts), mesh: VariantCache::new(mesh), ply: VariantCache::new(ply) });
        }
        cache
    }

    /// Returns (body, content-encoding or "") for one map binary route.
    /// Mirrors jetson/mapping/compress.py: cached per map version,
    /// zstd > br > gzip, identity below the 1 KiB floor.
    pub fn packet(&self, which: PacketKind, accept_encoding: &str) -> (Vec<u8>, &'static str) {
        let coding = negotiate_encoding(accept_encoding);
        let mut cache = self.packet_cached();
        let c = cache.as_mut().unwrap();
        let v = match which {
            PacketKind::Points => &mut c.pts,
            PacketKind::Mesh => &mut c.mesh,
            PacketKind::Ply => &mut c.ply,
        };
        if coding.is_none() || v.raw.len() < 1024 {
            return (v.raw.clone(), "");
        }
        let coding = coding.unwrap();
        (v.variant(coding), coding.name())
    }

    pub fn point_packet(&self, accept_encoding: &str) -> (Vec<u8>, &'static str) {
        self.packet(PacketKind::Points, accept_encoding)
    }

    pub fn mesh_packet(&self, accept_encoding: &str) -> (Vec<u8>, &'static str) {
        self.packet(PacketKind::Mesh, accept_encoding)
    }

    pub fn ply_packet(&self, accept_encoding: &str) -> (Vec<u8>, &'static str) {
        self.packet(PacketKind::Ply, accept_encoding)
    }

    pub fn ply(&self) -> Vec<u8> {
        let s = self.inner.lock().unwrap();
        let (points, colors) = s.cloud.snapshot();
        export_ply(&points, &colors)
    }

    pub fn set_paused(&self, paused: bool) {
        self.inner.lock().unwrap().paused = paused;
    }

    pub fn reject(&self, reason: String) {
        let mut s = self.inner.lock().unwrap();
        s.rejected += 1;
        s.reason = Some(format!("Frame rejected: {}", &reason[..reason.len().min(180)]));
    }

    pub fn disconnect(&self) {
        let mut s = self.inner.lock().unwrap();
        s.connected = false;
        s.processing_generation += 1;
    }
}

pub fn now_monotonic() -> f64 {
    monotonic()
}

#[derive(Clone, Copy)]
pub enum PacketKind {
    Points,
    Mesh,
    Ply,
}

/// Raw bytes plus lazily-built compressed variants, reset per map version.
struct VariantCache {
    raw: Vec<u8>,
    zstd: Option<Vec<u8>>,
    br: Option<Vec<u8>>,
    gz: Option<Vec<u8>>,
}

impl VariantCache {
    fn new(raw: Vec<u8>) -> Self {
        Self { raw, zstd: None, br: None, gz: None }
    }

    fn variant(&mut self, coding: Coding) -> Vec<u8> {
        let slot = match coding {
            Coding::Zstd => &mut self.zstd,
            Coding::Br => &mut self.br,
            Coding::Gzip => &mut self.gz,
        };
        if slot.is_none() {
            *slot = Some(match coding {
                Coding::Zstd => zstd_compress(&self.raw),
                Coding::Br => brotli_compress(&self.raw),
                Coding::Gzip => gzip_compress(&self.raw),
            });
        }
        slot.clone().unwrap()
    }
}

struct PacketCache {
    key: (u64, u64, usize),
    pts: VariantCache,
    mesh: VariantCache,
    ply: VariantCache,
}

#[derive(Clone, Copy)]
enum Coding {
    Zstd,
    Br,
    Gzip,
}

impl Coding {
    fn name(self) -> &'static str {
        match self {
            Coding::Zstd => "zstd",
            Coding::Br => "br",
            Coding::Gzip => "gzip",
        }
    }
}

/// zstd preferred over br over gzip; any q=0 refusal drops the coding.
fn negotiate_encoding(accept: &str) -> Option<Coding> {
    let mut zstd = false;
    let mut br = false;
    let mut gz = false;
    for part in accept.split(',') {
        let mut seg = part.trim().splitn(2, ';');
        let name = seg.next().unwrap_or("").trim().to_ascii_lowercase();
        let refused = seg.next().map(|p| p.replace(' ', "").contains("q=0")).unwrap_or(false);
        match name.as_str() {
            "zstd" if !refused => zstd = true,
            "br" if !refused => br = true,
            "gzip" if !refused => gz = true,
            _ => {}
        }
    }
    if zstd {
        Some(Coding::Zstd)
    } else if br {
        Some(Coding::Br)
    } else if gz {
        Some(Coding::Gzip)
    } else {
        None
    }
}

fn gzip_compress(data: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::new(5));
    let _ = enc.write_all(data);
    enc.finish().unwrap_or_default()
}

fn brotli_compress(data: &[u8]) -> Vec<u8> {
    use std::io::Read;
    let mut out = Vec::new();
    let mut r = brotli::CompressorReader::new(data, 1 << 20, 4, 22);
    let _ = r.read_to_end(&mut out);
    out
}

/// Level 9: encodes faster than gzip-5 and br-4 while beating both on
/// points/ply bytes; higher levels cost seconds on the Jetson per version.
fn zstd_compress(data: &[u8]) -> Vec<u8> {
    zstd::bulk::compress(data, 9).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Enough well-separated cells to push points.bin past the 1 KiB floor.
    fn filled_map() -> MapState {
        let map = MapState::new(true);
        let points: Vec<[f32; 3]> = (0..300)
            .map(|i| [(i % 17) as f32, (i / 17) as f32, (i % 5) as f32])
            .collect();
        let colors: Vec<[u8; 3]> = (0..300)
            .map(|i| [(i % 256) as u8, (i % 128) as u8, 42])
            .collect();
        map.inner.lock().unwrap().cloud.integrate(&points, &colors);
        map
    }

    #[test]
    fn negotiate_prefers_zstd_then_br_then_gzip() {
        assert!(matches!(negotiate_encoding("gzip, br, zstd"), Some(Coding::Zstd)));
        assert!(matches!(negotiate_encoding("zstd"), Some(Coding::Zstd)));
        assert!(matches!(negotiate_encoding("br, gzip"), Some(Coding::Br)));
        assert!(matches!(negotiate_encoding("gzip"), Some(Coding::Gzip)));
        assert!(negotiate_encoding("").is_none());
        assert!(negotiate_encoding("identity").is_none());
    }

    #[test]
    fn negotiate_honors_q0_refusal() {
        assert!(matches!(negotiate_encoding("zstd;q=0, gzip"), Some(Coding::Gzip)));
        assert!(matches!(negotiate_encoding("zstd;q=0, br;q=0, gzip;q=0"), None));
    }

    #[test]
    fn zstd_variant_served_only_when_advertised() {
        let map = filled_map();
        let (_, coding) = map.point_packet("br, gzip");
        assert_eq!(coding, "br", "zstd must not be served to a client that did not advertise it");

        let (body, coding) = map.point_packet("gzip, br, zstd");
        assert_eq!(coding, "zstd");
        let decoded = zstd::decode_all(&body[..]).unwrap();
        let (identity, _) = map.point_packet("");
        assert_eq!(decoded, identity, "zstd body must decode to the identity payload");
    }

    #[test]
    fn zstd_refused_falls_back_to_next_offered() {
        let map = filled_map();
        let (_, coding) = map.point_packet("zstd;q=0, gzip");
        assert_eq!(coding, "gzip");
    }

    #[test]
    fn zstd_variants_cover_mesh_and_ply() {
        let map = filled_map();
        let (mesh_body, coding) = map.mesh_packet("zstd");
        // The demo mesh may sit below the 1 KiB floor; ply is the reliable check.
        if coding == "zstd" {
            assert!(!zstd::decode_all(&mesh_body[..]).unwrap().is_empty());
        }
        let (ply_body, coding) = map.ply_packet("zstd");
        assert_eq!(coding, "zstd");
        let (ply_raw, _) = map.ply_packet("");
        assert_eq!(zstd::decode_all(&ply_body[..]).unwrap(), ply_raw);
    }

    #[test]
    fn small_payloads_stay_identity() {
        let map = MapState::new(true); // empty cloud -> raw < 1 KiB
        let (_, coding) = map.point_packet("zstd, br, gzip");
        assert_eq!(coding, "");
    }
}
