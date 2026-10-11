//! Pip Scout v1-v3 RGB-D wire format.
//! ARKit camera coordinates are x-right, y-up, z-backward; world is Y-up.
//! Matrices on the wire are column-major. Depth is optical-axis metres.
//! Port of jetson/mapping/phone.py.

use serde::Deserialize;
use serde_json::Value;

use crate::geometry::{rigid_transform, valid_depth, Calibration, Mat4};

pub const MAX_PACKET: usize = 8 * 1024 * 1024;

#[derive(Debug, Deserialize)]
pub struct Hello {
    pub version: u32,
    #[serde(rename = "type")]
    pub kind: String,
    pub session_id: String,
    pub map_epoch: u64,
    #[serde(default = "default_device")]
    pub device: String,
    pub supports_scene_depth: bool,
    #[serde(default)]
    pub supports_mesh: bool,
    #[serde(default)]
    pub supports_capture_permits: bool,
}

fn default_device() -> String {
    "iPhone".into()
}

impl Hello {
    /// Port of the pydantic Hello validation.
    pub fn parse(text: &str) -> Result<Self, String> {
        let hello: Hello = serde_json::from_str(text).map_err(|e| format!("Invalid hello: {e}"))?;
        if hello.version != 1 {
            return Err("Unsupported hello version".into());
        }
        if hello.kind != "hello" {
            return Err("Expected hello message".into());
        }
        if hello.session_id.is_empty() || hello.session_id.len() > 256 {
            return Err("Invalid session_id".into());
        }
        if hello.device.len() > 256 {
            return Err("Invalid device".into());
        }
        Ok(hello)
    }
}

pub type Identity = (String, u64);

/// Port of validate_pose: checks identity, version, tracking, frame id and
/// capture timestamp, then validates the rigid transform.
pub fn validate_pose(header: &Value, identity: &Identity) -> Result<Mat4, String> {
    if !header.is_object() {
        return Err("Frame header must be an object".into());
    }
    let session = header.get("session_id").and_then(Value::as_str);
    let epoch = header.get("map_epoch").and_then(Value::as_u64);
    if session != Some(identity.0.as_str()) || epoch != Some(identity.1) {
        return Err("Session changed: reconnect with a new hello".into());
    }
    match header.get("version").and_then(Value::as_u64) {
        Some(1..=3) => {}
        _ => return Err("Unsupported phone protocol version".into()),
    }
    match header.get("tracking").and_then(Value::as_str) {
        Some("normal" | "limited" | "not_available") => {}
        _ => return Err("Unknown ARKit tracking state".into()),
    }
    let frame_id = header.get("frame_id").and_then(Value::as_i64);
    if frame_id.is_none() || frame_id.unwrap() < 0 {
        return Err("Invalid frame ID".into());
    }
    let capture = header.get("t_capture").and_then(Value::as_f64);
    match capture {
        Some(t) if t.is_finite() && t >= 0.0 => {}
        _ => return Err("Invalid capture timestamp".into()),
    }
    let transform = header
        .get("transform")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_f64).collect::<Vec<_>>());
    match transform {
        Some(values) => rigid_transform(&values),
        None => Err("Camera transform must contain 16 finite numbers".into()),
    }
}

fn dimensions(spec: Option<&Value>, limit: usize) -> Result<(usize, usize), String> {
    let spec = spec.filter(|v| v.is_object()).ok_or("Missing image/depth metadata")?;
    let w = spec.get("width").and_then(Value::as_u64);
    let h = spec.get("height").and_then(Value::as_u64);
    match (w, h) {
        (Some(w), Some(h)) if w >= 2 && h >= 2 && w.max(h) <= limit as u64 => {
            Ok((w as usize, h as usize))
        }
        _ => Err("Invalid sensor dimensions".into()),
    }
}

/// One decoded RGB-D packet.
#[derive(Clone)]
pub struct RgbdFrame {
    pub header: Value,
    /// RGB at depth resolution, row-major (h, w, 3).
    pub rgb: Vec<u8>,
    /// Depth metres, row-major (h, w).
    pub depth: Vec<f32>,
    pub confidence: Vec<u8>,
    pub calibration: Calibration,
    pub transform: Mat4,
    pub jpeg: Vec<u8>,
    pub depth_width: usize,
    pub depth_height: usize,
}

/// Port of decode_frame: strict packet framing, identity, dimension, format,
/// intrinsics and payload-length validation before JPEG decode.
pub fn decode_frame(packet: &[u8], identity: &Identity) -> Result<RgbdFrame, String> {
    match decode(packet, identity, false)? {
        DecodedFrame::Rgbd(frame) => Ok(frame),
        DecodedFrame::Camera(..) => unreachable!(),
    }
}

fn decode(packet: &[u8], identity: &Identity, camera_only: bool) -> Result<DecodedFrame, String> {
    if packet.len() <= 4 || packet.len() > MAX_PACKET {
        return Err("Frame exceeds the 8 MiB limit or is empty".into());
    }
    let header_size = u32::from_le_bytes(packet[0..4].try_into().unwrap()) as usize;
    if header_size == 0 || header_size > 65536 || 4 + header_size > packet.len() {
        return Err("Invalid frame header length".into());
    }
    let header: Value = serde_json::from_slice(&packet[4..4 + header_size])
        .map_err(|e| format!("Invalid frame header JSON: {e}"))?;
    let transform = validate_pose(&header, identity)?;
    if header.get("type").and_then(Value::as_str) != Some("frame") {
        return Err("Binary packets must be RGB-D frames".into());
    }
    let (iw, ih) = dimensions(header.get("image"), 1920)?;
    let (dw, dh) = dimensions(header.get("depth"), 640)?;
    if dimensions(header.get("confidence"), 640)? != (dw, dh) {
        return Err("Depth and confidence dimensions differ".into());
    }
    if ((iw as f64 / ih as f64) / (dw as f64 / dh as f64) - 1.0).abs() > 0.01 {
        return Err("RGB and depth aspect ratios differ".into());
    }
    let image = header.get("image").unwrap();
    let depth_meta = header.get("depth").unwrap();
    let conf_meta = header.get("confidence").unwrap();
    if image.get("orientation").and_then(Value::as_str) != Some("landscape_right")
        || depth_meta.get("format").and_then(Value::as_str) != Some("float32_m")
        || conf_meta.get("format").and_then(Value::as_str) != Some("uint8_0_2")
    {
        return Err("Unsupported orientation or depth/confidence format".into());
    }
    let jpeg_len = image.get("jpeg_len").and_then(Value::as_u64);
    let depth_len = depth_meta.get("len").and_then(Value::as_u64);
    let conf_len = conf_meta.get("len").and_then(Value::as_u64);
    let (jl, dl, cl) = match (jpeg_len, depth_len, conf_len) {
        (Some(j), Some(d), Some(c)) if j > 0 && d > 0 && c > 0 => {
            (j as usize, d as usize, c as usize)
        }
        _ => return Err("Invalid payload lengths".into()),
    };
    if dl != dw * dh * 4 || cl != dw * dh {
        return Err("Depth/confidence payload length mismatch".into());
    }
    let start = 4 + header_size;
    if start + jl + dl + cl != packet.len() {
        return Err("Truncated frame or trailing payload".into());
    }
    let intrinsics = image
        .get("intrinsics")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_f64).collect::<Vec<_>>())
        .unwrap_or_default();
    if intrinsics.len() != 9 || !intrinsics.iter().all(|v| v.is_finite()) {
        return Err("Invalid camera intrinsics".into());
    }
    // Column-major 3x3: k[row][col] = intrinsics[col*3+row]
    let k = |r: usize, c: usize| intrinsics[c * 3 + r];
    if !(k(2, 0).abs() <= 1e-9 && k(2, 1).abs() <= 1e-9 && (k(2, 2) - 1.0).abs() <= 1e-9)
        || k(0, 1).abs() > 1e-5
        || k(1, 0).abs() > 1e-5
        || !(0.0..iw as f64).contains(&k(0, 2))
        || !(0.0..ih as f64).contains(&k(1, 2))
    {
        return Err("Unsupported camera intrinsic matrix".into());
    }
    // np.allclose(k[2], [0,0,1]) check equivalent (atol 1e-8 + rtol) relaxed to above.
    // ARKit principal points scale directly with the sensor image.
    let calibration = Calibration::new(
        dw,
        dh,
        k(0, 0) * dw as f64 / iw as f64,
        k(1, 1) * dh as f64 / ih as f64,
        k(0, 2) * dw as f64 / iw as f64,
        k(1, 2) * dh as f64 / ih as f64,
    )
    .map_err(|e| format!("Unsupported camera intrinsic matrix: {e}"))?;
    // Confidence payload is validated before any decode work so the
    // camera-only lane can trust a packet without copying depth.
    let conf_offset = start + jl + dl;
    if packet[conf_offset..conf_offset + cl].iter().any(|&c| c > 2) {
        return Err("Confidence values must be 0, 1 or 2".into());
    }
    if camera_only {
        return Ok(DecodedFrame::Camera(header, packet[start..start + jl].to_vec()));
    }
    let jpeg = packet[start..start + jl].to_vec();
    let decoded = image::load_from_memory_with_format(&jpeg, image::ImageFormat::Jpeg)
        .map_err(|_| "JPEG dimensions do not match the frame header".to_string())?;
    let (w, h) = (decoded.width() as usize, decoded.height() as usize);
    if w != iw || h != ih {
        return Err("JPEG dimensions do not match the frame header".into());
    }
    let resized = image::imageops::resize(
        &decoded.to_rgb8(),
        dw as u32,
        dh as u32,
        image::imageops::FilterType::Triangle,
    );
    let rgb = resized.into_raw();
    let mut depth = Vec::with_capacity(dw * dh);
    let cursor = start + jl;
    for chunk in packet[cursor..cursor + dl].chunks_exact(4) {
        depth.push(f32::from_le_bytes(chunk.try_into().unwrap()));
    }
    let confidence = packet[conf_offset..conf_offset + cl].to_vec();
    Ok(DecodedFrame::Rgbd(RgbdFrame {
        header,
        rgb,
        depth,
        confidence,
        calibration,
        transform,
        jpeg,
        depth_width: dw,
        depth_height: dh,
    }))
}

enum DecodedFrame {
    Rgbd(RgbdFrame),
    Camera(Value, Vec<u8>),
}

/// Validate the wire packet without touching depth or reconstruction: the
/// camera fast-lane needs only the header and the JPEG bytes.
/// Mirrors decode_frame(camera_only=True).
pub fn decode_frame_camera(packet: &[u8], identity: &Identity) -> Result<(Value, Vec<u8>), String> {
    match decode(packet, identity, true)? {
        DecodedFrame::Camera(header, jpeg) => Ok((header, jpeg)),
        DecodedFrame::Rgbd(_) => unreachable!(),
    }
}

/// A compacted triangle mesh: world positions, colors, triangle corner indices.

#[derive(Clone)]
pub struct Mesh {
    pub positions: Vec<[f32; 3]>,
    pub colors: Vec<[u8; 3]>,
    /// One entry per triangle: 3 vertex indices.
    pub indices: Vec<[u32; 3]>,
}

/// Observed native pixels with RGB; bounded samples rotate across captures.
/// Port of measured_points: valid measured depth + confidence >= 1 at native
/// resolution (no stride), optional `indices[phase % step :: step][:max]`
/// rotating subsample for the bounded dense worker.
pub fn measured_points(
    frame: &RgbdFrame,
    max_points: Option<usize>,
    phase: usize,
) -> (Vec<[f32; 3]>, Vec<[u8; 3]>) {
    let (dw, dh) = (frame.depth_width, frame.depth_height);
    let usable = valid_depth(&frame.depth, dw, dh, 0.15, 8.0, 0.12);
    let mut indices: Vec<usize> = (0..dw * dh)
        .filter(|&i| usable[i] && frame.confidence[i] >= 1)
        .collect();
    if let Some(max) = max_points {
        if indices.len() > max {
            let step = indices.len().div_ceil(max);
            indices = indices.into_iter().skip(phase % step).step_by(step).take(max).collect();
        }
    }
    let k = &frame.calibration;
    let mut points = Vec::with_capacity(indices.len());
    let mut colors = Vec::with_capacity(indices.len());
    for idx in indices {
        let (v, u) = (idx / dw, idx % dw);
        let z = frame.depth[idx] as f64;
        let local = [
            (u as f64 - k.cx) * z / k.fx,
            -((v as f64 - k.cy) * z / k.fy),
            -z,
        ];
        points.push(crate::geometry::transform_point(&frame.transform, local).map(|v| v as f32));
        colors.push([frame.rgb[idx * 3], frame.rgb[idx * 3 + 1], frame.rgb[idx * 3 + 2]]);
    }
    (points, colors)
}

/// Coarser grid strides kept per keyframe for the capped visual mesh export.
/// Matches jetson/mapping/phone.py VISUAL_LODS.
pub const VISUAL_LODS: [usize; 2] = [4, 8];
/// Voxel-cloud sampling stride (#176): ~3k samples per 256x192 frame keeps
/// integrate under ~30ms — the mesh and its LODs keep their own sampling.
pub const VOXEL_POINT_STRIDE: usize = 4;

/// Project measured samples and build triangles only across observed surfaces.
/// Port of reconstruct: strided sampling, depth-edge rejection, vertex compaction.
pub fn reconstruct(frame: &RgbdFrame, stride: usize, min_confidence: u8) -> (Vec<[f32; 3]>, Vec<[u8; 3]>, Mesh) {
    let (points, colors, mesh, _) = reconstruct_lods(frame, stride, min_confidence, &[], None);
    (points, colors, mesh)
}

/// Like `reconstruct`, plus a coarse triangulation of the same depth grid per
/// entry in `lods` — (stride, mesh) pairs for the capped visual export.
/// `point_stride` samples the returned points more coarsely than the mesh
/// (#176: the voxel cloud takes stride-4 samples while the mesh keeps `stride`).
pub fn reconstruct_lods(
    frame: &RgbdFrame,
    stride: usize,
    min_confidence: u8,
    lods: &[usize],
    point_stride: Option<usize>,
) -> (Vec<[f32; 3]>, Vec<[u8; 3]>, Mesh, Vec<(usize, Mesh)>) {
    let (dw, dh) = (frame.depth_width, frame.depth_height);
    let usable_full = valid_depth(&frame.depth, dw, dh, 0.15, 8.0, 0.12);
    let (mut points, mut colors, mesh) =
        reconstruct_at(frame, stride, min_confidence, &usable_full);
    if let Some(ps) = point_stride {
        (points, colors) = sample_points(frame, ps, min_confidence, &usable_full);
    }
    let lod_meshes: Vec<(usize, Mesh)> = lods
        .iter()
        .map(|&step| (step, grid_mesh(frame, step, min_confidence, &usable_full)))
        .collect();
    (points, colors, mesh, lod_meshes)
}

/// Strided point-only sampling — the same projection/usable rules as
/// `reconstruct_at` without the triangle build (phone.py sample(point_stride)).
fn sample_points(
    frame: &RgbdFrame,
    stride: usize,
    min_confidence: u8,
    usable_full: &[bool],
) -> (Vec<[f32; 3]>, Vec<[u8; 3]>) {
    let (dw, dh) = (frame.depth_width, frame.depth_height);
    let h2 = dh.div_ceil(stride);
    let w2 = dw.div_ceil(stride);
    let k = &frame.calibration;
    let mut positions = Vec::with_capacity(h2 * w2);
    let mut colors = Vec::with_capacity(h2 * w2);
    for vy in 0..h2 {
        let v = vy * stride;
        for ux in 0..w2 {
            let u = ux * stride;
            let src = v * dw + u;
            if !(usable_full[src] && frame.confidence[src] >= min_confidence) {
                continue;
            }
            let z = frame.depth[src] as f64;
            let local = [
                (u as f64 - k.cx) * z / k.fx,
                -((v as f64 - k.cy) * z / k.fy),
                -z,
            ];
            let w = crate::geometry::transform_point(&frame.transform, local);
            positions.push([w[0] as f32, w[1] as f32, w[2] as f32]);
            colors.push([frame.rgb[src * 3], frame.rgb[src * 3 + 1], frame.rgb[src * 3 + 2]]);
        }
    }
    (positions, colors)
}

/// Triangulate one strided sampling of the depth grid — the `grid_mesh` port.
fn grid_mesh(frame: &RgbdFrame, stride: usize, min_confidence: u8, usable_full: &[bool]) -> Mesh {
    let (_, _, mesh) = reconstruct_at(frame, stride, min_confidence, usable_full);
    mesh
}

fn reconstruct_at(
    frame: &RgbdFrame,
    stride: usize,
    min_confidence: u8,
    usable_full: &[bool],
) -> (Vec<[f32; 3]>, Vec<[u8; 3]>, Mesh) {
    let (dw, dh) = (frame.depth_width, frame.depth_height);
    // Strided sampling keeps the same strided grid layout as [::stride, ::stride].
    let h2 = dh.div_ceil(stride);
    let w2 = dw.div_ceil(stride);
    let n = h2 * w2;
    let mut positions = vec![[0f32; 3]; n];
    let mut colors = vec![[0u8; 3]; n];
    let mut usable = vec![false; n];
    let mut depths = vec![0f32; n];
    let k = &frame.calibration;
    for vy in 0..h2 {
        let v = vy * stride;
        for ux in 0..w2 {
            let u = ux * stride;
            let src = v * dw + u;
            let dst = vy * w2 + ux;
            depths[dst] = frame.depth[src];
            usable[dst] = usable_full[src] && frame.confidence[src] >= min_confidence;
            colors[dst] = [frame.rgb[src * 3], frame.rgb[src * 3 + 1], frame.rgb[src * 3 + 2]];
            let z = frame.depth[src] as f64;
            let local = [
                (u as f64 - k.cx) * z / k.fx,
                -((v as f64 - k.cy) * z / k.fy),
                -z,
            ];
            let w = crate::geometry::transform_point(&frame.transform, local);
            positions[dst] = [w[0] as f32, w[1] as f32, w[2] as f32];
        }
    }
    // Triangles: (a, c, b) and (b, c, d) per quad, vertex index = vy*w2 + ux.
    let mut triangles: Vec<[u32; 3]> = Vec::with_capacity((h2 - 1) * (w2 - 1) * 2);
    for y in 0..h2 - 1 {
        for x in 0..w2 - 1 {
            let a = (y * w2 + x) as u32;
            let b = (y * w2 + x + 1) as u32;
            let c = ((y + 1) * w2 + x) as u32;
            let d = ((y + 1) * w2 + x + 1) as u32;
            triangles.push([a, c, b]);
            triangles.push([b, c, d]);
        }
    }
    triangles.retain(|tri| {
        if !tri.iter().all(|&i| usable[i as usize]) {
            return false;
        }
        let s = [
            depths[tri[0] as usize],
            depths[tri[1] as usize],
            depths[tri[2] as usize],
        ];
        let (mut mn, mut mx) = (f32::MAX, f32::MIN);
        for d in s {
            mn = mn.min(d);
            mx = mx.max(d);
        }
        (mx - mn) <= 0.10 + 0.04 * mn
    });
    // Compact: keep only referenced vertices.
    let mut remap = vec![u32::MAX; n];
    let mut positions_out: Vec<[f32; 3]> = Vec::new();
    let mut colors_out: Vec<[u8; 3]> = Vec::new();
    let mut indices: Vec<[u32; 3]> = Vec::with_capacity(triangles.len());
    for tri in triangles {
        let mut mapped = [0u32; 3];
        for (slot, &idx) in mapped.iter_mut().zip(tri.iter()) {
            if remap[idx as usize] == u32::MAX {
                remap[idx as usize] = positions_out.len() as u32;
                positions_out.push(positions[idx as usize]);
                colors_out.push(colors[idx as usize]);
            }
            *slot = remap[idx as usize];
        }
        indices.push(mapped);
    }
    // np.unique sorts referenced vertices; Python's order differs but the set is
    // identical — viewer never depends on vertex ordering.
    let points: Vec<[f32; 3]> = positions
        .iter()
        .zip(usable.iter())
        .filter(|(_, u)| **u)
        .map(|(p, _)| *p)
        .collect();
    let point_colors: Vec<[u8; 3]> = colors
        .iter()
        .zip(usable.iter())
        .filter(|(_, u)| **u)
        .map(|(c, _)| *c)
        .collect();
    (points, point_colors, Mesh { positions: positions_out, colors: colors_out, indices })
}
