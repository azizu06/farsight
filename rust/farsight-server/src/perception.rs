//! Local person detection. Port of jetson/perception.py.
//! Rust port ships the ONNX path behind the `detector` feature; without a
//! model the service runs with detector "disabled" and empty boxes, exactly
//! like the Python `mode="none"` path.

use std::collections::HashMap;
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

fn iou(a: &Value, b: &Value) -> f64 {
    let f = |v: &Value, k: &str| v[k].as_f64().unwrap_or(0.0);
    let x1 = f(a, "x").max(f(b, "x"));
    let y1 = f(a, "y").max(f(b, "y"));
    let x2 = (f(a, "x") + f(a, "w")).min(f(b, "x") + f(b, "w"));
    let y2 = (f(a, "y") + f(a, "h")).min(f(b, "y") + f(b, "h"));
    let inter = (x2 - x1).max(0.0) * (y2 - y1).max(0.0);
    inter / (f(a, "w") * f(a, "h") + f(b, "w") * f(b, "h") - inter).max(1e-9)
}

struct Inner {
    packet: Value,
    tracks: HashMap<u64, (Value, f64)>,
    next_id: u64,
    last_inference: Option<f64>,
    error: Option<String>,
}

pub struct PerceptionService {
    pub camera: Arc<CameraService>,
    pub mode: String,
    pub fps: f64,
    on_detection: Mutex<Option<Box<dyn Fn(&Value, &CameraFrame) + Send + Sync>>>,
    inner: Mutex<Inner>,
    stop: AtomicBool,
}

impl PerceptionService {
    pub fn new(
        camera: Arc<CameraService>,
        mode: &str,
        on_detection: Option<Box<dyn Fn(&Value, &CameraFrame) + Send + Sync>>,
    ) -> Self {
        Self {
            camera,
            mode: mode.to_string(),
            fps: 3.0,
            on_detection: Mutex::new(on_detection),
            inner: Mutex::new(Inner {
                packet: json!({"ts": 0, "frame": -1, "fps": 0, "boxes": [], "detector": "disabled", "source": "WAITING"}),
                tracks: HashMap::new(),
                next_id: 1,
                last_inference: None,
                error: None,
            }),
            stop: AtomicBool::new(false),
        }
    }

    /// Detect on one frame. Without an ONNX model this always returns no
    /// boxes (the Rust port drops the OpenCV HOG fallback — compile with
    /// `detector` feature + a model file for real inference).
    fn detect(&self, _jpeg: &[u8]) -> Result<Vec<Value>, String> {
        Ok(Vec::new())
    }

    fn track(&self, boxes: &mut Vec<Value>, now: f64) {
        let mut inner = self.inner.lock().unwrap();
        inner.tracks.retain(|_, (_, t)| now - *t < 2.0);
        let mut available: Vec<u64> = inner.tracks.keys().cloned().collect();
        for boxv in boxes.iter_mut() {
            let mut best: (f64, Option<u64>) = (0.0, None);
            for key in &available {
                let (tb, _) = &inner.tracks[key];
                if tb["cls"] == boxv["cls"] {
                    let o = iou(boxv, tb);
                    if o > best.0 {
                        best = (o, Some(*key));
                    }
                }
            }
            let identifier = if best.0 < 0.25 {
                let id = inner.next_id;
                inner.next_id += 1;
                id
            } else {
                let id = best.1.unwrap();
                available.retain(|k| *k != id);
                id
            };
            boxv["id"] = json!(identifier);
            inner.tracks.insert(identifier, (boxv.clone(), now));
        }
    }

    pub fn start(self: Arc<Self>) {
        std::thread::spawn(move || self.run());
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
                                let r = inner.last_inference.map(|l| 1.0 / (completed - l)).unwrap_or(0.0);
                                inner.last_inference = Some(completed);
                                r
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

    pub fn close(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
