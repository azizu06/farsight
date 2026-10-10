//! Port of jetson/control/tests/test_autonomy.py (policy tests).

use farsight_server::autonomy::Autonomy;
use serde_json::{json, Value};

fn detection(x: f64, identifier: i64, age_ms: f64) -> Value {
    json!({
        "fresh": true,
        "error": null,
        "age_ms": age_ms,
        "boxes": [{"id": identifier, "cls": "person", "x": x, "w": 0.2}],
    })
}

fn empty() -> Value {
    json!({"fresh": true, "error": null, "age_ms": 0, "boxes": []})
}

fn eq(got: (f64, f64), want: (f64, f64)) {
    assert_eq!(got, want, "expected {want:?}, got {got:?}");
}

#[test]
fn seek_centres_tapers_and_stops_at_sixty() {
    let mut p = Autonomy::new();
    p.select("seek", Some(7), 10.0);
    let (v, w) = p.step(10.0, Some(160.0), &detection(0.7, 7, 0.0));
    assert!(v > 0.0 && w > 0.0); // target right -> right turn
    let (near, left) = p.step(10.1, Some(80.0), &detection(0.1, 7, 0.0));
    assert!(near < v && left < 0.0);
    eq(p.step(10.2, Some(60.0), &detection(0.4, 7, 0.0)), (0.0, 0.0));
    assert_eq!(p.phase, "arrived");
    eq(p.step(10.3, Some(150.0), &detection(0.4, 7, 0.0)), (0.0, 0.0));
}

#[test]
fn missing_target_latches_lost() {
    let mut p = Autonomy::new();
    p.select("seek", Some(7), 10.0);
    p.step(10.0, Some(160.0), &detection(0.4, 7, 0.0));
    eq(p.step(10.5, Some(160.0), &empty()), (0.0, 0.0));
    assert_eq!(p.phase, "target_missing");
    eq(p.step(11.0, Some(160.0), &empty()), (0.0, 0.0));
    assert_eq!(p.phase, "target_lost");
    eq(p.step(11.1, Some(160.0), &detection(0.4, 7, 0.0)), (0.0, 0.0));
}

#[test]
fn stale_and_error_snapshots_cannot_steer() {
    for snapshot in [
        json!({"fresh": false, "error": null, "age_ms": 0, "boxes": [{"id":7,"cls":"person","x":0.4,"w":0.2}]}),
        json!({"fresh": true, "error": "failed", "age_ms": 0, "boxes": [{"id":7,"cls":"person","x":0.4,"w":0.2}]}),
        detection(0.4, 7, 1000.0),
        detection(0.4, 8, 0.0),
    ] {
        let mut p = Autonomy::new();
        p.select("seek", Some(7), 10.0);
        eq(p.step(10.0, Some(120.0), &snapshot), (0.0, 0.0));
    }
}

#[test]
fn explore_turns_on_obstacle_and_timeout_then_hands_to_seek() {
    for (trigger_time, range_cm) in [(10.1, 65.0), (14.0, 120.0)] {
        let mut p = Autonomy::new();
        p.choose_turn = Box::new(|| -1.0);
        p.select("explore", None, 10.0);
        eq(p.step(10.0, Some(120.0), &empty()), (0.25, 0.0));
        eq(p.step(trigger_time, Some(range_cm), &empty()), (0.0, -0.35));
        assert_eq!(p.phase, "turning");
        eq(p.step(trigger_time + 1.01, Some(120.0), &empty()), (0.25, 0.0));
        eq(p.step(trigger_time + 1.1, Some(120.0), &detection(0.4, 7, 0.0)), (0.3, 0.0));
        assert_eq!((p.mode.as_str(), p.target_id), ("seek", Some(7)));
        p.select("explore", None, trigger_time + 1.2); // operator heartbeat
        assert_eq!(p.mode, "seek");
    }
}

#[test]
fn unknown_range_and_estop_stop_motion() {
    for mode in ["explore", "seek"] {
        for cm in [None, Some(f64::NAN), Some(0.0), Some(20.0)] {
            let mut p = Autonomy::new();
            p.select(mode, if mode == "seek" { Some(7) } else { None }, 10.0);
            eq(p.step(10.0, cm, &detection(0.7, 7, 0.0)), (0.0, 0.0));
        }
    }
}

#[test]
fn malformed_person_box_is_not_a_target() {
    for (x, w) in [(f64::NAN, 0.2), (0.2, -1.0), (0.9, 0.5)] {
        let mut p = Autonomy::new();
        p.select("seek", Some(7), 10.0);
        let mut snapshot = detection(x, 7, 0.0);
        snapshot["boxes"][0]["w"] = json!(w);
        eq(p.step(10.0, Some(120.0), &snapshot), (0.0, 0.0));
    }
    // non-numeric x
    let mut p = Autonomy::new();
    p.select("seek", Some(7), 10.0);
    let mut snapshot = detection(0.4, 7, 0.0);
    snapshot["boxes"][0]["x"] = json!(".4");
    eq(p.step(10.0, Some(120.0), &snapshot), (0.0, 0.0));
}
