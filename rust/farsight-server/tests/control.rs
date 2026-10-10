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
    let state = ControlState::new(false, 0, 100, None, None, Some(clock), None, false, true, "servo", None).unwrap();
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
        assert!(ControlState::new(false, floor, ceiling, None, None, None, None, false, true, "servo", None).is_err());
    }
    let state = ControlState::new(false, 70, 110, None, None, None, None, false, true, "servo", None).unwrap();
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
        None,
        false, true, "servo", None,
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

// Port of ScanStartRaceTests (#72): a zero-stick teleop heartbeat carrying a
// permit issued before begin_scan races POST /scan and must renew the scan.
#[test]
fn scan_race_zero_teleop_heartbeat_keeps_scan() {
    let f = fixture();
    assert!(f.send(json!({"v": 0.0, "w": 0.0})));
    let permit = f.state.status(true)["permit"].as_str().unwrap().to_string();
    let token = f.state.begin_scan(Some(&f.session), Some(&permit)).unwrap();
    // The pre-POST heartbeat arriving late: teleop, zero sticks, same permit.
    assert!(f.send(json!({"v": 0.0, "w": 0.0, "permit": permit.clone(), "pan": 40})));
    assert!(f.state.scan_command(Some(token), 0.3, 0.0).is_ok());
    assert_eq!(f.state.status(true)["mode"], "scan");
    // pan is ignored on the racing heartbeat — scan keeps its own pan.
    assert_eq!(f.state.inner.lock().unwrap().pan, 0);
}

#[test]
fn scan_race_stick_input_still_cancels() {
    let f = fixture();
    assert!(f.send(json!({"v": 0.0, "w": 0.0})));
    let permit = f.state.status(true)["permit"].as_str().unwrap().to_string();
    let token = f.state.begin_scan(Some(&f.session), Some(&permit)).unwrap();
    f.send(json!({"v": 0.2, "permit": permit.clone()}));
    assert!(f.state.scan_command(Some(token), 0.3, 0.0).is_err());
    assert_ne!(f.state.status(true)["mode"], "scan");
}

#[test]
fn scan_race_release_or_stale_view_still_cancels() {
    for values in [json!({"deadman": false}), json!({"video_age_ms": 501}), json!({"video_fresh": false})] {
        let f = fixture();
        assert!(f.send(json!({"v": 0.0, "w": 0.0})));
        let permit = f.state.status(true)["permit"].as_str().unwrap().to_string();
        let token = f.state.begin_scan(Some(&f.session), Some(&permit)).unwrap();
        f.send({ let mut v = values.clone(); v["permit"] = json!(permit.clone()); v });
        assert!(f.state.scan_command(Some(token), 0.3, 0.0).is_err());
        assert!(!f.send(json!({"mode": "scan", "permit": permit.clone()})));
    }
}

#[test]
fn scan_race_teleop_heartbeat_after_scan_mode_still_cancels() {
    let f = fixture();
    assert!(f.send(json!({"v": 0.0, "w": 0.0})));
    let permit = f.state.status(true)["permit"].as_str().unwrap().to_string();
    let token = f.state.begin_scan(Some(&f.session), Some(&permit)).unwrap();
    // Client properly enters scan mode...
    assert!(f.send(json!({"mode": "scan", "v": 0.0, "w": 0.0, "permit": permit.clone()})));
    assert_eq!(f.state.status(true)["mode"], "scan");
    // ...then a teleop heartbeat is a real override again.
    f.send(json!({"v": 0.0, "w": 0.0, "permit": permit.clone()}));
    assert!(f.state.scan_command(Some(token), 0.3, 0.0).is_err());
}

// ---- ControlModeTests (9a3b8b8): velocity + calibrate drive modes ----

fn make_profile() -> farsight_server::drive_profile::DriveProfile {
    farsight_server::drive_profile::DriveProfile::from_dict(&json!({
        "version": 1,
        "max_pwm": 100,
        "min_pwm": {"left": 24, "right": 30},
        "linear": {"pwm": [40, 60, 80, 100], "mps": [0.16, 0.29, 0.42, 0.54]},
        "angular": {"pwm": [40, 100], "radps": [1.4, 4.7]},
        "half_track_m": 0.115,
        "trim": {"left": 1.03, "right": 0.97},
        "camera_yaw_offset_rad": -0.35,
    })).unwrap()
}

fn mode_fixture(
    drive_profile: Option<farsight_server::drive_profile::DriveProfile>,
    allow_calibration: bool,
    min_pwm: i64,
) -> Fixture {
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let state = ControlState::new(
        false, min_pwm, 100, None, None, Some(clock), drive_profile, allow_calibration, true, "servo", None,
    ).unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    state.update_range(150.0);
    let f = Fixture { state, session, seq: std::cell::Cell::new(0), now };
    f.send(json!({"deadman": false, "v": 0.0, "w": 0.0, "pan": 0}));
    f
}

#[test]
fn drive_default_rejects_new_modes() {
    let f = mode_fixture(None, false, 0);
    assert!(!f.send(json!({"mode": "velocity", "v_mps": 0.2, "w_radps": 0.0})));
    assert_eq!(f.state.inner.lock().unwrap().reason, "unsupported_mode");
    assert_eq!(f.output(), (0, 0));
    f.send(json!({"deadman": false}));
    assert!(!f.send(json!({"mode": "calibrate", "left_pwm": 40, "right_pwm": 40})));
    assert_eq!(f.state.inner.lock().unwrap().reason, "unsupported_mode");
    let status = f.state.status(false);
    assert_eq!(status["drive_profile"], false);
    assert_eq!(status["calibration_allowed"], false);
    assert_eq!(status["drive_mode"], "normalized");
}

#[test]
fn drive_teleop_pwm_identical_with_profile_loaded() {
    let f = mode_fixture(Some(make_profile()), false, 0);
    assert!(f.send(json!({"v": 0.5, "w": 0.0})));
    assert_eq!(f.output(), (50, 50));
}

#[test]
fn drive_velocity_mode_uses_profile() {
    let profile = make_profile();
    let expected = profile.wheel_pwm(0.3, 0.4, Some(100));
    let f = mode_fixture(Some(profile), false, 0);
    assert!(f.send(json!({"mode": "velocity", "v_mps": 0.3, "w_radps": 0.4})));
    let (left, right, pan) = f.state.output();
    assert_eq!((left, right), expected);
    assert_eq!(pan, 90);
    assert_eq!(f.state.status(false)["drive_mode"], "velocity");
}

#[test]
fn drive_velocity_keeps_every_safety_gate() {
    let f = mode_fixture(Some(make_profile()), false, 0);
    assert!(f.send(json!({"mode": "velocity", "v_mps": 0.3, "w_radps": 0.0})));
    f.state.update_range(10.0);
    assert_eq!(f.output(), (0, 0));
    assert_eq!(f.state.inner.lock().unwrap().reason, "forward_blocked");
    f.state.update_range(150.0);
    *f.now.lock().unwrap() += 0.3; // lease expired
    assert_eq!(f.output(), (0, 0));
    assert!(f.state.inner.lock().unwrap().requires_release);
    f.send(json!({"deadman": false}));
    // Non-finite / out-of-range velocities rejected like any bad command.
    assert!(!f.send(json!({"mode": "velocity", "v_mps": f64::NAN, "w_radps": 0.0})));
    f.send(json!({"deadman": false}));
    assert!(!f.send(json!({"mode": "velocity", "v_mps": 9.0, "w_radps": 0.0})));
}

#[test]
fn drive_velocity_spin_not_blocked_by_missing_range() {
    let f = mode_fixture(Some(make_profile()), false, 0);
    f.state.update_range(0.0); // clears range_cm like Python's `state.range_cm = None`
    assert!(f.send(json!({"mode": "velocity", "v_mps": 0.0, "w_radps": 1.0})));
    let (left, right, _) = f.state.output();
    assert!(left < 0);
    assert!(right > 0);
}

#[test]
fn proximity_stop_off_lets_forward_run_without_range() {
    // --proximity-stop off: the ultrasonic gate never fires (nav runs steer
    // around obstacles); the pan mount rule still applies.
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let state = ControlState::new(false, 0, 100, None, None, Some(clock), None, false, false, "servo", None).unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    let f = Fixture { state, session, seq: std::cell::Cell::new(0), now };
    f.send(json!({"deadman": false}));
    // No range at all: forward drives, estop is false.
    assert!(f.send(json!({})));
    assert_eq!(f.state.status(false)["estop"], false);
    assert_eq!(f.output(), (50, 50));
    // A pan beyond the mount limit still blocks.
    f.send(json!({"pan": 20}));
    assert_eq!(f.state.status(false)["estop"], true);
    assert_eq!(f.output(), (0, 0));
}

#[test]
fn drive_calibrate_raw_pwm_and_bounds() {
    let f = mode_fixture(None, true, 20);
    assert!(f.send(json!({"mode": "calibrate", "left_pwm": 33, "right_pwm": 0})));
    // min_pwm floor does not rewrite raw values.
    assert_eq!(f.output(), (33, 0));
    assert_eq!(f.state.status(false)["drive_mode"], "calibrate");
    assert!(!f.send(json!({"mode": "calibrate", "left_pwm": 101, "right_pwm": 0})));
    assert_eq!(f.output(), (0, 0));
}

// --- NoPanMountTests: --pan-mount none; the headset pans digitally and
// nothing else about the gates changes (control/tests/test_control.py). ---

fn no_pan_fixture() -> Fixture {
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let state =
        ControlState::new(false, 0, 100, None, None, Some(clock), None, false, false, "none", None)
            .unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    let f = Fixture { state, session, seq: std::cell::Cell::new(0), now };
    f.send(json!({"deadman": false}));
    f
}

#[test]
fn no_pan_commanded_pan_never_blocks_forward_or_moves_a_servo() {
    let f = no_pan_fixture();
    assert!(f.send(json!({"pan": 40})));
    assert_eq!(f.state.output(), (50, 50, 90));
    assert!(f.send(json!({"pan": -80, "v": 0.5, "w": 0.25})));
    let (left, right, pan) = f.state.output();
    assert!(left > right);
    assert_eq!(pan, 90);
    assert_eq!(f.state.status(false)["pan_deg"], 90);
    assert_eq!(f.state.status(false)["estop"], false);
}

#[test]
fn no_pan_is_still_validated_and_every_stop_remains() {
    let mut f = no_pan_fixture();
    assert!(!f.send(json!({"pan": 90}))); // outside -80..80 is still invalid
    assert_eq!(f.state.status(false)["reason"], "invalid_command");
    f.send(json!({"deadman": false}));
    assert!(f.send(json!({"pan": 40})));
    *f.now.lock().unwrap() += 0.26;
    assert_eq!(f.state.output(), (0, 0, 90));
    assert_eq!(f.state.status(false)["reason"], "lease_expired");
    f.send(json!({"deadman": false}));
    assert!(f.send(json!({"pan": 40})));
    f.send(json!({"deadman": false}));
    assert_eq!(f.state.output(), (0, 0, 90));
    f.state.camera_ready = Some(Arc::new(|| false));
    assert!(!f.send(json!({"pan": 40})));
    assert_eq!(f.state.output(), (0, 0, 90));
}

#[test]
fn no_pan_unknown_mount_is_rejected() {
    assert!(ControlState::new(false, 0, 100, None, None, None, None, false, false, "gimbal", None).is_err());
}

// Port of test_autonomy.py::test_autonomy_ceiling_keeps_autonomous_speed_while_teleop_uses_full_range (#99).
#[test]
fn autonomy_ceiling_keeps_autonomous_speed_while_teleop_uses_full_range() {
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let snapshot = json!({"fresh": true, "error": null, "age_ms": 0, "boxes": [{"id": 7, "cls": "person", "x": 0.4, "w": 0.2}]});
    let state = ControlState::new(
        false, 70, 255, None,
        Some(Arc::new(move || snapshot.clone())),
        Some(clock), None, false, true, "servo", Some(100),
    ).unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    let f = Fixture { state, session, seq: std::cell::Cell::new(0), now };
    f.send(json!({"deadman": false}));
    f.send(json!({"mode": "seek", "target_id": 7, "v": 0.0}));
    f.state.update_range(120.0);
    assert_eq!(f.output(), (79, 79)); // 70 + (100 - 70) * 0.3
    f.send(json!({"v": -0.5})); // stick override: teleop gets the full ceiling
    assert_eq!(f.output(), (-162, -162)); // 70 + (255 - 70) * 0.5
    assert_eq!(f.state.status(true)["max_pwm"], 255);
}

#[test]
fn autonomy_ceiling_validates_bounds_and_defaults_to_max() {
    for bad in [256, 70, 60] {
        assert!(
            ControlState::new(false, 70, 255, None, None, None, None, false, true, "servo", Some(bad)).is_err(),
            "autonomy_max_pwm={bad} must be rejected"
        );
    }
    let s = ControlState::new(false, 0, 200, None, None, None, None, false, true, "servo", None).unwrap();
    assert_eq!(s.autonomy_max_pwm, 200); // default: unchanged behaviour
}
