//! Rescue drill scoring: timed face search, e-stop count and a dead-reckoned
//! route home. Port of jetson/drill.py — server-authoritative so the headset
//! only renders.

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{json, Value};

use crate::state::now_monotonic;

const DWELL_S: f64 = 1.0;
// A face missing for longer than this, or from more than MAX_MISSES detector
// frames, restarts its dwell: one dropped frame is tolerated, id flicker is not.
const GAP_S: f64 = 0.7;
const MAX_MISSES: f64 = 1.0;
// Repeated blocks within this window are one obstacle (sensor chatter).
const ESTOP_DEBOUNCE_S: f64 = 0.5;
// Nominal full-command speeds of the rover; uncalibrated, hence "approximate".
const MAX_SPEED_MPS: f64 = 0.35;
const MAX_TURN_RADPS: f64 = 1.6;
// Control outputs arrive at ~20 Hz; a longer silence is a stall, not motion.
const MAX_DT: f64 = 0.25;
const CRUMB_M: f64 = 0.1;
const MAX_CRUMBS: usize = 600;

struct Seen {
    first: f64,
    last: f64,
    missed: f64,
}

struct Inner {
    started: Option<f64>,
    duration: f64,
    victims: i64,
    ended: Option<f64>,
    visible: HashMap<i64, Seen>,
    found: HashMap<i64, f64>,
    estops: u64,
    blocked: bool,
    blocked_at: f64,
    x: f64,
    y: f64,
    heading: f64,
    command: (f64, f64),
    command_at: Option<f64>,
    crumbs: Vec<Value>,
}

impl Inner {
    fn reset(&mut self) {
        self.ended = None;
        self.visible.clear();
        self.found.clear();
        self.estops = 0;
        self.blocked = false;
        self.blocked_at = f64::NEG_INFINITY;
        self.x = 0.0;
        self.y = 0.0;
        self.heading = 0.0;
        self.command = (0.0, 0.0);
        self.command_at = None;
        self.crumbs = vec![json!({"t": 0.0, "x": 0.0, "y": 0.0})];
    }

    fn running(&self, now: f64) -> bool {
        let Some(started) = self.started else { return false };
        if self.ended.is_some() {
            return false;
        }
        if now - started >= self.duration {
            return false;
        }
        true
    }

    fn crumb(&self, now: f64) -> Value {
        let started = self.started.unwrap_or(now);
        json!({"t": round2(now - started), "x": round3(self.x), "y": round3(self.y)})
    }

    fn integrate(&mut self, dt: f64, now: f64) {
        if dt <= 0.0 {
            return;
        }
        let (v, w) = self.command;
        let turn = -w * MAX_TURN_RADPS * dt;
        let mid = self.heading + turn / 2.0;
        self.x += v * MAX_SPEED_MPS * dt * mid.cos();
        self.y += v * MAX_SPEED_MPS * dt * mid.sin();
        self.heading = (self.heading + turn).sin().atan2((self.heading + turn).cos());
        let last = &self.crumbs[self.crumbs.len() - 1];
        let (lx, ly) = (last["x"].as_f64().unwrap_or(0.0), last["y"].as_f64().unwrap_or(0.0));
        if (self.x - lx).hypot(self.y - ly) >= CRUMB_M {
            self.crumbs.push(self.crumb(now));
            if self.crumbs.len() > MAX_CRUMBS {
                let mut kept = vec![self.crumbs[0].clone()];
                kept.extend(self.crumbs.iter().skip(2).step_by(2).cloned());
                self.crumbs = kept;
            }
        }
    }

    fn status(&mut self) -> Value {
        let now = now_monotonic();
        // Lazily stamp the natural expiry exactly like _running does.
        if self.ended.is_none() {
            if let Some(started) = self.started {
                if now - started >= self.duration {
                    self.ended = Some(started + self.duration);
                }
            }
        }
        let running = self.running(now);
        let (mut elapsed, remaining) = match self.started {
            None => (Value::Null, Value::Null),
            Some(started) => {
                let end = self.ended.unwrap_or(now);
                let el = round1((end - started).min(self.duration));
                let rem = if running { round1((self.duration - el).max(0.0)) } else { 0.0 };
                (json!(el), json!(rem))
            }
        };
        let mut crumbs = self.crumbs.clone();
        if self.started.is_some() {
            let current = self.crumb(self.ended.unwrap_or(now));
            let last = &crumbs[crumbs.len() - 1];
            let (lx, ly) = (last["x"].as_f64().unwrap_or(0.0), last["y"].as_f64().unwrap_or(0.0));
            if (current["x"].as_f64().unwrap_or(0.0) - lx)
                .hypot(current["y"].as_f64().unwrap_or(0.0) - ly)
                >= 0.01
            {
                crumbs.push(current);
            }
        }
        // found ids ordered by the time they were located.
        let mut pairs: Vec<(i64, f64)> = self.found.iter().map(|(a, b)| (*a, *b)).collect();
        pairs.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
        let found: Vec<i64> = pairs.iter().map(|p| p.0).collect();
        let found_at: Vec<Value> = pairs.iter().map(|p| json!(p.1)).collect();
        let mut status = json!({
            "active": running,
            "remaining_s": remaining,
            "elapsed_s": elapsed,
            "duration_s": self.duration,
            "found": found.len(),
            "total": self.victims,
            "found_ids": found,
            "found_at_s": found_at,
            "estops": self.estops,
            "pose": {"x": round3(self.x), "y": round3(self.y), "heading_rad": round3(self.heading)},
            "breadcrumb": crumbs,
            "route_home": crumbs.iter().rev().map(|c| json!({"x": c["x"], "y": c["y"]})).collect::<Vec<_>>(),
            "route_approximate": true,
            "dwell_s": DWELL_S,
            "summary": Value::Null,
        });
        if self.started.is_some() && !running {
            elapsed = status["elapsed_s"].clone();
            status["summary"] = json!({
                "time_s": elapsed,
                "found": found.len(),
                "total": self.victims,
                "estops_avoided": self.estops,
                "found_at_s": status["found_at_s"].clone(),
                "breadcrumb": crumbs,
                "route_approximate": true,
            });
        }
        status
    }
}

fn round1(v: f64) -> f64 {
    (v * 10.0).round() / 10.0
}
fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}
fn round3(v: f64) -> f64 {
    (v * 1000.0).round() / 1000.0
}

pub struct Drill {
    inner: Mutex<Inner>,
}

impl Drill {
    pub fn new() -> Self {
        let mut inner = Inner {
            started: None,
            duration: 90.0,
            victims: 3,
            ended: None,
            visible: HashMap::new(),
            found: HashMap::new(),
            estops: 0,
            blocked: false,
            blocked_at: f64::NEG_INFINITY,
            x: 0.0,
            y: 0.0,
            heading: 0.0,
            command: (0.0, 0.0),
            command_at: None,
            crumbs: Vec::new(),
        };
        inner.reset();
        Self { inner: Mutex::new(inner) }
    }

    pub fn start(&self, duration: f64, victims: i64) -> Value {
        let mut inner = self.inner.lock().unwrap();
        inner.started = Some(now_monotonic());
        inner.duration = duration;
        inner.victims = victims;
        inner.reset();
        inner.status()
    }

    pub fn running(&self) -> bool {
        let mut inner = self.inner.lock().unwrap();
        let now = now_monotonic();
        if inner.ended.is_none() {
            if let Some(started) = inner.started {
                if now - started >= inner.duration {
                    inner.ended = Some(started + inner.duration);
                }
            }
        }
        inner.running(now)
    }

    pub fn observe(&self, boxes: &[Value]) {
        let mut inner = self.inner.lock().unwrap();
        let now = now_monotonic();
        if inner.ended.is_none() {
            if let Some(started) = inner.started {
                if now - started >= inner.duration {
                    inner.ended = Some(started + inner.duration);
                }
            }
        }
        if !inner.running(now) {
            return;
        }
        let started = inner.started.unwrap_or(now);
        let mut present = std::collections::HashSet::new();
        for b in boxes {
            let Some(track) = b["id"].as_i64() else { continue };
            if b["cls"].as_str() != Some("face") {
                continue;
            }
            present.insert(track);
            match inner.visible.get_mut(&track) {
                Some(seen) if now - seen.last <= GAP_S => {}
                _ => {
                    inner.visible.insert(track, Seen { first: now, last: now, missed: 0.0 });
                }
            }
            let dwell_ready = {
                let seen = inner.visible.get_mut(&track).unwrap();
                seen.last = now;
                now - seen.first >= DWELL_S
            };
            if !inner.found.contains_key(&track) && dwell_ready {
                inner.found.insert(track, round1(now - started));
                if inner.found.len() as i64 >= inner.victims {
                    inner.ended = Some(now);
                }
            }
        }
        let absent: Vec<i64> = inner
            .visible
            .keys()
            .filter(|t| !present.contains(t))
            .copied()
            .collect();
        for track in absent {
            if let Some(seen) = inner.visible.get_mut(&track) {
                seen.missed += 1.0;
            }
        }
        inner.visible.retain(|_, s| s.missed <= MAX_MISSES && now - s.last <= GAP_S);
    }

    /// Record the command applied from now on; v,w in [-1,1], positive w turns right.
    pub fn motion(&self, v: f64, w: f64, blocked: bool) {
        let mut inner = self.inner.lock().unwrap();
        let now = now_monotonic();
        if inner.ended.is_none() {
            if let Some(started) = inner.started {
                if now - started >= inner.duration {
                    inner.ended = Some(started + inner.duration);
                }
            }
        }
        if !inner.running(now) {
            inner.command_at = None;
            return;
        }
        if let Some(at) = inner.command_at {
            let dt = (now - at).min(MAX_DT);
            inner.integrate(dt, now);
        }
        inner.command = (v, w);
        inner.command_at = Some(now);
        if blocked {
            if !inner.blocked && now - inner.blocked_at > ESTOP_DEBOUNCE_S {
                inner.estops += 1;
            }
            inner.blocked_at = now;
        }
        inner.blocked = blocked;
    }

    pub fn status(&self) -> Value {
        self.inner.lock().unwrap().status()
    }
}
