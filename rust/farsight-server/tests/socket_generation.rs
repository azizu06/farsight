// Port of d25aed0's core fix semantics (state.py / test_nav.py /
// test_scan.py): a socket generation binds every producer to the socket
// lifetime that accepted it — a cancelled socket worker cannot publish
// into a later session even when (session_id, map_epoch) is reused, and a
// visual map reset does NOT invalidate the live socket.

use farsight_server::control::ControlState;
use farsight_server::geometry::IDENTITY;
use farsight_server::phone::RgbdFrame;
use farsight_server::state::MapState;
use serde_json::{json, Value};

fn frame(id: i64, capture: f64) -> RgbdFrame {
    let header = json!({
        "version": 1, "type": "frame",
        "session_id": "phone", "map_epoch": 1,
        "frame_id": id, "t_capture": capture,
        "tracking": "normal",
        "transform": farsight_server::geometry::flatten_f(&IDENTITY).to_vec(),
    });
    RgbdFrame {
        header,
        rgb: vec![0; 16 * 16 * 3],
        depth: vec![2.0; 16 * 16],
        confidence: vec![2; 16 * 16],
        calibration: farsight_server::geometry::Calibration {
            width: 16,
            height: 16,
            fx: 100.0,
            fy: 100.0,
            cx: 8.0,
            cy: 8.0,
        },
        transform: IDENTITY,
        jpeg: vec![1, 2, 3],
        depth_width: 16,
        depth_height: 16,
    }
}

#[test]
fn start_session_returns_and_bumps_socket_generation() {
    let m = MapState::new(false);
    let identity = ("phone".into(), 1);
    let g1 = m.start_session(identity.clone(), "iPhone".into());
    let g2 = m.start_session(identity, "iPhone".into());
    assert_eq!(g2, g1 + 1, "every socket session bumps the generation");
}

#[test]
fn clear_preserves_socket_generation_but_disconnect_bumps_it() {
    let m = MapState::new(false);
    let identity = ("phone".into(), 1);
    let g = m.start_session(identity.clone(), "iPhone".into());
    m.clear(); // a visual map reset preserves the live socket
    assert_eq!(m.inner.lock().unwrap().socket_generation, g);
    m.disconnect();
    assert_eq!(m.inner.lock().unwrap().socket_generation, g + 1);
}

#[test]
fn accepted_frame_is_stamped_with_generation_and_sensor_key() {
    let m = MapState::new(false);
    let g = m.start_session(("phone".into(), 1), "iPhone".into());
    let mut f = frame(1, 1.0);
    // A producer must not be able to smuggle its own bindings.
    f.header["_mapping_socket_generation"] = json!(999);
    f.header["_mapping_sensor_key"] = json!(["forged", 9, 9, 9, 9]);
    m.process_permits(&mut f, None, Some(g)).unwrap();
    assert_eq!(f.header["_mapping_socket_generation"], json!(g));
    assert_eq!(
        f.header["_mapping_sensor_key"],
        json!(["phone", 1, g, 1, 1.0])
    );
}

#[test]
fn stale_socket_generation_cannot_publish() {
    let m = MapState::new(false);
    let identity = ("phone".into(), 1);
    let g1 = m.start_session(identity.clone(), "iPhone".into());
    let g2 = m.start_session(identity, "iPhone".into());
    // A cancelled worker finishing on the OLD generation is dropped.
    let mut f = frame(1, 1.0);
    m.process_permits(&mut f, None, Some(g1)).unwrap();
    assert_eq!(m.inner.lock().unwrap().frames, 0);
    assert!(f.header.get("_mapping_sensor_key").is_none());
    let mut f2 = frame(1, 1.0);
    m.process_permits(&mut f2, None, Some(g2)).unwrap();
    assert_eq!(m.inner.lock().unwrap().frames, 1);
}

#[test]
fn stale_pose_heartbeat_is_dropped_and_received_at_drives_age() {
    let m = MapState::new(false);
    let identity = ("phone".into(), 1);
    let g1 = m.start_session(identity.clone(), "iPhone".into());
    let g2 = m.start_session(identity, "iPhone".into());
    let header = json!({
        "session_id": "phone", "map_epoch": 1, "t_capture": 5.0, "tracking": "normal",
    });
    // Old generation: dropped entirely.
    m.observe_pose(&header, &IDENTITY, Some(1.0), Some(g1)).unwrap();
    assert_eq!(m.inner.lock().unwrap().last_received, 0.0);
    // Current generation: last_received comes from received_at, not now —
    // CPU processing must not renew an old capture's age.
    let receipt = farsight_server::state::now_monotonic() - 0.42;
    m.observe_pose(&header, &IDENTITY, Some(receipt), Some(g2)).unwrap();
    assert_eq!(m.inner.lock().unwrap().last_received, receipt);
}

#[test]
fn scan_cancel_errors_report_first_stop_reason() {
    let control = ControlState::new(
        true, 0, 255, None, None, None, None, false, false, "none", None, None, false,
    )
    .unwrap();
    control.set_connected(true, None);
    let session = control.begin().unwrap();
    let hb = |seq: i64| {
        json!({
            "session_id": session, "seq": seq, "deadman": true,
            "permit": control.status(true)["permit"],
            "v": 0.0, "w": 0.0, "pan": 0, "mode": "teleop",
            "video_fresh": true, "video_age_ms": 0,
        })
    };
    let mut release = hb(1);
    release["deadman"] = json!(false);
    control.accept(&release, &session); // ordered release handshake
    assert!(control.accept(&hb(2), &session));
    let permit = control.status(true)["permit"].as_str().unwrap().to_string();
    let token = control.begin_scan(Some(&session), Some(&permit)).unwrap();
    // The first reason that cancelled the scan is retained.
    control.stop("safety_stop", false);
    assert_eq!(
        control.scan_command(Some(token), 0.3, 0.0).unwrap_err(),
        "Scan cancelled: operator lease, override, or safety stop (safety_stop)"
    );
    // An unknown-initiator cancel still reports "(unknown)".
    let mut release = hb(3);
    release["deadman"] = json!(false);
    control.accept(&release, &session);
    assert!(control.accept(&hb(4), &session));
    let permit = control.status(true)["permit"].as_str().unwrap().to_string();
    let token = control.begin_scan(Some(&session), Some(&permit)).unwrap();
    control.inner.lock().unwrap().scan_token = None; // cancelled with no recorded reason
    assert_eq!(
        control.scan_command(Some(token), 0.3, 0.0).unwrap_err(),
        "Scan cancelled: operator lease, override, or safety stop (unknown)"
    );
}
