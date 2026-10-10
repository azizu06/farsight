//! Port of jetson/tests/test_camera_fastlane.py (48bb941): the camera lane
//! publishes independently of RGB-D fusion, latest-wins queueing counts
//! displaced packets, and /video/status stays answerable while fusion is held.

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use farsight_server::demo::simulated_frame;
use farsight_server::phone::RgbdFrame;
use farsight_server::server::{build_state, router, AppState};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;

const IDENTITY: (&str, u64) = ("fastlane-room", 1);

fn app_state() -> Arc<AppState> {
    build_state(
        false, None, None, false, "none", None, None, false, None, 100, 0, None, None, None, None,
        None, None, None, None, None, 3.0, None, false, 70.0, None, 45.0, 10, 500_000, false, None,
        None, false, false, "none", None, false, false, 0.0, 0.0, 0.0, 0.0, 0.0, None, true,
        (256, 192), false, false,
    )
}

async fn serve(state: Arc<AppState>) -> std::net::SocketAddr {
    let app = router(state, None);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    addr
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

fn rgb_packet(session_id: &str, frame_id: i64, jpeg: &[u8]) -> Vec<u8> {
    let header = json!({
        "version": 1, "type": "rgb_frame", "session_id": session_id,
        "frame_id": frame_id, "t_capture": frame_id as f64, "jpeg_len": jpeg.len(),
    });
    let meta = serde_json::to_vec(&header).unwrap();
    let mut packet = (meta.len() as u32).to_le_bytes().to_vec();
    packet.extend_from_slice(&meta);
    packet.extend_from_slice(jpeg);
    packet
}

/// A large JPEG whose decode keeps the consumer busy while the reader floods it.
fn big_jpeg() -> Vec<u8> {
    // 1024² of high-frequency content: decodes slowly enough that 20 back-to-back
    // offers displace each other, while staying under the 2 MB rgb packet cap.
    let (w, h) = (1024u32, 1024u32);
    let mut rgb = vec![0u8; (w * h * 3) as usize];
    for (i, px) in rgb.chunks_exact_mut(3).enumerate() {
        let v = (i * 2654435761usize >> 8) as u8;
        px.copy_from_slice(&[v, v.wrapping_mul(3), v.wrapping_mul(7)]);
    }
    let mut buf = Vec::new();
    let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 90);
    image::ImageEncoder::write_image(enc, &rgb, w, h, image::ExtendedColorType::Rgb8).unwrap();
    buf
}

async fn wait_for<F: FnMut() -> bool>(mut f: F, timeout: Duration, what: &str) {
    let deadline = Instant::now() + timeout;
    while !f() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn rgb_decoder_uses_the_latest_packet_only() {
    let state = app_state();
    let addr = serve(state.clone()).await;
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/phone/rgb")).await.unwrap();
    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        json!({
            "version": 1, "type": "hello", "session_id": IDENTITY.0,
            "map_epoch": IDENTITY.1, "device": "Test", "supports_scene_depth": false,
        })
        .to_string()
        .into(),
    ))
    .await
    .unwrap();
    let jpeg = big_jpeg();
    let total = 20i64;
    for i in 0..total {
        ws.send(tokio_tungstenite::tungstenite::Message::Binary(
            rgb_packet(IDENTITY.0, i, &jpeg).into(),
        ))
        .await
        .unwrap();
    }
    // The decoder publishes the newest frame; displaced packets are counted.
    wait_for(
        || state.camera.snapshot().map(|f| f.frame_id == total - 1).unwrap_or(false),
        Duration::from_secs(30),
        "newest rgb frame",
    )
    .await;
    let status = state.camera.status();
    let dropped = status["camera_ingest_dropped"].as_i64().unwrap_or(-1);
    // Latest-wins: offers raced a single slow decode, so almost every packet
    // displaced a pending one.
    assert!(dropped >= 10, "latest-wins lane should have displaced packets, got {dropped}");
    assert_eq!(status["camera_source"], "PHONE RGB");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn rgbd_camera_and_tracking_advance_while_fusion_is_blocked() {
    let state = app_state();
    let addr = serve(state.clone()).await;
    let (ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/phone")).await.unwrap();
    let (mut ws, mut wrx) = ws.split();
    tokio::spawn(async move {
        while let Some(m) = wrx.next().await {
            eprintln!("ws inbound: {m:?}");
        }
    });
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
    wait_for(
        || state.mapping.status()["connected"].as_bool() == Some(true),
        Duration::from_secs(10),
        "session to connect",
    )
    .await;
    let id = (IDENTITY.0.to_string(), IDENTITY.1);
    let mut frame = simulated_frame(1.0, 0, &id);
    frame.header["t_capture"] = json!(0.0);
    ws.send(tokio_tungstenite::tungstenite::Message::Binary(bundle(&frame).into()))
        .await
        .unwrap();
    // Wait for the camera lane to publish (before the map lock is held this is
    // safe: snapshot() falls through to the unlocked mapping read only while
    // no phone frame exists).
    wait_for(
        || state.camera.snapshot().map(|f| f.frame_id == 0).unwrap_or(false),
        Duration::from_secs(10),
        "first camera frame",
    )
    .await;

    // Freeze the fusion lock on a dedicated thread, bounded so the test can
    // never wedge itself (the Python test's patched process + state.lock).
    let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
    let guard_thread = {
        let mapping = state.mapping.clone();
        std::thread::spawn(move || {
            let _guard = mapping.inner.lock().unwrap();
            let _ = release_rx.recv_timeout(Duration::from_secs(15));
        })
    };
    // Give the guard thread a beat to acquire the lock before the burst.
    std::thread::sleep(Duration::from_millis(50));

    // Pose heartbeats must not hold up the next camera packet. The pose
    // message carries the frame header's identity fields (dict(frame.header)
    // parity).
    let mut pose_header = frame.header.clone();
    pose_header["type"] = json!("pose");
    pose_header["frame_id"] = json!(1);
    pose_header["t_capture"] = json!(1.0);
    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        pose_header.to_string().into(),
    ))
    .await
    .unwrap();
    frame.header["frame_id"] = json!(2);
    frame.header["t_capture"] = json!(2.0);
    frame.header["tracking"] = json!("limited");
    ws.send(tokio_tungstenite::tungstenite::Message::Binary(bundle(&frame).into()))
        .await
        .unwrap();
    // camera.status() is lock-free once a phone frame exists (it reads the
    // phone pose/binding, never the map lock).
    wait_for(
        || state.camera.status()["camera_frame"].as_i64() == Some(2),
        Duration::from_secs(10),
        "camera frame 2 while fusion frozen",
    )
    .await;
    let status = state.camera.status();
    assert!(status["camera_age_ms"].as_f64().unwrap() < 5_000.0);
    assert_eq!(status["camera_drive_ready"], false);
    assert_eq!(status["camera_capture_verified"], false);
    state
        .mapping
        .processing_ms
        .store(900.0f64.to_bits(), Ordering::Relaxed);
    let result = reqwest::get(format!("http://{addr}/video/status"))
        .await
        .unwrap()
        .json::<serde_json::Value>()
        .await
        .unwrap();
    assert_eq!(result["mapping_processing_ms"].as_f64(), Some(900.0));
    assert_eq!(result["camera_frame"].as_i64(), Some(2));
    let _ = release_tx.send(());
    guard_thread.join().unwrap();

    // Tracking back to normal in the frame header revives drive eligibility.
    frame.header["frame_id"] = json!(3);
    frame.header["t_capture"] = json!(3.0);
    frame.header["tracking"] = json!("normal");
    ws.send(tokio_tungstenite::tungstenite::Message::Binary(bundle(&frame).into()))
        .await
        .unwrap();
    eprintln!("diag: {}", state.mapping.status());
    wait_for(
        || state.camera.status()["camera_frame"].as_i64() == Some(3),
        Duration::from_secs(10),
        "camera frame 3",
    )
    .await;
    assert_eq!(state.camera.status()["camera_drive_ready"], true);

    // A tracking-limited pose under a held map lock still revokes drive.
    let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
    let guard_thread = {
        let mapping = state.mapping.clone();
        std::thread::spawn(move || {
            let _guard = mapping.inner.lock().unwrap();
            let _ = release_rx.recv_timeout(Duration::from_secs(15));
        })
    };
    std::thread::sleep(Duration::from_millis(50));
    let mut pose_header = frame.header.clone();
    pose_header["type"] = json!("pose");
    pose_header["frame_id"] = json!(4);
    pose_header["t_capture"] = json!(4.0);
    pose_header["tracking"] = json!("limited");
    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        pose_header.to_string().into(),
    ))
    .await
    .unwrap();
    wait_for(
        || state.camera.status()["camera_drive_ready"].as_bool() == Some(false),
        Duration::from_secs(10),
        "drive revocation while fusion frozen",
    )
    .await;
    let _ = release_tx.send(());
    guard_thread.join().unwrap();
    drop(ws);
}
