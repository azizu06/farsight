//! Timestamped mission playback. No serial link or motor control is constructed.
//! Port of jetson/replay.py.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{json, Value};

use crate::camera::{CameraFrame, CameraService};
use crate::perception::PerceptionService;
use crate::state::now_monotonic;

pub struct ReplayRun {
    pub events: Vec<Value>,
    pub frame_span: i64,
    pub duration: f64,
}

impl ReplayRun {
    pub fn new(directory: &Path) -> Result<Self, String> {
        let root = directory
            .canonicalize()
            .map_err(|e| format!("Replay directory unavailable: {e}"))?;
        let manifest: Value = serde_json::from_str(
            &std::fs::read_to_string(root.join("replay.json"))
                .map_err(|e| format!("Missing replay manifest: {e}"))?,
        )
        .map_err(|e| format!("Invalid replay manifest: {e}"))?;
        if manifest["version"].as_i64() != Some(1) {
            return Err("Unsupported replay version".into());
        }
        let mut events: Vec<Value> = std::fs::read_to_string(root.join("replay.jsonl"))
            .map_err(|e| format!("Missing replay events: {e}"))?
            .lines()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect();
        events.sort_by(|a, b| {
            a["t"].as_f64()
                .unwrap_or(0.0)
                .partial_cmp(&b["t"].as_f64().unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        if events.is_empty() || !events.iter().any(|e| e["kind"] == "frame") {
            return Err("Replay has no recorded frames".into());
        }
        for event in events.iter_mut() {
            if event["kind"] == "frame" {
                let file = event["file"].as_str().unwrap_or("");
                let path = root.join(file);
                // Mirrors resolve()+is_relative_to(root): reject escapes.
                let resolved = path.canonicalize().unwrap_or_else(|_| PathBuf::from("__missing__"));
                if !resolved.starts_with(&root) {
                    return Err("Replay frame escapes mission directory".into());
                }
                if !path.is_file() {
                    return Err(format!("Missing replay frame: {file}"));
                }
                event["path"] = json!(path.to_string_lossy().to_string());
            }
        }
        let frame_span = 1 + events
            .iter()
            .filter(|e| e["kind"] == "frame")
            .map(|e| e["frame"].as_i64().unwrap_or(0))
            .max()
            .unwrap_or(0);
        let duration = manifest["duration_s"]
            .as_f64()
            .unwrap_or(0.0)
            .max(events.last().unwrap()["t"].as_f64().unwrap_or(0.0) + 0.1);
        Ok(Self { events, frame_span, duration })
    }
}

/// Read-only control stand-in: replays the recorded packet and discards
/// every incoming command. Port of ReplayControl.
pub struct ReplayControl {
    pub packet: Mutex<Value>,
}

impl ReplayControl {
    pub fn new() -> Self {
        Self { packet: Mutex::new(json!({})) }
    }

    pub fn status(&self) -> Value {
        let packet = self.packet.lock().unwrap().clone();
        let mut recorded = packet.as_object().cloned().unwrap_or_default();
        for key in ["permit", "session_id"] {
            recorded.remove(key);
        }
        let mut status = serde_json::Map::new();
        status.extend(recorded.clone());
        status.insert("type".into(), json!("control_status"));
        status.insert("replay".into(), json!(true));
        status.insert("rover".into(), json!(false));
        status.insert("permit".into(), json!(""));
        status.insert("permit_ms".into(), json!(0));
        status.insert("session_id".into(), json!("replay"));
        status.insert("requires_release".into(), json!(true));
        status.insert("reason".into(), json!("replay_read_only"));
        status.insert("estop".into(), json!(true));
        status.insert("left_pwm".into(), json!(0));
        status.insert("right_pwm".into(), json!(0));
        status.insert("recorded_left_pwm".into(), json!(packet["left_pwm"].as_i64().unwrap_or(0)));
        status.insert("recorded_right_pwm".into(), json!(packet["right_pwm"].as_i64().unwrap_or(0)));
        Value::Object(status)
    }

    /// GET /control/status — status minus the session token.
    pub fn public_status(&self) -> Value {
        let mut status = self.status();
        status.as_object_mut().unwrap().remove("session_id");
        status
    }
}

/// Feeds recorded frames/detections/control status back through the live
/// services. Port of ReplaySession.
pub struct ReplaySession {
    pub loop_index: Mutex<i64>,
    pub run: ReplayRun,
    pub camera: Arc<CameraService>,
    pub perception: Arc<PerceptionService>,
    pub control: Arc<ReplayControl>,
}

use std::sync::Arc;

impl ReplaySession {
    pub fn new(
        directory: &Path,
        camera: Arc<CameraService>,
        perception: Arc<PerceptionService>,
        control: Arc<ReplayControl>,
    ) -> Result<Self, String> {
        Ok(Self {
            loop_index: Mutex::new(0),
            run: ReplayRun::new(directory)?,
            camera,
            perception,
            control,
        })
    }

    pub fn publish(&self, event: Option<&Value>) {
        let mut loop_index = self.loop_index.lock().unwrap();
        match event {
            None => {
                // Clear all streams at the loop boundary.
                *loop_index += 1;
                self.camera.set_phone_rgb(None);
                *self.control.packet.lock().unwrap() = json!({});
                self.perception.set_packet(json!({
                    "ts": 0, "frame": -1, "fps": 0, "boxes": [], "source": "REPLAY"
                }));
            }
            Some(event) => match event["kind"].as_str() {
                Some("frame") => {
                    let path = PathBuf::from(event["path"].as_str().unwrap_or(""));
                    if let Ok(jpeg) = std::fs::read(&path) {
                        let offset = (*loop_index - 1) * self.run.frame_span;
                        self.camera.set_phone_rgb(Some(CameraFrame {
                            jpeg,
                            received: now_monotonic(),
                            frame_id: event["frame"].as_i64().unwrap_or(0) + offset,
                            source: format!("REPLAY: {}", event["source"].as_str().unwrap_or("")),
                        }));
                    }
                }
                Some("detections") => {
                    let mut packet = event["packet"].as_object().cloned().unwrap_or_default();
                    let offset = (*loop_index - 1) * self.run.frame_span;
                    let recorded = packet["frame"].as_i64().unwrap_or(0);
                    packet.insert("replay".into(), json!(true));
                    packet.insert("replay_loop".into(), json!(*loop_index));
                    packet.insert("frame".into(), json!(recorded + offset));
                    packet.insert("recorded_frame".into(), json!(recorded));
                    packet.insert("received".into(), json!(now_monotonic()));
                    let source = packet
                        .get("source")
                        .and_then(Value::as_str)
                        .unwrap_or("unknown")
                        .to_string();
                    packet.insert("source".into(), json!(format!("REPLAY: {source}")));
                    self.perception.set_packet(Value::Object(packet));
                }
                Some("control") => {
                    *self.control.packet.lock().unwrap() = event["packet"].clone();
                }
                _ => {}
            },
        }
    }
}
