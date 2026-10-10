//! Port of jetson/tests/test_orientation.py (d9bd03e): mount hysteresis,
//! upright_view parity with an upright baseline, and the mirrored annotate
//! path plus a live /phone flip.

use std::sync::Arc;
use std::time::{Duration, Instant};

use farsight_server::camera::CameraFrame;
use farsight_server::demo::simulated_frame;
use farsight_server::geometry::{flatten_f, Mat4};
use farsight_server::orientation::{
    rotate_jpeg_180, up_cosine, upright_view, UprightNormalizer, CONFIRM_FRAMES,
};
use farsight_server::people::PersonLocator;
use farsight_server::phone::{decode_frame, RgbdFrame};
use farsight_server::server::build_state;
use futures_util::SinkExt;
use serde_json::json;

const IDENTITY: (&str, u64) = ("simulated-room", 1);
const FLIP: Mat4 = [
    [-1.0, 0.0, 0.0, 0.0],
    [0.0, -1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
];

fn mat4_mul(a: &Mat4, b: &Mat4) -> Mat4 {
    let mut out = [[0.0f64; 4]; 4];
    for r in 0..4 {
        for c in 0..4 {
            out[r][c] = (0..4).map(|k| a[r][k] * b[k][c]).sum();
        }
    }
    out
}

/// Camera-to-world pose rolled about the optical axis; pitch tilts toward the floor.
fn roll_pose(degrees: f64, pitch: f64) -> Mat4 {
    let (r, p) = (degrees.to_radians(), pitch.to_radians());
    let roll = [
        [r.cos(), -r.sin(), 0.0],
        [r.sin(), r.cos(), 0.0],
        [0.0, 0.0, 1.0],
    ];
    let tilt = [
        [1.0, 0.0, 0.0],
        [0.0, p.cos(), -p.sin()],
        [0.0, p.sin(), p.cos()],
    ];
    let mut pose = [[0.0f64; 4]; 4];
    for i in 0..3 {
        for j in 0..3 {
            pose[i][j] = (0..3).map(|k| tilt[i][k] * roll[k][j]).sum();
        }
    }
    pose[3][3] = 1.0;
    pose[0][3] = 0.4;
    pose[1][3] = 0.3;
    pose[2][3] = -0.2;
    pose
}

/// What a phone rotated 180 degrees reports for the same scene: sensor-native flip.
fn phone_upside_down(frame: &RgbdFrame) -> RgbdFrame {
    let (w, h) = (frame.depth_width, frame.depth_height);
    let n = w * h;
    let mut rgb = vec![0u8; frame.rgb.len()];
    let mut depth = vec![0f32; n];
    let mut confidence = vec![0u8; n];
    for i in 0..n {
        rgb[3 * i..3 * i + 3].copy_from_slice(&frame.rgb[3 * (n - 1 - i)..3 * (n - 1 - i) + 3]);
        depth[i] = frame.depth[n - 1 - i];
        confidence[i] = frame.confidence[n - 1 - i];
    }
    let pose = mat4_mul(&frame.transform, &FLIP);
    let mut k = frame.calibration;
    k.cx = w as f64 - 1.0 - k.cx;
    k.cy = h as f64 - 1.0 - k.cy;
    let mut header = frame.header.clone();
    header["transform"] = json!(flatten_f(&pose).to_vec());
    RgbdFrame {
        header,
        rgb,
        depth,
        confidence,
        calibration: k,
        transform: pose,
        jpeg: rotate_jpeg_180(&frame.jpeg).unwrap(),
        depth_width: w,
        depth_height: h,
    }
}

fn bundle(frame: &RgbdFrame) -> Vec<u8> {
    let img = image::load_from_memory_with_format(&frame.jpeg, image::ImageFormat::Jpeg).unwrap();
    let (iw, ih) = (img.width() as f64, img.height() as f64);
    let (w, h) = (frame.depth_width as f64, frame.depth_height as f64);
    let k = &frame.calibration;
    let mut header = frame.header.clone();
    header["image"] = json!({
        "width": img.width(), "height": img.height(), "jpeg_len": frame.jpeg.len(),
        "intrinsics": [k.fx * iw / w, 0.0, 0.0, 0.0, k.fy * ih / h, 0.0, k.cx * iw / w, k.cy * ih / h, 1.0],
        "orientation": "landscape_right",
    });
    header["depth"] = json!({"width": w as u64, "height": h as u64, "format": "float32_m", "len": (w * h * 4.0) as u64});
    header["confidence"] = json!({"width": w as u64, "height": h as u64, "format": "uint8_0_2", "len": (w * h) as u64});
    let metadata = serde_json::to_vec(&header).unwrap();
    let mut packet = (metadata.len() as u32).to_le_bytes().to_vec();
    packet.extend_from_slice(&metadata);
    packet.extend_from_slice(&frame.jpeg);
    for d in &frame.depth {
        packet.extend_from_slice(&d.to_le_bytes());
    }
    packet.extend_from_slice(&frame.confidence);
    packet
}

#[test]
fn gravity_cosine_is_plus_one_upright_and_minus_one_inverted() {
    assert!((up_cosine(&roll_pose(0.0, 0.0)).0 - 1.0).abs() < 1e-9);
    assert!((up_cosine(&roll_pose(180.0, 0.0)).0 - -1.0).abs() < 1e-9);
    assert!((up_cosine(&roll_pose(180.0, 20.0)).0 - -1.0).abs() < 1e-9);
}

#[test]
fn flips_after_confirmation_and_back_with_hysteresis() {
    let mut mount = UprightNormalizer::new();
    for _ in 0..CONFIRM_FRAMES + 2 {
        assert!(!mount.observe(&roll_pose(0.0, 0.0)));
    }
    for _ in 0..10 {
        // Near portrait: inside the dead band, never flips.
        assert!(!mount.observe(&roll_pose(95.0, 0.0)));
    }
    for _ in 0..CONFIRM_FRAMES - 1 {
        assert!(!mount.observe(&roll_pose(170.0, 0.0)));
    }
    // One good frame resets the count.
    assert!(!mount.observe(&roll_pose(0.0, 0.0)));
    for _ in 0..CONFIRM_FRAMES - 1 {
        assert!(!mount.observe(&roll_pose(170.0, 0.0)));
    }
    assert!(mount.observe(&roll_pose(170.0, 0.0)));
    for _ in 0..10 {
        // Wobble stays inverted.
        assert!(mount.observe(&roll_pose(100.0, 0.0)));
    }
    for _ in 0..CONFIRM_FRAMES - 1 {
        assert!(mount.observe(&roll_pose(5.0, 0.0)));
    }
    assert!(!mount.observe(&roll_pose(5.0, 0.0)));
}

#[test]
fn looking_straight_down_or_up_holds_the_current_mount() {
    let mut mount = UprightNormalizer::new();
    for _ in 0..CONFIRM_FRAMES {
        mount.observe(&roll_pose(180.0, 0.0));
    }
    assert!(mount.inverted());
    for _ in 0..10 {
        // Roll undefined near vertical.
        assert!(mount.observe(&roll_pose(0.0, 85.0)));
    }
}

#[test]
fn box_on_the_upright_picture_finds_the_same_depth_transform_and_calibration() {
    let id = (IDENTITY.0.to_string(), IDENTITY.1);
    let upright = simulated_frame(1.0, 4, &id);
    let sensor = decode_frame(&bundle(&phone_upside_down(&upright)), &id).unwrap();
    let baseline = decode_frame(&bundle(&upright), &id).unwrap();
    let view = upright_view(&sensor);
    assert_eq!(view.depth, baseline.depth);
    assert_eq!(view.confidence, baseline.confidence);
    for r in 0..4 {
        for c in 0..4 {
            assert!((view.transform[r][c] - baseline.transform[r][c]).abs() < 1e-9);
        }
    }
    assert_eq!(view.calibration, baseline.calibration);
    // The annotate path finds the same world point through a mirrored camera frame.
    let locator = PersonLocator::new();
    locator.offer_frame(&sensor);
    let camera = CameraFrame {
        jpeg: rotate_jpeg_180(&upright.jpeg).unwrap(),
        received: 0.0,
        frame_id: upright.header["frame_id"].as_i64().unwrap(),
        source: "IPHONE LIDAR".into(),
        capture_verified: false,
        capture_binding: None,
        tracking: None,
        mirrored: true,
        socket_received: None,
        published: None,
    };
    let mut boxes = vec![json!({"x": 0.31, "y": 0.22, "w": 0.2, "h": 0.4, "cls": "person", "id": 1})];
    locator.annotate(&mut boxes, &camera);
    let through_mirror = boxes.clone();

    let baseline_locator = PersonLocator::new();
    baseline_locator.offer_frame(&baseline);
    let baseline_camera = CameraFrame {
        jpeg: baseline.jpeg.clone(),
        received: 0.0,
        frame_id: baseline.header["frame_id"].as_i64().unwrap(),
        source: "IPHONE LIDAR".into(),
        capture_verified: false,
        capture_binding: None,
        tracking: None,
        mirrored: false,
        socket_received: None,
        published: None,
    };
    let mut baseline_boxes = vec![json!({"x": 0.31, "y": 0.22, "w": 0.2, "h": 0.4, "cls": "person", "id": 1})];
    baseline_locator.annotate(&mut baseline_boxes, &baseline_camera);
    let upright_out = baseline_boxes;
    for i in 0..3 {
        let (a, b) = (through_mirror[0]["world"][i].as_f64().unwrap(), upright_out[0]["world"][i].as_f64().unwrap());
        assert!((a - b).abs() < 2e-3, "world[{i}]: {a} vs {b}");
    }
}

#[test]
fn up_cosine_reports_in_plane_component() {
    let (cosine, in_plane) = up_cosine(&roll_pose(0.0, 0.0));
    assert!((cosine - 1.0).abs() < 1e-9 && (in_plane - 1.0).abs() < 1e-9);
    let (_, in_plane) = up_cosine(&roll_pose(0.0, 85.0));
    assert!(in_plane < 0.35, "near-vertical pose has no in-plane gravity");
}

#[tokio::test]
async fn camera_view_follows_a_live_flip_while_the_map_is_untouched() {
    let state = app_state();
    let addr = serve(state.clone()).await;
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/phone")).await.unwrap();
    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        json!({
            "version": 1, "type": "hello", "session_id": IDENTITY.0,
            "map_epoch": IDENTITY.1, "device": "Test", "supports_scene_depth": true,
        })
        .to_string()
        .into(),
    ))
    .await
    .unwrap();
    let id = (IDENTITY.0.to_string(), IDENTITY.1);
    let upright = simulated_frame(1.0, 4, &id);
    // The bounded latest-wins queue drops a burst, so pace sends: each flipped
    // packet must be decoded for the mount's confirmation count to advance.
    let sent = CONFIRM_FRAMES as i64 + 2;
    for i in 0..sent {
        let mut flipped = phone_upside_down(&upright);
        flipped.header["frame_id"] = json!(i);
        flipped.header["t_capture"] = json!(i as f64);
        ws.send(tokio_tungstenite::tungstenite::Message::Binary(bundle(&flipped).into()))
            .await
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        while state
            .camera
            .snapshot()
            .map(|f| f.frame_id >= i)
            .unwrap_or(false)
            == false
        {
            if Instant::now() >= deadline {
                eprintln!("camera status: {}", state.camera.status());
                eprintln!("mapping status: {}", state.mapping.status());
                panic!("camera lane never published frame {i}");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
    // The camera lane flips without waiting on fusion.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(f) = state.camera.snapshot() {
            if f.frame_id >= sent - 1 && f.mirrored {
                break;
            }
        }
        if Instant::now() >= deadline {
            eprintln!("camera status: {}", state.camera.status());
            eprintln!("mapping status: {}", state.mapping.status());
            panic!("camera never published the flipped frame");
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let frame = state.camera.snapshot().unwrap();
    assert!(frame.mirrored);
    assert_eq!(frame.source, "IPHONE LIDAR");
    // Mapping rejects nothing: the flipped bundles are still valid frames.
    assert_eq!(state.mapping.inner.lock().unwrap().rejected, 0);
    drop(ws);
}

fn app_state() -> Arc<farsight_server::server::AppState> {
    build_state(
        false, None, None, false, "none", None, None, false, None, 100, 0, None, None, None, None,
        None, None, None, None, None, 3.0, None, false, 70.0, None, 45.0, 10, 500_000, false, None,
        None, false, false, "none", None, false, false, 0.0, 0.0, 0.0, 0.0, 0.0, None, true,
        (256, 192), false, false,
    )
}

async fn serve(state: Arc<farsight_server::server::AppState>) -> std::net::SocketAddr {
    let app = farsight_server::server::router(state, None);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    addr
}

