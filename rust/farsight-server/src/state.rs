//! Single-session map state. Port of jetson/mapping/state.py.

use bytes::Bytes;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde_json::{json, Value};

use crate::dense::DenseMap;
use crate::dense_worker::DenseWorker;
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
    /// Measured dense cloud, swapped whole on reset/session change — a
    /// running visual job retains the old Arc and its stale generation
    /// fails the worker's current() check (7ca27e0 swap-in-new-cloud).
    pub dense: Arc<DenseMap>,
    pub connected: bool,
    pub identity: Option<Identity>,
    pub device: String,
    pub generation: u64,
    pub processing_generation: u64,
    /// Socket lifetime; a visual map reset preserves the live socket.
    /// Incremented on each new phone socket session (and disconnect) so a
    /// cancelled socket worker cannot publish into a later session even
    /// when (session_id, map_epoch) identity is reused.
    pub socket_generation: u64,
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

/// Exact Python reason when --fusion-bypass is set (state.py FUSION_BYPASSED).
const FUSION_BYPASSED: &str = "Geometry lane external; fusion bypassed";

/// Fused map geometry behind its own mutex. The voxel+surface integrate
/// used to run under `inner`, stalling every `inner` reader
/// (observe_pose, status, packet encoders) for the whole integration.
/// Lock order: `inner` before `geometry` — never hold `geometry` while
/// acquiring `inner`.
struct Geometry {
    cloud: VoxelMap,
    surfaces: SurfaceMap,
}

pub struct MapState {
    pub demo: bool,
    pub inner: Arc<Mutex<Inner>>,
    /// Heavy-write geometry lane (fused cloud + surfaces); see Geometry.
    geometry: Mutex<Geometry>,
    /// Bounded latest-only measured visual worker (dense_worker.py port).
    pub dense_worker: DenseWorker,
    /// Encoded packet cache keyed on (generation, frames, keyframe count).
    /// Compressible variants (zstd/br/gzip) are cached per map version like
    /// jetson/mapping/compress.py — no re-encoding per request.
    packets: Mutex<Option<PacketCache>>,
    /// Frame consumers (e.g. the nav seam) called with every accepted
    /// RGB-D frame, outside the map lock (state.frame_observers parity).
    pub frame_observers: Mutex<Vec<Arc<dyn Fn(&RgbdFrame) + Send + Sync>>>,
    /// Bumped on every map revision so /ws/map subscribers push the fresh
    /// packet at fusion time instead of waiting out the 500 ms status tick.
    revision_tx: tokio::sync::watch::Sender<u64>,
    /// Visual wire caps (board-agreed defaults 30k points / 40k triangles).
    /// Bounds only the served/exported payload; the internal map, Nav2
    /// depth/occupancy and labels keep full resolution. 0 = uncapped.
    pub points_cap: usize,
    pub triangles_cap: usize,
    /// Last fusion duration, stored as f64 bits for lock-free reads (the
    /// /video/status handler answers while the map worker is busy).
    pub processing_ms: AtomicU64,
    /// Opt-in Jetson fusion bypass (--fusion-bypass / FARSIGHT_FUSION_BYPASS,
    /// #109): keep /phone pose +
    /// depth (Nav2) and the camera feed (inference/control), but skip RGB-D
    /// point/mesh fusion — geometry arrives via the phone ARKit lane
    /// assembled on the Mac instead. Status stays honest about map source.
    pub fusion_bypass: bool,
}

impl MapState {
    pub fn new(demo: bool) -> Self {
        let inner = Inner {
            connected: demo,
            identity: if demo { Some(("simulated-room".into(), 1)) } else { None },
            device: if demo { "Ray-cast room".into() } else { "No sensor".into() },
            generation: 0,
            processing_generation: 0,
            socket_generation: 0,
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
            reason: Some("Waiting for iPhone RGB-D stream".into()),
            jpeg: None,
            depth_jpeg: None,
            capture_diagnostics: serde_json::Map::new(),
            depth_range: None,
            pose_history: VecDeque::new(),
            scan: None,
            dense: Arc::new(DenseMap::new(true)),
        };
        let inner = Arc::new(Mutex::new(inner));
        // dense_worker.current: generation + cloud identity + live session.
        let inner_for_worker = inner.clone();
        let dense_worker = DenseWorker::new(Arc::new(
            move |generation: u64, cloud: &Arc<DenseMap>| {
                let s = inner_for_worker.lock().unwrap();
                generation == s.processing_generation
                    && Arc::ptr_eq(cloud, &s.dense)
                    && s.connected
                    && !s.paused
                    && s.tracking == "normal"
            },
        ));
        let (revision_tx, _) = tokio::sync::watch::channel(0u64);
        Self {
            demo,
            inner,
            geometry: Mutex::new(Geometry {
                cloud: VoxelMap::default(),
                surfaces: SurfaceMap::default(),
            }),
            dense_worker,
            packets: Mutex::new(None),
            frame_observers: Mutex::new(Vec::new()),
            revision_tx,
            points_cap: 0,
            triangles_cap: 0,
            processing_ms: AtomicU64::new(0),
            fusion_bypass: false,
        }
    }

    /// Fused-cloud cell count (geometry lane) for tests/introspection.
    pub fn cloud_cells(&self) -> usize {
        self.geometry.lock().unwrap().cloud.cells.len()
    }

    /// Fused-surface triangle count (geometry lane) for tests/introspection.
    pub fn surfaces_count(&self) -> usize {
        self.geometry.lock().unwrap().surfaces.count
    }

    /// Capped-export flags for the /map/status 'capped' label.
    pub fn visual_capped(&self) -> (bool, bool) {
        let g = self.geometry.lock().unwrap();
        (
            self.points_cap > 0 && g.cloud.cells.len() > self.points_cap,
            self.triangles_cap > 0 && g.surfaces.count > self.triangles_cap,
        )
    }

    pub fn revision_watch(&self) -> tokio::sync::watch::Receiver<u64> {
        self.revision_tx.subscribe()
    }

    /// Returns the new socket generation: callers pass it back to
    /// observe_pose/process so a cancelled socket worker cannot publish
    /// into a later session even when the identity is reused.
    pub fn start_session(&self, identity: Identity, device: String) -> u64 {
        let mut s = self.inner.lock().unwrap();
        if s.identity.as_ref() != Some(&identity) {
            let mut g = self.geometry.lock().unwrap();
            Self::clear_locked(&mut s, &mut g);
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
        if let Some((session_id, map_epoch)) = &s.identity {
            s.dense.set_source(json!([session_id, map_epoch]));
        }
        s.processing_generation += 1;
        s.socket_generation += 1;
        s.last_capture = -1.0;
        s.last_pose_capture = -1.0;
        s.pose_history.clear();
        s.tracking = "not_available".into();
        s.reason = Some("Waiting for synchronized RGB-D frames".into());
        s.socket_generation
    }

    fn clear_locked(s: &mut Inner, g: &mut Geometry) {
        g.cloud.clear();
        // A running visual job retains the old cloud, never the new epoch.
        // Swapping avoids waiting for its integration/delta lock on reset.
        s.dense = Arc::new(DenseMap::new(true));
        s.dense.set_source(match &s.identity {
            Some((session_id, map_epoch)) => json!([session_id, map_epoch]),
            None => Value::Null,
        });
        if s.scan.is_some() {
            g.cloud = VoxelMap::default(); // restore phone fusion's voxel size/budget
        }
        g.surfaces.clear();
        s.generation += 1;
        s.processing_generation += 1;
        s.revision += 1;
        s.frames = 0;
        s.scan = None;
    }

    pub fn clear(&self) {
        let mut s = self.inner.lock().unwrap();
        let mut g = self.geometry.lock().unwrap();
        Self::clear_locked(&mut s, &mut g);
        let _ = self.revision_tx.send(s.revision);
    }

    /// Replace, never stitch separate rover origins or mix with ARKit geometry.
    pub fn install_scan(&self, cloud: VoxelMap, metadata: Value) {
        let mut s = self.inner.lock().unwrap();
        let mut g = self.geometry.lock().unwrap();
        Self::clear_locked(&mut s, &mut g);
        g.cloud = cloud;
        s.scan = Some(metadata);
        s.paused = true;
        s.reason = Some("Camera-only snapshot retained; estimated metric depth".into());
        let _ = self.revision_tx.send(s.revision);
    }

    /// Pose heartbeat applied only while the header names the connected
    /// session's identity and `socket_generation` (when given) is current:
    /// a stale socket's pose is dropped instead of corrupting the new
    /// session's pose history. `received_at` is the capture's true receipt
    /// time — CPU processing must not renew an old capture's age.
    pub fn observe_pose(
        &self,
        header: &Value,
        transform: &Mat4,
        received_at: Option<f64>,
        socket_generation: Option<u64>,
    ) -> Result<(), String> {
        let mut s = self.inner.lock().unwrap();
        let frame_identity = (
            header["session_id"].as_str().unwrap_or("").to_string(),
            header["map_epoch"].as_u64().unwrap_or(0),
        );
        if !s.connected
            || s.identity.as_ref() != Some(&frame_identity)
            || socket_generation.map(|g| g != s.socket_generation).unwrap_or(false)
        {
            return Ok(()); // a cancelled socket worker cannot publish into a later session
        }
        Self::observe_pose_locked(&mut s, header, transform, received_at)
    }

    /// Count a fusion-lane packet displaced by a newer one (bounded queue).
    pub fn note_dropped(&self) {
        self.inner.lock().unwrap().dropped += 1;
    }

    fn observe_pose_locked(
        s: &mut Inner,
        header: &Value,
        transform: &Mat4,
        received_at: Option<f64>,
    ) -> Result<(), String> {
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
        s.last_received = received_at.unwrap_or_else(monotonic);
        s.pose = *transform;
        s.tracking = header["tracking"].as_str().unwrap_or("not_available").to_string();
        Ok(())
    }

    /// Process one decoded RGB-D frame. Heavy reconstruction happens unlocked,
    /// matching the Python split-lock structure.
    pub fn process(&self, frame: &mut RgbdFrame) -> Result<(), String> {
        self.process_permits(frame, None, None)
    }

    /// state.process(frame, permits): the camera fast-lane stamps
    /// `_received_monotonic` at socket receipt so fusion never delays the
    /// receipt time; a verified permit origin back-dates it further.
    pub fn process_permits(
        &self,
        frame: &mut RgbdFrame,
        permits: Option<&std::sync::Arc<crate::capture_permits::CapturePermits>>,
        socket_generation: Option<u64>,
    ) -> Result<(), String> {
        let started = Instant::now();
        // Mirrors camera_receipt: receipt time minus the producer's encode
        // delay, validated before the identity check like Python.
        let receipt_base = frame
            .header
            .get("_received_monotonic")
            .and_then(|v| v.as_f64())
            .filter(|t| t.is_finite() && *t > 0.0)
            .unwrap_or_else(monotonic);
        let mut camera_receipt = crate::camera::adjusted_receipt(&frame.header, receipt_base)?;
        if let Some(origin) = permits.and_then(|p| p.verified_origin(&frame.header)) {
            camera_receipt = camera_receipt.min(origin);
        }
        let generation;
        let may_fuse;
        let may_dense;
        let dense_cloud;
        {
            let mut s = self.inner.lock().unwrap();
            let frame_identity = (
                frame.header["session_id"].as_str().unwrap_or("").to_string(),
                frame.header["map_epoch"].as_u64().unwrap_or(0),
            );
            if !s.connected
                || s.identity.as_ref() != Some(&frame_identity)
                || socket_generation.map(|g| g != s.socket_generation).unwrap_or(false)
            {
                return Ok(());
            }
            let capture = frame.header["t_capture"].as_f64().unwrap_or(-1.0);
            if capture <= s.last_capture {
                s.rejected += 1;
                return Ok(());
            }
            s.last_capture = capture;
            // Bind observers to this accepted socket lifetime. Overwrite any
            // producer field: an observer can finish after a reconnect even
            // when sensor identity is reused.
            let (session_id, map_epoch) = s.identity.clone().unwrap();
            frame.header["_mapping_socket_generation"] = json!(s.socket_generation);
            frame.header["_mapping_sensor_key"] = json!([
                session_id,
                map_epoch,
                s.socket_generation,
                frame.header["frame_id"].clone(),
                frame.header["t_capture"].clone(),
            ]);
            generation = s.processing_generation;
            Self::observe_pose_locked(&mut s, &frame.header, &frame.transform, Some(camera_receipt))?;
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
            may_fuse = !self.fusion_bypass
                && !s.paused
                && frame.header["tracking"].as_str() == Some("normal")
                && s.tracking == "normal";
            may_dense = !s.paused
                && frame.header["tracking"].as_str() == Some("normal")
                && s.tracking == "normal";
            dense_cloud = s.dense.clone();
            s.reason = if self.fusion_bypass {
                Some(FUSION_BYPASSED.into())
            } else if s.paused {
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
        if may_dense {
            // Latest-only offer; sampling/integration happens off this path.
            self.dense_worker.offer(frame.clone(), dense_cloud, generation);
        }
        // #109: bypassed frames keep the pose/depth/camera lanes live but are
        // not fused and not counted — frames/processing_ms stay frozen at the
        // last fused values (state.py parity).
        if !may_fuse {
            return Ok(());
        }
        let (points, colors, mesh, lods) =
            crate::phone::reconstruct_lods(frame, 2, 1, &crate::phone::VISUAL_LODS);
        let point_count = points.len();
        // Admit under `inner`, then take `geometry` while still holding it
        // (lock order inner -> geometry) so a reset/disconnect cannot clear
        // the cloud between the check and the integrate. The heavy
        // voxel+surface integrate then runs under `geometry` alone —
        // observe_pose/status/packet lanes no longer stall behind it.
        let mut g = {
            let s = self.inner.lock().unwrap();
            if generation != s.processing_generation
                || !s.connected
                || s.paused
                || s.tracking != "normal"
            {
                return Ok(());
            }
            self.geometry.lock().unwrap()
        };
        g.cloud.integrate(&points, &colors);
        g.surfaces.integrate(mesh, &frame.transform, lods);
        drop(g);
        let mut s = self.inner.lock().unwrap();
        if generation != s.processing_generation {
            // A reset/disconnect won the race while the integrate ran: its
            // clear already owns or wipes these points — don't count it.
            return Ok(());
        }
        s.frames += 1;
        s.revision += 1;
        let _ = self.revision_tx.send(s.revision);
        let duration = started.elapsed().as_secs_f64();
        self.processing_ms.store(
            ((duration * 1000.0 * 10.0).round() / 10.0).to_bits(),
            Ordering::Relaxed,
        );
        s.fps = (1.0 / duration.max(1e-4) * 10.0).round() / 10.0;
        if point_count == 0 {
            s.reason = Some("No confident depth samples in this frame".into());
        }
        Ok(())
    }

    pub fn status(&self) -> Value {
        let s = self.inner.lock().unwrap();
        let g = self.geometry.lock().unwrap();
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
            "mesh_revision": g.surfaces.revision,
            "paused": s.paused,
            "tracking": if fresh { s.tracking.clone() } else { "stale".into() },
            "points": g.cloud.cells.len(),
            "triangles": g.surfaces.count,
            "capped": (self.points_cap > 0 && g.cloud.cells.len() > self.points_cap)
                || (self.triangles_cap > 0 && g.surfaces.count > self.triangles_cap),
            "fusion_bypass": self.fusion_bypass,
            "dense_worker": self.dense_worker.status(),
            "keyframes": g.surfaces.keyframes.len(),
            "frames": s.frames,
            "rejected": s.rejected,
            "dropped": s.dropped,
            "processing_ms": f64::from_bits(self.processing_ms.load(Ordering::Relaxed)),
            "processing_fps": s.fps,
            "age_ms": age.map(|a| (a * 1000.0).round() as i64),
            "position": [pose[0][3], pose[1][3], pose[2][3]],
            "transform": flatten_f(&pose).to_vec(),
            "scan": s.scan.clone().unwrap_or(Value::Null),
            "voxel_m": g.cloud.size,
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

    /// Lock-free read of the last fusion duration (for /video/status, which
    /// must answer while the map worker is busy).
    pub fn processing_ms(&self) -> f64 {
        f64::from_bits(self.processing_ms.load(Ordering::Relaxed))
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
        let g = self.geometry.lock().unwrap();
        let key = (s.generation, s.frames, g.surfaces.keyframes.len());
        let mut cache = self.packets.lock().unwrap();
        let stale = cache.as_ref().map(|c| c.key != key).unwrap_or(true);
        if stale {
            // Snapshot under the locks, encode after releasing them — the
            // 16 B/point byte expansion must not stall frame fusion.
            let (points, colors) = g.cloud.snapshot();
            let mesh = g.surfaces.encode();
            // Connected coarse keyframes at cap (bd28508 parity): when the
            // wire cap bites, serve the finest stored LOD level at which all
            // keyframes fit, else the newest whole keyframes at stride 8 —
            // faces stay connected patches instead of stride-deleted scraps.
            // Keyframes without stored LODs keep the stride fallback. Runs
            // under the same lock that already owns the encode() copy.
            let (mesh, visual_packed) = if self.triangles_cap > 0
                && self.triangles_cap < g.surfaces.count
            {
                match g.surfaces.visual(self.triangles_cap) {
                    Some(meshes) => (crate::surfaces::SurfaceMap::pack(&meshes), true),
                    None => (mesh, false),
                }
            } else {
                (mesh, false)
            };
            drop(g);
            drop(s);
            // Visual wire caps bound only the exported payload; the
            // internal map keeps full resolution (board-agreed 30k/40k
            // defaults, deterministic stride decimation, PLY follows the
            // capped points since it derives from pts.raw).
            let pts = crate::geometry::decimate_points_bin(
                &encode_points(&points, &colors), self.points_cap);
            let mesh = if visual_packed {
                mesh // already a capped connected-keyframe packet
            } else {
                crate::geometry::decimate_mesh_bin(&mesh, self.triangles_cap)
            };
            // PLY is derived lazily from the cached points.bin records on
            // first request — running export_ply eagerly every map version
            // cost ~10-30 ms under this mutex even when nobody downloads it.
            *cache = Some(PacketCache { key, pts: VariantCache::new(pts), mesh: VariantCache::new(mesh), ply: None });
        }
        cache
    }

    /// Returns (body, content-encoding or "", etag) for one map binary route.
    /// Mirrors jetson/mapping/compress.py: cached per map version,
    /// zstd > br > gzip, identity below the 1 KiB floor. The etag keys the
    /// map version plus the representation so unchanged polls can 304.
    pub fn packet(&self, which: PacketKind, accept_encoding: &str) -> (Bytes, &'static str, String) {
        let coding = negotiate_encoding(accept_encoding);
        let mut cache = self.packet_cached();
        let c = cache.as_mut().unwrap();
        let (v, kind) = match which {
            PacketKind::Points => (&mut c.pts, "points"),
            PacketKind::Mesh => (&mut c.mesh, "mesh"),
            PacketKind::Ply => (
                c.ply.get_or_insert_with(|| VariantCache::new(ply_from_points(&c.pts.raw))),
                "ply",
            ),
        };
        let (body, enc) = if coding.is_none() || v.raw.len() < 1024 {
            (v.raw.clone(), "")
        } else {
            let cd = coding.unwrap();
            (v.variant(cd), cd.name())
        };
        let tag = format!(
            "\"{}-{}-{}-{}-{}\"",
            c.key.0, c.key.1, c.key.2, kind,
            if enc.is_empty() { "i" } else { enc }
        );
        (body, enc, tag)
    }

    pub fn point_packet(&self, accept_encoding: &str) -> (Bytes, &'static str, String) {
        self.packet(PacketKind::Points, accept_encoding)
    }

    pub fn mesh_packet(&self, accept_encoding: &str) -> (Bytes, &'static str, String) {
        self.packet(PacketKind::Mesh, accept_encoding)
    }

    pub fn ply_packet(&self, accept_encoding: &str) -> (Bytes, &'static str, String) {
        self.packet(PacketKind::Ply, accept_encoding)
    }

    pub fn ply(&self) -> Vec<u8> {
        let (points, colors) = self.geometry.lock().unwrap().cloud.snapshot();
        export_ply(&points, &colors)
    }

    /// The live measured dense cloud (swapped whole on reset/session).
    pub fn dense(&self) -> Arc<DenseMap> {
        self.inner.lock().unwrap().dense.clone()
    }

    /// Live /phone session with normal ARKit tracking (--lidar-drive opt-in).
    pub fn lidar_drive_eligible(&self) -> bool {
        let s = self.inner.lock().unwrap();
        s.connected && !self.demo && s.tracking == "normal"
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
        // Invalidate any CPU worker still finishing after disconnect.
        s.processing_generation += 1;
        s.socket_generation += 1;
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

/// Derive a binary PLY export from cached points.bin records. The record
/// layout (12 B pos + 3 B rgb + 1 B pad) is the PLY vertex (12 + 3) plus
/// padding, so this is byte-identical to export_ply on the same cloud.
fn ply_from_points(raw: &[u8]) -> Vec<u8> {
    let n = raw
        .get(..4)
        .map(|b| u32::from_le_bytes(b.try_into().unwrap()) as usize)
        .unwrap_or(0);
    let header = format!(
        "ply\nformat binary_little_endian 1.0\nelement vertex {}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n",
        n
    );
    let mut out = Vec::with_capacity(header.len() + n * 15);
    out.extend_from_slice(header.as_bytes());
    for rec in raw[4..].chunks_exact(16).take(n) {
        out.extend_from_slice(&rec[..15]);
    }
    out
}

/// Raw bytes plus lazily-built compressed variants, reset per map version.
/// Bytes keeps every serve a refcount bump instead of a multi-MB memcpy.
struct VariantCache {
    raw: Bytes,
    zstd: Option<Bytes>,
    br: Option<Bytes>,
    gz: Option<Bytes>,
}

impl VariantCache {
    fn new(raw: Vec<u8>) -> Self {
        Self { raw: raw.into(), zstd: None, br: None, gz: None }
    }

    fn variant(&mut self, coding: Coding) -> Bytes {
        let slot = match coding {
            Coding::Zstd => &mut self.zstd,
            Coding::Br => &mut self.br,
            Coding::Gzip => &mut self.gz,
        };
        if slot.is_none() {
            *slot = Some(Bytes::from(match coding {
                Coding::Zstd => zstd_compress(&self.raw),
                Coding::Br => brotli_compress(&self.raw),
                Coding::Gzip => gzip_compress(&self.raw),
            }));
        }
        slot.clone().unwrap()
    }
}

struct PacketCache {
    key: (u64, u64, usize),
    pts: VariantCache,
    mesh: VariantCache,
    ply: Option<VariantCache>,
}

#[derive(Clone, Copy)]
pub(crate) enum Coding {
    Zstd,
    Br,
    Gzip,
}

impl Coding {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Coding::Zstd => "zstd",
            Coding::Br => "br",
            Coding::Gzip => "gzip",
        }
    }
}

/// zstd preferred over br over gzip; any q=0 refusal drops the coding.
pub(crate) fn negotiate_encoding(accept: &str) -> Option<Coding> {
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

pub(crate) fn gzip_compress(data: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::new(5));
    let _ = enc.write_all(data);
    enc.finish().unwrap_or_default()
}

pub(crate) fn brotli_compress(data: &[u8]) -> Vec<u8> {
    use std::io::Read;
    let mut out = Vec::new();
    let mut r = brotli::CompressorReader::new(data, 1 << 20, 4, 22);
    let _ = r.read_to_end(&mut out);
    out
}

/// Level 9: encodes faster than gzip-5 and br-4 while beating both on
/// points/ply bytes; higher levels cost seconds on the Jetson per version.
pub(crate) fn zstd_compress(data: &[u8]) -> Vec<u8> {
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
        map.geometry.lock().unwrap().cloud.integrate(&points, &colors);
        map
    }

    /// Fusion's voxel+surface integrate runs under `geometry`, not `inner`:
    /// pose/status-lane calls must not stall behind a mid-flight commit
    /// (the 323 ms Python stall this split removes).
    #[test]
    fn inner_lane_not_blocked_by_geometry_integrate() {
        let map = Arc::new(MapState::new(true));
        let (held_tx, held_rx) = std::sync::mpsc::channel::<()>();
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        let worker = {
            let map = map.clone();
            std::thread::spawn(move || {
                let _g = map.geometry.lock().unwrap();
                held_tx.send(()).unwrap();
                // Hold the geometry lock like a slow voxel+surface integrate.
                let _ = release_rx.recv();
            })
        };
        held_rx.recv().unwrap(); // integrate is mid-flight
        let t = Instant::now();
        map.observe_pose(&json!({}), &IDENTITY, None, None).unwrap();
        let _ = map.camera_snapshot();
        map.note_dropped();
        let _ = map.capture_diagnostics();
        assert!(
            t.elapsed() < std::time::Duration::from_millis(500),
            "inner-lane calls stalled on the geometry lock"
        );
        release_tx.send(()).unwrap();
        worker.join().unwrap();
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
        let (_, coding, _) = map.point_packet("br, gzip");
        assert_eq!(coding, "br", "zstd must not be served to a client that did not advertise it");

        let (body, coding, _) = map.point_packet("gzip, br, zstd");
        assert_eq!(coding, "zstd");
        let decoded = zstd::decode_all(&body[..]).unwrap();
        let (identity, ..) = map.point_packet("");
        assert_eq!(decoded, identity, "zstd body must decode to the identity payload");
    }

    #[test]
    fn zstd_refused_falls_back_to_next_offered() {
        let map = filled_map();
        let (_, coding, _) = map.point_packet("zstd;q=0, gzip");
        assert_eq!(coding, "gzip");
    }

    #[test]
    fn zstd_variants_cover_mesh_and_ply() {
        let map = filled_map();
        let (mesh_body, coding, _) = map.mesh_packet("zstd");
        // The demo mesh may sit below the 1 KiB floor; ply is the reliable check.
        if coding == "zstd" {
            assert!(!zstd::decode_all(&mesh_body[..]).unwrap().is_empty());
        }
        let (ply_body, coding, _) = map.ply_packet("zstd");
        assert_eq!(coding, "zstd");
        let (ply_raw, ..) = map.ply_packet("");
        assert_eq!(zstd::decode_all(&ply_body[..]).unwrap(), ply_raw);
    }

    #[test]
    fn small_payloads_stay_identity() {
        let map = MapState::new(true); // empty cloud -> raw < 1 KiB
        let (_, coding, _) = map.point_packet("zstd, br, gzip");
        assert_eq!(coding, "");
    }
}
