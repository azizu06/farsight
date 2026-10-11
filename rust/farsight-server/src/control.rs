//! Control policy, serial pump, and service glue.
//! Ports of jetson/control/{state,serial_link,service}.py — same permits,
//! lease, e-stop, autonomy modes, and Uno wire protocol.

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use base64::Engine;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::autonomy::Autonomy;
use crate::state::now_monotonic;

pub const LEASE: f64 = 0.250;
/// A session silent this long is dead (#139): a new socket replaces it
/// instead of being refused for the seconds a dead peer took to reap.
pub const STALE_SESSION: f64 = 1.0;
pub const RANGE_TTL: f64 = 0.350;
pub const PAN_MOUNTS: [&str; 2] = ["servo", "none"];

pub type CameraReady = Arc<dyn Fn() -> bool + Send + Sync>;
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;
pub type Detections = Arc<dyn Fn() -> Value + Send + Sync>;

pub struct ControlInner {
    pub session_id: Option<String>,
    /// Last message from the current controller session (#139): a live
    /// controller sends at least every 50 ms, so a session silent for
    /// STALE_SESSION is a dead socket the server has not noticed yet.
    pub last_message_at: f64,
    pub permits: VecDeque<(String, f64)>,
    pub seq: i64,
    pub connected: bool,
    pub range_cm: Option<f64>,
    pub range_at: f64,
    pub pan: i64,
    pub v: f64,
    pub w: f64,
    pub deadline: f64,
    pub requires_release: bool,
    pub reason: String,
    pub link_ms: Option<f64>,
    pub stick_override: bool,
    pub autonomy: Option<Autonomy>,
    pub scan_token: Option<u64>,
    /// First stop reason that cancelled a running scan; retained so the
    /// scan lane can report the initiating cause even after a later
    /// viewer release resets the controller (state._scan_stop_reason).
    pub scan_stop_reason: Option<String>,
    /// Permits already issued when the scan began: a zero-stick teleop
    /// heartbeat carrying one was sent before POST /scan and may race it (#72).
    pub scan_race_permits: std::collections::HashSet<String>,
    pub scan_serial: u64,
    pub scan_w: f64,
    /// Opt-in drive command: raw-PWM calibration or SI-unit velocity
    /// heartbeat (jetson/control/state.py `self.drive`).
    pub drive: Option<DriveCmd>,
    /// Demo autonomy: when a short sensor stall began holding the motors
    /// (state.sensor_pause_since). Clears only on fresh sensors.
    pub sensor_pause_since: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
pub enum DriveCmd {
    Calibrate(i64, i64),
    Velocity(f64, f64),
}

impl DriveCmd {
    pub fn kind(&self) -> &'static str {
        match self {
            DriveCmd::Calibrate(..) => "calibrate",
            DriveCmd::Velocity(..) => "velocity",
        }
    }
}

pub type OnOutput = Box<dyn Fn(f64, f64, bool) + Send + Sync>;

pub struct ControlState {
    pub simulated: bool,
    pub max_pwm: i64,
    pub min_pwm: i64,
    /// Ceiling for motion the operator does not steer directly (explore,
    /// seek, goto, Nav2, scan sweeps); defaults to max_pwm (unchanged
    /// behaviour). Port of #99 autonomy_max_pwm.
    pub autonomy_max_pwm: i64,
    /// Opt-in measured drive profile; enables `mode: "velocity"`.
    pub drive_profile: Option<crate::drive_profile::DriveProfile>,
    /// Opt-in raw-PWM `mode: "calibrate"` for tools.calibrate_drive.
    pub allow_calibration: bool,
    /// Ultrasonic stop (range < 25 cm or stale) blocks forward; `off` is an
    /// explicit opt-in for navigation runs that steer around obstacles.
    pub proximity_stop: bool,
    /// "servo": a physical pan head follows the commanded pan, and forward
    /// drive is blocked while it is off-centre. "none": no pan hardware; the
    /// headset pans digitally, so a valid commanded pan is accepted but never
    /// moves anything or blocks forward drive (pan stays 0, the Uno gets no P).
    pub pan_mount: String,
    pub camera_ready: Option<CameraReady>,
    pub detections: Option<Detections>,
    pub clock: Clock,
    pub inner: Mutex<ControlInner>,
    /// Telemetry sink invoked with (v, w, blocked) after motor clamping;
    /// must never affect motor output. Port of `state.on_output`.
    pub on_output: Mutex<Option<OnOutput>>,
    /// Opt-in Nav2 seam (jetson/control/state.py `self.navigator`); the
    /// server stays motor authority — the nav backend supplies commands.
    pub navigator: Option<Arc<crate::nav::NavService>>,
    /// #134 --demo-mode: transient stops still stop the motors but do not
    /// latch requires_release, and teleop is not gated on camera freshness.
    pub demo_mode: bool,
}

/// Stops that demo mode does not latch: the next fresh deadman=true
/// heartbeat resumes. Lease expiry still stops (link loss) and the Nav2
/// sensor gate still holds motion at zero; they just need no new press.
const DEMO_TRANSIENT: &[&str] = &[
    "serial_disconnected", "release_required", "controller_disconnected",
    "camera_stale", "video_stale", "lease_expired", "expired_permit",
    "stale_sequence", "nav_pose_lost", "nav_tracking_limited",
    "nav_depth_lost", "nav_sensor_reset",
];

/// Demo autonomy (#175): a pose, depth or camera stall shorter than
/// SENSOR_PAUSE_S holds the motors at zero and resumes the same
/// Explore/Seek/Goto on its own — AUTO never stops on outside things.
/// A longer stall still stops as before.
const SENSOR_PAUSE: &[&str] = &[
    "camera_stale", "nav_pose_lost", "nav_tracking_limited", "nav_depth_lost",
];
const SENSOR_PAUSE_S: f64 = 2.0;

fn round1(v: f64) -> f64 {
    (v * 10.0).round_ties_even() / 10.0
}

fn token() -> String {
    let mut bytes = [0u8; 18];
    rand::fill(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

impl ControlState {
    pub fn new(
        simulated: bool,
        min_pwm: i64,
        max_pwm: i64,
        camera_ready: Option<CameraReady>,
        detections: Option<Detections>,
        clock: Option<Clock>,
        drive_profile: Option<crate::drive_profile::DriveProfile>,
        allow_calibration: bool,
        proximity_stop: bool,
        pan_mount: &str,
        autonomy_max_pwm: Option<i64>,
        navigator: Option<Arc<crate::nav::NavService>>,
        demo_mode: bool,
    ) -> Result<Self, String> {
        if !(0 <= min_pwm && min_pwm < max_pwm && max_pwm <= 255) {
            return Err("PWM bounds must satisfy 0 <= min_pwm < max_pwm <= 255".into());
        }
        if !PAN_MOUNTS.contains(&pan_mount) {
            return Err(format!("pan_mount must be one of {PAN_MOUNTS:?}"));
        }
        let autonomy_max_pwm = autonomy_max_pwm.unwrap_or(max_pwm);
        if !(min_pwm < autonomy_max_pwm && autonomy_max_pwm <= max_pwm) {
            return Err("autonomy_max_pwm must satisfy min_pwm < autonomy_max_pwm <= max_pwm".into());
        }
        Ok(Self {
            simulated,
            max_pwm,
            min_pwm,
            drive_profile,
            allow_calibration,
            proximity_stop,
            pan_mount: pan_mount.to_string(),
            autonomy_max_pwm,
            camera_ready,
            detections: detections.clone(),
            clock: clock.unwrap_or_else(|| Arc::new(now_monotonic)),
            inner: Mutex::new(ControlInner {
                session_id: None,
                last_message_at: f64::NEG_INFINITY,
                permits: VecDeque::new(),
                seq: -1,
                connected: false,
                range_cm: None,
                range_at: f64::NEG_INFINITY,
                pan: 0,
                v: 0.0,
                w: 0.0,
                deadline: 0.0,
                requires_release: true,
                reason: "rover_disabled".into(),
                link_ms: None,
                stick_override: false,
                autonomy: if detections.is_some() || navigator.is_some() {
                    Some(Autonomy::with_proximity(proximity_stop))
                } else {
                    None
                },
                scan_token: None,
                scan_stop_reason: None,
                scan_race_permits: std::collections::HashSet::new(),
                scan_serial: 0,
                scan_w: 0.0,
                drive: None,
                sensor_pause_since: None,
            }),
            on_output: Mutex::new(None),
            navigator,
            demo_mode,
        })
    }

    pub(crate) fn now(&self) -> f64 {
        (self.clock)()
    }

    pub fn stop(&self, reason: &str, require_release: bool) {
        self.stop_locked(&mut self.inner.lock().unwrap(), reason, require_release);
    }

    fn stop_locked(&self, s: &mut ControlInner, reason: &str, require_release: bool) {
        s.v = 0.0;
        s.w = 0.0;
        s.deadline = 0.0;
        s.drive = None;
        if s.scan_token.is_some() {
            s.scan_stop_reason = Some(reason.into());
        }
        s.scan_token = None;
        s.scan_race_permits.clear();
        s.scan_w = 0.0;
        s.reason = reason.into();
        if let Some(a) = s.autonomy.as_mut() {
            a.reset();
        }
        s.stick_override = false;
        if require_release && !(self.demo_mode && DEMO_TRANSIENT.contains(&reason)) {
            s.requires_release = true;
        }
    }

    pub fn set_connected(&self, value: bool, reason: Option<&str>) {
        let mut s = self.inner.lock().unwrap();
        if value != s.connected {
            let reason = if value { "release_required" } else { reason.unwrap_or("serial_disconnected") };
            self.stop_locked(&mut s, reason, true);
            if let Some(nav) = &self.navigator {
                nav.disarm(); // a serial fault needs a fresh goal, not a silent resume
            }
            s.permits.clear();
            s.range_cm = None;
            s.range_at = f64::NEG_INFINITY;
        }
        s.connected = value;
        if let Some(r) = reason {
            s.reason = r.into();
        }
    }

    pub fn begin(&self) -> Option<String> {
        let mut s = self.inner.lock().unwrap();
        if s.session_id.is_some() {
            if self.now() - s.last_message_at < STALE_SESSION {
                return None;
            }
            // Dead socket: stop it and let the new controller take over;
            // the old handler sees the session change and closes (#139).
            // Inline end() — the inner lock is already held.
            self.stop_locked(&mut s, "controller_disconnected", true);
            s.session_id = None;
            s.permits.clear();
            if let Some(nav) = &self.navigator {
                nav.disarm();
            }
        }
        let id = Uuid::new_v4().to_string();
        s.session_id = Some(id.clone());
        s.last_message_at = self.now();
        s.seq = -1;
        s.permits.clear();
        self.stop_locked(&mut s, "release_required", true);
        if let Some(nav) = &self.navigator {
            nav.disarm();
        }
        Some(id)
    }

    pub fn end(&self, session_id: &str) {
        let mut s = self.inner.lock().unwrap();
        if s.session_id.as_deref() == Some(session_id) {
            self.stop_locked(&mut s, "controller_disconnected", true);
            s.session_id = None;
            s.permits.clear();
            if let Some(nav) = &self.navigator {
                nav.disarm(); // a go-to goal belongs to the controller that set it
            }
        }
    }

    /// Reserve stopped control; never issue/renew an operator lease here.
    pub fn begin_scan(&self, session_id: Option<&str>, permit: Option<&str>) -> Result<u64, String> {
        let mut s = self.inner.lock().unwrap();
        let (left, right, _) = self.output_locked(&mut s);
        let permit_expires = permit
            .and_then(|t| s.permits.iter().find(|(k, _)| k == t).map(|(_, e)| *e))
            .unwrap_or(0.0);
        let now = self.now();
        if s.scan_token.is_some()
            || s.session_id.is_none()
            || session_id != s.session_id.as_deref()
            || permit.is_none()
            || permit_expires <= now
            || !s.connected
            || s.requires_release
            || s.deadline <= now
            || !self.camera_is_ready()
            || left != 0
            || right != 0
            || s.v != 0.0
            || s.w != 0.0
            || s.autonomy.as_ref().map(|a| a.mode != "teleop").unwrap_or(false)
        {
            return Err("Scan requires a stopped rover and a live operator-held permit".into());
        }
        s.scan_serial += 1;
        s.scan_stop_reason = None;
        s.scan_token = Some(s.scan_serial);
        s.scan_race_permits = s.permits.iter().map(|(k, _)| k.clone()).collect();
        s.scan_w = 0.0;
        Ok(s.scan_serial)
    }

    pub fn scan_command(&self, token: Option<u64>, w: f64, pan: f64) -> Result<(), String> {
        let mut s = self.inner.lock().unwrap();
        self.output_locked(&mut s); // enforce expiry, camera and range gates
        if token.is_none() || token != s.scan_token {
            let reason = s.scan_stop_reason.as_deref().unwrap_or("unknown");
            return Err(format!(
                "Scan cancelled: operator lease, override, or safety stop ({reason})"
            ));
        }
        if !w.is_finite() || !(-0.35..=0.35).contains(&w) || !(-80.0..=80.0).contains(&pan) {
            return Err("Scan command outside bounded sweep".into());
        }
        s.scan_w = w;
        s.pan = if self.pan_mount == "servo" { pan.round_ties_even() as i64 } else { 0 };
        self.output_locked(&mut s);
        if token != s.scan_token {
            let reason = s.scan_stop_reason.as_deref().unwrap_or("unknown");
            return Err(format!("Scan cancelled: camera or range unavailable ({reason})"));
        }
        Ok(())
    }

    pub fn end_scan(&self, token: u64) {
        let mut s = self.inner.lock().unwrap();
        if s.scan_token == Some(token) {
            self.stop_locked(&mut s, "scan_finished", true);
        }
    }

    pub fn update_range(&self, cm: f64) {
        let mut s = self.inner.lock().unwrap();
        s.range_cm = if cm.is_finite() && cm > 0.0 && cm <= 500.0 { Some(cm) } else { None };
        s.range_at = self.now();
    }

    /// The ultrasonic proximity rule; never true when the option is off.
    /// Caller holds the lock. True while a demo-autonomy sensor stall may
    /// hold the motors instead of stopping (state._sensor_pause).
    fn sensor_pause(&self, s: &mut ControlInner, fault: &str) -> bool {
        if !SENSOR_PAUSE.contains(&fault) {
            return false;
        }
        let now = self.now();
        let since = s.sensor_pause_since.get_or_insert(now);
        if now - *since >= SENSOR_PAUSE_S {
            return false; // a long stall stops as before; the timer clears only on fresh sensors
        }
        s.reason = fault.to_string();
        true
    }

    fn range_unsafe(&self, s: &ControlInner) -> bool {
        self.proximity_stop
            && (s.range_cm.is_none()
                || self.now() - s.range_at >= RANGE_TTL
                || s.range_cm.unwrap() < 25.0)
    }

    fn forward_blocked(&self, s: &ControlInner) -> bool {
        // The pan rule protects a servo pan mount and stays on with one;
        // without one, pan is always 0 here.
        self.range_unsafe(s) || s.pan.abs() > 10
    }

    pub(crate) fn camera_is_ready(&self) -> bool {
        self.camera_ready.as_ref().map(|f| f()).unwrap_or(true)
    }

    fn json_int(v: &Value) -> Option<i64> {
        match v {
            Value::Number(n) => n.as_i64(),
            _ => None,
        }
    }

    fn json_num(v: &Value) -> Option<f64> {
        match v {
            Value::Number(n) => n.as_f64().filter(|x| x.is_finite()),
            _ => None,
        }
    }

    pub fn accept(&self, message: &Value, session_id: &str) -> bool {
        let mut s = self.inner.lock().unwrap();
        self.accept_locked(&mut s, message, session_id)
    }

    /// Accept while the caller already holds `inner`. Bonded dedup checks
    /// the seq history and applies the command under one lock hold, mirroring
    /// jetson/control/bonded.py's `with state.lock:` re-entry.
    pub(crate) fn accept_locked(&self, s: &mut ControlInner, message: &Value, session_id: &str) -> bool {
        if !message.is_object() || s.session_id.as_deref() != Some(session_id) {
            return false;
        }
        s.last_message_at = self.now();
        if message.get("session_id").and_then(Value::as_str) != Some(session_id) {
            self.stop_locked(s, "wrong_session", true);
            return false;
        }
        // JSON integer in 0..2^53-1; booleans and floats rejected.
        let seq = match message.get("seq").and_then(Self::json_int) {
            Some(seq) if (0..=(1i64 << 53) - 1).contains(&seq) => seq,
            _ => {
                self.stop_locked(s, "invalid_sequence", true);
                return false;
            }
        };
        // Any release stops, including stale releases, but only an ordered
        // release can unlock the next press.
        if message.get("deadman") == Some(&Value::Bool(false)) {
            self.stop_locked(s, "deadman_released", true);
            if seq > s.seq {
                s.seq = seq;
                s.requires_release = false;
            }
            return true;
        }
        if seq <= s.seq || message.get("deadman") != Some(&Value::Bool(true)) {
            self.stop_locked(s, "stale_sequence", true);
            return false;
        }
        let token = message.get("permit").and_then(Value::as_str);
        let expires = token
            .and_then(|t| s.permits.iter().find(|(k, _)| k == t).map(|(_, e)| *e))
            .unwrap_or(0.0);
        let now = self.now();
        if expires <= now {
            self.stop_locked(s, "expired_permit", true);
            return false;
        }
        if !s.connected || s.requires_release {
            let reason = if s.connected { "release_required" } else { "serial_disconnected" };
            self.stop_locked(s, reason, true);
            return false;
        }
        let mut mode = message.get("mode").and_then(Value::as_str).unwrap_or("teleop");
        // #134: in demo mode teleop drives by hand with no camera gate.
        let teleop_ungated = self.demo_mode && mode == "teleop";
        // Demo autonomy drives on the rover's own camera, pose and depth, which
        // output() checks every tick (a short stall pauses it there); it is
        // never gated on the viewer's picture. (#175)
        let autonomy_ungated =
            self.demo_mode && (mode == "explore" || mode == "seek" || mode == "goto");
        if !teleop_ungated && !autonomy_ungated && !self.camera_is_ready() {
            self.stop_locked(s, "camera_stale", true);
            return false;
        }
        if mode == "scan" {
            if s.scan_token.is_none() {
                self.stop_locked(s, "scan_not_active", true);
                return false;
            }
        } else if mode == "calibrate" && !self.allow_calibration {
            self.stop_locked(s, "unsupported_mode", true);
            return false;
        } else if mode == "velocity" && self.drive_profile.is_none() {
            self.stop_locked(s, "unsupported_mode", true);
            return false;
        } else if !(mode == "teleop" || mode == "calibrate" || mode == "velocity")
            && (s.autonomy.is_none()
                || !(mode == "explore" || mode == "seek" || mode == "goto")
                || (mode == "goto" && self.navigator.is_none()))
        {
            self.stop_locked(s, "unsupported_mode", true);
            return false;
        }
        let target_id = if mode == "seek" {
            match message.get("target_id").and_then(Self::json_int) {
                Some(t) if (0..=(1i64 << 53) - 1).contains(&t) => Some(t),
                _ => {
                    self.stop_locked(s, "invalid_target", true);
                    return false;
                }
            }
        } else {
            None
        };
        let video_age = message.get("video_age_ms").and_then(Self::json_num);
        // #175: demo-mode AUTO heartbeats skip video_fresh/video_age_ms — the
        // Quest's H.264 runs ~450ms so the 500ms stop cancelled goals every few
        // seconds. Strict mode (?demo=0, no --demo-mode) is unchanged.
        if !teleop_ungated
            && !autonomy_ungated
            && (message.get("video_fresh") != Some(&Value::Bool(true))
                || !video_age.map(|a| (0.0..=500.0).contains(&a)).unwrap_or(false))
        {
            self.stop_locked(s, "video_stale", true);
            return false;
        }
        if mode == "calibrate" || mode == "velocity" {
            return self.accept_drive(s, mode, message, seq, now, expires);
        }
        let v = message.get("v").and_then(Self::json_num);
        let w = message.get("w").and_then(Self::json_num);
        let pan = message.get("pan").map(Self::json_num).unwrap_or(Some(0.0));
        let (Some(v), Some(w), Some(pan)) = (v, w, pan) else {
            self.stop_locked(s, "invalid_command", true);
            return false;
        };
        if !(-1.0..=1.0).contains(&v) || !(-1.0..=1.0).contains(&w) || !(-80.0..=80.0).contains(&pan) {
            self.stop_locked(s, "invalid_command", true);
            return false;
        }
        let mut pan = pan;
        if s.scan_token.is_some()
            && mode == "teleop"
            && v == 0.0
            && w == 0.0
            && token.is_some_and(|t| s.scan_race_permits.contains(t))
        {
            // Pre-POST heartbeat racing POST /scan: renews like a scan
            // heartbeat, within its own permit (#72).
            mode = "scan";
        } else if mode == "scan" {
            // Client is in scan mode; a later teleop heartbeat overrides.
            s.scan_race_permits.clear();
        }
        if s.scan_token.is_some() {
            if mode != "scan" || v != 0.0 || w != 0.0 {
                self.stop_locked(s, "scan_override", false);
                mode = "teleop";
            } else {
                pan = s.pan as f64;
            }
        }
        if self.navigator.is_some() && (v != 0.0 || w != 0.0) {
            if let Some(nav) = &self.navigator {
                nav.disarm(); // stick input disarms go-to-point
            }
        }
        let stick_override = s.stick_override;
        if let Some(autonomy) = s.autonomy.as_mut() {
            if mode == "teleop" || mode == "scan" {
                autonomy.reset();
                s.stick_override = false;
            } else if v != 0.0 || w != 0.0 || stick_override {
                autonomy.reset();
                s.stick_override = true;
            } else {
                autonomy.select(mode, target_id, now);
                pan = 0.0; // autonomy needs a forward-facing ultrasonic/camera
            }
        }
        if self.pan_mount == "none" {
            pan = 0.0; // validated above; the headset pans digitally
        }
        let pan_i = pan.round_ties_even() as i64;
        if pan_i != s.pan {
            // A reading from a previous servo direction is not a forward
            // obstacle reading, even when the servo returns to the centre.
            s.range_cm = None;
            s.range_at = f64::NEG_INFINITY;
        }
        s.seq = seq;
        s.v = v;
        s.w = w;
        s.pan = pan_i;
        s.drive = None;
        s.deadline = (now + LEASE).min(expires);
        s.link_ms = Some(round1((LEASE - (expires - now)) * 1000.0));
        s.reason = "active".into();
        true
    }

    /// Raw-PWM calibration or SI-unit velocity heartbeat; caller holds the
    /// lock. Same permit/deadman/camera/seq gates as teleop; autonomy is
    /// cancelled and the pan head is forced forward.
    fn accept_drive(
        &self,
        s: &mut ControlInner,
        mode: &str,
        message: &Value,
        seq: i64,
        now: f64,
        expires: f64,
    ) -> bool {
        let drive = if mode == "calibrate" {
            // Python requires real ints, not floats, for raw PWM.
            let left = message.get("left_pwm").and_then(Self::json_int);
            let right = message.get("right_pwm").and_then(Self::json_int);
            match (left, right) {
                (Some(l), Some(r))
                    if l.abs() <= self.max_pwm && r.abs() <= self.max_pwm =>
                {
                    DriveCmd::Calibrate(l, r)
                }
                _ => {
                    self.stop_locked(s, "invalid_command", true);
                    return false;
                }
            }
        } else {
            let v = message.get("v_mps").and_then(Self::json_num);
            let w = message.get("w_radps").and_then(Self::json_num);
            match (v, w) {
                (Some(v), Some(w)) if v.abs() <= 5.0 && w.abs() <= 10.0 => {
                    DriveCmd::Velocity(v, w)
                }
                _ => {
                    self.stop_locked(s, "invalid_command", true);
                    return false;
                }
            }
        };
        if s.scan_token.is_some() {
            self.stop_locked(s, "scan_override", false);
            return false;
        }
        if let Some(a) = s.autonomy.as_mut() {
            a.reset();
        }
        s.stick_override = false;
        if s.pan != 0 {
            s.range_cm = None;
            s.range_at = f64::NEG_INFINITY;
        }
        s.drive = Some(drive);
        s.seq = seq;
        s.v = 0.0;
        s.w = 0.0;
        s.pan = 0;
        s.deadline = (now + LEASE).min(expires);
        s.link_ms = Some(round1((LEASE - (expires - now)) * 1000.0));
        s.reason = "active".into();
        true
    }

    /// Motor output for an active drive command; mirrors _drive_output.
    fn drive_output(&self, s: &mut ControlInner) -> (i64, i64, i64) {
        let Some(drive) = s.drive else {
            return (0, 0, s.pan + 90);
        };
        self.drive_output_values(s, drive)
    }

    /// _drive_output(kind, values): the Nav2 calibrated path also lands here.
    fn drive_output_values(&self, s: &mut ControlInner, drive: DriveCmd) -> (i64, i64, i64) {
        let (mut left, mut right) = if s.connected {
            match drive {
                DriveCmd::Calibrate(l, r) => (l, r),
                DriveCmd::Velocity(v, w) => self
                    .drive_profile
                    .as_ref()
                    .map(|p| p.wheel_pwm(v, w, Some(self.max_pwm)))
                    .unwrap_or((0, 0)),
            }
        } else {
            (0, 0)
        };
        // Translation intent decides the range stop: a spin with slightly
        // unequal trimmed wheels must not be blocked by its own bias.
        let translating = match drive {
            DriveCmd::Velocity(v, _) => v > 0.0,
            DriveCmd::Calibrate(..) => left + right > 0,
        };
        let blocked = translating && (left > 0 || right > 0) && self.forward_blocked(s);
        if blocked {
            left = 0;
            right = 0;
            s.reason = "forward_blocked".into();
        }
        if let Some(hook) = self.on_output.lock().unwrap().as_ref() {
            hook(
                (left + right) as f64 / 2.0 / self.max_pwm as f64,
                (left - right) as f64 / 2.0 / self.max_pwm as f64,
                blocked,
            );
        }
        (left, right, s.pan + 90)
    }

    pub fn output(&self) -> (i64, i64, i64) {
        let mut s = self.inner.lock().unwrap();
        self.output_locked(&mut s)
    }

    pub(crate) fn output_locked(&self, s: &mut ControlInner) -> (i64, i64, i64) {
        if s.deadline != 0.0 && self.now() >= s.deadline {
            self.stop_locked(s, "lease_expired", true);
        }
        let autonomous = s.autonomy.as_ref().map(|a| a.mode != "teleop").unwrap_or(false);
        // #134: autonomous/drive/scan stay camera-gated; held teleop does not.
        let gated = autonomous
            || s.drive.is_some()
            || s.scan_token.is_some()
            || ((s.v != 0.0 || s.w != 0.0) && !self.demo_mode);
        let camera_ok = self.camera_is_ready();
        // #175: demo autonomy is not gated on the rover-camera gate here — a
        // short stall pauses below instead of stopping.
        let demo_autonomy =
            self.demo_mode && autonomous && s.deadline != 0.0 && s.drive.is_none();
        if gated && !camera_ok && !demo_autonomy {
            self.stop_locked(s, "camera_stale", true);
        }
        if s.drive.is_some() && s.deadline != 0.0 {
            return self.drive_output(s);
        }
        let (mut v, mut w) = (s.v, s.w);
        if s.scan_token.is_some() {
            v = 0.0;
            w = s.scan_w;
            if w != 0.0 && self.range_unsafe(&s) {
                self.stop_locked(s, "scan_range_stop", true);
                v = 0.0;
                w = 0.0;
            }
        }
        let engaged = s
            .autonomy
            .as_ref()
            .map(|a| a.mode != "teleop")
            .unwrap_or(false)
            && s.deadline != 0.0;
        if let (Some(nav), Some(a)) = (self.navigator.as_ref(), s.autonomy.as_ref()) {
            nav.sync(Some(a.mode.as_str()), a.target_id, engaged);
        }
        let backed = engaged
            && self.navigator.is_some()
            && s.autonomy
                .as_ref()
                .map(|a| self.navigator.as_ref().unwrap().handles(&a.mode))
                .unwrap_or(false);
        // #130: Explore never hands over to Seek — a person indoors is a
        // moving obstacle for the costmaps to avoid, not a target. Seek
        // runs only when the operator selects it.
        if backed && s.autonomy.as_ref().map(|a| a.mode.as_str()) == Some("seek") {
            let nav = self.navigator.as_ref().unwrap();
            let snapshot = self.detections.as_ref().map(|d| d()).unwrap_or(json!({}));
            nav.update_seek(&snapshot);
        }
        let mut fault = if backed {
            self.navigator.as_ref().unwrap().sensor_fault()
        } else {
            None
        };
        // #175: in demo autonomy a camera stall is itself a pausable fault,
        // and a fully fresh tick clears the pause timer.
        if demo_autonomy && engaged {
            if fault.is_none() && !camera_ok {
                fault = Some("camera_stale");
            }
            if fault.is_none() {
                s.sensor_pause_since = None;
            }
        }
        if let Some(f) = fault {
            if demo_autonomy && engaged && self.sensor_pause(s, f) {
                // Hold still; the session, lease, goal and Nav2 intent all stay.
                v = 0.0;
                w = 0.0;
            } else {
                // No fresh normal-tracking pose or measured depth of the current
                // sensor session: stop now, even with a live lease and fresh
                // cmd_vel, and require a new press.
                self.stop_locked(s, f, true);
                v = 0.0;
                w = 0.0;
            }
        } else if backed {
            let nav = self.navigator.as_ref().unwrap();
            if let Some(profile) = &self.drive_profile {
                // Calibrated: Nav2 m/s, rad/s straight to wheel PWM.
                let command = nav.command(true).unwrap_or((0.0, 0.0));
                let top_v = profile.max_linear_mps(Some(self.max_pwm));
                let top_w = profile.max_angular_radps(Some(self.max_pwm));
                return self.drive_output_values(
                    s,
                    DriveCmd::Velocity(
                        command.0.clamp(-top_v, top_v),
                        command.1.clamp(-top_w, top_w),
                    ),
                );
            }
            let command = nav.command(false);
            let (nv, nw) = command.unwrap_or((0.0, 0.0));
            v = nv;
            w = nw;
        } else if engaged && self.navigator.is_some() {
            // #115: Nav2 is the only navigation when configured — hold still,
            // never drive the reactive policy blind.
            v = 0.0;
            w = 0.0;
        } else if engaged {
            let cm = if self.now() - s.range_at < RANGE_TTL { s.range_cm } else { None };
            let snapshot = self.detections.as_ref().map(|d| d()).unwrap_or(json!({}));
            if let Some(a) = s.autonomy.as_mut() {
                // detections() never does I/O; a fault still stops motion.
                let (av, aw) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    a.step(self.now(), cm, &snapshot)
                }))
                .unwrap_or_else(|_| {
                    (f64::NAN, f64::NAN)
                });
                if av.is_nan() {
                    self.stop_locked(s, "autonomy_unavailable", true);
                    v = 0.0;
                    w = 0.0;
                } else {
                    v = av;
                    w = aw;
                }
            }
        }
        if !s.connected {
            v = 0.0;
            w = 0.0;
        }
        let blocked = v > 0.0 && self.forward_blocked(s);
        if blocked {
            v = 0.0;
            s.reason = "forward_blocked".into();
        }
        let (left, right) = (v + w, v - w); // positive w turns right
        let scale = 1.0f64.max(left.abs()).max(right.abs());
        if let Some(hook) = self.on_output.lock().unwrap().as_ref() {
            hook(v / scale, w / scale, blocked);
        }
        // Autonomy-driven motion and scan sweeps get the lower ceiling;
        // operator-steered motion (including stick override) keeps max_pwm.
        let engaged = autonomous && s.deadline != 0.0;
        let top = if engaged || s.scan_token.is_some() {
            self.autonomy_max_pwm
        } else {
            self.max_pwm
        };
        let pwm = |value: f64| -> i64 {
            if value == 0.0 {
                return 0;
            }
            let magnitude = (self.min_pwm as f64
                + (top - self.min_pwm) as f64 * (value / scale).abs())
            .round_ties_even() as i64;
            if value > 0.0 {
                magnitude
            } else {
                -magnitude
            }
        };
        (pwm(left), pwm(right), s.pan + 90)
    }

    pub fn status(&self, issue_permit: bool) -> Value {
        let mut s = self.inner.lock().unwrap();
        self.status_locked(&mut s, issue_permit)
    }

    /// status() with the caller already holding the lock (the nav seam's
    /// set_goal/cancel need the status nav object mid-transaction).
    pub(crate) fn status_locked(&self, s: &mut ControlInner, issue_permit: bool) -> Value {
        let (left, right, pan) = self.output_locked(s);
        let now = self.now();
        s.permits.retain(|(_, expiry)| *expiry > now);
        let permit = if issue_permit && s.session_id.is_some() {
            let t = token();
            s.permits.push_back((t.clone(), now + LEASE));
            Some(t)
        } else {
            None
        };
        let mut out = json!({
            "type": "control_status",
            "session_id": s.session_id,
            "permit": permit,
            "permit_ms": 250,
            "seq": s.seq,
            "range_cm": s.range_cm,
            "estop": self.forward_blocked(s),
            "mode": if s.scan_token.is_some() {
                Value::from("scan")
            } else {
                s.autonomy.as_ref().map(|a| a.mode.clone().into()).unwrap_or_else(|| Value::from("teleop"))
            },
            "link_ms": s.link_ms,
            "autonomy_state": if s.stick_override {
                "stick_override"
            } else {
                s.autonomy.as_ref().map(|a| a.phase.as_str()).unwrap_or("idle")
            },
            "target_id": s.autonomy.as_ref().and_then(|a| a.target_id),
            "rover": s.connected,
            "simulated": self.simulated,
            "requires_release": s.requires_release,
            "reason": s.reason,
            "left_pwm": left,
            "right_pwm": right,
            "pan_deg": pan,
            "max_pwm": self.max_pwm,
            "min_pwm": self.min_pwm,
            "drive_profile": self.drive_profile.is_some(),
            "calibration_allowed": self.allow_calibration,
            "drive_mode": s.drive.map(|d| d.kind()).unwrap_or("normalized"),
        });
        if let Some(nav) = &self.navigator {
            let (mode, engaged) = match s.autonomy.as_ref() {
                Some(a) => (
                    Some(a.mode.as_str()),
                    a.mode != "teleop" && s.deadline != 0.0,
                ),
                None => (None, false),
            };
            out.as_object_mut()
                .unwrap()
                .insert("nav".into(), nav.nav_object(mode, Some(engaged)));
        }
        out
    }

    pub fn public_status(&self) -> Value {
        let mut v = self.status(false);
        v.as_object_mut().unwrap().remove("session_id");
        v
    }
}

/// Dedicated serial pump: a blocked HTTP/inference worker cannot renew motion.
/// Port of SerialPump.
pub struct SerialPump {
    pub state: Arc<ControlState>,
    pub port: Option<String>,
    pub simulate: bool,
    /// Sim world HC-SR04 reading; None means no echo (control/serial_link.py).
    pub simulated_range: Option<Arc<dyn Fn() -> Option<i64> + Send + Sync>>,
    quit: Arc<(Mutex<bool>, Condvar)>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl SerialPump {
    pub fn new(
        state: Arc<ControlState>,
        port: Option<String>,
        simulate: bool,
        simulated_range: Option<Arc<dyn Fn() -> Option<i64> + Send + Sync>>,
    ) -> Self {
        if port.is_some() && simulate {
            panic!("Choose a real serial port OR simulated rover");
        }
        Self { state, port, simulate, simulated_range, quit: Arc::new((Mutex::new(false), Condvar::new())), thread: Mutex::new(None) }
    }

    fn wait(&self, secs: f64) -> bool {
        let (lock, cvar) = &*self.quit;
        let guard = lock.lock().unwrap();
        if *guard {
            return true;
        }
        let (guard, _) = cvar.wait_timeout(guard, Duration::from_secs_f64(secs)).unwrap();
        *guard
    }

    fn quitting(&self) -> bool {
        *self.quit.0.lock().unwrap()
    }

    pub fn start(self: Arc<Self>) {
        if self.thread.lock().unwrap().is_some() {
            return;
        }
        if self.port.is_none() && !self.simulate {
            self.state.stop("rover_disabled", true);
            return;
        }
        let me = self.clone();
        *self.thread.lock().unwrap() = Some(std::thread::spawn(move || me.run()));
    }

    pub fn close(&self) {
        self.state.stop("shutdown", true);
        *self.quit.0.lock().unwrap() = true;
        self.quit.1.notify_all();
        if let Some(t) = self.thread.lock().unwrap().take() {
            let _ = t.join();
        }
    }

    fn run(&self) {
        if self.simulate {
            tracing::warn!("SIMULATED ROVER: no motor hardware is connected");
            self.state.set_connected(true, None);
            while !self.quitting() {
                let cm = self.simulated_range.as_ref().map(|f| f()).unwrap_or(Some(120));
                self.state.update_range(cm.map(|c| c as f64).unwrap_or(-1.0));
                self.state.output();
                self.wait(0.05);
            }
            self.state.set_connected(false, Some("shutdown"));
            return;
        }
        while !self.quitting() {
            match self.pump_once() {
                Ok(()) => break,
                Err(e) => {
                    tracing::warn!("Rover serial disconnected: {e}");
                    self.state.set_connected(false, Some("serial_disconnected"));
                }
            }
            if self.quitting() {
                self.state.set_connected(false, Some("shutdown"));
            }
            self.wait(1.0);
        }
    }

    fn pump_once(&self) -> Result<(), String> {
        let mut device = serialport::new(self.port.as_deref().unwrap(), 115200)
            .timeout(Duration::from_millis(0))
            .open()
            .map_err(|e| e.to_string())?;
        // #116: non-blocking writes like pyserial write_timeout=0 — a stalled
        // or partial write is a serial fault, not a silent queue. The Uno
        // watchdog and reply timeout prove liveness instead.
        fn send(device: &mut Box<dyn serialport::SerialPort>, data: &[u8]) -> Result<(), String> {
            if device.bytes_to_write().unwrap_or(0) > 256 {
                return Err("Uno serial output stalled".into());
            }
            match device.write(data) {
                Ok(n) if n == data.len() => Ok(()),
                Ok(_) => Err("Uno serial write incomplete".into()),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
                {
                    Err("Uno serial write incomplete".into())
                }
                Err(e) => Err(e.to_string()),
            }
        }
        // Uno's USB open can reset its bootloader; wait without blocking the server.
        if self.wait(2.0) {
            return Ok(());
        }
        let _ = device.clear(serialport::ClearBuffer::All);
        let servo = self.state.pan_mount == "servo";
        send(&mut device, if servo { b"M 0 0\nP 90\nR\n" as &[u8] } else { b"M 0 0\nR\n" })?;
        let mut pending: Vec<u8> = Vec::new();
        let started = self.state.now();
        let mut last_response = started;
        let mut unanswered = 0u32;
        let mut next_tick = started;
        let mut next_range = started;
        let mut last_pan: i64 = 90;
        let mut settle_at = started;
        let mut ready = false;
        while !self.quitting() {
            let now = self.state.now();
            let waiting = device.bytes_to_read().unwrap_or(0).min(256) as usize;
            if waiting > 0 {
                let mut chunk = vec![0u8; waiting];
                if let Ok(n) = device.read(&mut chunk) {
                    pending.extend_from_slice(&chunk[..n]);
                }
            }
            if pending.len() > 1024 {
                return Err("Uno sent an overlong serial reply".into());
            }
            while let Some(pos) = pending.iter().position(|&b| b == b'\n') {
                let raw: Vec<u8> = pending.drain(..=pos).collect();
                let reply = String::from_utf8_lossy(&raw[..raw.len() - 1]).trim().to_string();
                if reply == "OK" || reply.starts_with("R ") {
                    last_response = now;
                    unanswered = 0;
                }
                if !ready && reply == "OK" {
                    self.state.set_connected(true, None);
                    ready = true;
                }
                if ready && reply.starts_with("R ") && now >= settle_at {
                    let cm: f64 = reply[2..].parse().unwrap_or(-1.0);
                    self.state.update_range(cm);
                }
                if reply.starts_with("ERR") {
                    self.state.stop("uno_rejected_command", true);
                }
            }
            // Silence only counts while commands go out: a starved pump sends
            // nothing, and blaming the Uno costs a port reopen + bootloader
            // reset (be2ec07 parity).
            if now - last_response > if ready { 0.5 } else { 2.0 } && (unanswered >= 4 || !ready) {
                return Err("Uno response timeout".into());
            }
            if now >= next_tick {
                let (left, right, pan) = if ready { self.state.output() } else { (0, 0, 90) };
                // Commands are never buffered for replay; recompute from
                // monotonic lease immediately before every serial write.
                let mut data = format!("M {left} {right}\n");
                if servo && pan != last_pan {
                    data.push_str(&format!("P {pan}\n"));
                    last_pan = pan;
                    settle_at = now + 0.2;
                }
                if now >= next_range {
                    data.push_str("R\n");
                    next_range = now + 0.1;
                }
                send(&mut device, data.as_bytes())?;
                unanswered += 1;
                next_tick = now + 0.05;
            }
            self.wait(0.005);
        }
        // Motor stop on exit (mirrors the Python finally block).
        let _ = device.write_all(b"M 0 0\n");
        Ok(())
    }
}
