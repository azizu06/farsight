//! Local person detection. Port of jetson/perception.py.
//! Rust port ships the ONNX path behind the `detector` feature; without a
//! model the service runs with detector "disabled" and empty boxes, exactly
//! like the Python `mode="none"` path.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::camera::{CameraFrame, CameraService};
use crate::state::now_monotonic;

fn epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

struct Inner {
    packet: Value,
    tracker: crate::tracker::Tracker,
    // Monotonic inference completions in the last FPS_WINDOW seconds (#118).
    completions: std::collections::VecDeque<f64>,
    error: Option<String>,
}

/// The rate between only the last two inferences swung 2-30 with phone
/// frame-arrival jitter at a steady ~6 fps; a 2 s window measures
/// throughput, not jitter.
const FPS_WINDOW: f64 = 2.0;

pub struct PerceptionService {
    pub camera: Arc<CameraService>,
    pub mode: String,
    pub fps: f64,
    on_detection: Mutex<Option<Box<dyn Fn(&Value, &CameraFrame) + Send + Sync>>>,
    #[cfg(feature = "detector")]
    onnx: Mutex<Option<crate::detector::OnnxDetector>>,
    #[cfg(feature = "detector")]
    face: Mutex<Option<crate::detector::FaceDetector>>,
    /// The server points this at the rescue drill so the face pass only costs
    /// CPU while a drill runs (perception.face_wanted, #81).
    face_wanted: Mutex<Box<dyn Fn() -> bool + Send + Sync>>,
    #[allow(dead_code)] // Only read under the detector feature.
    face_error: Mutex<Option<String>>,
    inner: Mutex<Inner>,
    stop: AtomicBool,
}

impl PerceptionService {
    pub fn new(
        camera: Arc<CameraService>,
        mode: &str,
        on_detection: Option<Box<dyn Fn(&Value, &CameraFrame) + Send + Sync>>,
        model_path: Option<&str>,
        face_model_path: Option<&str>,
    ) -> Self {
        #[cfg(not(feature = "detector"))]
        let _ = (model_path, face_model_path);
        #[cfg(feature = "detector")]
        let onnx = if mode == "onnx" || mode == "pose" {
            match model_path {
                Some(path) => crate::detector::OnnxDetector::new(path, mode == "pose")
                    .map_err(|e| tracing::warn!("Detector unavailable: {e}"))
                    .ok(),
                None => None,
            }
        } else {
            None
        };
        // A bad face model disables only the face pass; primary detection
        // still starts and the error is reported (face_error, #79).
        #[cfg(feature = "detector")]
        let mut face_error = None;
        #[cfg(feature = "detector")]
        let face = face_model_path.and_then(|p| {
            crate::detector::FaceDetector::new(p)
                .map_err(|e| {
                    tracing::warn!("Face detector unavailable: {e}");
                    face_error = Some(format!("Face model unavailable: {e}"));
                })
                .ok()
        });
        #[cfg(not(feature = "detector"))]
        let face_error: Option<String> = None;
        Self {
            camera,
            mode: mode.to_string(),
            fps: 3.0,
            on_detection: Mutex::new(on_detection),
            #[cfg(feature = "detector")]
            onnx: Mutex::new(onnx),
            #[cfg(feature = "detector")]
            face: Mutex::new(face),
            face_wanted: Mutex::new(Box::new(|| true)),
            face_error: Mutex::new(face_error),
            inner: Mutex::new(Inner {
                packet: json!({"ts": 0, "frame": -1, "fps": 0, "boxes": [], "detector": "disabled", "source": "WAITING"}),
                tracker: crate::tracker::Tracker::new(),
                completions: std::collections::VecDeque::new(),
                error: None,
            }),
            stop: AtomicBool::new(false),
        }
    }

    /// Detect on one frame. Without an ONNX model this always returns no
    /// boxes (the Rust port drops the OpenCV HOG fallback — compile with
    /// `detector` feature + a model file for real inference).
    fn detect(&self, jpeg: &[u8]) -> Result<Vec<Value>, String> {
        #[cfg(feature = "detector")]
        {
            let mut boxes = if let Some(det) = self.onnx.lock().unwrap().as_mut() {
                det.detect(jpeg)?
            } else {
                Vec::new()
            };
            if (self.face_wanted.lock().unwrap())() {
                if let Some(face) = self.face.lock().unwrap().as_mut() {
                    if let Ok(faces) = face.detect(jpeg) {
                        boxes.extend(faces);
                    }
                }
            }
            return Ok(boxes);
        }
        #[allow(unreachable_code)]
        {
            let _ = jpeg;
            Ok(Vec::new())
        }
    }

    fn track(&self, boxes: &mut Vec<Value>, now: f64) {
        self.inner.lock().unwrap().tracker.update(boxes, now);
    }

    /// Server hook: face pass only while the rescue drill is active.
    pub fn set_face_wanted(&self, callback: Box<dyn Fn() -> bool + Send + Sync>) {
        *self.face_wanted.lock().unwrap() = callback;
    }

    /// Whether the cls==face pass the rescue drill scores on is loaded.
    pub fn face_status(&self) -> Value {
        #[cfg(feature = "detector")]
        {
            return json!({
                "face_detector": self.face.lock().unwrap().is_some(),
                "face_error": self.face_error.lock().unwrap().clone(),
            });
        }
        #[cfg(not(feature = "detector"))]
        {
            json!({"face_detector": false, "face_error": Value::Null})
        }
    }

    pub fn start(self: Arc<Self>) {
        if self.mode == "replay" {
            return; // Recorded inference only; never rerun a detector.
        }
        std::thread::spawn(move || self.run());
    }

    /// Replay only: inject a recorded detection packet (ReplayPerception).
    pub fn set_packet(&self, packet: Value) {
        self.inner.lock().unwrap().packet = packet;
    }

    fn run(self: Arc<Self>) {
        let mut previous: Option<(String, i64, i64)> = None;
        while !self.stop.load(Ordering::Relaxed) {
            let started = now_monotonic();
            let frame = self.camera.snapshot();
            let identity = frame.as_ref().map(|f| (f.source.clone(), f.frame_id, f.received.to_bits() as i64));
            if let Some(frame) = &frame {
                if frame.age_ms() < 500 && identity != previous {
                    previous = identity;
                    match self.detect(&frame.jpeg) {
                        Ok(mut boxes) => {
                            let duration = now_monotonic() - started;
                            let completed = now_monotonic();
                            let rate = {
                                let mut inner = self.inner.lock().unwrap();
                                inner.completions.push_back(completed);
                                while completed - inner.completions[0] > FPS_WINDOW {
                                    inner.completions.pop_front();
                                }
                                let span = completed - inner.completions[0];
                                if span > 0.0 {
                                    (inner.completions.len() - 1) as f64 / span
                                } else {
                                    0.0
                                }
                            };
                            self.track(&mut boxes, now_monotonic());
                            let mut inner = self.inner.lock().unwrap();
                            inner.packet = json!({
                                "ts": epoch(),
                                "frame": frame.frame_id,
                                "fps": (rate * 10.0).round() / 10.0,
                                "inference_ms": (duration * 1000.0 * 10.0).round() / 10.0,
                                "boxes": boxes,
                                "detector": self.mode,
                                "source": frame.source,
                                "received": frame.received,
                                "age_ms": frame.age_ms(),
                            });
                            inner.error = None;
                            drop(inner);
                            if let Some(cb) = self.on_detection.lock().unwrap().as_ref() {
                                cb(&self.inner.lock().unwrap().packet, frame);
                            }
                        }
                        Err(e) => {
                            self.inner.lock().unwrap().error = Some(e);
                        }
                    }
                }
            }
            let elapsed = now_monotonic() - started;
            std::thread::sleep(Duration::from_secs_f64((1.0 / self.fps - elapsed).max(0.01)));
        }
    }

    /// Mirrors snapshot(): packet copy with live age/fresh + error, boxes
    /// emptied when stale.
    pub fn snapshot(&self) -> Value {
        let inner = self.inner.lock().unwrap();
        let mut packet = inner.packet.clone();
        let received = packet.as_object_mut().unwrap().remove("received").and_then(|v| v.as_f64());
        let age_ms = received.map(|r| ((now_monotonic() - r) * 1000.0).round() as i64);
        packet["age_ms"] = json!(age_ms);
        let fresh = age_ms.map(|a| a < 1000).unwrap_or(false);
        packet["fresh"] = json!(fresh);
        packet["error"] = json!(inner.error);
        if !fresh {
            packet["boxes"] = json!([]);
        }
        packet
    }

    /// Detector fields for GET /health: mode, concrete backend, error, and
    /// live fps/latency. Port of perception.health().
    pub fn health(&self) -> Value {
        let packet = self.snapshot();
        let fresh = packet["fresh"].as_bool().unwrap_or(false);
        let backend = {
            #[cfg(feature = "detector")]
            {
                if self.mode == "pose" && self.onnx.lock().unwrap().is_some() {
                    "pose-onnx".to_string()
                } else {
                    self.mode.clone()
                }
            }
            #[cfg(not(feature = "detector"))]
            {
                self.mode.clone()
            }
        };
        let mut health = json!({
            "detector": self.mode,
            "detector_backend": backend,
            "detector_error": self.inner.lock().unwrap().error,
            "detector_fps": if fresh { packet["fps"].clone() } else { json!(0) },
            "detector_inference_ms": if fresh { packet["inference_ms"].clone() } else { Value::Null },
        });
        // face_detector + face_error are part of /health (#79).
        if let Some(h) = health.as_object_mut() {
            for (k, v) in self.face_status().as_object().cloned().unwrap_or_default() {
                h.insert(k, v);
            }
        }
        health
    }

    pub fn close(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
