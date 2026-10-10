//! Port of jetson/control/tests/test_control.py (policy tests).

use std::sync::{Arc, Mutex};

use farsight_server::control::{Clock, ControlState};
use serde_json::{json, Value};

struct Fixture {
    state: ControlState,
    session: String,
    seq: std::cell::Cell<i64>,
    now: Arc<Mutex<f64>>,
}

fn fixture() -> Fixture {
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let state = ControlState::new(false, 0, 100, None, None, Some(clock)).unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    state.update_range(100.0);
    let f = Fixture { state, session, seq: std::cell::Cell::new(0), now };
    f.send(json!({"deadman": false}));
    f
}

impl Fixture {
    fn send(&self, values: Value) -> bool {
        self.seq.set(self.seq.get() + 1);
        let status = self.state.status(true);
        let mut m = json!({
            "session_id": self.session,
            "permit": status["permit"],
            "seq": self.seq.get(),
            "deadman": true,
            "v": 0.5, "w": 0.0, "pan": 0,
            "mode": "teleop",
            "video_fresh": true,
            "video_age_ms": 0,
        });
        for (k, v) in values.as_object().unwrap() {
            m[k] = v.clone();
        }
        self.state.accept(&m, &self.session)
    }

    fn output(&self) -> (i64, i64) {
        let (l, r, _) = self.state.output();
        (l, r)
    }
}

#[test]
fn active_drive_then_deadman_stop() {
    let f = fixture();
    assert!(f.send(json!({})));
    assert_eq!(f.output(), (50, 50));
    f.send(json!({"deadman": false}));
    assert_eq!(f.output(), (0, 0));
}

#[test]
fn stale_permit_stops() {
    let f = fixture();
    let token = f.state.status(true)["permit"].clone();
    assert!(f.send(json!({"permit": token})));
    assert_eq!(f.output(), (50, 50));
    // Past the 250ms lease, output() forces a stop requiring a fresh release.
    *f.now.lock().unwrap() += 0.4;
    assert_eq!(f.output(), (0, 0));
    assert!(f.state.inner.lock().unwrap().requires_release);
    assert!(!f.send(json!({"permit": "forged"})));
    assert!(!f.send(json!({}))); // new permit cannot replace physical release
}

#[test]
fn sequence_monotonic() {
    let f = fixture();
    assert!(f.send(json!({})));
    assert!(!f.send(json!({"seq": f.seq.get() - 1})));
    assert_eq!(f.output(), (0, 0));
}

#[test]
fn turn_and_reverse() {
    let f = fixture();
    assert!(f.send(json!({"v": 0.0, "w": 0.3})));
    assert_eq!(f.output(), (30, -30));
    assert!(f.send(json!({"v": -0.5, "w": 0.0})));
    assert_eq!(f.output(), (-50, -50));
}

#[test]
fn forward_blocked_by_range() {
    let f = fixture();
    f.state.update_range(10.0);
    assert!(f.send(json!({})));
    assert_eq!(f.output(), (0, 0));
    f.state.update_range(100.0);
    assert!(f.send(json!({})));
    assert_eq!(f.output(), (50, 50));
}

#[test]
fn session_rotation() {
    let f = fixture();
    let old = f.session.clone();
    let f2 = fixture();
    assert_ne!(old, f2.session);
}

#[test]
fn invalid_commands() {
    let f = fixture();
    for invalid in [
        json!({"v": 2.0}),
        json!({"v": "fast"}),
        json!({"pan": 200.0}),
        json!({"mode": "flight"}),
        json!({"video_fresh": false}),
        json!({"video_age_ms": 900.0}),
        json!({"video_age_ms": true}),
        json!({"deadman": "held"}),
        json!({"seq": 1.5}),
    ] {
        assert!(!f.send(invalid));
        assert_eq!(f.output(), (0, 0));
        f.send(json!({"deadman": false}));
    }
}

#[test]
fn pwm_scaling() {
    let f = fixture();
    assert!(f.send(json!({"v": 1.0, "w": 0.0})));
    assert_eq!(f.output(), (100, 100));
    assert!(f.send(json!({"v": 1.0, "w": 1.0})));
    assert_eq!(f.output(), (100, 0)); // left=2 clipped to scale, right=0
    assert!(f.send(json!({"v": -1.0, "w": 0.0})));
    assert_eq!(f.output(), (-100, -100));
    assert!(f.send(json!({"v": 0.6, "w": 0.0})));
    assert_eq!(f.output(), (60, 60));
    assert!(f.send(json!({"v": 0.0, "w": 1.0})));
    assert_eq!(f.output(), (100, -100)); // positive w turns right
}

#[test]
fn pwm_validation_and_floor() {
    for (floor, ceiling) in [(-1, 100), (100, 100), (101, 100), (0, 256), (0, 0)] {
        assert!(ControlState::new(false, floor, ceiling, None, None, None).is_err());
    }
    let state = ControlState::new(false, 70, 110, None, None, None).unwrap();
    let status = state.status(false);
    assert_eq!(status["min_pwm"], 70);
    assert_eq!(status["max_pwm"], 110);
    assert_eq!(state.output(), (0, 0, 90));
    // floor still stops when forward-blocked
    let f = fixture();
    f.state.update_range(10.0);
    assert!(f.send(json!({"v": 0.5})));
    assert_eq!(f.output(), (0, 0));
}

#[test]
fn camera_guard() {
    let ready = Arc::new(Mutex::new(true));
    let r = ready.clone();
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let state = ControlState::new(
        false, 0, 100,
        Some(Arc::new(move || *r.lock().unwrap())),
        None,
        Some(Arc::new(move || *n.lock().unwrap())),
    ).unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    state.update_range(100.0);
    let f = Fixture { state, session, seq: std::cell::Cell::new(0), now };
    f.send(json!({"deadman": false}));
    assert!(f.send(json!({"v": -0.5})));
    assert_eq!(f.output(), (-50, -50));
    *ready.lock().unwrap() = false;
    assert_eq!(f.output(), (0, 0));
    assert_eq!(f.state.inner.lock().unwrap().reason, "camera_stale");
    assert!(f.state.inner.lock().unwrap().requires_release);
    *ready.lock().unwrap() = true;
    assert!(!f.send(json!({})));
    f.send(json!({"deadman": false}));
    assert!(f.send(json!({})));
}
