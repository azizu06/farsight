//! Navigation seam between farsight-server and the ROS 2 Nav2 stack
//! (ros2/farsight_nav). Port of jetson/nav.py — same states, same contract,
//! same loopback-only /ws/nav bridge protocol (version 1).
//!
//! The server stays the only authority over motors. The bridge connects over
//! WebSocket; the server streams pose, depth frames and range to it and tells
//! it the operator intent; the bridge streams cmd_vel and status back. A
//! velocity command only moves the rover while ControlState sees a live
//! held-deadman lease in an autonomy mode; a missing bridge falls back to the
//! reactive autonomy policy.
//!
//! Frames (docs/navigation.md): ARKit/voxel-map world is metres with Y up and
//! the rover starting toward -Z. ROS odom is X forward, Y left, Z up, so
//! X = -z, Y = -x. Viewer-facing coordinates are always voxel-map x, z.

use std::sync::{Arc, Mutex, Weak};

use serde_json::{json, Map, Value};

use crate::control::ControlState;
use crate::geometry::{flatten_f, Mat4};
use crate::phone::RgbdFrame;
use crate::state::{now_monotonic, MapState};

pub const PROTOCOL: i64 = 1;
pub const STATES: [&str; 7] = [
    "idle",
    "planning",
    "driving",
    "recovering",
    "arrived",
    "failed",
    "unavailable",
];
pub const NAV_MODES: [&str; 3] = ["explore", "seek", "goto"];

/// A velocity command older than one control lease stops the rover.
pub const CMD_TTL: f64 = 0.25;
/// No bridge status for this long: Nav2 is unavailable, use the fallback.
pub const BRIDGE_TTL: f64 = 1.5;
/// Nav2 motion needs a normal-tracking ARKit pose at least this fresh ...
pub const POSE_TTL: f64 = 0.5;
/// ... and measured depth of the same sensor session at least this fresh.
pub const DEPTH_TTL: f64 = 1.0;
/// Target unseen this long: Seek fails and stops.
pub const SEEK_LOST_S: f64 = 3.0;
/// Re-send the standoff goal when the person moved this far.
pub const SEEK_MOVE_M: f64 = 0.3;
/// The bridge must run on this computer; anything else is refused.
pub const LOOPBACK: [&str; 4] = ["127.0.0.1", "::1", "localhost", "testclient"];

/// Voxel-map world (x, z) to ROS odom (X, Y).
pub fn world_to_odom(x: f64, z: f64) -> (f64, f64) {
    (-z, -x)
}

/// ROS odom (X, Y) to voxel-map world (x, z).
pub fn odom_to_world(x: f64, y: f64) -> (f64, f64) {
    (-y, -x)
}

/// Douglas-Peucker polyline reduction, then a uniform cap that keeps both ends.
pub fn simplify(points: &[(f64, f64)], tolerance: f64, limit: usize) -> Vec<(f64, f64)> {
    let points: Vec<(f64, f64)> = points.to_vec();
    if points.len() <= 2 {
        return points;
    }
    let n = points.len();
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[n - 1] = true;
    let mut stack = vec![(0usize, n - 1)];
    while let Some((first, last)) = stack.pop() {
        let (ax, ay) = points[first];
        let (bx, by) = points[last];
        let (dx, dy) = (bx - ax, by - ay);
        let norm = dx.hypot(dy);
        let (mut worst, mut index) = (-1.0f64, None);
        for i in first + 1..last {
            let (px, py) = points[i];
            let distance = if norm > 1e-9 {
                (dy * (px - ax) - dx * (py - ay)).abs() / norm
            } else {
                (px - ax).hypot(py - ay)
            };
            if distance > worst {
                worst = distance;
                index = Some(i);
            }
        }
        if let Some(i) = index {
            if worst > tolerance {
                keep[i] = true;
                stack.push((first, i));
                stack.push((i, last));
            }
        }
    }
    let mut reduced: Vec<(f64, f64)> = points
        .into_iter()
        .zip(keep)
        .filter_map(|(p, k)| if k { Some(p) } else { None })
        .collect();
    if reduced.len() > limit {
        let step = (reduced.len() - 1) as f64 / (limit - 1) as f64;
        reduced = (0..limit)
            .map(|i| reduced[(i as f64 * step).round_ties_even() as usize])
            .collect();
    }
    reduced
}

/// uint32 header length, JSON header, then little-endian uint16 millimetres
/// (0 = no return). Low-confidence samples are dropped so the bridge publishes
/// measured points only. Intrinsics are rescaled for the subsampled grid.
pub fn encode_depth(
    frame: &RgbdFrame,
    floor_y: f64,
    source: &str,
    age_ms: f64,
    generation: i64,
    max_width: usize,
    max_range: f64,
) -> Vec<u8> {
    let (w, h) = (frame.depth_width, frame.depth_height);
    let stride = ((w as f64 / max_width as f64).ceil() as usize).max(1);
    let (sw, sh) = (w.div_ceil(stride), h.div_ceil(stride));
    let mut mm = Vec::with_capacity(sw * sh * 2);
    for r in (0..h).step_by(stride) {
        for c in (0..w).step_by(stride) {
            let i = r * w + c;
            let d = frame.depth[i] as f64;
            let valid = d.is_finite() && d > 0.15 && d <= max_range && frame.confidence[i] >= 1;
            let v = if valid {
                (d * 1000.0).round_ties_even() as u16
            } else {
                0
            };
            mm.extend_from_slice(&v.to_le_bytes());
        }
    }
    let k = &frame.calibration;
    let header = json!({
        "type": "depth",
        "version": PROTOCOL,
        "frame_id": frame.header.get("frame_id").and_then(Value::as_i64).unwrap_or(0),
        "width": sw,
        "height": sh,
        "fx": k.fx / stride as f64,
        "fy": k.fy / stride as f64,
        "cx": k.cx / stride as f64,
        "cy": k.cy / stride as f64,
        "transform": flatten_f(&frame.transform).to_vec(),
        "floor_y": floor_y,
        "source": source,
        "age_ms": age_ms,
        "generation": generation,
        "tracking": frame.header.get("tracking").cloned().unwrap_or(Value::Null),
    });
    let data = serde_json::to_vec(&header).expect("depth header JSON");
    let mut packet = Vec::with_capacity(4 + data.len() + mm.len());
    packet.extend_from_slice(&(data.len() as u32).to_le_bytes());
    packet.extend_from_slice(&data);
    packet.extend_from_slice(&mm);
    packet
}

/// Inverse of encode_depth: (header, float32 metres) for tests and the bridge.
pub fn decode_depth(packet: &[u8]) -> (Value, Vec<f32>) {
    let length = u32::from_le_bytes(packet[0..4].try_into().unwrap()) as usize;
    let header: Value = serde_json::from_slice(&packet[4..4 + length]).expect("depth header");
    let (w, h) = (
        header["width"].as_u64().unwrap() as usize,
        header["height"].as_u64().unwrap() as usize,
    );
    let raw = &packet[4 + length..4 + length + w * h * 2];
    let depth: Vec<f32> = raw
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]) as f32 / 1000.0)
        .collect();
    (header, depth)
}

/// A nav object with no backend (field-for-field with the Python server).
pub fn empty_nav(state: &str, message: &str) -> Value {
    json!({
        "state": state,
        "mode": Value::Null,
        "goal": Value::Null,
        "path": [],
        "frontiers": [],
        "backend": Value::Null,
        "message": message,
        "revision": 0,
    })
}

pub fn sensor_message(fault: &str) -> &'static str {
    match fault {
        "nav_pose_lost" => "No fresh ARKit pose; navigation stopped",
        "nav_tracking_limited" => "ARKit tracking limited; navigation stopped",
        "nav_depth_lost" => "No fresh LiDAR depth; navigation stopped",
        "nav_sensor_reset" => "Sensor session changed; navigation reset",
        _ => "Navigation sensors unavailable",
    }
}

/// POST /nav/goal outcomes: 409 {"nav": <nav object>, "message": reason} or
/// 422 for a non-finite / out-of-range goal.
pub enum GoalError {
    Refused { nav: Value, message: String },
    Unprocessable(String),
}

#[derive(Clone)]
pub struct Seek {
    pub target_id: Option<i64>,
    pub person: Option<(f64, f64)>,
    pub seen_at: f64,
    pub goal: Option<(f64, f64)>,
    pub goal_id: Option<String>,
    pub state: String,
    pub distance: Option<f64>,
}

pub struct NavInner {
    /// Some(token) while a bridge WebSocket is claimed.
    pub bridge: Option<u64>,
    pub bridge_seen: f64,
    pub bridge_hello: Option<Value>,
    /// (v, w normalized, received monotonic, linear m/s, angular rad/s CCW)
    pub cmd: Option<(f64, f64, f64, f64, f64)>,
    /// Latest bridge status (status keys whitelisted by `receive`).
    pub remote: Map<String, Value>,
    pub mode: Option<String>,
    pub target_id: Option<i64>,
    pub active: bool,
    /// Voxel-map (x, z) go-to-point goal.
    pub goal: Option<(f64, f64)>,
    pub goal_id: i64,
    /// Newest normal-tracking RGB-D frame of the current sensor session.
    pub frame: Option<RgbdFrame>,
    pub frame_at: Option<f64>,
    pub frame_seq: i64,
    pub identity: Option<crate::phone::Identity>,
    pub generation: i64,
    pub seek: Option<Seek>,
    pub seek_seq: i64,
    pub seek_checked: f64,
    /// Last (path, frontiers) emitted by nav_object, and their version —
    /// bumped whenever the arrays change (nav.py #110).
    pub arrays: (Vec<Value>, Vec<Value>),
    pub revision: i64,
}

pub struct NavService {
    pub mapping: Arc<MapState>,
    pub clock: crate::control::Clock,
    pub speed_mps: f64,
    pub turn_radps: f64,
    pub max_v: f64,
    pub max_w: f64,
    pub camera_height: f64,
    pub backed_modes: std::collections::HashSet<String>,
    pub standoff: f64,
    control: Mutex<Option<Weak<ControlState>>>,
    pub inner: Mutex<NavInner>,
}

impl NavService {
    pub fn new(
        mapping: Arc<MapState>,
        speed_mps: f64,
        turn_radps: f64,
        max_v: f64,
        max_w: f64,
        camera_height: f64,
        clock: Option<crate::control::Clock>,
    ) -> Result<Self, String> {
        for (name, value) in [
            ("speed_mps", speed_mps),
            ("turn_radps", turn_radps),
            ("max_v", max_v),
            ("max_w", max_w),
            ("camera_height", camera_height),
        ] {
            if !value.is_finite() || value <= 0.0 {
                return Err(format!("Navigation {name} must be positive"));
            }
        }
        let identity = mapping.inner.lock().unwrap().identity.clone();
        Ok(Self {
            mapping,
            clock: clock.unwrap_or_else(|| Arc::new(now_monotonic)),
            speed_mps,
            turn_radps,
            max_v: max_v.min(1.0),
            max_w: max_w.min(1.0),
            camera_height,
            backed_modes: NAV_MODES.iter().map(|m| m.to_string()).collect(),
            standoff: 0.8,
            control: Mutex::new(None),
            inner: Mutex::new(NavInner {
                bridge: None,
                bridge_seen: f64::NEG_INFINITY,
                bridge_hello: None,
                cmd: None,
                remote: Map::new(),
                mode: None,
                target_id: None,
                active: false,
                goal: None,
                goal_id: 0,
                frame: None,
                frame_at: None,
                frame_seq: 0,
                identity,
                generation: 0,
                seek: None,
                seek_seq: 0,
                seek_checked: 0.0,
                arrays: (Vec::new(), Vec::new()),
                revision: 0,
            }),
        })
    }

    pub fn attach(&self, control: &Arc<ControlState>) {
        *self.control.lock().unwrap() = Some(Arc::downgrade(control));
    }

    pub fn control(&self) -> Option<Arc<ControlState>> {
        self.control
            .lock()
            .unwrap()
            .as_ref()
            .and_then(Weak::upgrade)
    }

    // -- inputs from mapping and control ---------------------------------------

    /// Detect a new sensor session; returns the current generation. Old goals,
    /// commands and depth never carry across, and the bridge resets ROS state.
    pub fn sync_generation(&self) -> i64 {
        let identity = self.mapping.inner.lock().unwrap().identity.clone();
        let mut s = self.inner.lock().unwrap();
        if identity != s.identity {
            s.identity = identity;
            s.generation += 1;
            s.goal = None;
            s.cmd = None;
            s.frame = None;
            s.frame_at = None;
            s.remote.insert("state".into(), json!("idle"));
            s.remote.insert("goal_id".into(), Value::Null);
            s.remote.insert("path".into(), json!([]));
            s.remote.insert("frontiers".into(), json!([]));
        }
        s.generation
    }

    /// MapState frame observer: keep only the newest depth frame (no queue).
    /// Only normal-tracking frames of the current sensor session count; any
    /// other frame withdraws depth authority until a good one arrives.
    pub fn offer_frame(&self, frame: &RgbdFrame) {
        self.sync_generation();
        let header = &frame.header;
        let mapping_tracking = self.mapping.inner.lock().unwrap().tracking.clone();
        let good = header.get("tracking").and_then(Value::as_str) == Some("normal")
            && mapping_tracking == "normal"
            && {
                let s = self.inner.lock().unwrap();
                let identity = (
                    header
                        .get("session_id")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                    header.get("map_epoch").and_then(Value::as_u64),
                );
                s.identity
                    .as_ref()
                    .map(|(sid, epoch)| {
                        identity.0.as_deref() == Some(sid.as_str()) && identity.1 == Some(*epoch)
                    })
                    .unwrap_or(false)
            };
        let mut s = self.inner.lock().unwrap();
        s.frame = if good { Some(frame.clone()) } else { None };
        s.frame_at = if good {
            Some(
                header
                    .get("_received_monotonic")
                    .and_then(Value::as_f64)
                    .unwrap_or_else(now_monotonic),
            )
        } else {
            None
        };
        s.frame_seq += 1;
    }

    /// None when measured navigation sensors are fresh, else a stop reason.
    /// A fresh RGB camera is enough to teleoperate, not to navigate: Nav2
    /// motion also needs current-session normal ARKit tracking and LiDAR depth.
    pub fn sensor_fault(&self) -> Option<&'static str> {
        let generation = self.sync_generation();
        let (transform, _, source, _) = self.pose_snapshot();
        if transform.is_none() {
            return Some(if source == "limited" {
                "nav_tracking_limited"
            } else {
                "nav_pose_lost"
            });
        }
        let s = self.inner.lock().unwrap();
        if generation != s.generation {
            return Some("nav_sensor_reset");
        }
        if s.frame.is_none()
            || s.frame_at.is_none()
            || now_monotonic() - s.frame_at.unwrap() >= DEPTH_TTL
        {
            return Some("nav_depth_lost");
        }
        None
    }

    /// Called by ControlState on every output tick with the operator's intent.
    pub fn sync(&self, mode: Option<&str>, target_id: Option<i64>, active: bool) {
        let mode = mode.filter(|m| NAV_MODES.contains(m)).map(str::to_string);
        self.sync_generation();
        let mut s = self.inner.lock().unwrap();
        if matches!(mode.as_deref(), Some("explore") | Some("seek")) && s.goal.is_some() {
            s.goal = None; // selecting another autonomy mode disarms go-to-point
        }
        // A release keeps the goal armed; the next held "goto" heartbeat resumes it.
        let active = active && mode.is_some();
        if mode.as_deref() != Some("seek")
            || !active
            || s.seek
                .as_ref()
                .map(|sk| sk.target_id != target_id)
                .unwrap_or(false)
        {
            s.seek = None;
        }
        if active && !s.active {
            s.cmd = None; // never resume on a command computed before this press
        }
        s.mode = mode;
        s.target_id = target_id;
        s.active = active;
    }

    /// Stick input, cancel, STOP or a controller change: forget the goal.
    pub fn disarm(&self) {
        let mut s = self.inner.lock().unwrap();
        s.goal = None;
        s.cmd = None;
    }

    // -- backend state ---------------------------------------------------------

    pub fn available(&self) -> bool {
        let s = self.inner.lock().unwrap();
        s.bridge.is_some() && (self.clock)() - s.bridge_seen < BRIDGE_TTL
    }

    /// Test seam: mark a bridge as connected without a WebSocket (Python
    /// tests assign `nav.bridge = object()` directly).
    #[doc(hidden)]
    pub fn connect_bridge_for_test(&self) {
        let mut s = self.inner.lock().unwrap();
        s.bridge = Some(u64::MAX);
        s.bridge_seen = (self.clock)();
    }

    pub fn handles(&self, mode: &str) -> bool {
        self.backed_modes.contains(mode) && self.available()
    }

    /// Fresh Nav2 command for ControlState, or None when none exists.
    /// Normalized (v, w) with positive w turning right by default; with `si`
    /// the raw cmd_vel (m/s, rad/s counter-clockwise) for a calibrated profile.
    pub fn command(&self, si: bool) -> Option<(f64, f64)> {
        let s = self.inner.lock().unwrap();
        if s.bridge.is_none()
            || (self.clock)() - s.bridge_seen >= BRIDGE_TTL
            || !s
                .mode
                .as_deref()
                .map(|m| self.backed_modes.contains(m))
                .unwrap_or(false)
            || !s.active
        {
            return None;
        }
        if s.mode.as_deref() == Some("goto")
            && (s.goal.is_none() || Self::remote_state_locked(&s) == "arrived")
        {
            return Some((0.0, 0.0));
        }
        if s.mode.as_deref() == Some("seek")
            && (s.seek.is_none()
                || matches!(
                    s.seek.as_ref().unwrap().state.as_str(),
                    "arrived" | "failed"
                )
                || s.seek.as_ref().unwrap().goal.is_none())
        {
            return Some((0.0, 0.0)); // no located person yet, at standoff, or target lost
        }
        let cmd = s.cmd?;
        if (self.clock)() - cmd.2 > CMD_TTL {
            return None;
        }
        Some(if si { (cmd.3, cmd.4) } else { (cmd.0, cmd.1) })
    }

    /// Python `value == goal_id`: numbers compare numerically, anything else
    /// (missing, null, string, bool) never equals the integer goal id.
    fn goal_id_eq(v: Option<&Value>, goal_id: i64) -> bool {
        v.and_then(Value::as_f64)
            .map(|g| g == goal_id as f64)
            .unwrap_or(false)
    }

    fn remote_state_locked(s: &NavInner) -> String {
        if s.mode.as_deref() == Some("goto")
            && !Self::goal_id_eq(s.remote.get("goal_id"), s.goal_id)
        {
            return "planning".into();
        }
        match s.remote.get("state").and_then(Value::as_str) {
            Some(state) if STATES.contains(&state) => state.to_string(),
            _ => "planning".into(),
        }
    }

    /// The viewer contract (docs/interfaces.md); mode/phase from ControlState.
    pub fn nav_object(
        &self,
        mode: Option<&str>,
        phase: Option<&str>,
        active: Option<bool>,
    ) -> Value {
        let fault = if self.available() {
            self.sensor_fault()
        } else {
            None
        };
        let mut s = self.inner.lock().unwrap();
        let mode = mode.filter(|m| NAV_MODES.contains(m)).map(str::to_string);
        let active = active.unwrap_or(s.active);
        let available = s.bridge.is_some() && (self.clock)() - s.bridge_seen < BRIDGE_TTL;
        let backed = mode
            .as_deref()
            .map(|m| self.backed_modes.contains(m))
            .unwrap_or(false);
        let mut goal: Option<Value> = None;
        let mut path: Option<Vec<Value>> = None;
        let mut frontiers: Vec<Value> = Vec::new();
        let state: &str;
        let message: String;
        let mut backend: Option<&'static str> = if available && (mode.is_none() || backed) {
            Some("nav2")
        } else {
            Some("fallback")
        };
        if backend == Some("nav2") {
            let mut st: String;
            let mut msg: String;
            if mode.is_none() && s.goal.is_none() {
                st = "idle".into();
                msg = "Nav2 ready".into();
            } else if mode.as_deref() == Some("goto") && s.goal.is_none() {
                st = "idle".into();
                msg = "No go-to-point goal; POST /nav/goal".into();
            } else if mode.is_none() && s.goal.is_some() {
                st = "idle".into();
                msg = "Go-to-point armed; hold the deadman to resume".into();
            } else if !active {
                st = "idle".into();
                msg = "Hold the deadman to navigate".into();
            } else {
                st = Self::remote_state_locked(&s);
                msg = s
                    .remote
                    .get("message")
                    .map(|m| match m {
                        Value::String(t) => t.clone(),
                        Value::Null => String::new(),
                        other => other.to_string(),
                    })
                    .unwrap_or_default()
                    .chars()
                    .take(200)
                    .collect();
                let mut p = Some(Self::world_points(
                    s.remote.get("path").cloned().unwrap_or(json!([])),
                    200,
                    0.03,
                ));
                let seek = if mode.as_deref() == Some("seek") {
                    s.seek.clone()
                } else {
                    None
                };
                if mode.as_deref() == Some("seek")
                    && (seek.is_none()
                        || (seek.as_ref().unwrap().goal.is_none()
                            && seek.as_ref().unwrap().state == "planning"))
                {
                    st = "planning".into();
                    msg = "Locating the person in LiDAR depth".into();
                    p = None;
                } else if let Some(sk) = &seek {
                    if sk.state == "arrived" {
                        st = "arrived".into();
                        msg = format!("At the person, {:.1} m away", sk.distance.unwrap_or(0.0));
                        p = None;
                    } else if sk.state == "failed" {
                        st = "failed".into();
                        msg = "Target lost".into();
                        p = None;
                    } else {
                        msg = format!(
                            "Following the person, {:.1} m away",
                            sk.distance.unwrap_or(0.0)
                        );
                    }
                }
                path = p;
            }
            if let Some(f) = fault {
                // Nav2 is up but cannot be trusted to drive: say why.
                st = "unavailable".into();
                msg = sensor_message(f).into();
                path = None;
            }
            if let Some(g) = s.goal {
                goal = Some(json!({"x": r3(g.0), "z": r3(g.1)}));
            }
            if mode.as_deref() == Some("seek") && s.seek.as_ref().and_then(|sk| sk.goal).is_some() {
                let g = s.seek.as_ref().unwrap().goal.unwrap();
                goal = Some(json!({"x": r3(g.0), "z": r3(g.1)}));
            }
            frontiers = Self::world_points(
                s.remote.get("frontiers").cloned().unwrap_or(json!([])),
                50,
                0.0,
            );
            let arrays = (path.unwrap_or_default(), frontiers);
            if arrays != s.arrays {
                s.arrays = arrays.clone();
                s.revision += 1;
            }
            return json!({
                "state": st,
                "mode": mode,
                "goal": goal.unwrap_or(Value::Null),
                "path": arrays.0,
                "frontiers": arrays.1,
                "backend": backend,
                "message": msg,
                "revision": s.revision,
            });
        } else if matches!(mode.as_deref(), Some("explore") | Some("seek")) {
            state = match phase {
                Some("arrived") => "arrived",
                Some("target_lost") => "failed",
                _ => {
                    if active {
                        "driving"
                    } else {
                        "idle"
                    }
                }
            };
            message = if !available {
                "Nav2 bridge not connected; reactive fallback".into()
            } else {
                format!("{} uses the reactive policy", mode.as_deref().unwrap())
            };
        } else {
            state = "unavailable";
            backend = None;
            message = if s.bridge.is_none() {
                "Nav2 bridge not connected".into()
            } else {
                "Nav2 bridge silent; commands stopped".into()
            };
            if mode.as_deref() == Some("goto") {
                if let Some(g) = s.goal {
                    goal = Some(json!({"x": r3(g.0), "z": r3(g.1)}));
                }
            }
        }
        let arrays = (path.unwrap_or_default(), frontiers);
        if arrays != s.arrays {
            s.arrays = arrays.clone();
            s.revision += 1;
        }
        json!({
            "state": state,
            "mode": mode,
            "goal": goal.unwrap_or(Value::Null),
            "path": arrays.0,
            "frontiers": arrays.1,
            "backend": backend,
            "message": message,
            "revision": s.revision,
        })
    }

    fn world_points(odom_points: Value, limit: usize, tolerance: f64) -> Vec<Value> {
        let mut points = Vec::new();
        if let Some(arr) = odom_points.as_array() {
            for point in arr {
                if let Some(p) = point.as_array() {
                    if p.len() >= 2
                        && p[0].as_f64().map(|v| v.is_finite()).unwrap_or(false)
                        && p[1].as_f64().map(|v| v.is_finite()).unwrap_or(false)
                    {
                        let (x, z) = odom_to_world(p[0].as_f64().unwrap(), p[1].as_f64().unwrap());
                        points.push((x, z));
                    }
                }
            }
        }
        let points = if tolerance != 0.0 {
            simplify(&points, tolerance, limit)
        } else {
            points.into_iter().take(limit).collect()
        };
        points
            .into_iter()
            .map(|(x, z)| {
                // no "-0.0" in JSON
                json!([r3(x) + 0.0, r3(z) + 0.0])
            })
            .collect()
    }

    pub fn detail(&self) -> Value {
        let pose_source = self.pose_snapshot().2;
        let fault = self.sensor_fault();
        let s = self.inner.lock().unwrap();
        let bridge_age_ms = if s.bridge.is_some() {
            Some(((self.clock)() - s.bridge_seen) * 1000.0)
        } else {
            None
        };
        let remote = {
            let keys = [
                "state",
                "goal_id",
                "message",
                "controller_hz",
                "planner_ms",
                "recoveries",
                "result",
                "costmap",
                "nav2",
            ];
            let mut m = Map::new();
            for key in keys {
                m.insert(
                    key.to_string(),
                    s.remote.get(key).cloned().unwrap_or(Value::Null),
                );
            }
            m
        };
        let mut backed: Vec<String> = self.backed_modes.iter().cloned().collect();
        backed.sort();
        json!({
            "available": s.bridge.is_some() && (self.clock)() - s.bridge_seen < BRIDGE_TTL,
            "bridge": s.bridge.is_some(),
            "bridge_age_ms": bridge_age_ms.map(|v| v.round_ties_even() as i64),
            "bridge_hello": s.bridge_hello.clone().unwrap_or(Value::Null),
            "goal_id": s.goal_id,
            "backed_modes": backed,
            "remote": remote,
            "pose_source": pose_source,
            "sensor_fault": fault,
            "generation": s.generation,
            "depth_age_ms": s.frame_at.map(|at| ((now_monotonic() - at) * 1000.0).round_ties_even() as i64),
            "speed_mps": self.speed_mps,
            "turn_radps": self.turn_radps,
            "max_v": self.max_v,
            "max_w": self.max_w,
        })
    }

    // -- pose and sensor streams for the bridge ----------------------------------

    /// (camera-to-world transform, tracking, source, age_s); transform is None
    /// unless the current sensor session has a fresh pose with normal ARKit
    /// tracking (source then says why: "limited" or "lost"). Nothing else is
    /// navigation authority: no dead reckoning.
    pub fn pose_snapshot(&self) -> (Option<Mat4>, String, &'static str, Option<f64>) {
        let (transform, tracking, received, connected, scan, identity) = {
            let s = self.mapping.inner.lock().unwrap();
            (
                s.pose,
                s.tracking.clone(),
                s.last_received,
                s.connected,
                s.scan.is_some(),
                s.identity.clone(),
            )
        };
        let demo = self.mapping.demo;
        let age = if received != 0.0 {
            Some(now_monotonic() - received)
        } else {
            None
        };
        if !connected || age.is_none() || age.unwrap() >= POSE_TTL || scan || identity.is_none() {
            return (None, tracking, "lost", age);
        }
        if tracking != "normal" {
            return (None, tracking, "limited", age);
        }
        (
            Some(transform),
            tracking,
            if demo { "simulated" } else { "arkit" },
            age,
        )
    }

    /// Server hello: drive scaling and, with a drive profile, chassis calibration.
    pub fn hello(&self) -> Value {
        let mut message = json!({
            "type": "hello",
            "version": PROTOCOL,
            "speed_mps": self.speed_mps,
            "turn_radps": self.turn_radps,
            "max_v": self.max_v,
            "max_w": self.max_w,
            "camera_height": self.camera_height,
            "frame": "arkit",
            "camera_yaw_offset_rad": 0.0,
            "drive_profile": Value::Null,
            "generation": self.inner.lock().unwrap().generation,
        });
        if let Some(control) = self.control() {
            if let Some(profile) = &control.drive_profile {
                let max_pwm = control.max_pwm;
                message["camera_yaw_offset_rad"] = json!(profile.camera_yaw_offset_rad);
                message["drive_profile"] = json!({
                    "max_linear_mps": r3(profile.max_linear_mps(Some(max_pwm))),
                    "max_angular_radps": r3(profile.max_angular_radps(Some(max_pwm))),
                    "half_track_m": profile.half_track_m,
                });
            }
        }
        message
    }

    pub fn intent(&self) -> Value {
        let s = self.inner.lock().unwrap();
        let mut goal = Value::Null;
        let mut goal_id: Value = json!(s.goal_id);
        if s.mode.as_deref() == Some("goto") {
            if let Some((x, z)) = s.goal {
                let (ox, oy) = world_to_odom(x, z);
                goal = json!({"x": ox, "y": oy, "yaw": Value::Null});
            }
        } else if s.mode.as_deref() == Some("seek") {
            if let Some(sk) = &s.seek {
                if sk.goal.is_some() && sk.state == "driving" {
                    let (x, z) = sk.goal.unwrap();
                    let (ox, oy) = world_to_odom(x, z);
                    goal = json!({"x": ox, "y": oy, "yaw": Value::Null});
                    goal_id = sk.goal_id.clone().map(Value::from).unwrap_or(Value::Null);
                }
            }
        }
        json!({
            "type": "intent",
            "generation": s.generation,
            "active": s.active && s.mode.as_deref().map(|m| self.backed_modes.contains(m)).unwrap_or(false),
            "mode": s.mode.as_deref().filter(|m| self.backed_modes.contains(*m)),
            "goal": goal,
            "goal_id": goal_id,
            "target_id": s.target_id,
        })
    }

    // -- Seek --------------------------------------------------------------------

    /// Floor (x, z) of a detected person from the LiDAR depth inside their box,
    /// or None.
    pub fn locate(&self, box_: &Value, frame: &RgbdFrame) -> Option<(f64, f64)> {
        let (w, h) = (frame.depth_width, frame.depth_height);
        let (x, y, bw, bh) = (
            box_["x"].as_f64()?,
            box_["y"].as_f64()?,
            box_["w"].as_f64()?,
            box_["h"].as_f64()?,
        );
        let u0 = ((x + 0.3 * bw) * w as f64) as usize;
        let u1 = ((x + 0.7 * bw) * w as f64).ceil() as usize;
        let v0 = ((y + 0.25 * bh) * h as f64) as usize;
        let v1 = ((y + 0.6 * bh) * h as f64).ceil() as usize;
        let (r0, r1) = (v0.min(h), v1.min(h));
        let (c0, c1) = (u0.min(w), u1.min(w));
        let mut patch = Vec::new();
        for r in r0..r1 {
            for c in c0..c1 {
                let i = r * w + c;
                let d = frame.depth[i] as f64;
                if d.is_finite() && d > 0.15 && d <= 8.0 && frame.confidence[i] >= 1 {
                    patch.push(d);
                }
            }
        }
        if patch.len() < 3 {
            return None;
        }
        patch.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let d = if patch.len() % 2 == 1 {
            patch[patch.len() / 2]
        } else {
            (patch[patch.len() / 2 - 1] + patch[patch.len() / 2]) / 2.0
        };
        let k = &frame.calibration;
        let u = (u0 + u1) as f64 / 2.0;
        let v = (v0 + v1) as f64 / 2.0;
        let p = [(u - k.cx) / k.fx * d, -(v - k.cy) / k.fy * d, -d, 1.0];
        let t = &frame.transform;
        let out = [
            t[0][0] * p[0] + t[0][1] * p[1] + t[0][2] * p[2] + t[0][3] * p[3],
            t[1][0] * p[0] + t[1][1] * p[1] + t[1][2] * p[2] + t[1][3] * p[3],
            t[2][0] * p[0] + t[2][1] * p[1] + t[2][2] * p[2] + t[2][3] * p[3],
        ];
        Some((out[0], out[2]))
    }

    /// ControlState calls this each tick in a Nav2-backed Seek with its
    /// detection snapshot.
    pub fn update_seek(&self, detections: &Value) {
        let now = now_monotonic();
        {
            let s = self.inner.lock().unwrap();
            if s.mode.as_deref() != Some("seek") || !s.active || now - s.seek_checked < 0.2 {
                return;
            }
        }
        let (frame, target_id) = {
            let mut s = self.inner.lock().unwrap();
            s.seek_checked = now;
            (s.frame.clone(), s.target_id)
        };
        let seek = self
            .inner
            .lock()
            .unwrap()
            .seek
            .clone()
            .unwrap_or_else(|| Seek {
                target_id,
                person: None,
                seen_at: now,
                goal: None,
                goal_id: None,
                state: "planning".into(),
                distance: None,
            });
        let mut seek = seek;
        let boxes = if detections
            .get("fresh")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && !detections.is_null()
            && detections.get("error").map(|e| e.is_null()).unwrap_or(true)
        {
            detections
                .get("boxes")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        } else {
            Vec::new()
        };
        let found = boxes.iter().find(|b| {
            b.get("id").and_then(Value::as_i64) == target_id
                && b.get("cls").and_then(Value::as_str) == Some("person")
        });
        let person = match (found, &frame) {
            (Some(b), Some(f)) => self.locate(b, f),
            _ => None,
        };
        let transform = self.pose_snapshot().0;
        if let (Some((px, pz)), Some(t)) = (person, transform) {
            let rover = (t[0][3], t[2][3]);
            let (dx, dz) = (px - rover.0, pz - rover.1);
            let distance = dx.hypot(dz);
            seek.person = Some((px, pz));
            seek.seen_at = now;
            seek.distance = Some(distance);
            if distance <= self.standoff + 0.15 {
                seek.state = "arrived".into();
            } else {
                let goal = (
                    px - self.standoff * dx / distance,
                    pz - self.standoff * dz / distance,
                );
                if seek.goal.is_none()
                    || seek.state != "driving"
                    || (goal.0 - seek.goal.unwrap().0).hypot(goal.1 - seek.goal.unwrap().1)
                        > SEEK_MOVE_M
                {
                    let mut s = self.inner.lock().unwrap();
                    s.seek_seq += 1;
                    seek.goal_id = Some(format!("seek-{}", s.seek_seq));
                    drop(s);
                    seek.goal = Some(goal);
                } else {
                    seek.goal = Some(goal);
                }
                seek.state = "driving".into();
            }
        } else if now - seek.seen_at >= SEEK_LOST_S {
            seek.state = "failed".into();
        }
        let mut s = self.inner.lock().unwrap();
        if s.mode.as_deref() == Some("seek") && s.active && s.target_id == seek.target_id {
            s.seek = Some(seek);
        }
    }

    /// Apply one bridge message (cmd_vel or status).
    pub fn receive(&self, message: &Value) -> Result<(), String> {
        if !message.is_object() {
            return Err("Bridge message must be an object".into());
        }
        let kind = message.get("type").and_then(Value::as_str);
        let mut s = self.inner.lock().unwrap();
        let now = (self.clock)();
        match kind {
            Some("cmd_vel") => {
                let linear = message
                    .get("linear")
                    .and_then(Value::as_f64)
                    .filter(|v| v.is_finite());
                let angular = message
                    .get("angular")
                    .and_then(Value::as_f64)
                    .filter(|v| v.is_finite());
                let (Some(linear), Some(angular)) = (linear, angular) else {
                    return Err("cmd_vel needs finite linear and angular".into());
                };
                if s.mode.as_deref() == Some("goto")
                    && message
                        .get("goal_id")
                        .map(|g| !Self::goal_id_eq(Some(g), s.goal_id))
                        .unwrap_or(false)
                {
                    s.bridge_seen = now;
                    return Ok(()); // computed for an earlier goal
                }
                // ROS angular.z is counter-clockwise (left); ControlState positive w turns right.
                let v = (linear / self.speed_mps).clamp(-self.max_v, self.max_v);
                let w = (-angular / self.turn_radps).clamp(-self.max_w, self.max_w);
                s.cmd = Some((v, w, now, linear, angular));
            }
            Some("status") => {
                let keys = [
                    "state",
                    "goal_id",
                    "path",
                    "frontiers",
                    "message",
                    "controller_hz",
                    "planner_ms",
                    "recoveries",
                    "result",
                    "costmap",
                    "nav2",
                ];
                let mut remote = Map::new();
                for key in keys {
                    remote.insert(
                        key.to_string(),
                        message.get(key).cloned().unwrap_or(Value::Null),
                    );
                }
                s.remote = remote;
            }
            Some("pong") => {}
            _ => return Err("Unknown bridge message type".into()),
        }
        s.bridge_seen = now;
        Ok(())
    }

    // -- the loopback bridge websocket --------------------------------------------

    /// /ws/nav bridge endpoint (NavService.websocket). The upgrade already
    /// accepted the socket; `host` is the client IP string (ws.client.host).
    pub async fn run_ws(self: &Arc<Self>, socket: axum::extract::ws::WebSocket, host: String) {
        use axum::extract::ws::{CloseFrame, Message};
        use futures_util::{SinkExt, StreamExt};
        const MAX_TEXT: usize = 262144;
        let close = |code: u16, reason: &str| {
            Message::Close(Some(CloseFrame {
                code,
                reason: reason.into(),
            }))
        };
        if !LOOPBACK.contains(&host.as_str()) {
            let mut socket = socket;
            let _ = socket
                .send(close(
                    1008,
                    "The navigation bridge must run on this computer",
                ))
                .await;
            return;
        }
        static NEXT_TOKEN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
        let token = NEXT_TOKEN.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let busy = {
            let mut s = self.inner.lock().unwrap();
            if s.bridge.is_some() {
                true
            } else {
                s.bridge = Some(token);
                false
            }
        };
        let (mut tx, mut rx) = socket.split();
        if busy {
            // never await while holding the threading lock — released above
            let _ = tx
                .send(close(1008, "Another navigation bridge is connected"))
                .await;
            return;
        }
        let clear_bridge = || {
            let mut s = self.inner.lock().unwrap();
            if s.bridge == Some(token) {
                s.bridge = None;
                s.cmd = None;
                s.remote = Map::new();
            }
        };
        let hello = tokio::time::timeout(std::time::Duration::from_secs(10), rx.next()).await;
        let hello = match hello {
            Ok(Some(Ok(Message::Text(text)))) => serde_json::from_str::<Value>(&text).ok(),
            _ => None,
        };
        let hello_valid = hello
            .as_ref()
            .filter(|h| h.is_object())
            .filter(|h| h.get("type").and_then(Value::as_str) == Some("hello"))
            .filter(|h| h.get("version").and_then(Value::as_f64) == Some(PROTOCOL as f64))
            .is_some();
        if !hello_valid {
            // Any received JSON that is not a bridge hello gets the reason;
            // timeouts, parse errors and disconnects close bare like Python.
            let reason = if hello.is_some() {
                "Expected bridge hello version 1"
            } else {
                ""
            };
            let _ = tx.send(close(1008, reason)).await;
            clear_bridge();
            return;
        }
        let hello = hello.unwrap();
        {
            let mut s = self.inner.lock().unwrap();
            let mut bridge_hello = Map::new();
            for key in ["node", "nav2", "controller", "slam", "costmap_source"] {
                bridge_hello.insert(key.into(), hello.get(key).cloned().unwrap_or(Value::Null));
            }
            s.bridge_hello = Some(Value::Object(bridge_hello));
            s.bridge_seen = (self.clock)();
        }
        if tx
            .send(Message::Text(self.hello().to_string().into()))
            .await
            .is_err()
        {
            let _ = tx.send(close(1008, "")).await;
            clear_bridge();
            return;
        }

        let send = async {
            let mut last_pose = 0.0f64;
            let mut last_range = 0.0f64;
            let mut last_intent_at = 0.0f64;
            let mut sent_frame = -1i64;
            let mut sent_intent: Option<Value> = None;
            let mut sent_generation = self.inner.lock().unwrap().generation;
            loop {
                let now = now_monotonic();
                let generation = self.sync_generation();
                if generation != sent_generation {
                    // New sensor session: the bridge cancels its goal, clears
                    // costmaps and odometry history, drops stale samples.
                    let msg = json!({
                        "type": "reset",
                        "generation": generation,
                        "reason": "sensor session changed",
                    });
                    if tx
                        .send(Message::Text(msg.to_string().into()))
                        .await
                        .is_err()
                    {
                        return;
                    }
                    sent_generation = generation;
                }
                if now - last_pose >= 0.04 {
                    // Limited/lost tracking never renews TF or odometry.
                    let (transform, tracking, source, age) = self.pose_snapshot();
                    if let Some(t) = transform {
                        let msg = json!({
                            "type": "pose",
                            "transform": flatten_f(&t).to_vec(),
                            "tracking": tracking,
                            "source": source,
                            "generation": generation,
                            "age_ms": (age.unwrap_or(0.0) * 1000.0 * 10.0).round_ties_even() / 10.0,
                            "floor_y": -self.camera_height,
                        });
                        if tx
                            .send(Message::Text(msg.to_string().into()))
                            .await
                            .is_err()
                        {
                            return;
                        }
                    }
                    last_pose = now;
                }
                let (frame, seq, frame_generation) = {
                    let s = self.inner.lock().unwrap();
                    (s.frame.clone(), s.frame_seq, s.generation)
                };
                if let Some(frame) = frame.as_ref() {
                    if seq != sent_frame {
                        let received = frame
                            .header
                            .get("_received_monotonic")
                            .and_then(Value::as_f64)
                            .unwrap_or(now);
                        let source = if self.mapping.demo {
                            "simulated"
                        } else {
                            "arkit"
                        };
                        let age_ms =
                            ((now - received).max(0.0) * 1000.0 * 10.0).round_ties_even() / 10.0;
                        let packet = encode_depth(
                            frame,
                            -self.camera_height,
                            source,
                            age_ms,
                            frame_generation,
                            160,
                            6.0,
                        );
                        if tx.send(Message::Binary(packet.into())).await.is_err() {
                            return;
                        }
                    }
                }
                sent_frame = seq;
                if let Some(control) = self.control() {
                    if now - last_range >= 0.1 {
                        let (cm, at) = {
                            let s = control.inner.lock().unwrap();
                            (s.range_cm, s.range_at)
                        };
                        let msg = json!({
                            "type": "range",
                            "cm": cm,
                            "age_ms": if at.is_finite() {
                                // Python round(): an integer age.
                                json!((((self.clock)() - at) * 1000.0).round_ties_even() as i64)
                            } else {
                                Value::Null
                            },
                        });
                        if tx
                            .send(Message::Text(msg.to_string().into()))
                            .await
                            .is_err()
                        {
                            return;
                        }
                        last_range = now;
                    }
                }
                let intent = self.intent();
                if sent_intent.as_ref() != Some(&intent) || now - last_intent_at >= 0.2 {
                    if tx
                        .send(Message::Text(intent.to_string().into()))
                        .await
                        .is_err()
                    {
                        return;
                    }
                    sent_intent = Some(intent);
                    last_intent_at = now;
                }
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
        };
        let recv = async {
            loop {
                match rx.next().await {
                    Some(Ok(Message::Text(text))) => {
                        if text.len() > MAX_TEXT {
                            return; // ValueError: bounded JSON text
                        }
                        match serde_json::from_str::<Value>(&text) {
                            Ok(v) => {
                                if self.receive(&v).is_err() {
                                    return;
                                }
                            }
                            Err(_) => return,
                        }
                    }
                    Some(Ok(Message::Binary(_))) => return, // ValueError: bounded JSON text
                    Some(Ok(Message::Close(_))) | None => return,
                    Some(Ok(_)) => {} // ping/pong handled by the transport
                    Some(Err(_)) => return,
                }
            }
        };
        tokio::select! {
            _ = send => {}
            _ = recv => {}
        }
        let _ = tx.send(close(1008, "")).await;
        clear_bridge();
    }

    // -- HTTP -------------------------------------------------------------------

    pub fn status_object(&self) -> Value {
        match self.control() {
            None => self.nav_object(None, None, None),
            Some(control) => control.status(false)["nav"].clone(),
        }
    }

    pub fn set_goal(
        &self,
        x: f64,
        z: f64,
        session_id: Option<&str>,
        permit: Option<&str>,
    ) -> Result<Value, GoalError> {
        if !(x.is_finite() && z.is_finite()) || x.abs().max(z.abs()) > 500.0 {
            return Err(GoalError::Unprocessable(
                "Goal must be finite voxel-map metres".into(),
            ));
        }
        let Some(control) = self.control() else {
            return Err(GoalError::Refused {
                nav: self.nav_object(None, None, None),
                message: "No rover control on this server".into(),
            });
        };
        if !self.available() {
            return Err(GoalError::Refused {
                nav: self.status_object(),
                message: "Navigation backend unavailable (Nav2 bridge not running)".into(),
            });
        }
        if let Some(fault) = self.sensor_fault() {
            return Err(GoalError::Refused {
                nav: self.status_object(),
                message: sensor_message(fault).into(),
            });
        }
        let mut s = control.inner.lock().unwrap();
        let now = control.now();
        control.output_locked(&mut s); // apply expiry before judging the lease
        if s.session_id.is_none() || s.autonomy.is_none() {
            return Err(GoalError::Refused {
                nav: control.status_locked(&mut s, false)["nav"].clone(),
                message: "Go-to-point needs a connected operator controller".into(),
            });
        }
        if let Some(sid) = session_id {
            if Some(sid.to_string()) != s.session_id {
                return Err(GoalError::Refused {
                    nav: control.status_locked(&mut s, false)["nav"].clone(),
                    message: "Goal is for another controller session".into(),
                });
            }
        }
        if s.scan_token.is_some() {
            return Err(GoalError::Refused {
                nav: control.status_locked(&mut s, false)["nav"].clone(),
                message: "A scan is running".into(),
            });
        }
        // The operator is pressing right now.
        let held = s.deadline > now;
        if held {
            let mut live = s.connected && !s.requires_release && control.camera_is_ready();
            if permit.is_some() || session_id.is_some() {
                let permit_expires = permit
                    .and_then(|t| s.permits.iter().find(|(k, _)| k == t).map(|(_, e)| *e))
                    .unwrap_or(0.0);
                live = live && permit_expires > now;
            }
            if !live {
                return Err(GoalError::Refused {
                    nav: control.status_locked(&mut s, false)["nav"].clone(),
                    message: "Go-to-point needs a live operator permit: hold the deadman".into(),
                });
            }
        }
        // Arm only: the operator's own held "goto" heartbeats start motion. A POST
        // never changes the drive mode, so no other client can make the rover move.
        // Without a held lease the goal is staged and waits for a new deadman press
        // that passes every heartbeat gate (#105).
        {
            let mut n = self.inner.lock().unwrap();
            n.goal_id += 1;
            n.goal = Some((x, z));
            n.cmd = None;
            n.remote.insert("state".into(), json!("planning"));
            n.remote.insert("path".into(), json!([]));
        }
        s.stick_override = false;
        let staged_nav = if held {
            None
        } else {
            Some(control.status_locked(&mut s, false)["nav"].clone())
        };
        drop(s);
        Ok(match staged_nav {
            Some(nav) => nav,
            None => self.nav_object(Some("goto"), Some("goto"), Some(true)),
        })
    }

    pub fn cancel(&self) -> Value {
        if let Some(control) = self.control() {
            let mut s = control.inner.lock().unwrap();
            if let Some(a) = s.autonomy.as_mut() {
                if NAV_MODES.contains(&a.mode.as_str()) {
                    a.reset();
                    s.stick_override = false;
                }
            }
            self.disarm();
            control.output_locked(&mut s);
            return control.status_locked(&mut s, false)["nav"].clone();
        }
        self.disarm();
        self.nav_object(None, None, None)
    }
}

fn r3(v: f64) -> f64 {
    (v * 1000.0).round_ties_even() / 1000.0
}

/// Per-subscriber filter that keeps bulky nav arrays out of most 40 Hz
/// control statuses (jetson/nav.py NavLane, #110). A client that opted in
/// via `"nav_lane": "revision"` gets `nav.path` and `nav.frontiers` only
/// when `nav.revision` changed (at most every MIN_INTERVAL, coalescing
/// changes) or as a REFRESH; every other status omits both keys, which
/// means "unchanged" — clients keep the last arrays (an empty array
/// clears). The first status always carries them. Lease and status
/// fields are untouched.
pub struct NavLane {
    sent_revision: Option<i64>,
    sent_at: f64,
}

impl NavLane {
    const MIN_INTERVAL: f64 = 0.5;
    const REFRESH: f64 = 2.0;

    pub fn new() -> Self {
        Self { sent_revision: None, sent_at: f64::NEG_INFINITY }
    }

    /// Returns the status to send — full when the arrays are due, a lean
    /// copy with `nav.path`/`nav.frontiers` removed otherwise.
    pub fn apply(&mut self, status: &Value, now: f64) -> Value {
        let Some(nav) = status.get("nav").filter(|n| n.is_object()) else {
            return status.clone();
        };
        let revision = nav.get("revision").and_then(Value::as_i64);
        if (revision != self.sent_revision && now - self.sent_at >= Self::MIN_INTERVAL)
            || now - self.sent_at >= Self::REFRESH
        {
            self.sent_revision = revision;
            self.sent_at = now;
            return status.clone();
        }
        let mut lean = status.clone();
        if let Some(obj) = lean.get_mut("nav").and_then(Value::as_object_mut) {
            obj.remove("path");
            obj.remove("frontiers");
        }
        lean
    }
}
