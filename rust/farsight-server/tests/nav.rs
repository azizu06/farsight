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
    let service = NavService::new(mapping.clone(), 0.45, 3.0, 0.7, 0.8, 0.15, None).unwrap();
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
    // No bridge connected: nav_object's fallback else-branch emits
    // state "unavailable", backend null, "Nav2 bridge not connected".
    assert!(!service.available());
    let status = service.status_object();
    assert_eq!(status["state"], json!("unavailable"));
    assert_eq!(status["backend"], Value::Null);
    assert_eq!(status["message"], json!("Nav2 bridge not connected"));
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
        )
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
    let service = NavService::new(mapping, 0.45, 3.0, 0.7, 0.8, 0.15, None).unwrap();
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
