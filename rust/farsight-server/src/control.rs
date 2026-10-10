//! Control policy, serial pump, and service glue.
//! Ports of jetson/control/{state,serial_link,service}.py — same permits,
//! lease, e-stop, and Uno wire protocol.

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use base64::Engine;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::state::now_monotonic;

pub const LEASE: f64 = 0.250;
pub const RANGE_TTL: f64 = 0.350;

pub type CameraReady = Arc<dyn Fn() -> bool + Send + Sync>;

pub struct ControlInner {
    pub session_id: Option<String>,
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
}

pub struct ControlState {
    pub simulated: bool,
    pub max_pwm: i64,
    pub camera_ready: Option<CameraReady>,
    pub inner: Mutex<ControlInner>,
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round_ties_even() / 10.0
}

fn token() -> String {
    let mut bytes = [0u8; 18];
    rand::fill(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

impl ControlState {
    pub fn new(simulated: bool, max_pwm: i64, camera_ready: Option<CameraReady>) -> Self {
        assert!((1..=255).contains(&max_pwm), "max_pwm must be 1..255");
        Self {
            simulated,
            max_pwm,
            camera_ready,
            inner: Mutex::new(ControlInner {
                session_id: None,
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
            }),
        }
    }

    pub fn stop(&self, reason: &str, require_release: bool) {
        let mut s = self.inner.lock().unwrap();
        Self::stop_locked(&mut s, reason, require_release);
    }

    fn stop_locked(s: &mut ControlInner, reason: &str, require_release: bool) {
        s.v = 0.0;
        s.w = 0.0;
        s.deadline = 0.0;
        s.reason = reason.into();
        if require_release {
            s.requires_release = true;
        }
    }

    pub fn set_connected(&self, value: bool, reason: Option<&str>) {
        let mut s = self.inner.lock().unwrap();
        if value != s.connected {
            let reason = if value { "release_required" } else { reason.unwrap_or("serial_disconnected") };
            Self::stop_locked(&mut s, reason, true);
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
            return None;
        }
        let id = Uuid::new_v4().to_string();
        s.session_id = Some(id.clone());
        s.seq = -1;
        s.permits.clear();
        Self::stop_locked(&mut s, "release_required", true);
        Some(id)
    }

    pub fn end(&self, session_id: &str) {
        let mut s = self.inner.lock().unwrap();
        if s.session_id.as_deref() == Some(session_id) {
            Self::stop_locked(&mut s, "controller_disconnected", true);
            s.session_id = None;
            s.permits.clear();
        }
    }

    pub fn update_range(&self, cm: f64) {
        let mut s = self.inner.lock().unwrap();
        s.range_cm = if cm.is_finite() && cm > 0.0 && cm <= 500.0 { Some(cm) } else { None };
        s.range_at = now_monotonic();
    }

    fn forward_blocked(s: &ControlInner) -> bool {
        s.range_cm.is_none()
            || now_monotonic() - s.range_at >= RANGE_TTL
            || s.range_cm.unwrap() < 25.0
            || s.pan.abs() > 10
    }

    fn camera_is_ready(&self) -> bool {
        self.camera_ready.as_ref().map(|f| f()).unwrap_or(true)
    }

    pub fn accept(&self, message: &Value, session_id: &str) -> bool {
        let mut s = self.inner.lock().unwrap();
        if !message.is_object() || s.session_id.as_deref() != Some(session_id) {
            return false;
        }
        if message.get("session_id").and_then(Value::as_str) != Some(session_id) {
            Self::stop_locked(&mut s, "wrong_session", true);
            return false;
        }
        // JSON integer in 0..2^53-1; booleans and floats rejected.
        let seq = match message.get("seq") {
            Some(Value::Number(n)) => {
                if let Some(u) = n.as_u64() {
                    if u <= (1u64 << 53) - 1 {
                        u as i64
                    } else {
                        Self::stop_locked(&mut s, "invalid_sequence", true);
                        return false;
                    }
                } else {
                    Self::stop_locked(&mut s, "invalid_sequence", true);
                    return false;
                }
            }
            _ => {
                Self::stop_locked(&mut s, "invalid_sequence", true);
                return false;
            }
        };
        // Any release stops, including stale releases, but only an ordered
        // release can unlock the next press.
        if message.get("deadman") == Some(&Value::Bool(false)) {
            Self::stop_locked(&mut s, "deadman_released", true);
            if seq > s.seq {
                s.seq = seq;
                s.requires_release = false;
            }
            return true;
        }
        if seq <= s.seq || message.get("deadman") != Some(&Value::Bool(true)) {
            Self::stop_locked(&mut s, "stale_sequence", true);
            return false;
        }
        let token = message.get("permit").and_then(Value::as_str);
        let expires = token
            .and_then(|t| s.permits.iter().find(|(k, _)| k == t).map(|(_, e)| *e))
            .unwrap_or(0.0);
        let now = now_monotonic();
        if expires <= now {
            Self::stop_locked(&mut s, "expired_permit", true);
            return false;
        }
        if !s.connected || s.requires_release {
            let reason = if s.connected { "release_required" } else { "serial_disconnected" };
            Self::stop_locked(&mut s, reason, true);
            return false;
        }
        if !self.camera_is_ready() {
            Self::stop_locked(&mut s, "camera_stale", true);
            return false;
        }
        if message.get("mode").and_then(Value::as_str).unwrap_or("teleop") != "teleop" {
            Self::stop_locked(&mut s, "unsupported_mode", true);
            return false;
        }
        let video_age = message.get("video_age_ms").and_then(Value::as_f64);
        if message.get("video_fresh") != Some(&Value::Bool(true))
            || !video_age.map(|a| a.is_finite() && (0.0..=500.0).contains(&a)).unwrap_or(false)
        {
            Self::stop_locked(&mut s, "video_stale", true);
            return false;
        }
        let v = message.get("v").and_then(Value::as_f64);
        let w = message.get("w").and_then(Value::as_f64);
        let pan = message.get("pan").and_then(Value::as_f64).unwrap_or(0.0);
        if [v, w, Some(pan)].iter().flatten().any(|x| !x.is_finite()) {
            Self::stop_locked(&mut s, "invalid_command", true);
            return false;
        }
        let (v, w) = (v.unwrap(), w.unwrap());
        if !(-1.0..=1.0).contains(&v) || !(-1.0..=1.0).contains(&w) || !(-80.0..=80.0).contains(&pan) {
            Self::stop_locked(&mut s, "invalid_command", true);
            return false;
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
        s.deadline = (now + LEASE).min(expires);
        s.link_ms = Some(round1((LEASE - (expires - now)) * 1000.0));
        s.reason = "active".into();
        true
    }

    pub fn output(&self) -> (i64, i64, i64) {
        let mut s = self.inner.lock().unwrap();
        self.output_locked(&mut s)
    }

    fn output_locked(&self, s: &mut ControlInner) -> (i64, i64, i64) {
        if s.deadline != 0.0 && now_monotonic() >= s.deadline {
            Self::stop_locked(s, "lease_expired", true);
        }
        if (s.v != 0.0 || s.w != 0.0) && !self.camera_is_ready() {
            Self::stop_locked(s, "camera_stale", true);
        }
        let (mut v, w) = (s.v, s.w);
        if !s.connected {
            v = 0.0;
            // w is still used below; Python zeroes both.
        }
        if !s.connected {
            // mirror `v = w = 0`
        }
        let w = if s.connected { w } else { 0.0 };
        if v > 0.0 && Self::forward_blocked(s) {
            v = 0.0;
            s.reason = "forward_blocked".into();
        }
        let (left, right) = (v + w, v - w); // positive w turns right
        let scale = 1.0f64.max(left.abs()).max(right.abs());
        (
            (left / scale * self.max_pwm as f64).round_ties_even() as i64,
            (right / scale * self.max_pwm as f64).round_ties_even() as i64,
            s.pan + 90,
        )
    }

    pub fn status(&self, issue_permit: bool) -> Value {
        let mut s = self.inner.lock().unwrap();
        let (left, right, pan) = self.output_locked(&mut s);
        let now = now_monotonic();
        s.permits.retain(|(_, expiry)| *expiry > now);
        let permit = if issue_permit && s.session_id.is_some() {
            let t = token();
            s.permits.push_back((t.clone(), now + LEASE));
            Some(t)
        } else {
            None
        };
        json!({
            "type": "control_status",
            "session_id": s.session_id,
            "permit": permit,
            "permit_ms": 250,
            "seq": s.seq,
            "range_cm": s.range_cm,
            "estop": Self::forward_blocked(&s),
            "mode": "teleop",
            "link_ms": s.link_ms,
            "rover": s.connected,
            "simulated": self.simulated,
            "requires_release": s.requires_release,
            "reason": s.reason,
            "left_pwm": left,
            "right_pwm": right,
            "pan_deg": pan,
            "max_pwm": self.max_pwm,
        })
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
    quit: Arc<(Mutex<bool>, Condvar)>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl SerialPump {
    pub fn new(state: Arc<ControlState>, port: Option<String>, simulate: bool) -> Self {
        if port.is_some() && simulate {
            panic!("Choose a real serial port OR simulated rover");
        }
        Self { state, port, simulate, quit: Arc::new((Mutex::new(false), Condvar::new())), thread: Mutex::new(None) }
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
                self.state.update_range(120.0);
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
        // Uno's USB open can reset its bootloader; wait without blocking the server.
        if self.wait(2.0) {
            return Ok(());
        }
        let _ = device.clear(serialport::ClearBuffer::All);
        device.write_all(b"M 0 0\nP 90\nR\n").map_err(|e| e.to_string())?;
        let mut pending: Vec<u8> = Vec::new();
        let started = now_monotonic();
        let mut last_response = started;
        let mut next_tick = started;
        let mut next_range = started;
        let mut last_pan: i64 = 90;
        let mut settle_at = started;
        let mut ready = false;
        while !self.quitting() {
            let now = now_monotonic();
            let waiting = device.bytes_to_read().unwrap_or(0).min(256) as usize;
            if waiting > 0 {
                let mut chunk = vec![0u8; waiting];
                match device.read(&mut chunk) {
                    Ok(n) => pending.extend_from_slice(&chunk[..n]),
                    Err(_) => {}
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
            if now - last_response > if ready { 0.5 } else { 2.0 } {
                return Err("Uno response timeout".into());
            }
            if now >= next_tick {
                let (left, right, pan) = if ready { self.state.output() } else { (0, 0, 90) };
                // Commands are never buffered for replay; recompute from
                // monotonic lease immediately before every serial write.
                let mut data = format!("M {left} {right}\n");
                if pan != last_pan {
                    data.push_str(&format!("P {pan}\n"));
                    last_pan = pan;
                    settle_at = now + 0.2;
                }
                if now >= next_range {
                    data.push_str("R\n");
                    next_range = now + 0.1;
                }
                device.write_all(data.as_bytes()).map_err(|e| e.to_string())?;
                next_tick = now + 0.05;
            }
            self.wait(0.005);
        }
        // Motor stop on exit (mirrors the Python finally block).
        let _ = device.write_all(b"M 0 0\n");
        Ok(())
    }
}
