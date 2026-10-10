//! Opt-in fusion bypass (new offload architecture): pose + depth still
//! feed Nav2 and the camera still serves inference/control, but RGB-D
//! fusion is skipped — geometry arrives via the phone ARKit lane.

use farsight_server::geometry::Calibration;
use farsight_server::phone::RgbdFrame;
use farsight_server::state::MapState;
use serde_json::json;
use std::sync::Arc;

fn frame() -> RgbdFrame {
    let (w, h) = (8usize, 6usize);
    RgbdFrame {
        header: json!({
            "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
            "frame_id": 1, "t_capture": 1.0, "tracking": "normal",
            "transform": farsight_server::geometry::flatten_f(&farsight_server::geometry::IDENTITY).to_vec(),
        }),
        rgb: vec![127u8; w * h * 3],
        depth: vec![2.0; w * h],
        confidence: vec![2; w * h],
        calibration: Calibration { width: w, height: h, fx: 8.0, fy: 8.0, cx: 4.0, cy: 3.0 },
        transform: farsight_server::geometry::IDENTITY,
        jpeg: vec![0xff, 0xd8, 0xff, 0xd9],
        depth_width: w,
        depth_height: h,
    }
}

#[test]
fn bypass_keeps_pose_and_depth_but_never_fuses() {
    let mut state = MapState::new(false);
    state.fusion_bypass = true;
    let mapping = Arc::new(state);
    mapping.start_session(("test".to_string(), 1), "synthetic fixture".into());
    mapping.process(&frame()).unwrap();
    let s = mapping.status();
    // The depth lane still recorded (Nav2 keeps working); "frames" counts
    // fused frames only, so bypass leaves it at 0.
    assert_eq!(s["depth_range"], json!([2.0, 2.0]));
    assert_eq!(s["fusion_bypass"], true);
    assert_eq!(s["reason"], "Geometry lane external; fusion bypassed");
    // Fusion skipped: no voxels, no surfaces.
    assert_eq!(s["points"], 0);
    assert_eq!(s["triangles"], 0);
    assert_eq!(s["keyframes"], 0);
    assert_eq!(s["frames"], 0);
}

#[test]
fn default_still_fuses() {
    let mapping = Arc::new(MapState::new(false));
    mapping.start_session(("test".to_string(), 1), "synthetic fixture".into());
    mapping.process(&frame()).unwrap();
    let s = mapping.status();
    assert_eq!(s["fusion_bypass"], false);
    assert!(s["points"].as_u64().unwrap() > 0);
}
