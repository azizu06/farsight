//! Port of jetson/tests/test_mission_report.py (#80 telemetry).

use farsight_server::camera::CameraFrame;
use farsight_server::mission::Mission;
use serde_json::{json, Value};

fn frame(frame_id: i64, source: &str) -> CameraFrame {
    CameraFrame {
        jpeg: vec![0xFF, 0xD8, 0xFF, 0xD9],
        received: 0.0,
        frame_id,
        source: source.to_string(),
        capture_verified: false,
        capture_binding: None,
    }
}

fn boxv(track: i64, cls: &str) -> Value {
    json!({"id": track, "cls": cls, "conf": 0.9, "x": 0.4, "y": 0.2, "w": 0.2, "h": 0.6})
}

fn packet(range_cm: i64, estop: bool, reason: &str, rover: bool) -> Value {
    json!({
        "type": "control_status",
        "session_id": "s",
        "range_cm": range_cm,
        "estop": estop,
        "reason": reason,
        "rover": rover,
        "requires_release": false,
    })
}

#[test]
fn scripted_drive_estop_and_release_summarise() {
    let dir = std::env::temp_dir().join(format!("mission-test-{}", std::process::id()));
    let mission = Mission::new(Some(dir.clone()), false).unwrap();
    mission.observe_control(&packet(120, false, "active", true), None);
    mission.detect(&json!({"boxes": [boxv(1, "person")]}), &frame(1, "USB CAMERA"));
    mission.observe_control(&packet(18, true, "forward_blocked", true), None);
    mission.observe_control(&packet(18, true, "forward_blocked", true), None);
    mission.observe_control(&packet(60, false, "deadman_released", true), None);
    mission.observe_control(&packet(60, false, "serial_disconnected", false), None);
    mission.observe_control(&packet(60, false, "release_required", true), None);
    mission.observe_control(&packet(60, false, "active", true), Some(true));
    mission.observe_control(&packet(60, false, "active", true), Some(false));
    mission.detect(
        &json!({"boxes": [boxv(1, "person"), boxv(2, "dog")]}),
        &frame(2, "USB CAMERA"),
    );
    let report = mission.report();
    let _ = std::fs::remove_dir_all(&dir);

    let summary = &report["summary"];
    assert_eq!(summary["min_range_cm"], 18);
    assert_eq!(summary["estop_count"], 1);
    assert_eq!(summary["stop_reasons"]["forward_blocked"]["count"], 1);
    assert!(
        summary["stop_reasons"]["forward_blocked"]["first_ts"].as_f64()
            <= summary["stop_reasons"]["forward_blocked"]["last_ts"].as_f64()
    );
    assert_eq!(summary["lost"]["rover_link"]["count"], 1);
    assert!(!summary["lost"]["rover_link"]["intervals"][0]["end"].is_null());
    assert_eq!(summary["lost"]["camera"]["count"], 1);
    assert!(summary["lost"]["camera"]["intervals"][0]["end"].is_null());
    assert_eq!(summary["detections_by_class"], json!({"person": 1, "dog": 1}));

    let person = report["encounters"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["id"] == 1)
        .cloned()
        .unwrap();
    // Range when first seen, not the latest.
    assert_eq!(person["first_range_cm"], 120);
    assert!(person["first_seen"].as_f64() <= person["last_seen"].as_f64());
    let kinds: Vec<&str> = report["timeline"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|e| e["kind"].as_str())
        .collect();
    for kind in ["detection", "estop", "stop", "rover_link_lost", "rover_link_restored", "camera_lost"] {
        assert!(kinds.contains(&kind), "missing {kind} in {kinds:?}");
    }
}

#[test]
fn real_control_status_packets_drive_the_summary() {
    let state = farsight_server::control::ControlState::new(
        true, 0, 100, None, None, None, None, false, true,
    )
    .unwrap();
    let mission = Mission::new(None, false).unwrap();
    state.set_connected(true, None);
    let session = state.begin().unwrap();
    state.update_range(18.0);
    mission.observe_control(&state.status(false), None);
    assert!(state.accept(
        &json!({"session_id": session, "seq": 0, "deadman": false}),
        &session
    ));
    mission.observe_control(&state.status(false), None);
    let summary = mission.report()["summary"].clone();
    assert_eq!(summary["min_range_cm"], 18);
    assert_eq!(summary["estop_count"], 1);
    assert!(!summary["stop_reasons"]["deadman_released"].is_null());
}

#[test]
fn startup_without_a_link_is_not_a_loss() {
    let mission = Mission::new(None, false).unwrap();
    let mut p = packet(100, false, "rover_disabled", false);
    p["session_id"] = Value::Null;
    mission.observe_control(&p, Some(false));
    let lost = &mission.report()["summary"]["lost"];
    for kind in ["rover_link", "controller", "camera"] {
        assert_eq!(lost[kind]["count"], 0, "{kind}");
    }
}

#[test]
fn memory_stays_bounded_after_many_packets() {
    let mission = Mission::new(None, false).unwrap();
    let reasons = ["forward_blocked", "deadman_released", "lease_expired", "active"];
    for i in 0..10_000u64 {
        let mut p = packet(
            18 + (i % 300) as i64,
            i % 3 == 0,
            if i % 11 == 0 { "odd" } else { reasons[(i % 4) as usize] },
            i % 5 != 0,
        );
        if i % 11 == 0 {
            p["reason"] = json!(format!("odd_{i}"));
        }
        mission.observe_control(&p, Some(i % 9 != 0));
    }
    let report = mission.report();
    assert_eq!(report["summary"]["samples"], 10_000);
    assert!(report["timeline"].as_array().unwrap().len() <= 300);
    assert!(report["summary"]["stop_reasons"].as_object().unwrap().len() <= 64);
    for lost in report["summary"]["lost"].as_object().unwrap().values() {
        assert!(lost["intervals"].as_array().unwrap().len() <= 101);
    }
    assert!(serde_json::to_string(&report).unwrap().len() < 512 * 1024);
}
