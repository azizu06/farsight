//! Small policies only: no sockets, hardware, clocks, or lease authority.
//! Port of jetson/autonomy.py.

use serde_json::Value;

pub fn valid_person(b: &Value) -> bool {
    let (x, w, id) = (b["x"].as_f64(), b["w"].as_f64(), b["id"].as_i64());
    b["cls"].as_str() == Some("person")
        && id.map(|i| i >= 0).unwrap_or(false)
        && x.map(|v| v.is_finite() && (0.0..=1.0).contains(&v)).unwrap_or(false)
        && w.map(|v| v.is_finite() && v > 0.0 && v <= 1.0).unwrap_or(false)
        && x.unwrap_or(2.0) + w.unwrap_or(2.0) <= 1.000001
}

pub struct Autonomy {
    pub choose_turn: Box<dyn Fn() -> f64 + Send + Sync>,
    pub mode: String,
    pub phase: String,
    pub target_id: Option<i64>,
    last_seen: Option<f64>,
    request: (String, Option<i64>),
    phase_at: f64,
    direction: f64,
}

impl Autonomy {
    pub fn new() -> Self {
        Self {
            choose_turn: Box::new(|| if rand::random::<bool>() { 1.0 } else { -1.0 }),
            mode: "teleop".into(),
            phase: "idle".into(),
            target_id: None,
            last_seen: None,
            request: ("teleop".into(), None),
            phase_at: 0.0,
            direction: 1.0,
        }
    }

    pub fn reset(&mut self) {
        self.mode = "teleop".into();
        self.phase = "idle".into();
        self.target_id = None;
        self.last_seen = None;
        self.request = ("teleop".into(), None);
        self.phase_at = 0.0;
        self.direction = 1.0;
    }

    pub fn select(&mut self, mode: &str, target_id: Option<i64>, now: f64) {
        if (mode, target_id) != (&self.request.0[..], self.request.1) {
            self.request = (mode.into(), target_id);
            self.mode = mode.into();
            self.target_id = target_id;
            self.phase = if mode == "explore" { "forward" } else { "tracking" }.into();
            self.phase_at = now;
            self.last_seen = Some(now);
        }
    }

    /// snapshot: the perception packet (age_ms, fresh, error, boxes).
    pub fn step(&mut self, now: f64, range_cm: Option<f64>, snapshot: &Value) -> (f64, f64) {
        if self.phase == "arrived" || self.phase == "target_lost" {
            return (0.0, 0.0);
        }
        let Some(cm) = range_cm.filter(|c| c.is_finite() && *c > 0.0) else {
            return (0.0, 0.0);
        };
        if cm < 25.0 {
            self.phase = "estop".into();
            return (0.0, 0.0);
        }
        let age = snapshot["age_ms"].as_f64();
        let boxes: Vec<&Value> = if snapshot["fresh"].as_bool().unwrap_or(false)
            && snapshot["error"].is_null()
            && age.map(|a| (0.0..1000.0).contains(&a)).unwrap_or(false)
        {
            snapshot["boxes"]
                .as_array()
                .map(|b| b.iter().filter(|x| valid_person(x)).collect())
                .unwrap_or_default()
        } else {
            Vec::new()
        };
        if self.mode == "explore" {
            if let Some(person) = boxes.first() {
                self.mode = "seek".into();
                self.target_id = person["id"].as_i64();
                self.last_seen = Some(now);
            } else {
                if self.phase == "turning" {
                    if now - self.phase_at < 1.0 {
                        return (0.0, self.direction * 0.35);
                    }
                    self.phase = "forward".into();
                    self.phase_at = now;
                }
                if cm < 70.0 || now - self.phase_at >= 4.0 {
                    self.phase = "turning".into();
                    self.phase_at = now;
                    self.direction = (self.choose_turn)();
                    return (0.0, self.direction * 0.35);
                }
                self.phase = "forward".into();
                return (0.25, 0.0);
            }
        }
        let target = boxes
            .iter()
            .find(|b| b["id"].as_i64() == self.target_id && b["cls"].as_str() == Some("person"));
        let Some(target) = target else {
            self.phase = if now - self.last_seen.unwrap_or(0.0) >= 1.0 { "target_lost" } else { "target_missing" }.into();
            return (0.0, 0.0);
        };
        self.last_seen = Some(now - age.unwrap_or(0.0) / 1000.0);
        self.phase = "tracking".into();
        if cm <= 60.0 {
            self.phase = "arrived".into();
            return (0.0, 0.0);
        }
        let error = target["x"].as_f64().unwrap_or(0.0) + target["w"].as_f64().unwrap_or(0.0) / 2.0 - 0.5;
        (0.35f64.min((cm - 60.0) / 200.0), (error * 1.5).clamp(-0.45, 0.45))
    }
}
