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
/// Nav2 motion needs a normal-tracking ARKit pose at least this fresh (#175:
/// 0.5 tripped on the rig's 500-750ms camera stalls every ~5s and cut AUTO
/// into 0.75s runs) ...
pub const POSE_TTL: f64 = 1.0;
/// ... and measured depth of the same sensor session at least this fresh.
pub const DEPTH_TTL: f64 = 1.0;
/// Target unseen this long: Seek fails and stops.
pub const SEEK_LOST_S: f64 = 3.0;
/// Re-send the standoff goal when the person moved this far.
pub const SEEK_MOVE_M: f64 = 0.3;
/// The bridge must run on this computer; anything else is refused.
pub const LOOPBACK: [&str; 4] = ["127.0.0.1", "::1", "localhost", "testclient"];

/// Trusted RGB-D input key stamped by MapState:
/// (session_id, map_epoch, socket_generation, frame_id, t_capture).
pub type SensorKey = (String, u64, u64, i64, f64);

/// Validate the trusted RGB-D input key, including JSON tuple/list copies
/// (nav.py _capture_key): 5 items, non-empty str id, three non-negative
/// ints < 2^53, and a finite non-negative capture timestamp.
pub fn capture_key(value: &Value) -> Option<SensorKey> {
    let arr = value.as_array()?;
    if arr.len() != 5 {
        return None;
    }
    let session_id = arr[0].as_str().filter(|s| !s.is_empty())?.to_string();
    let mut ints = [0u64; 3];
    for (i, item) in arr[1..4].iter().enumerate() {
        // type(item) is int — JSON booleans/floats never qualify.
        if !(item.is_i64() || item.is_u64()) {
            return None;
        }
        ints[i] = item.as_u64().filter(|v| *v < (1u64 << 53))?;
    }
    let t_capture = arr[4]
        .as_f64()
        .filter(|t| t.is_finite() && *t >= 0.0)?;
    Some((session_id, ints[0], ints[1], ints[2] as i64, t_capture))
}

/// A retained upright depth capture paired with a Seek detection input.
type SeekCapture = (Arc<RgbdFrame>, f64, &'static str);

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
    /// (v, w normalized, received monotonic, linear m/s, angular rad/s CCW,
    /// generation, goal_id) — authority-bound so stale commands die.
    pub cmd: Option<(f64, f64, f64, f64, f64, i64, Value)>,
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
    /// Socket lifetime the generation tracks alongside the identity; a
    /// visual map reset preserves it, a reconnect invalidates all work.
    pub socket_generation: u64,
    pub generation: i64,
    pub seek: Option<Seek>,
    pub seek_seq: i64,
    pub seek_checked: f64,
    /// At most 12 exact captures for delayed inference (OrderedDict parity:
    /// insertion order with move-to-end on re-insert).
    pub seek_frames: std::collections::VecDeque<(SensorKey, SeekCapture)>,
    /// Detached last successfully projected detector input (dict, received).
    pub seek_input: Option<(Value, f64)>,
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
    /// Exact camera capture evidence for pending Seek depth pairing
    /// (nav.py camera_snapshot ctor arg; None when unwired).
    pub camera_snapshot: Option<Arc<dyn Fn() -> Option<crate::camera::CameraFrame> + Send + Sync>>,
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
        camera_snapshot: Option<Arc<dyn Fn() -> Option<crate::camera::CameraFrame> + Send + Sync>>,
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
        let (identity, socket_generation) = {
            let m = mapping.inner.lock().unwrap();
            (m.identity.clone(), m.socket_generation)
        };
        Ok(Self {
            mapping,
            clock: clock.unwrap_or_else(|| Arc::new(now_monotonic)),
            camera_snapshot,
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
                socket_generation,
                generation: 0,
                seek: None,
                seek_seq: 0,
                seek_checked: 0.0,
                seek_frames: std::collections::VecDeque::new(),
                seek_input: None,
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

    /// Detect a new sensor session; returns the current generation. Sensor
    /// generation follows both the sensor identity and the phone socket
    /// lifetime: old goals, commands and depth never carry across a
    /// reconnect, including when the phone reuses its session and epoch. A
    /// visual map reset preserves this authority.
    pub fn sync_generation(&self) -> i64 {
        let mut s = self.inner.lock().unwrap();
        self.sync_generation_locked(&mut s)
    }

    /// sync_generation for callers already holding `inner` (Python RLock
    /// parity — its callers often sit inside `with self.lock`).
    fn sync_generation_locked(&self, s: &mut NavInner) -> i64 {
        let (identity, socket_generation) = {
            let m = self.mapping.inner.lock().unwrap();
            (m.identity.clone(), m.socket_generation)
        };
        if identity != s.identity || socket_generation != s.socket_generation {
            s.identity = identity;
            s.socket_generation = socket_generation;
            s.generation += 1;
            s.goal = None;
            s.cmd = None;
            s.frame = None;
            s.frame_at = None;
            s.seek_frames.clear();
            s.seek_input = None;
            s.seek = None;
            s.seek_checked = 0.0;
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
        let mut s = self.inner.lock().unwrap();
        self.sync_generation_locked(&mut s);
        let socket_generation = self.mapping.inner.lock().unwrap().socket_generation;
        let identity = (
            header
                .get("session_id")
                .and_then(Value::as_str)
                .map(str::to_string),
            header.get("map_epoch").and_then(Value::as_u64),
        );
        if s.identity.as_ref().map(|(sid, epoch)| {
                identity.0.as_deref() != Some(sid.as_str()) || identity.1 != Some(*epoch)
            }).unwrap_or(true)
            || header
                .get("_mapping_socket_generation")
                .and_then(Value::as_u64)
                .unwrap_or(0)
                != socket_generation
        {
            return; // a finishing old-session observer must not withdraw newer depth
        }
        let good = header.get("tracking").and_then(Value::as_str) == Some("normal")
            && self.mapping.inner.lock().unwrap().tracking == "normal";
        s.frame = if good { Some(frame.clone()) } else { None };
        if good {
            let base = header
                .get("_received_monotonic")
                .and_then(Value::as_f64)
                .unwrap_or_else(now_monotonic);
            match crate::camera::adjusted_receipt(header, base) {
                Ok(receipt) => s.frame_at = Some(receipt),
                // An observer failure is swallowed upstream (state.py parity);
                // leave the previous receipt time untouched like Python.
                Err(_) => return,
            }
            if let Some(key) = header.get("_mapping_sensor_key").and_then(capture_key) {
                let matches = s.identity.as_ref().map(|(sid, epoch)| {
                        key.0 == *sid && key.1 == *epoch
                    }).unwrap_or(false)
                    && key.2 == s.socket_generation
                    && key.3 == header.get("frame_id").and_then(Value::as_i64).unwrap_or(i64::MIN)
                    && header.get("t_capture").and_then(Value::as_f64) == Some(key.4);
                if matches {
                    let source = if self.mapping.demo { "SIMULATED" } else { "IPHONE LIDAR" };
                    let received = s.frame_at.unwrap();
                    s.seek_frames.retain(|(k, _)| *k != key);
                    s.seek_frames
                        .push_back((key, (Arc::new(frame.clone()), received, source)));
                    while s.seek_frames.len() > 12 {
                        s.seek_frames.pop_front();
                    }
                }
            }
        } else {
            s.frame_at = None;
            s.seek_frames.clear();
        }
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
            s.seek_input = None;
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
                || s.seek.as_ref().unwrap().state != "driving"
                || s.seek.as_ref().unwrap().goal.is_none())
        {
            return Some((0.0, 0.0)); // no located person yet, at standoff, or target lost
        }
        let cmd = s.cmd.clone()?;
        if (self.clock)() - cmd.2 > CMD_TTL || cmd.5 != s.generation {
            return None;
        }
        let goal_id = if s.mode.as_deref() == Some("seek") {
            s.seek
                .as_ref()
                .and_then(|sk| sk.goal_id.clone())
                .map(Value::String)
                .unwrap_or(Value::Null)
        } else {
            json!(s.goal_id)
        };
        if matches!(s.mode.as_deref(), Some("goto") | Some("seek"))
            && !Self::goal_value_eq(&cmd.6, &goal_id)
        {
            return None;
        }
        Some(if si { (cmd.3, cmd.4) } else { (cmd.0, cmd.1) })
    }

    /// Python `!=` semantics for a bridge-supplied goal id against the
    /// expected value: JSON numbers compare numerically (5.0 == 5), every
    /// other value compares by equality (missing/null never equals a number).
    fn goal_value_eq(a: &Value, b: &Value) -> bool {
        match (a, b) {
            (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
            (x, y) => x == y,
        }
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
        // #115: backend is "nav2" while Nav2 drives and null when nothing can;
        // the reactive policy no longer reports a "fallback" backend.
        let mut backend: Option<&'static str> = if available && (mode.is_none() || backed) {
            Some("nav2")
        } else {
            None
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
        } else {
            // #115: with --nav2, Nav2 is the only navigation — Explore and
            // Seek hold still rather than run the reactive policy blind.
            state = "unavailable";
            backend = None;
            message = if s.bridge.is_none() {
                "Nav2 bridge not connected; holding still".into()
            } else if !available {
                "Nav2 bridge silent; holding still".into()
            } else {
                format!("Nav2 does not run {}; holding still", mode.as_deref().unwrap_or("this mode"))
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
            "command_generation_required": true,
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

    /// The exact sensor key a Seek detection belongs to — when it is
    /// sufficiently complete to accept as a Seek input (nav.py _seek_key).
    /// Missing fields are None, never errors: this function runs in the
    /// control hot path.
    fn seek_key(&self, s: &NavInner, detections: &Value) -> Option<SensorKey> {
        if !detections.is_object()
            || detections.get("fresh") != Some(&Value::Bool(true))
            || !detections.get("error").map(|e| e.is_null()).unwrap_or(false)
            || !detections.get("mirrored").map(|m| m.is_boolean()).unwrap_or(false)
        {
            return None;
        }
        {
            let m = self.mapping.inner.lock().unwrap();
            if !m.connected || m.tracking != "normal" {
                return None;
            }
        }
        let key = capture_key(detections.get("sensor_key")?)?;
        let identity = s.identity.as_ref()?;
        if key.0 != identity.0 || key.1 != identity.1 || key.2 != s.socket_generation {
            return None;
        }
        let frame = detections.get("frame");
        if !frame.map(|f| f.is_i64() || f.is_u64()).unwrap_or(false)
            || frame.and_then(Value::as_i64) != Some(key.3)
        {
            return None;
        }
        let expected = if self.mapping.demo { "SIMULATED" } else { "IPHONE LIDAR" };
        if detections.get("source").and_then(Value::as_str) != Some(expected) {
            return None;
        }
        Some(key)
    }

    /// The retained upright depth capture paired with this detection input
    /// (nav.py _seek_capture).
    fn seek_capture(
        &self,
        s: &NavInner,
        detections: &Value,
        now: f64,
    ) -> Option<(Arc<RgbdFrame>, f64, &'static str, bool)> {
        let key = self.seek_key(s, detections)?;
        let (frame, received, source) = s
            .seek_frames
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, e)| e)?;
        if detections.get("source").and_then(Value::as_str) != Some(*source)
            || !(now - *received >= 0.0 && now - *received < DEPTH_TTL)
        {
            return None;
        }
        Some((
            frame.clone(),
            *received,
            *source,
            detections.get("mirrored").and_then(Value::as_bool).unwrap_or(false),
        ))
    }

    /// The new Seek input has the exact camera capture while its retained
    /// depth stays pending (nav.py _pending_seek_capture): a valid newer
    /// detection that arrives before depth acceptance does not force a hold.
    fn pending_seek_capture(&self, s: &mut NavInner, detections: &Value, now: f64) -> bool {
        let (Some(key), Some(snapshot)) = (self.seek_key(s, detections), self.camera_snapshot.clone()) else {
            return false;
        };
        let (Some((prior, received)), Some(seek)) = (s.seek_input.clone(), s.seek.clone()) else {
            return false;
        };
        let Some(prior_key) = prior.get("sensor_key").and_then(capture_key) else {
            return false;
        };
        if seek.state != "driving"
            || seek.goal.is_none()
            || prior
                .get("box")
                .and_then(|b| b.get("id"))
                .and_then(Value::as_i64)
            != s.target_id
            || !(now - received >= 0.0 && now - received < DEPTH_TTL)
            || self.seek_capture(s, &prior, now).is_none()
            || s.seek_frames.iter().any(|(k, _)| *k == key)
            || key.3 <= prior_key.3
            || key.4 <= prior_key.4
        {
            return false;
        }
        let generation = s.generation;
        // The camera snapshot's own contract cannot fail update_seek
        // (nav.py try/except parity).
        let camera = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| (snapshot)())) {
            Ok(c) => c,
            Err(_) => return false,
        };
        let proven = camera
            .as_ref()
            .map(|c| {
                c.sensor_key.as_ref().and_then(capture_key) == Some(key.clone())
                    && c.frame_id == key.3
                    && Some(c.source.as_str()) == detections.get("source").and_then(Value::as_str)
                    && Some(c.mirrored) == detections.get("mirrored").and_then(Value::as_bool)
                    && c.tracking.as_deref() == Some("normal")
                    && now - c.received >= 0.0
                    && now - c.received < 0.5
            })
            .unwrap_or(false);
        let checked = now_monotonic();
        proven
            && generation == self.sync_generation_locked(s)
            && checked - received >= 0.0
            && checked - received < DEPTH_TTL
            && camera
                .as_ref()
                .map(|c| checked - c.received >= 0.0 && checked - c.received < 0.5)
                .unwrap_or(false)
            && self.seek_capture(s, &prior, checked).is_some()
    }

    /// Withdraw the Seek goal while keeping the drive authority: the next
    /// paired capture replans instead of resetting (nav.py _hold_seek).
    fn hold_seek(&self, s: &mut NavInner, now: f64) {
        let seen_at = s.seek.as_ref().map(|sk| sk.seen_at).unwrap_or(now);
        s.seek = Some(Seek {
            target_id: s.target_id,
            person: None,
            seen_at,
            goal: None,
            goal_id: None,
            state: if now - seen_at >= SEEK_LOST_S { "failed" } else { "planning" }.into(),
            distance: None,
        });
        s.seek_checked = 0.0;
        s.seek_input = None;
        s.cmd = None;
    }

    /// ControlState calls this each tick in a Nav2-backed Seek with its
    /// detection snapshot. Seek targets pair with the retained upright
    /// depth capture the detector actually saw — never the latest frame.
    pub fn update_seek(&self, detections: &Value) {
        let now = now_monotonic();
        let mut s = self.inner.lock().unwrap();
        self.sync_generation_locked(&mut s);
        if s.mode.as_deref() != Some("seek") || !s.active {
            return;
        }
        let generation = s.generation;
        let paired = self.seek_capture(&s, detections, now);
        let box_value = detections
            .get("boxes")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|b| {
                        b.is_object()
                            && b.get("id").map(|i| i.is_i64() || i.is_u64()).unwrap_or(false)
                            && b.get("id").and_then(Value::as_i64) == s.target_id
                            && b.get("cls").and_then(Value::as_str) == Some("person")
                            && ["x", "y", "w", "h"].iter().all(|k| {
                                b.get(*k)
                                    .and_then(Value::as_f64)
                                    .map(|v| v.is_finite())
                                    .unwrap_or(false)
                            })
                            && {
                                let (x, y, w, h) = (
                                    b["x"].as_f64().unwrap(),
                                    b["y"].as_f64().unwrap(),
                                    b["w"].as_f64().unwrap(),
                                    b["h"].as_f64().unwrap(),
                                );
                                0.0 <= x
                                    && x <= 1.0
                                    && 0.0 < w
                                    && w <= 1.0
                                    && x + w <= 1.0 + 1e-6
                                    && 0.0 <= y
                                    && y <= 1.0
                                    && 0.0 < h
                                    && h <= 1.0
                                    && y + h <= 1.0 + 1e-6
                            }
                    })
                    .cloned()
            });
        if paired.is_none() || box_value.is_none() {
            // Preserve the Seek goal while a newer detection's paired depth
            // is pending; its camera proof keeps the command alive.
            if box_value.is_some() && self.pending_seek_capture(&mut s, detections, now) {
                return;
            }
            if generation != self.sync_generation_locked(&mut s) {
                return;
            }
            self.hold_seek(&mut s, now);
            return;
        }
        if now - s.seek_checked < 0.2 {
            return;
        }
        s.seek_checked = now;
        let (frame, paired_received, _source, mirrored) = paired.unwrap();
        let box_value = box_value.unwrap();
        let target_id = s.target_id;
        let generation = s.generation;
        let mut seek = s.seek.clone().unwrap_or_else(|| Seek {
            target_id,
            person: None,
            seen_at: now,
            goal: None,
            goal_id: None,
            state: "planning".into(),
            distance: None,
        });
        drop(s);
        // Locate and pose math run without the nav lock (Python's lock is
        // held, but these readers take their own locks; parity is in the
        // post-projection authority re-check below, not the timing).
        let view;
        let view = if mirrored {
            view = crate::orientation::upright_view(&frame);
            &view
        } else {
            frame.as_ref()
        };
        let person = self.locate(&box_value, view);
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
                    seek.goal = Some(goal);
                    seek.state = "new_goal".into(); // marker; rewritten below
                } else {
                    seek.goal = Some(goal);
                    seek.state = "driving".into();
                }
            }
        } else if now - seek.seen_at >= SEEK_LOST_S {
            seek.state = "failed".into();
        }
        let mut s = self.inner.lock().unwrap();
        if generation == self.sync_generation_locked(&mut s)
            && s.mode.as_deref() == Some("seek")
            && s.active
            && s.target_id == seek.target_id
        {
            // The capture used for projection must still be the one the
            // retained-depth store holds for this detection (nav.py: current[0] is frame).
            let current = self.seek_capture(&s, detections, now_monotonic());
            if person.is_none()
                || transform.is_none()
                || current.is_none()
                || !Arc::ptr_eq(&current.as_ref().unwrap().0, &frame)
            {
                self.hold_seek(&mut s, now_monotonic());
                return;
            }
            if seek.state == "new_goal" {
                s.seek_seq += 1;
                seek.goal_id = Some(format!("seek-{}", s.seek_seq));
                seek.state = "driving".into();
                s.cmd = None; // only the goal-owner's commands drive
            }
            s.seek = Some(seek);
            let now2 = now_monotonic();
            s.seek_input = detections
                .get("age_ms")
                .and_then(Value::as_f64)
                .filter(|a| a.is_finite() && *a >= 0.0)
                .map(|a| {
                    (
                        json!({
                            "sensor_key": detections.get("sensor_key").cloned().unwrap_or(Value::Null),
                            "frame": detections.get("frame").cloned().unwrap_or(Value::Null),
                            "source": detections.get("source").cloned().unwrap_or(Value::Null),
                            "mirrored": mirrored,
                            "fresh": true,
                            "error": Value::Null,
                            "box": box_value,
                        }),
                        paired_received.min(now2 - a / 1000.0),
                    )
                });
        }
    }

    /// Apply one bridge message (cmd_vel or status).
    pub fn receive(&self, message: &Value) -> Result<(), String> {
        if !message.is_object() {
            return Err("Bridge message must be an object".into());
        }
        let kind = message.get("type").and_then(Value::as_str);
        self.sync_generation();
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
                let generation = message.get("generation");
                if !generation.map(|g| g.is_i64() || g.is_u64()).unwrap_or(false)
                    || generation.and_then(Value::as_i64) != Some(s.generation)
                {
                    return Ok(()); // computed for an earlier sensor authority
                }
                let goal_id = if s.mode.as_deref() == Some("seek") && s.seek.is_some() {
                    s.seek
                        .as_ref()
                        .and_then(|sk| sk.goal_id.clone())
                        .map(Value::String)
                        .unwrap_or(Value::Null)
                } else {
                    json!(s.goal_id)
                };
                if matches!(s.mode.as_deref(), Some("goto") | Some("seek"))
                    && !Self::goal_value_eq(
                        message.get("goal_id").unwrap_or(&Value::Null),
                        &goal_id,
                    )
                {
                    s.bridge_seen = now;
                    return Ok(()); // computed for an earlier goal
                }
                // ROS angular.z is counter-clockwise (left); ControlState positive w turns right.
                let v = (linear / self.speed_mps).clamp(-self.max_v, self.max_v);
                let w = (-angular / self.turn_radps).clamp(-self.max_w, self.max_w);
                s.cmd = Some((
                    v,
                    w,
                    now,
                    linear,
                    angular,
                    s.generation,
                    message.get("goal_id").cloned().unwrap_or(Value::Null),
                ));
            }
            Some("status") => {
                let generation = message.get("generation");
                if !generation.map(|g| g.is_i64() || g.is_u64()).unwrap_or(false)
                    || generation.and_then(Value::as_i64) != Some(s.generation)
                {
                    return Ok(()); // computed for an earlier sensor authority
                }
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
                    // The encode above may outlive the session that produced
                    // the sample; never publish it under a newer generation.
                    if generation != self.sync_generation() {
                        continue;
                    }
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
                let (frame, seq, frame_generation, received) = {
                    let s = self.inner.lock().unwrap();
                    (s.frame.clone(), s.frame_seq, s.generation, s.frame_at)
                };
                if frame_generation != sent_generation {
                    continue;
                }
                if let Some(frame) = frame.as_ref() {
                    if seq != sent_frame {
                        let source = if self.mapping.demo {
                            "simulated"
                        } else {
                            "arkit"
                        };
                        let frame = frame.clone();
                        let floor = -self.camera_height;
                        let source = source.to_string();
                        // Encoding one grid off the async lane is bounded CPU
                        // work; the generation is re-verified after it.
                        let packet = tokio::task::spawn_blocking(move || {
                            encode_depth(&frame, floor, &source, 0.0, frame_generation, 160, 6.0)
                        })
                        .await;
                        if frame_generation != self.sync_generation() {
                            continue;
                        }
                        // The header stamped before encode_depth ran is
                        // stale by now; rebuild with the true age. A sender
                        // that took too long to encode drops the sample
                        // rather than claiming fresh depth (nav.py parity).
                        let age = (now_monotonic() - received.unwrap_or_else(now_monotonic)).max(0.0);
                        if age < DEPTH_TTL {
                            let packet = match packet {
                                Ok(p) => p,
                                Err(_) => continue,
                            };
                            let header_len =
                                u32::from_le_bytes(packet[0..4].try_into().unwrap()) as usize;
                            let mut header: Value = serde_json::from_slice(&packet[4..4 + header_len])
                                .unwrap_or_else(|_| json!({}));
                            header["age_ms"] = json!((age * 1000.0 * 10.0).round_ties_even() / 10.0);
                            let data = serde_json::to_vec(&header).unwrap_or_default();
                            let mut out = Vec::with_capacity(4 + data.len() + packet.len());
                            out.extend_from_slice(&(data.len() as u32).to_le_bytes());
                            out.extend_from_slice(&data);
                            out.extend_from_slice(&packet[4 + header_len..]);
                            if tx.send(Message::Binary(out.into())).await.is_err() {
                                return;
                            }
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
                if intent["generation"] != sent_generation {
                    continue;
                }
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
            None => self.nav_object(None, None),
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
                nav: self.nav_object(None, None),
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
            None => self.nav_object(Some("goto"), Some(true)),
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
        self.nav_object(None, None)
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
