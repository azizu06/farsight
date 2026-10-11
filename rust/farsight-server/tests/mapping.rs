//! Port of jetson/mapping/tests/test_mapping.py.

use farsight_server::demo::simulated_frame;
use farsight_server::geometry::{encode_points, export_ply, rigid_transform, Calibration, POINT_RECORD};
use farsight_server::phone::{decode_frame, reconstruct, Identity, RgbdFrame};
use farsight_server::state::MapState;
use farsight_server::surfaces::SurfaceMap;
use farsight_server::voxel::VoxelMap;
use serde_json::{json, Value};

fn jpeg_rgb(w: u32, h: u32, px: [u8; 3]) -> Vec<u8> {
    let rgb: Vec<u8> = (0..w * h).flat_map(|_| px).collect();
    let mut buf = Vec::new();
    let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 90);
    image::ImageEncoder::write_image(enc, &rgb, w, h, image::ExtendedColorType::Rgb8).unwrap();
    buf
}

fn flat_frame() -> RgbdFrame {
    let (w, h) = (8usize, 6usize);
    let jpeg = jpeg_rgb(w as u32, h as u32, [127, 127, 127]);
    RgbdFrame {
        header: json!({
            "version": 1, "type": "frame", "session_id": "test", "map_epoch": 1,
            "frame_id": 0, "t_capture": 0.0, "tracking": "normal",
            "transform": farsight_server::geometry::flatten_f(&farsight_server::geometry::IDENTITY).to_vec(),
        }),
        rgb: vec![127; w * h * 3],
        depth: vec![2.0; w * h],
        confidence: vec![2; w * h],
        calibration: Calibration { width: w, height: h, fx: 8.0, fy: 8.0, cx: 4.0, cy: 3.0 },
        transform: farsight_server::geometry::IDENTITY,
        jpeg,
        depth_width: w,
        depth_height: h,
    }
}

fn bundle(frame: &RgbdFrame, changes: Value) -> Vec<u8> {
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
    for (k2, v) in changes.as_object().unwrap() {
        header[k2] = v.clone();
    }
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
fn arkit_camera_axis_and_column_major_pose() {
    let mut frame = flat_frame();
    frame.transform[0][3] = 1.0;
    frame.transform[1][3] = 2.0;
    frame.transform[2][3] = 3.0;
    let (points, _, _) = reconstruct(&frame, 1, 1);
    // ARKit camera looks down -Z. Top-left pixels are left and up.
    let p = points[0];
    assert!((p[0] - 0.0).abs() < 1e-4 && (p[1] - 2.75).abs() < 1e-4 && (p[2] - 1.0).abs() < 1e-4, "{p:?}");
    let q = points[3 * 8 + 4];
    assert!((q[0] - 1.0).abs() < 1e-4 && (q[1] - 2.0).abs() < 1e-4 && (q[2] - 1.0).abs() < 1e-4, "{q:?}");
}

#[test]
fn decode_frame_roundtrip() {
    let frame = flat_frame();
    let packet = bundle(&frame, json!({}));
    let decoded = decode_frame(&packet, &("test".into(), 1)).unwrap();
    assert_eq!(decoded.depth, frame.depth);
    assert_eq!(decoded.transform, frame.transform);
    assert_eq!(decoded.calibration.width, frame.calibration.width);
}

#[test]
fn reconstruct_rejects_bad_confidence() {
    let mut frame = flat_frame();
    frame.confidence = vec![0; frame.confidence.len()];
    let (points, _, mesh) = reconstruct(&frame, 1, 1);
    assert!(points.is_empty());
    assert!(mesh.indices.is_empty());
    frame.confidence = vec![1; frame.confidence.len()];
    assert!(reconstruct(&frame, 1, 2).0.is_empty());
}

#[test]
fn triangle_gap_gate() {
    // A depth discontinuity must not be bridged by a surface triangle.
    let mut frame = flat_frame();
    for (i, d) in frame.depth.iter_mut().enumerate() {
        if i % 8 >= 4 {
            *d = 6.0;
        }
    }
    let (_, _, mesh) = reconstruct(&frame, 1, 1);
    for tri in &mesh.indices {
        let zs: Vec<f32> = tri.iter().map(|i| mesh.positions[*i as usize][2]).collect();
        let span = zs.iter().cloned().fold(f32::MIN, f32::max) - zs.iter().cloned().fold(f32::MAX, f32::min);
        assert!(span < 0.01, "triangle spans a depth cliff: {zs:?}");
    }
}

#[test]
fn decode_frame_validation() {
    let frame = flat_frame();
    let identity: Identity = ("test".into(), 1);
    for changes in [
        json!({"session_id": "other"}),
        json!({"type": "pose"}),
        json!({"tracking": "gone"}),
        json!({"t_capture": -1.0}),
    ] {
        assert!(decode_frame(&bundle(&frame, changes), &identity).is_err());
    }
}

#[test]
fn voxel_blend_and_capacity() {
    let mut voxels = VoxelMap::new(0.5, 4);
    let cluster: Vec<[f32; 3]> = (0..6).map(|i| [(i / 2) as f32 + (i % 2) as f32 * 0.4, 0.0, 0.0]).collect();
    let colors = vec![[200, 200, 200]; 6];
    voxels.integrate(&cluster, &colors, 1);
    assert_eq!(voxels.cells.len(), 3); // pairs merge into 0.5m voxels
    let points: Vec<[f32; 3]> = (0..6).map(|i| [i as f32, 0.0, 0.0]).collect();
    let mut voxels = VoxelMap::new(0.01, 4);
    voxels.integrate(&points, &colors, 2);
    assert_eq!(voxels.cells.len(), 4); // capacity eviction
    let (p, c) = voxels.snapshot();
    assert_eq!(p.len(), 4);
    assert!(c.iter().all(|c| *c == [200, 200, 200]));
}

#[test]
fn surfaces_skip_and_encode() {
    let mut surfaces = SurfaceMap::default();
    let mesh = farsight_server::phone::Mesh {
        positions: vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        colors: vec![[1, 2, 3]; 3],
        indices: vec![[0, 1, 2]],
    };
    surfaces.integrate(mesh.clone(), &farsight_server::geometry::IDENTITY, Vec::new());
    assert_eq!(surfaces.keyframes.len(), 1);
    // Same pose again is skipped (delta < 0.18m, angle < 14deg).
    surfaces.integrate(mesh.clone(), &farsight_server::geometry::IDENTITY, Vec::new());
    assert_eq!(surfaces.keyframes.len(), 1);
    let encoded = surfaces.encode();
    assert_eq!(encoded.len(), 8 + 16 * 3 + 4 * 3);
}

#[test]
fn map_state_process_and_clear() {
    let state = MapState::new(false);
    state.start_session(("test".into(), 1), "iPhone".into());
    let mut frame = flat_frame();
    state.process(&mut frame).unwrap();
    assert_eq!(state.inner.lock().unwrap().frames, 1);
    // Stale t_capture is rejected.
    state.process(&mut frame).unwrap();
    assert_eq!(state.inner.lock().unwrap().frames, 1);
    assert_eq!(state.inner.lock().unwrap().rejected, 1);
    frame.header["t_capture"] = json!(1.0);
    frame.header["frame_id"] = json!(1);
    state.process(&mut frame).unwrap();
    assert_eq!(state.inner.lock().unwrap().frames, 2);
    assert!(state.cloud_cells() > 0);
    state.clear();
    assert_eq!(state.cloud_cells(), 0);
    assert_eq!(state.surfaces_count(), 0);
    assert_eq!(state.inner.lock().unwrap().frames, 0);
}

#[test]
fn wire_packets() {
    let points = vec![[1.0f32, 2.0, 3.0]];
    let colors = vec![[10u8, 20, 30]];
    let packet = encode_points(&points, &colors);
    assert_eq!(packet.len(), 4 + POINT_RECORD);
    assert_eq!(u32::from_le_bytes(packet[..4].try_into().unwrap()), 1);
    let ply = export_ply(&points, &colors);
    assert!(ply.starts_with(b"ply\nformat binary_little_endian 1.0"));
}

#[test]
fn rigid_transform_validation() {
    assert!(rigid_transform(&[1.0; 16]).is_err()); // bottom row wrong
    let id: Vec<f64> = farsight_server::geometry::flatten_f(&farsight_server::geometry::IDENTITY).to_vec();
    assert!(rigid_transform(&id).is_ok());
    assert!(rigid_transform(&id[..15]).is_err());
}

#[test]
fn simulated_frame_integrates() {
    let identity: Identity = ("simulated-room".into(), 1);
    let frame = simulated_frame(0.5, 0, &identity);
    let packet_len = frame.depth.len();
    assert_eq!(packet_len, 160 * 120);
    let (points, _, _) = reconstruct(&frame, 2, 1);
    assert!(!points.is_empty());
}
