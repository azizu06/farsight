//! Port of jetson/mapping/tests/test_dense.py.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::http::StatusCode;
use farsight_server::dense::{unpack, DenseMap, Record, RECORD_SIZE};
use farsight_server::dense_worker::{env_enabled, DenseWorker, DENSE_FRAME_POINTS};
use farsight_server::geometry::Calibration;
use farsight_server::phone::{measured_points, RgbdFrame};
use farsight_server::server::{dense_response, DenseParams};
use farsight_server::state::MapState;
use serde_json::json;

fn flat_frame() -> RgbdFrame {
    let (w, h) = (8usize, 6usize);
    RgbdFrame {
        header: json!({
            "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
            "frame_id": 0, "t_capture": 0.0, "tracking": "normal",
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

fn wait_processed(worker: &DenseWorker, at_least: u64, secs: f64) -> bool {
    let deadline = Instant::now() + Duration::from_secs_f64(secs);
    while Instant::now() < deadline {
        if worker.counters().1 >= at_least {
            return true;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    false
}

#[test]
fn measured_native_pixels_rgb_pose_and_tracking_bypass() {
    let mut frame = flat_frame();
    frame.transform[0][3] = 1.0;
    frame.transform[1][3] = 2.0;
    frame.transform[2][3] = 3.0;
    let (xyz, rgb) = measured_points(&frame, None, 0);
    assert_eq!(xyz.len(), frame.depth.len());
    let p = xyz[0];
    assert!((p[0] - 0.0).abs() < 1e-4 && (p[1] - 2.75).abs() < 1e-4 && (p[2] - 1.0).abs() < 1e-4, "{p:?}");
    assert_eq!(rgb, frame.rgb.chunks_exact(3).map(|c| [c[0], c[1], c[2]]).collect::<Vec<_>>());

    let mut state = MapState::new(false);
    state.fusion_bypass = true;
    state.start_session(("test".to_string(), 1), "test".into());
    state.process(&mut frame).unwrap();
    assert!(wait_processed(&state.dense_worker, 1, 2.0));
    assert_eq!(state.inner.lock().unwrap().cloud.cells.len(), 0);
    assert_eq!(state.dense().len(), frame.depth.len());
    let previous = state.dense().revision();
    frame.header["t_capture"] = json!(2);
    frame.header["tracking"] = json!("limited");
    state.process(&mut frame).unwrap();
    assert_eq!(state.dense().revision(), previous);
    frame.header["t_capture"] = json!(3);
    frame.header["tracking"] = json!("normal");
    frame.confidence = vec![0; frame.confidence.len()];
    state.process(&mut frame).unwrap();
    assert!(wait_processed(&state.dense_worker, 2, 2.0));
    assert_eq!(state.dense().revision(), previous);
    let epoch = state.dense().epoch();
    state.start_session(("other".to_string(), 2), "other".into());
    assert_ne!(state.dense().epoch(), epoch);
    assert_eq!(state.dense().len(), 0);
    state.dense_worker.close();
}

#[test]
fn busy_dense_worker_never_blocks_offers_and_latest_only_wins() {
    // Python jams DenseMap.integrate; here the generation predicate blocks —
    // same property: the camera path only ever appends to a bounded slot.
    let cloud = Arc::new(DenseMap::new(true));
    let entered = Arc::new(AtomicBool::new(false));
    let release = Arc::new(AtomicBool::new(false));
    let predicate = {
        let entered = entered.clone();
        let release = release.clone();
        move |_: u64, _: &Arc<DenseMap>| {
            entered.store(true, Ordering::SeqCst);
            while !release.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(1));
            }
            true
        }
    };
    let worker = DenseWorker::with_enabled(true, Arc::new(predicate));
    worker.offer(flat_frame(), cloud.clone(), 0);
    let deadline = Instant::now() + Duration::from_secs(1);
    while !entered.load(Ordering::SeqCst) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(entered.load(Ordering::SeqCst));
    for i in 1..20u64 {
        let mut frame = flat_frame();
        frame.header["t_capture"] = json!(i);
        frame.header["frame_id"] = json!(i);
        let started = Instant::now();
        worker.offer(frame, cloud.clone(), i);
        assert!(started.elapsed() < Duration::from_millis(50));
    }
    assert!(worker.counters().2 > 0);
    assert_eq!(worker.status()["queued"], 1);
    release.store(true, Ordering::SeqCst);
    assert!(wait_processed(&worker, 2, 2.0));
    assert!(cloud.len() <= DENSE_FRAME_POINTS);
    worker.close();
}

#[test]
fn kill_switch_and_rotating_measured_sample_budget() {
    assert!(!env_enabled(Some("0")));
    assert!(!env_enabled(Some(" FALSE ")));
    assert!(!env_enabled(Some("off")));
    assert!(!env_enabled(Some("No")));
    assert!(env_enabled(None));
    assert!(env_enabled(Some("1")));

    let worker = DenseWorker::with_enabled(false, Arc::new(|_: u64, _: &Arc<DenseMap>| true));
    assert!(!worker.enabled);
    let cloud = Arc::new(DenseMap::new(true));
    worker.offer(flat_frame(), cloud.clone(), 0);
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(worker.counters(), (0, 0, 0));
    assert_eq!(worker.status()["enabled"], false);
    assert!(cloud.len() == 0);
    worker.close();

    let frame = flat_frame();
    let (xyz, rgb) = measured_points(&frame, None, 0);
    let (a, colors) = measured_points(&frame, Some(8), 0);
    let (b, _) = measured_points(&frame, Some(8), 1);
    assert_eq!(a.len(), 8);
    assert_eq!(a, xyz.iter().step_by(6).copied().collect::<Vec<_>>());
    assert_eq!(colors, rgb.iter().step_by(6).copied().collect::<Vec<_>>());
    assert_ne!(a, b);
}

#[test]
fn worker_rate_limit_latest_only_and_point_budget() {
    let cloud = Arc::new(DenseMap::new(true));
    let current = {
        let cloud = cloud.clone();
        move |_: u64, candidate: &Arc<DenseMap>| Arc::ptr_eq(candidate, &cloud)
    };
    let worker = DenseWorker::with_enabled(true, Arc::new(current));
    let frame = flat_frame();
    worker.offer(frame.clone(), cloud.clone(), 0);
    assert!(wait_processed(&worker, 1, 1.0));
    assert_eq!(worker.counters().1, 1);
    assert!(cloud.len() <= DENSE_FRAME_POINTS);
    for _ in 0..20 {
        worker.offer(frame.clone(), cloud.clone(), 0);
    }
    assert_eq!(worker.counters().1, 1);
    assert_eq!(worker.status()["queued"], 1);
    assert!(worker.counters().2 >= 19);
    worker.close();
}

#[test]
fn delta_pages_bootstrap_journal_gap_reset_and_concurrent_delete() {
    let cloud = DenseMap::with_options(
        farsight_server::dense::CAPACITY,
        farsight_server::dense::TTL_S,
        3,
        false,
        Arc::new(farsight_server::state::now_monotonic),
    );
    cloud
        .apply(&(0..6).map(|i| Record::new(i * 20, 0, 0, 1, 2, 3, 1)).collect::<Vec<_>>())
        .unwrap();
    let (mut meta, mut rows) = unpack(&cloud.delta(None, 0, false, 2, None).unwrap()).unwrap();
    assert_eq!(meta["reset"], true);
    assert_eq!(meta["snapshot"], true);
    assert_eq!(rows.len(), 2);
    let consumer = DenseMap::new(false);
    consumer.apply(&rows).unwrap();
    let original_anchor = meta["anchor"].as_i64().unwrap();
    cloud.apply(&[Record::new(0, 0, 0, 0, 0, 0, 0)]).unwrap();
    while meta["snapshot"] == true {
        let (m, page) = unpack(
            &cloud
                .delta(
                    meta["epoch"].as_str(),
                    meta["cursor"].as_i64().unwrap(),
                    true,
                    2,
                    meta["anchor"].as_i64(),
                )
                .unwrap(),
        )
        .unwrap();
        meta = m;
        rows.extend(page.iter().copied());
        assert_eq!(meta["reset"], false);
        consumer.apply(&page).unwrap();
    }
    assert_eq!(meta["cursor"].as_i64().unwrap(), original_anchor);
    let (next_meta, changes) = unpack(
        &cloud
            .delta(meta["epoch"].as_str(), meta["cursor"].as_i64().unwrap(), false, 8192, None)
            .unwrap(),
    )
    .unwrap();
    assert_eq!(changes[0].op, 0);
    consumer.apply(&changes).unwrap();
    assert_eq!(consumer.len(), 5);
    assert_eq!(next_meta["reset"], false);
    let (gap, _) = unpack(&cloud.delta(meta["epoch"].as_str(), 1, false, 8192, None).unwrap()).unwrap();
    assert_eq!(gap["reset"], true);
}

#[test]
fn capacity_does_not_evict_mac_detail_and_ttl_sends_tombstones() {
    let now = Arc::new(Mutex::new(0.0f64));
    let clock = {
        let now = now.clone();
        Arc::new(move || *now.lock().unwrap())
    };
    let cloud = DenseMap::with_options(2, 30.0, 200_000, true, clock);
    let epoch = cloud.epoch();
    cloud
        .apply(&(0..4).map(|i| Record::new(i * 20, 0, 0, 255, 0, 0, 1)).collect::<Vec<_>>())
        .unwrap();
    assert_eq!(cloud.len(), 2);
    let (_, rows) = unpack(&cloud.delta(Some(&epoch), 0, false, 8192, None).unwrap()).unwrap();
    assert_eq!(rows.len(), 4); // cache eviction is not a Mac deletion
    let (meta, _) = unpack(&cloud.delta(Some(&epoch), 4, false, 8192, None).unwrap()).unwrap();
    *now.lock().unwrap() = 31.0;
    let (_, rows) =
        unpack(&cloud.delta(Some(&epoch), meta["cursor"].as_i64().unwrap(), false, 8192, None).unwrap())
            .unwrap();
    assert_eq!(rows.iter().map(|r| r.op).collect::<Vec<_>>(), vec![0, 0]);
}

#[tokio::test]
async fn fine_cell_updates_and_compressed_http_endpoint() {
    let mut mapping = MapState::new(false);
    mapping.fusion_bypass = true;
    let cloud = mapping.dense();
    cloud
        .apply(&(0..100).map(|i| Record::new(i * 10, 0, 0, 20, 40, 60, 1)).collect::<Vec<_>>())
        .unwrap();
    let q = DenseParams { epoch: None, after: 0, snapshot: false, anchor: None, limit: 11 };
    let response = dense_response(&mapping, "br", &q);
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["content-encoding"], "br");
    assert_eq!(response.headers()["cache-control"], "no-store");
    let encoded = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
    let mut body = Vec::new();
    std::io::Read::read_to_end(&mut brotli::Decompressor::new(&encoded[..], 4096), &mut body).unwrap();
    let (meta, rows) = unpack(&body).unwrap();
    assert_eq!(rows.len(), 11);
    let header_size = u32::from_le_bytes(body[0..4].try_into().unwrap()) as usize;
    assert_eq!(body.len() - 4 - header_size, 11 * RECORD_SIZE);

    // snapshot=true without its anchor → 400 like the Python route.
    let epoch = cloud.epoch();
    let q = DenseParams { epoch: Some(epoch.clone()), after: 0, snapshot: true, anchor: None, limit: 8192 };
    let response = dense_response(&mapping, "", &q);
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let q = DenseParams { epoch: None, after: -1, snapshot: false, anchor: None, limit: 8192 };
    assert_eq!(dense_response(&mapping, "", &q).status(), StatusCode::BAD_REQUEST);
    let q = DenseParams { epoch: None, after: 0, snapshot: false, anchor: None, limit: 0 };
    assert_eq!(dense_response(&mapping, "", &q).status(), StatusCode::BAD_REQUEST);

    // /map/reset equivalent: clearing swaps the cloud and mints a new epoch.
    mapping.clear();
    assert_ne!(meta["epoch"].as_str().unwrap(), mapping.dense().epoch());
}

#[test]
fn dense_query_parsing() {
    let mut q = HashMap::from([("after".to_string(), "5".to_string()), ("limit".to_string(), "11".to_string())]);
    let p = DenseParams::parse(&q).unwrap();
    assert_eq!((p.after, p.limit, p.snapshot, p.anchor), (5, 11, false, None));
    q.insert("snapshot".to_string(), "true".to_string());
    q.insert("anchor".to_string(), "9".to_string());
    q.insert("epoch".to_string(), "abc".to_string());
    let p = DenseParams::parse(&q).unwrap();
    assert_eq!((p.snapshot, p.anchor), (true, Some(9)));
    assert_eq!(p.epoch.as_deref(), Some("abc"));
    q.insert("after".to_string(), "x".to_string());
    let err = DenseParams::parse(&q).unwrap_err();
    assert_eq!(err.status(), StatusCode::UNPROCESSABLE_ENTITY);
}
