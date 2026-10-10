//! Port of jetson/tests/test_lidar_drive.py: opt-in --lidar-drive lets fresh
//! /phone LiDAR frames with normal ARKit tracking drive without a capture
//! permit; default off keeps LiDAR view-only.

use farsight_server::camera::CameraService;
use farsight_server::geometry::Calibration;
use farsight_server::phone::RgbdFrame;
use farsight_server::state::MapState;
use serde_json::{json, Value};
use std::sync::Arc;

fn frame(tracking: &str) -> RgbdFrame {
    let (w, h) = (8usize, 6usize);
    let rgb = vec![127u8; w * h * 3];
    RgbdFrame {
        header: json!({
            "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
            "frame_id": 1, "t_capture": 1.0, "tracking": tracking,
            "transform": farsight_server::geometry::flatten_f(&farsight_server::geometry::IDENTITY).to_vec(),
        }),
        rgb,
        depth: vec![2.0; w * h],
        confidence: vec![2; w * h],
        calibration: Calibration { width: w, height: h, fx: 8.0, fy: 8.0, cx: 4.0, cy: 3.0 },
        transform: farsight_server::geometry::IDENTITY,
        jpeg: vec![0xff, 0xd8, 0xff, 0xd9],
        depth_width: w,
        depth_height: h,
    }
}

fn fixture(lidar_drive: bool, tracking: &str) -> (Arc<MapState>, CameraService) {
    let mapping = Arc::new(MapState::new(false));
    mapping.start_session(("test".to_string(), 1), "synthetic fixture".into());
    mapping.process(&frame(tracking)).unwrap();
    (mapping.clone(), CameraService::new(mapping, None, lidar_drive))
}

fn status(camera: &CameraService) -> Value {
    camera.status()
}

#[test]
fn default_off_keeps_lidar_view_only() {
    let (_, camera) = fixture(false, "normal");
    let s = status(&camera);
    assert_eq!(s["camera"], true);
    assert_eq!(s["camera_source"], "IPHONE LIDAR");
    assert_eq!(s["camera_drive_ready"], false);
}

#[test]
fn fresh_normal_tracking_is_drive_ready_without_verification() {
    let (_, camera) = fixture(true, "normal");
    let s = status(&camera);
    assert_eq!(s["camera_drive_ready"], true);
    assert_eq!(s["camera_capture_verified"], false);
}

#[test]
fn tracking_loss_disconnect_and_new_session_revoke() {
    for tracking in ["limited", "not_available"] {
        let (_, camera) = fixture(true, tracking);
        assert_eq!(status(&camera)["camera_drive_ready"], false, "{tracking}");
    }
    let (mapping, camera) = fixture(true, "normal");
    mapping.disconnect();
    assert_eq!(status(&camera)["camera_drive_ready"], false);
    let (mapping, camera) = fixture(true, "normal");
    mapping.start_session(("new".to_string(), 2), "new synthetic fixture".into());
    assert_eq!(status(&camera)["camera_drive_ready"], false);
}
