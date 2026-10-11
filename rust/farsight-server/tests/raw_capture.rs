// Port of jetson/mapping/tests/test_raw_capture.py: the opt-in
// /phone/frame.bin export retains exactly one accepted same-frame RGB-D
// observation and serves it in the u32le-header + JPEG + f4 depth + u8
// confidence wire layout, guarded by generation/session/tracking/age.

use std::sync::Arc;

use farsight_server::geometry::IDENTITY;
use farsight_server::phone::RgbdFrame;
use farsight_server::raw_capture::{RawCapture, SnapError};
use farsight_server::state::{now_monotonic, MapState};
use serde_json::{json, Value};

fn frame(id: i64) -> RgbdFrame {
    let header = json!({
        "version": 1, "type": "frame",
        "session_id": "phone", "map_epoch": 1,
        "frame_id": id, "t_capture": 1.0,
        "tracking": "normal",
        "transform": farsight_server::geometry::flatten_f(&IDENTITY).to_vec(),
        "image": {"width": 16, "height": 16},
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
        jpeg: vec![1, 2, 3, 4],
        depth_width: 16,
        depth_height: 16,
    }
}

fn rig() -> (Arc<MapState>, Arc<RawCapture>) {
    let mut ms = MapState::new(false);
    ms.fusion_bypass = true;
    let state = Arc::new(ms);
    state.start_session(("phone".into(), 1), "Fixture".into());
    let capture = RawCapture::new(state.clone());
    let observer = capture.clone();
    state
        .frame_observers
        .lock()
        .unwrap()
        .push(Arc::new(move |f: &RgbdFrame| observer.observe(f)));
    (state, capture)
}

fn feed(state: &MapState, mut f: RgbdFrame) {
    f.header["_received_monotonic"] = json!(now_monotonic());
    state.process(&mut f).unwrap();
}

#[test]
fn native_same_frame_roundtrip_and_cursor() {
    let (state, capture) = rig();
    feed(&state, frame(7));

    let snap = capture.snapshot(None).unwrap();
    let body = snap.body.expect("first fetch returns the body");
    // Wire layout: u32le json len + json + jpeg + <f4 depth + u8 conf.
    let jlen = u32::from_le_bytes(body[0..4].try_into().unwrap()) as usize;
    let header: Value = serde_json::from_slice(&body[4..4 + jlen]).unwrap();
    assert!(header["map_generation"].is_u64());
    assert_eq!(header["capture_source"], json!("IPHONE LIDAR"));
    assert_eq!(header["image"]["jpeg_len"], json!(4));
    assert_eq!(header["frame_id"], json!(7));
    assert!(header.get("_received_monotonic").is_none(), "private fields stay private");
    let jpeg = &body[4 + jlen..4 + jlen + 4];
    assert_eq!(jpeg, &[1, 2, 3, 4]);
    let depth = &body[4 + jlen + 4..4 + jlen + 4 + 16 * 16 * 4];
    assert_eq!(f32::from_le_bytes(depth[0..4].try_into().unwrap()), 2.0);
    let conf = &body[4 + jlen + 4 + 16 * 16 * 4..];
    assert_eq!(conf.len(), 16 * 16);
    assert!(conf.iter().all(|c| *c == 2));

    // after == key → 204 (no body); repeat fetch returns the cached Bytes.
    let snap2 = capture.snapshot(Some(&snap.key)).unwrap();
    assert!(snap2.body.is_none());
    assert_eq!(snap2.key, snap.key);
    let snap3 = capture.snapshot(None).unwrap();
    assert_eq!(snap3.body.unwrap().as_ptr(), body.as_ptr());
}

#[test]
fn reset_disconnect_limited_stale_and_reconnect_reject() {
    // clear(): a visual map reset preserves the socket, but bumps the map
    // generation — the held observation's generation is stale.
    {
        let (state, capture) = rig();
        feed(&state, frame(1));
        state.clear();
        assert!(matches!(
            capture.snapshot(None),
            Err(SnapError::Unavailable)
        ));
    }
    // disconnect: !connected rejects the scope.
    {
        let (state, capture) = rig();
        feed(&state, frame(1));
        state.disconnect();
        assert!(matches!(
            capture.snapshot(None),
            Err(SnapError::Unavailable)
        ));
    }
    // tracking != normal rejects.
    {
        let (state, capture) = rig();
        feed(&state, frame(1));
        state.inner.lock().unwrap().tracking = "limited".into();
        assert!(matches!(
            capture.snapshot(None),
            Err(SnapError::Unavailable)
        ));
    }
    // same-identity reconnect bumps socket_generation — stale binding rejects.
    {
        let (state, capture) = rig();
        feed(&state, frame(1));
        state.start_session(("phone".into(), 1), "reconnected".into());
        assert!(matches!(
            capture.snapshot(None),
            Err(SnapError::Unavailable)
        ));
    }
    // held receipt older than 2 s is stale.
    {
        let (state, capture) = rig();
        let mut f = frame(1);
        f.header["_received_monotonic"] = json!(now_monotonic() - 3.0);
        f.header["_mapping_socket_generation"] =
            json!(state.inner.lock().unwrap().socket_generation);
        // process() would set state tracking from the frame; the direct
        // observe path bypasses it, so pin it the same way.
        state.inner.lock().unwrap().tracking = "normal".into();
        capture.observe(&f);
        match capture.snapshot(None) {
            Err(SnapError::Stale) => {}
            Err(e) => panic!("expected Stale, got {e:?}"),
            Ok(_) => panic!("expected Stale, got a body"),
        }
    }
}

#[test]
fn empty_capture_reports_waiting() {
    let (_state, capture) = rig();
    assert!(matches!(capture.snapshot(None), Err(SnapError::Waiting)));
}

fn app_state() -> Arc<farsight_server::server::AppState> {
    farsight_server::server::build_state(
        false, None, None, false, "none", None, None, false, None, 100, 0, None, None, None,
        None, None, None, None, None, None, 3.0, None, false, 70.0, None, 45.0, 10, 500_000,
        false, None, None, false, false, "none", None, false, false, 0.0, 0.0, 0.0, 0.0, 0.0,
        None, true, (256, 192), false, false, (None, None, None),
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

// create_app parity: /phone/frame.bin exists only under
// FARSIGHT_RAW_CAPTURE=1; unset leaves the route unmounted.
#[tokio::test]
async fn env_opt_in_mounts_endpoint_default_off() {
    std::env::remove_var("FARSIGHT_RAW_CAPTURE");
    assert!(app_state().raw_capture.is_none());
    let addr = serve(app_state()).await;
    let resp = reqwest::get(format!("http://{addr}/phone/frame.bin"))
        .await
        .unwrap();
    assert_eq!(resp.status(), 404);

    std::env::set_var("FARSIGHT_RAW_CAPTURE", "1");
    let mounted = app_state();
    assert!(mounted.raw_capture.is_some());
    let addr = serve(mounted).await;
    let resp = reqwest::get(format!("http://{addr}/phone/frame.bin"))
        .await
        .unwrap();
    // Mounted but no accepted frame yet → 503 'Waiting for accepted RGB-D'.
    assert_eq!(resp.status(), 503);
    std::env::remove_var("FARSIGHT_RAW_CAPTURE");
}
