//! NavService seam tests — subset port of tests/test_nav.py covering the
//! wire contract (nav object fields, goal/cancel/status) and the offline
//! behaviour when no Nav2 bridge is connected.

use farsight_server::geometry::{Calibration, IDENTITY};
use farsight_server::nav::{self, GoalError, NavService, NAV_MODES, PROTOCOL};
use farsight_server::phone::RgbdFrame;
use farsight_server::state::MapState;
use serde_json::{json, Value};
use std::sync::Arc;

fn frame(w: usize, h: usize) -> RgbdFrame {
    RgbdFrame {
        header: json!({}),
        rgb: vec![0; w * h * 3],
        depth: vec![1.0; w * h],
        confidence: vec![4; w * h],
        calibration: Calibration::new(160, 120, 135.0, 135.0, 79.5, 59.5).unwrap(),
        transform: IDENTITY,
        jpeg: Vec::new(),
        depth_width: w,
        depth_height: h,
    }
}

fn nav() -> (Arc<NavService>, Arc<MapState>) {
    let mapping = Arc::new(MapState::new(true));
    let service = NavService::new(mapping.clone(), 0.45, 3.0, 0.7, 0.8, 0.15, None, None).unwrap();
    (Arc::new(service), mapping)
}

#[test]
fn world_odom_roundtrip() {
    // Python: world_to_odom(x, z) = (-z, -x); odom_to_world undoes it.
    for (x, z) in [(0.0, 0.0), (1.25, -0.5), (-3.0, 4.2)] {
        let (ox, oy) = nav::world_to_odom(x, z);
        assert_eq!((ox, oy), (-z, -x));
        let (wx, wz) = nav::odom_to_world(ox, oy);
        assert!((wx - x).abs() < 1e-9 && (wz - z).abs() < 1e-9);
    }
}

#[test]
fn simplify_limits_points() {
    // Cap of 200 path points / 50 frontiers lives behind simplify().
    let pts: Vec<(f64, f64)> = (0..500).map(|i| (i as f64, 0.0)).collect();
    assert!(nav::simplify(&pts, 0.0, 200).len() <= 200);
    assert!(nav::simplify(&pts, 0.0, 50).len() <= 50);
    // A straight line simplifies to its endpoints.
    assert_eq!(nav::simplify(&pts, 0.01, 200).len(), 2);
}

#[test]
fn depth_roundtrip() {
    let f = frame(8, 6);
    let packet = nav::encode_depth(&f, -0.15, "SIMULATED", 12.0, 3, 160, 6.0);
    assert!(packet.len() > 4);
    let len = u32::from_le_bytes(packet[0..4].try_into().unwrap()) as usize;
    assert_eq!(packet.len(), 4 + len + 8 * 6 * 2);
    let (header, mm) = nav::decode_depth(&packet);
    assert_eq!(header["width"], json!(8));
    assert_eq!(header["height"], json!(6));
    // Python writes the float the caller passed (round(..., 1)).
    assert_eq!(header["age_ms"], json!(12.0));
    assert_eq!(header["generation"], json!(3));
    assert_eq!(mm.len(), 48);
    assert!((mm[0] - 1.0).abs() < 0.001);
}

#[test]
fn depth_stride_and_validity() {
    // >160 px width strides; non-finite/out-of-range/low-conf depths -> 0.
    let mut f = frame(320, 2);
    f.depth[0] = f64::NAN as f32;
    f.depth[1] = 0.05; // below the 0.15 m floor
    f.depth[2] = 9.0; // beyond max_range
    f.confidence[3] = 0;
    let packet = nav::encode_depth(&f, -0.15, "SIMULATED", 0.0, 1, 160, 6.0);
    let (header, mm) = nav::decode_depth(&packet);
    assert_eq!(header["width"], json!(160));
    // stride 2 samples cols 0,2,4,...: depth[0]=NaN ->0, depth[2]=9m ->0,
    // depth[4]=1m -> 1.0 in metres.
    assert_eq!(mm[0], 0.0);
    assert_eq!(mm[1], 0.0);
    assert!((mm[2] - 1.0).abs() < 0.001);
}

#[test]
fn empty_nav_shape() {
    let n = nav::empty_nav("unavailable", "msg");
    assert_eq!(n["state"], json!("unavailable"));
    assert_eq!(n["mode"], Value::Null);
    assert_eq!(n["goal"], Value::Null);
    assert_eq!(n["path"], json!([]));
    assert_eq!(n["frontiers"], json!([]));
    assert_eq!(n["backend"], Value::Null);
    assert_eq!(n["message"], json!("msg"));
}

#[test]
fn constants_match_contract() {
    assert_eq!(PROTOCOL, 1);
    assert!(NAV_MODES.contains(&"explore"));
    assert!(NAV_MODES.contains(&"seek"));
    assert!(NAV_MODES.contains(&"goto"));
}

#[test]
fn offline_bridge_unavailable() {
    let (service, _mapping) = nav();
    // No bridge connected (#115): state "unavailable", backend null,
    // and the hold-still message — Nav2 is the only navigation.
    assert!(!service.available());
    let status = service.status_object();
    assert_eq!(status["state"], json!("unavailable"));
    assert_eq!(status["backend"], Value::Null);
    assert_eq!(status["message"], json!("Nav2 bridge not connected; holding still"));
}

#[test]
fn goal_validation() {
    let (service, _mapping) = nav();
    for (x, z) in [
        (f64::NAN, 0.0),
        (0.0, f64::INFINITY),
        (501.0, 0.0),
        (0.0, -600.0),
    ] {
        match service.set_goal(x, z, None, None) {
            Err(GoalError::Unprocessable(msg)) => {
                assert_eq!(msg, "Goal must be finite voxel-map metres")
            }
            _ => panic!("expected 422 for ({x}, {z})"),
        }
    }
}

#[test]
fn goal_refused_without_bridge() {
    // Attached to a control state but with no /ws/nav bridge connected.
    let (service, _mapping) = nav();
    let control = Arc::new(
        farsight_server::control::ControlState::new(
            false,
            0,
            100,
            None,
            None,
            None,
            None,
            false,
            true,
            "servo",
            None,
            Some(service.clone()),
        
            false,)
        .unwrap(),
    );
    service.attach(&control);
    match service.set_goal(1.0, -0.5, None, None) {
        Err(GoalError::Refused {
            nav: object,
            message,
        }) => {
            assert_eq!(
                message,
                "Navigation backend unavailable (Nav2 bridge not running)"
            );
            assert_eq!(object["state"], json!("unavailable"));
        }
        _ => panic!("expected refusal"),
    }
}

#[test]
fn goal_refused_without_control_when_unattached() {
    // Without attach() the refusal is the earlier "no rover control" link in
    // the Python refusal chain.
    let mapping = Arc::new(MapState::new(true));
    let service = NavService::new(mapping, 0.45, 3.0, 0.7, 0.8, 0.15, None, None).unwrap();
    match service.set_goal(1.0, 0.0, None, None) {
        Err(GoalError::Refused { message, .. }) => {
            assert_eq!(message, "No rover control on this server")
        }
        _ => panic!("expected refusal"),
    }
}

#[test]
fn cancel_returns_nav_object() {
    // No control attached: cancel() returns nav_object() -> "unavailable".
    let (service, _mapping) = nav();
    let out = service.cancel();
    assert_eq!(out["state"], json!("unavailable"));
    assert!(out["mode"].is_null());
    assert_eq!(out["path"], json!([]));
}

#[test]
fn bridge_status_updates_remote_state() {
    let (service, _mapping) = nav();
    service
        .receive(&json!({
            "type": "status",
            "state": "driving",
            "goal": {"x": 1.0, "z": -0.5},
            "path": [[0.1, 0.2], [0.3, 0.4]],
        }))
        .unwrap();
    service.receive(&json!({"type": "pong"})).unwrap();
    assert_eq!(
        service.receive(&json!({"type": "bogus"})),
        Err("Unknown bridge message type".into())
    );
}

#[test]
fn hello_carries_protocol_and_scaling() {
    let (service, _mapping) = nav();
    let hello = service.hello();
    assert_eq!(hello["type"], json!("hello"));
    assert_eq!(hello["version"], json!(1));
    assert_eq!(hello["speed_mps"], json!(0.45));
    assert_eq!(hello["turn_radps"], json!(3.0));
    assert_eq!(hello["max_v"], json!(0.7));
    assert_eq!(hello["max_w"], json!(0.8));
    assert_eq!(hello["frame"], json!("arkit"));
    assert!(hello["drive_profile"].is_null());
    assert!(hello["generation"].is_number());
}

#[test]
fn goal_stages_while_stopped_and_drives_only_on_a_new_press() {
    // #105: deadman never held — POST /nav/goal stages instead of 409, and only
    // a fresh deadman press carrying "goto" may drive it.
    use farsight_server::control::{Clock, ControlState};
    use std::sync::Mutex;
    let (service, _mapping) = nav();
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let control = Arc::new(
        ControlState::new(
            false, 0, 100, None, None, Some(clock), None, false, true, "servo", None,
            Some(service.clone()),
        
            false,)
        .unwrap(),
    );
    service.attach(&control);
    control.set_connected(true, None);
    let session = control.begin().unwrap();
    control.update_range(100.0);
    // Fresh measured sensors: a normal-tracking frame fused into the map,
    // then offered to the NavService depth observer.
    _mapping.start_session(("test".to_string(), 1), "synthetic fixture".into());
    let f = frame(8, 6);
    let mut f = f.clone();
    f.header = json!({
        "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
        "frame_id": 1, "t_capture": 1.0, "tracking": "normal",
        "transform": farsight_server::geometry::flatten_f(&IDENTITY).to_vec(),
    });
    _mapping.process(&mut f).unwrap();
    service.offer_frame(&f);
    service.connect_bridge_for_test();
    service
        .receive(&json!({"type": "status", "state": "idle", "goal_id": null}))
        .unwrap();
    let nav = match service.set_goal(1.0, -2.0, Some(&session), None) {
        Ok(nav) => nav,
        Err(GoalError::Refused { message, .. }) => panic!("staged goal refused: {message}"),
        Err(_) => panic!("unexpected set_goal error"),
    };
    assert!(nav["mode"].is_null());
    assert_eq!(nav["goal"], json!({"x": 1.0, "z": -2.0}));
    assert_eq!(service.intent()["active"], json!(false));
    service
        .receive(&json!({"type": "cmd_vel", "linear": 0.3, "angular": 0.0, "goal_id": 1, "generation": service.hello()["generation"]}))
        .unwrap();
    assert_eq!(control.output().0, 0); // staged: nothing moves without a press
    let status = control.status(true);
    let hb = |seq: i64, deadman: bool, mode: &str| {
        json!({
            "session_id": session, "permit": status["permit"], "seq": seq,
            "deadman": deadman, "v": 0.0, "w": 0.0, "pan": 0,
            "mode": mode, "video_fresh": true, "video_age_ms": 0,
        })
    };
    control.accept(&hb(1, false, "teleop"), &session); // ordered release
    assert!(control.accept(&hb(2, true, "goto"), &session));
    assert_eq!(control.output().0, 0); // pre-press cmd_vel is discarded
    service
        .receive(&json!({"type": "cmd_vel", "linear": 0.2, "angular": 0.0, "goal_id": 1, "generation": service.hello()["generation"]}))
        .unwrap();
    assert!(control.output().0 > 0);
}

#[test]
fn goal_refused_for_foreign_session_even_while_stopped() {
    // #105: a session_id naming another controller is 409 even without a lease.
    use farsight_server::control::{Clock, ControlState};
    use std::sync::Mutex;
    let (service, _mapping) = nav();
    let now = Arc::new(Mutex::new(10.0f64));
    let n = now.clone();
    let clock: Clock = Arc::new(move || *n.lock().unwrap());
    let control = Arc::new(
        ControlState::new(
            false, 0, 100, None, None, Some(clock), None, false, true, "servo", None,
            Some(service.clone()),
        
            false,)
        .unwrap(),
    );
    service.attach(&control);
    control.set_connected(true, None);
    let _session = control.begin().unwrap();
    control.update_range(100.0);
    // Fresh measured sensors: a normal-tracking frame fused into the map,
    // then offered to the NavService depth observer.
    _mapping.start_session(("test".to_string(), 1), "synthetic fixture".into());
    let f = frame(8, 6);
    let mut f = f.clone();
    f.header = json!({
        "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
        "frame_id": 1, "t_capture": 1.0, "tracking": "normal",
        "transform": farsight_server::geometry::flatten_f(&IDENTITY).to_vec(),
    });
    _mapping.process(&mut f).unwrap();
    service.offer_frame(&f);
    service.connect_bridge_for_test();
    service
        .receive(&json!({"type": "status", "state": "idle", "goal_id": null}))
        .unwrap();
    match service.set_goal(1.0, -2.0, Some("another-session"), None) {
        Err(GoalError::Refused { message, .. }) => {
            assert!(message.contains("session"), "{message}")
        }
        _ => panic!("expected refusal"),
    }
}

/// #110: the opt-in nav revision lane keeps path/frontiers out of most
/// statuses — first status carries them, changes coalesce to ≥0.5 s, a
/// 2 s refresh always emits, and lease/status fields are untouched
/// (mirrors jetson/tests/test_nav.py NavLaneTests).
#[test]
fn nav_lane_arrays_only_on_change_with_refresh() {
    use farsight_server::nav::NavLane;
    let mut lane = NavLane::new();
    let status = |revision: i64, path: Vec<serde_json::Value>| json!({
        "type": "control_status", "permit": "p",
        "nav": {"state": "driving", "goal": Value::Null, "path": path,
                "frontiers": [[1.0, 1.0]], "revision": revision}
    });
    let now = std::cell::Cell::new(0.0);
    // The first status always carries the arrays.
    let s = lane.apply(&status(1, vec![json!([0, 0])]), now.get());
    assert!(s["nav"].get("path").is_some());
    // Unchanged revision inside the window omits them; other fields stay.
    now.set(now.get() + 0.025);
    let lean = lane.apply(&status(1, vec![json!([0, 0])]), now.get());
    assert!(lean["nav"].get("path").is_none());
    assert!(lean["nav"].get("frontiers").is_none());
    assert_eq!(lean["permit"], "p");
    assert_eq!(lean["nav"]["revision"], 1);
    // Changed revision inside 0.5 s still omits (coalescing).
    now.set(now.get() + 0.1);
    assert!(lane.apply(&status(2, vec![json!([0, 1])]), now.get())["nav"].get("path").is_none());
    // Past 0.5 s the newest arrays win (coalesced).
    now.set(now.get() + 0.4);
    let changed = lane.apply(&status(3, vec![json!([0, 2])]), now.get());
    assert_eq!(changed["nav"]["path"], json!([[0, 2]]));
    assert_eq!(changed["nav"]["revision"], 3);
    // Refresh at 2 s emits the unchanged arrays.
    now.set(now.get() + 1.9);
    assert!(lane.apply(&status(3, vec![json!([0, 2])]), now.get())["nav"].get("path").is_none());
    now.set(now.get() + 0.2);
    assert!(lane.apply(&status(3, vec![json!([0, 2])]), now.get())["nav"].get("path").is_some());
    // A status without a nav object passes through untouched.
    let plain = json!({"type": "control_status"});
    assert_eq!(lane.apply(&plain, now.get()), plain);
}

// ---- #175: demo-autonomy sensor stalls pause instead of stopping ----

use farsight_server::control::{Clock as CtrlClock, ControlState as CtrlState};
use std::sync::Mutex as StdMutex;

fn pause_fixture(demo: bool) -> (
    Arc<NavService>,
    Arc<MapState>,
    Arc<CtrlState>,
    String,
    Arc<StdMutex<f64>>,
) {
    let (service, mapping) = nav();
    let now = Arc::new(StdMutex::new(10.0f64));
    let n = now.clone();
    let clock: CtrlClock = Arc::new(move || *n.lock().unwrap());
    let detections: farsight_server::control::Detections = Arc::new(|| json!([]));
    let control = Arc::new(
        CtrlState::new(
            false, 0, 100, None, Some(detections), Some(clock), None, false, true,
            "servo", None, Some(service.clone()), demo,
        )
        .unwrap(),
    );
    service.attach(&control);
    service.connect_bridge_for_test();
    control.set_connected(true, None);
    let session = control.begin().unwrap();
    control.update_range(100.0);
    (service, mapping, control, session, now)
}

fn feed_sensors(mapping: &MapState, service: &NavService, frame_id: i64) {
    // Fresh normal-tracking measured frame fused into the map, then offered to
    // the NavService depth observer (same recipe as the goto wire tests).
    mapping.start_session(("test".to_string(), 1), "synthetic fixture".into());
    let mut f = frame(8, 6);
    f.header = json!({
        "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
        "frame_id": frame_id, "t_capture": 1.0, "tracking": "normal",
        "transform": farsight_server::geometry::flatten_f(&IDENTITY).to_vec(),
    });
    mapping.process(&mut f).unwrap();
    service.offer_frame(&f);
}

fn explore_hb(session: &str, permit: &str, seq: i64) -> Value {
    json!({
        "session_id": session, "permit": permit, "seq": seq,
        "deadman": true, "v": 0.0, "w": 0.0, "pan": 0,
        "mode": "explore", "video_fresh": true, "video_age_ms": 0,
    })
}

#[test]
fn demo_autodrive_cruises_through_sensor_stalls() {
    let (_service, _mapping, control, session, now) = pause_fixture(true);
    // "Feeling of autodrive": engaged Explore with no pose must still move —
    // the demo driver cruises instead of pausing on nav_pose_lost.
    let permit = control.status(true)["permit"].clone();
    let mut release = explore_hb(&session, permit.as_str().unwrap(), 1);
    release["deadman"] = json!(false);
    release["mode"] = json!("teleop");
    control.accept(&release, &session); // ordered release: first press needs one
    assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), 2), &session));
    let (left, right, _) = control.output();
    assert!(left != 0 || right != 0, "autodrive must move, not stand");
    let st = control.status(false);
    assert_eq!(st["reason"], "demo_cruise");
    assert!(!st["requires_release"].as_bool().unwrap());
    // The stall continuing changes nothing — held heartbeats renew the
    // lease and the rover keeps cruising.
    *now.lock().unwrap() += 1.5;
    let permit = control.status(true)["permit"].clone();
    assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), 3), &session));
    let (left, right, _) = control.output();
    assert!(left != 0 || right != 0);
    let st = control.status(false);
    assert!(!st["requires_release"].as_bool().unwrap());
}

#[test]
fn demo_autodrive_never_stops_on_long_stalls() {
    let (_service, _mapping, control, session, now) = pause_fixture(true);
    let permit = control.status(true)["permit"].clone();
    let mut release = explore_hb(&session, permit.as_str().unwrap(), 1);
    release["deadman"] = json!(false);
    release["mode"] = json!("teleop");
    control.accept(&release, &session); // ordered release: first press needs one
    assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), 2), &session));
    control.output(); // would have started the old pause timer
    // Held heartbeats keep the lease alive far past the old 2 s pause
    // bound — the rover must still be moving, session intact.
    for (dt, seq) in [(1.2f64, 3i64), (1.2, 4)] {
        *now.lock().unwrap() += dt;
        let permit = control.status(true)["permit"].clone();
        assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), seq), &session));
        let (left, right, _) = control.output();
        assert!(left != 0 || right != 0, "autodrive must keep moving");
    }
    let st = control.status(false);
    assert_ne!(st["mode"], "teleop"); // the Explore session was never dropped
}

#[test]
fn demo_autodrive_escapes_when_boxed_in() {
    let (_service, _mapping, control, session, now) = pause_fixture(true);
    let permit = control.status(true)["permit"].clone();
    let mut release = explore_hb(&session, permit.as_str().unwrap(), 1);
    release["deadman"] = json!(false);
    release["mode"] = json!("teleop");
    control.accept(&release, &session); // ordered release: first press needs one
    assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), 2), &session));
    // Boxed in like the sketch: a wall 30 cm ahead. It must still translate —
    // an escape arc (v > 0 and w != 0), never a standstill.
    control.update_range(30.0);
    let (left, right, _) = control.output();
    assert!(left != 0 || right != 0, "boxed-in autodrive must still move");
    assert_ne!(left, right, "escape is an arc, not a straight push");
    assert_eq!(control.status(false)["reason"], "demo_escape");
    // The pocket holds for 7 s: the sweep direction must flip so a corner
    // or pillar cannot trap the arc forever.
    *now.lock().unwrap() += 7.0;
    control.update_range(30.0);
    let permit = control.status(true)["permit"].clone();
    assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), 3), &session));
    let (l2, r2, _) = control.output();
    assert_ne!(
        (l2 - r2).signum(),
        (left - right).signum(),
        "escape arc must reverse its sweep while the pocket holds"
    );
    // Range opens: back to a confident forward cruise.
    control.update_range(300.0);
    let (l3, r3, _) = control.output();
    assert!(l3 > 0 && r3 > 0);
    assert_eq!(control.status(false)["reason"], "demo_cruise");
}

#[test]
fn strict_mode_sensor_stall_stops_at_once() {
    let (_service, _mapping, control, session, _now) = pause_fixture(false);
    let permit = control.status(true)["permit"].clone();
    let mut release = explore_hb(&session, permit.as_str().unwrap(), 1);
    release["deadman"] = json!(false);
    release["mode"] = json!("teleop");
    control.accept(&release, &session); // ordered release: first press needs one
    assert!(control.accept(&explore_hb(&session, permit.as_str().unwrap(), 2), &session));
    control.output();
    let st = control.status(false);
    assert_eq!(st["reason"], "nav_pose_lost");
    assert!(st["requires_release"].as_bool().unwrap());
}
