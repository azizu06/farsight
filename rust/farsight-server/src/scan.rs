//! One stopped-position camera sweep, using the operator's existing control
//! lease. Port of jetson/scan.py.
//!
//! The sweep turns the rover (bounded w, open-loop timing) or pans the
//! physical camera between five headings, captures one RGB view each, runs
//! local metric depth, back-projects through the calibrated pinhole, and
//! replaces the map cloud with the stitched result.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use serde_json::{json, Value};

use crate::camera::{CameraFrame, CameraService};
use crate::control::ControlState;
use crate::depth::{back_project, intrinsics_from_fov, MetricDepth};
use crate::perception::PerceptionService;
use crate::state::{now_monotonic, MapState};
use crate::voxel::VoxelMap;

const YAWS: [i64; 5] = [-80, -40, 0, 40, 80];
const TIMEOUT: f64 = 90.0;

pub struct ScanService {
    mapping: Arc<MapState>,
    camera: Arc<CameraService>,
    control: Arc<ControlState>,
    perception: Arc<PerceptionService>,
    depth_model: Option<String>,
    depth: OnceLock<Result<MetricDepth, String>>,
    physical_pan: bool,
    fov: f64,
    intrinsics: Option<[f64; 4]>,
    turn_dps: f64,
    result: Mutex<Value>,
    task: Mutex<Option<std::thread::JoinHandle<()>>>,
    quit: AtomicBool,
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round_ties_even() / 100.0
}

impl ScanService {
    pub fn new(
        mapping: Arc<MapState>,
        camera: Arc<CameraService>,
        control: Arc<ControlState>,
        perception: Arc<PerceptionService>,
        model: Option<String>,
        physical_pan: bool,
        horizontal_fov: f64,
        intrinsics: Option<Vec<f64>>,
        turn_dps: f64,
    ) -> Result<Self, String> {
        if !turn_dps.is_finite() || !(20.0..=180.0).contains(&turn_dps) {
            return Err("Scan turn calibration must be 20..180 degrees/second".into());
        }
        intrinsics_from_fov(640, 480, horizontal_fov)?;
        let intrinsics = match intrinsics {
            Some(k) => {
                if k.len() != 4 || !k.iter().all(|v| v.is_finite()) || k[0].min(k[1]) <= 0.0 {
                    return Err(
                        "Scan intrinsics must be finite fx, fy, cx, cy with positive focal lengths"
                            .into(),
                    );
                }
                Some([k[0], k[1], k[2], k[3]])
            }
            None => None,
        };
        Ok(Self {
            mapping,
            camera,
            control,
            perception,
            depth_model: model,
            depth: OnceLock::new(),
            physical_pan,
            fov: horizontal_fov,
            intrinsics,
            turn_dps,
            result: Mutex::new(json!({"state": "idle", "running": false})),
            task: Mutex::new(None),
            quit: AtomicBool::new(false),
        })
    }

    fn depth(&self) -> Option<&Result<MetricDepth, String>> {
        self.depth_model.as_ref().map(|m| {
            self.depth.get_or_init(|| MetricDepth::new(m))
        })
    }

    fn backend(&self) -> String {
        self.depth()
            .and_then(|r| r.as_ref().ok())
            .map(|d| d.backend.clone())
            .unwrap_or_else(|| "synthetic-depth".into())
    }

    /// Lazily loads the ONNX session. Errors surface through the failing call.
    fn infer(&self, bgr: &[u8], w: usize, h: usize) -> Result<Vec<f32>, String> {
        match self.depth() {
            Some(Ok(d)) => d.predict(bgr, w, h),
            Some(Err(e)) => Err(e.clone()),
            None => Err("Depth model inference requires the `detector` build feature".into()),
        }
    }

    pub fn result(&self) -> Value {
        self.result.lock().unwrap().clone()
    }

    pub fn start(self: &Arc<Self>, session_id: Option<&str>, permit: Option<&str>) -> Result<Value, String> {
        {
            let task = self.task.lock().unwrap();
            if task.as_ref().map(|t| !t.is_finished()).unwrap_or(false) {
                return Err("A scan is already running".into());
            }
        }
        if self.depth_model.is_none() && !self.mapping.demo {
            return Err("Configure --depth-model for a camera-only scan".into());
        }
        // Surface a broken depth model before claiming control, like the
        // Python constructor does at server start.
        if let Some(Err(e)) = self.depth() {
            return Err(e.clone());
        }
        let frame = self.camera.snapshot();
        let frame = match frame {
            Some(f) if f.age_ms() < 500 && f.source != "IPHONE LIDAR" => f,
            _ => {
                return Err(
                    "Scan requires fresh camera-only RGB (LiDAR uses the existing mapper)".into(),
                )
            }
        };
        if !self.physical_pan && self.control.inner.lock().unwrap().pan != 0 {
            return Err("Centre physical pan before a fixed-camera rover sweep".into());
        }
        let token = self.control.begin_scan(session_id, permit)?;
        let generation = self.mapping.inner.lock().unwrap().generation;
        {
            let mut result = self.result.lock().unwrap();
            *result = json!({
                "state": "sweeping",
                "running": true,
                "simulated": self.mapping.demo,
                "approximate": !self.physical_pan,
                "views": [],
                "points": 0,
                "markers": [],
                "backend": self.backend(),
            });
        }
        let me = self.clone();
        *self.task.lock().unwrap() = Some(std::thread::spawn(move || {
            me.run(token, generation)
        }));
        let _ = frame;
        Ok(self.result.lock().unwrap().clone())
    }

    /// Loop scan_command so the safety gates keep running, then sleep.
    fn wait(&self, token: u64, seconds: f64, w: f64, pan: f64) -> Result<(), String> {
        let until = now_monotonic() + seconds;
        loop {
            self.control.scan_command(Some(token), w, pan)?;
            let remaining = until - now_monotonic();
            if remaining <= 0.0 {
                return Ok(());
            }
            std::thread::sleep(Duration::from_secs_f64(remaining.min(0.02)));
        }
    }

    /// One view: (bgr pixels, bgr w/h, synthetic depth or None, intrinsics,
    /// camera frame used for markers). Demo renders the ray-cast room.
    fn capture(&self, token: u64, yaw: f64, index: i64) -> Result<(Vec<u8>, usize, usize, Option<Vec<f32>>, [f64; 4], Option<CameraFrame>), String> {
        if self.mapping.demo {
            let frame = crate::demo::simulated_scan_frame(
                yaw,
                index,
                &("simulated-room".into(), 1),
            );
            let k = [
                frame.calibration.fx,
                frame.calibration.fy,
                frame.calibration.cx,
                frame.calibration.cy,
            ];
            // frame.rgb is RGB; the scan path consumes BGR.
            let mut bgr = frame.rgb.clone();
            for px in bgr.chunks_exact_mut(3) {
                px.swap(0, 2);
            }
            return Ok((
                bgr,
                frame.depth_width,
                frame.depth_height,
                Some(frame.depth),
                k,
                None,
            ));
        }
        // A frame received before the settled stop must not become this view.
        let after = now_monotonic();
        while now_monotonic() - after < 1.5 {
            self.control.scan_command(Some(token), 0.0, if self.physical_pan { yaw } else { 0.0 })?;
            if let Some(frame) = self.camera.snapshot() {
                if frame.received >= after && frame.age_ms() < 500 {
                    let img = image::load_from_memory_with_format(&frame.jpeg, image::ImageFormat::Jpeg)
                        .map_err(|_| "Scan camera JPEG decode failed".to_string())?
                        .to_rgb8();
                    let (w, h) = (img.width() as usize, img.height() as usize);
                    let mut bgr = img.into_raw();
                    for px in bgr.chunks_exact_mut(3) {
                        px.swap(0, 2);
                    }
                    let k = self
                        .intrinsics
                        .map(Ok)
                        .unwrap_or_else(|| intrinsics_from_fov(w as u32, h as u32, self.fov))?;
                    return Ok((bgr, w, h, None, k, Some(frame)));
                }
            }
            std::thread::sleep(Duration::from_secs_f64(0.02));
        }
        Err("No fresh camera frame after sweep settled".into())
    }

    fn run(&self, token: u64, generation: u64) {
        let started = now_monotonic();
        let mut cloud = VoxelMap::new(0.025, 100_000);
        let sweep = self.sweep(token, generation, &mut cloud, started);
        let mut result = self.result.lock().unwrap();
        match sweep {
            Ok(()) => {}
            Err(e) => {
                let msg = if e.is_empty() { "Server shutdown".to_string() } else { e };
                result["state"] = json!("aborted");
                result["running"] = json!(false);
                result["error"] = json!(msg);
                result["scan_ms"] = json!(round2((now_monotonic() - started) * 1000.0));
            }
        }
        drop(result);
        self.control.end_scan(token);
    }

    fn sweep(&self, token: u64, generation: u64, cloud: &mut VoxelMap, started: f64) -> Result<(), String> {
        if self.quit.load(Ordering::Relaxed) {
            return Err(String::new());
        }
        let mut previous = 0i64;
        for (index, yaw) in YAWS.iter().enumerate() {
            if now_monotonic() - started >= TIMEOUT {
                return Err(String::new()); // asyncio.TimeoutError -> str(exc) empty -> "Server shutdown"
            }
            let yaw = *yaw;
            if !self.mapping.demo {
                if self.physical_pan {
                    self.wait(token, 0.4, 0.0, yaw as f64)?;
                } else {
                    // Open-loop motor timing is explicitly approximate;
                    // bounded steps, fresh range, held operator permits.
                    let delta = (yaw - previous) as f64;
                    self.wait(token, delta.abs() / self.turn_dps, if delta > 0.0 { 0.3 } else { -0.3 }, 0.0)?;
                    self.wait(token, 0.3, 0.0, 0.0)?;
                }
            }
            self.control.scan_command(Some(token), 0.0, if self.physical_pan { yaw as f64 } else { 0.0 })?;
            let (bgr, w, h, synthetic, k, frame) = self.capture(token, yaw as f64, index as i64)?;
            let detections = self.perception.snapshot();
            let at = now_monotonic();
            let depth = match synthetic {
                Some(d) => d,
                None => self.infer(&bgr, w, h)?,
            };
            let latency = (now_monotonic() - at) * 1000.0;
            self.control.scan_command(Some(token), 0.0, if self.physical_pan { yaw as f64 } else { 0.0 })?;
            // bgr -> rgb for back-projected colors.
            let mut rgb = bgr;
            for px in rgb.chunks_exact_mut(3) {
                px.swap(0, 2);
            }
            let (xyz, colors) = back_project(&depth, w, h, &rgb, k, yaw as f64, 8.0)?;
            cloud.integrate(&xyz, &colors, 0);
            self.collect_markers(&frame, &depth, w, h, &rgb, k, yaw as f64, &detections);
            {
                let mut result = self.result.lock().unwrap();
                result["views"].as_array_mut().unwrap().push(json!({
                    "yaw_deg": yaw,
                    "depth_ms": round2(latency),
                    "samples": xyz.len(),
                }));
            }
            previous = yaw;
        }
        if now_monotonic() - started >= TIMEOUT {
            return Err(String::new());
        }
        self.control.scan_command(Some(token), 0.0, if self.physical_pan { previous as f64 } else { 0.0 })?;
        {
            let inner = self.mapping.inner.lock().unwrap();
            if generation != inner.generation {
                return Err("Map reset or sensor origin changed during scan".into());
            }
        }
        {
            let mut result = self.result.lock().unwrap();
            result["state"] = json!("complete");
            result["running"] = json!(false);
            result["points"] = json!(cloud.cells.len());
            result["scan_ms"] = json!(round2((now_monotonic() - started) * 1000.0));
            let metadata = result.clone();
            drop(result);
            self.mapping.install_scan(std::mem::replace(cloud, VoxelMap::default()), metadata);
        }
        Ok(())
    }

    fn collect_markers(
        &self,
        frame: &Option<CameraFrame>,
        depth: &[f32],
        w: usize,
        h: usize,
        rgb: &[u8],
        k: [f64; 4],
        yaw: f64,
        packet: &Value,
    ) {
        let Some(frame) = frame else { return };
        if packet["fresh"] != true
            || !packet["error"].is_null()
            || packet["source"].as_str() != Some(frame.source.as_str())
            || packet["frame"].as_i64() != Some(frame.frame_id)
        {
            return;
        }
        let Some(boxes) = packet["boxes"].as_array() else { return };
        let mut markers = Vec::new();
        for boxv in boxes.iter().take(100) {
            let (Some(bx), Some(by), Some(bw), Some(bh)) = (
                boxv["x"].as_f64(),
                boxv["y"].as_f64(),
                boxv["w"].as_f64(),
                boxv["h"].as_f64(),
            ) else {
                continue;
            };
            let u = (((bx + bw / 2.0) * w as f64) as i64).clamp(0, w as i64 - 1) as usize;
            let v = (((by + bh / 2.0) * h as f64) as i64).clamp(0, h as i64 - 1) as usize;
            // Project one box-centre sample through the same pinhole/yaw seam.
            let (fx, fy, cx, cy) = (k[0], k[1], k[2], k[3]);
            if let Ok((xyz, _)) = back_project(
                &depth[v * w + u..v * w + u + 1],
                1,
                1,
                &rgb[(v * w + u) * 3..(v * w + u) * 3 + 3],
                [fx, fy, cx - u as f64, cy - v as f64],
                yaw,
                8.0,
            ) {
                if let Some(p) = xyz.first() {
                    markers.push(json!({
                        "id": boxv["id"],
                        "cls": boxv["cls"],
                        "position": [p[0], p[1], p[2]],
                    }));
                }
            }
        }
        if !markers.is_empty() {
            let mut result = self.result.lock().unwrap();
            result["markers"].as_array_mut().unwrap().extend(markers);
        }
    }

    /// A close() without cancel: mark quit so an in-flight sweep aborts, then
    /// join the worker if it already finished.
    pub fn close(&self) {
        self.quit.store(true, Ordering::Relaxed);
        let mut task = self.task.lock().unwrap();
        if let Some(t) = task.as_ref() {
            if t.is_finished() {
                let _ = task.take().unwrap().join();
            }
        }
    }
}
