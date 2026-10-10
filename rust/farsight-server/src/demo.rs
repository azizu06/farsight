//! Ray-cast RGB-D room. Goes through the same fusion as a real phone.
//! Port of jetson/mapping/demo.py.

use serde_json::{json, Value};

use crate::geometry::{Calibration, IDENTITY};
use crate::phone::RgbdFrame;

/// Render the simulated room at `elapsed` seconds and return the RGB-D frame
/// the phone would have sent. Identical geometry, colors, and pose to the
/// Python implementation.
pub fn simulated_frame(elapsed: f64, frame_id: i64, identity: &(String, u64)) -> RgbdFrame {
    let (w, h) = (160usize, 120usize);
    let k = Calibration { width: w, height: h, fx: 135.0, fy: 135.0, cx: w as f64 / 2.0, cy: h as f64 / 2.0 };
    let yaw = elapsed * 0.27;
    let (cos, sin) = (yaw.cos(), yaw.sin());
    let mut pose = IDENTITY;
    pose[0][0] = cos;
    pose[0][2] = sin;
    pose[1][1] = 1.0;
    pose[2][0] = -sin;
    pose[2][2] = cos;
    pose[0][3] = 0.65 * (elapsed * 0.12).sin();
    pose[2][3] = -1.4 + 0.5 * (elapsed * 0.12).cos();
    let origin = [pose[0][3], pose[1][3], pose[2][3]];

    let bounds = [[-3.0f64, -0.48, -6.0], [3.0, 2.4, 2.0]];
    let mut nearest = vec![f64::INFINITY; w * h];
    let mut color = vec![0u8; w * h * 3];

    // Rays in camera space (x right, y up, z backward), rotated to world.
    // ray_world = R * ray (equivalent to rays @ R.T for row vectors).
    let mut dirs = vec![[0f64; 3]; w * h];
    for v in 0..h {
        for u in 0..w {
            let local = [
                (u as f64 - k.cx) / k.fx,
                -(v as f64 - k.cy) / k.fy,
                -1.0,
            ];
            let i = v * w + u;
            dirs[i] = [
                pose[0][0] * local[0] + pose[0][1] * local[1] + pose[0][2] * local[2],
                pose[1][0] * local[0] + pose[1][1] * local[1] + pose[1][2] * local[2],
                pose[2][0] * local[0] + pose[2][1] * local[1] + pose[2][2] * local[2],
            ];
        }
    }

    for axis in 0..3 {
        for side in 0..2 {
            for i in 0..w * h {
                let direction = dirs[i][axis];
                let direction = if direction.abs() < 1e-8 { 1e-8 } else { direction };
                let t = (bounds[side][axis] - origin[axis]) / direction;
                if t <= 0.15 || t >= nearest[i] {
                    continue;
                }
                let hit = [
                    origin[0] + dirs[i][0] * t,
                    origin[1] + dirs[i][1] * t,
                    origin[2] + dirs[i][2] * t,
                ];
                let inside = (0..3).all(|a| hit[a] >= bounds[0][a] - 1e-4 && hit[a] <= bounds[1][a] + 1e-4);
                if !inside {
                    continue;
                }
                let base: [f64; 3] = if axis != 1 { [87.0, 105.0, 111.0] } else { [48.0, 63.0, 66.0] };
                let pattern = ((hit[0] * 3.0).floor() + (hit[2] * 3.0).floor()).rem_euclid(2.0) * 10.0;
                let wall_band = if axis != 1 && hit[1] > 0.55 && hit[1] < 0.64 { 50.0 } else { 0.0 };
                for c in 0..3 {
                    color[i * 3 + c] = (base[c] + pattern + wall_band).clamp(0.0, 255.0) as u8;
                }
                nearest[i] = t;
            }
        }
    }

    let boxes: [([f64; 3], [f64; 3], [u8; 3]); 3] = [
        ([-1.9, -0.48, -3.6], [-0.9, 0.35, -2.5], [201, 146, 61]),
        ([0.5, -0.48, -4.6], [1.6, 0.75, -3.8], [66, 150, 145]),
        ([1.9, -0.48, -1.6], [2.6, 0.05, -0.8], [167, 92, 64]),
    ];
    for (lower, upper, tint) in boxes {
        for i in 0..w * h {
            let mut entry = f64::MIN;
            let mut exit = f64::MAX;
            for a in 0..3 {
                let d = if dirs[i][a].abs() < 1e-8 { 1e-8 } else { dirs[i][a] };
                let first = (lower[a] - origin[a]) / d;
                let last = (upper[a] - origin[a]) / d;
                entry = entry.max(first.min(last));
                exit = exit.min(first.max(last));
            }
            if entry > 0.15 && entry <= exit && entry < nearest[i] {
                color[i * 3] = tint[0];
                color[i * 3 + 1] = tint[1];
                color[i * 3 + 2] = tint[2];
                nearest[i] = entry;
            }
        }
    }

    let mut depth = vec![0f32; w * h];
    let mut confidence = vec![0u8; w * h];
    for i in 0..w * h {
        if nearest[i] <= 8.0 {
            depth[i] = nearest[i] as f32;
            confidence[i] = 2;
        }
    }

    // JPEG preview at 320x240 (cv2 resize + imencode equivalent).
    let img = image::RgbImage::from_raw(w as u32, h as u32, color.clone()).unwrap();
    let preview = image::imageops::resize(&img, 320, 240, image::imageops::FilterType::Triangle);
    let mut jpeg = Vec::new();
    let encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg, 95);
    image::ImageEncoder::write_image(encoder, preview.as_raw(), 320, 240, image::ExtendedColorType::Rgb8)
        .expect("demo JPEG encode");

    let header: Value = json!({
        "version": 1,
        "type": "frame",
        "session_id": identity.0,
        "map_epoch": identity.1,
        "frame_id": frame_id,
        "t_capture": elapsed,
        "t_wall_ms": (std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()) as i64,
        "transform": crate::geometry::flatten_f(&pose).to_vec(),
        "tracking": "normal",
    });
    RgbdFrame {
        header,
        rgb: color,
        depth,
        confidence,
        calibration: k,
        transform: pose,
        jpeg,
        depth_width: w,
        depth_height: h,
    }
}
