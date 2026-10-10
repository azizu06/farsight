//! Depth-backed 3D positions for tracked person detections (display markers
//! only). Port of jetson/mapping/people.py (f1b883c): a tracked person box
//! gains `world: [x, y, z]` (map frame, metres, Y up) only when the exact
//! RGB-D frame it was detected on is still held and has enough confident
//! LiDAR depth inside the box. Nothing here touches the fused map,
//! navigation or motion.

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{json, Value};

use crate::camera::CameraFrame;
use crate::geometry::{Calibration, Mat4};
use crate::phone::RgbdFrame;

/// ~2 s of phone RGB-D at 6 fps.
pub const HELD_FRAMES: usize = 12;

/// The frame subset the locator needs, kept small enough to clone per offer.
struct HeldFrame {
    jpeg: Vec<u8>,
    depth: Vec<f32>,
    confidence: Vec<u8>,
    width: usize,
    height: usize,
    calibration: Calibration,
    transform: Mat4,
}

/// Map-frame torso centre from the median measured depth in the box's
/// central patch, or None — port of torso_point().
fn torso_point(b: &Value, f: &HeldFrame) -> Option<[f64; 3]> {
    let (bw, bh) = (f.width as f64, f.height as f64);
    let x = b["x"].as_f64()?;
    let y = b["y"].as_f64()?;
    let w = b["w"].as_f64()?;
    let h = b["h"].as_f64()?;
    let u0 = ((x + 0.3 * w) * bw) as i64;
    let u1 = ((x + 0.7 * w) * bw).ceil() as i64;
    let v0 = ((y + 0.25 * h) * bh) as i64;
    let v1 = ((y + 0.6 * h) * bh).ceil() as i64;
    let (r0, r1) = (v0.max(0) as usize, (v1 as usize).min(f.height));
    let (c0, c1) = (u0.max(0) as usize, (u1 as usize).min(f.width));
    if r0 >= r1 || c0 >= c1 {
        return None;
    }
    let mut valid: Vec<f32> = Vec::with_capacity((r1 - r0) * (c1 - c0));
    for r in r0..r1 {
        for c in c0..c1 {
            let i = r * f.width + c;
            let d = f.depth[i];
            if d.is_finite() && d > 0.15 && d <= 8.0 && f.confidence[i] >= 1 {
                valid.push(d);
            }
        }
    }
    if valid.len() < 3 {
        return None;
    }
    valid.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mid = valid.len() / 2;
    let d = if valid.len() % 2 == 1 {
        valid[mid] as f64
    } else {
        (valid[mid - 1] as f64 + valid[mid] as f64) / 2.0
    };
    let k = &f.calibration;
    let (u, v) = ((u0 + u1) as f64 / 2.0, (v0 + v1) as f64 / 2.0);
    let local = [(u - k.cx) / k.fx * d, -(v - k.cy) / k.fy * d, -d];
    let p = crate::geometry::transform_point(&f.transform, local);
    if p.iter().all(|c| c.is_finite()) {
        Some([(p[0] * 1000.0).round() / 1000.0,
              (p[1] * 1000.0).round() / 1000.0,
              (p[2] * 1000.0).round() / 1000.0])
    } else {
        None
    }
}

/// Holds the newest normal-tracking RGB-D frames by frame id; a MapState
/// frame observer on the offer side and a perception annotate hook on the
/// detection side — same split as people.py.
pub struct PersonLocator {
    frames: Mutex<HashMap<i64, HeldFrame>>,
    order: Mutex<std::collections::VecDeque<i64>>,
}

impl PersonLocator {
    pub fn new() -> Self {
        Self {
            frames: Mutex::new(HashMap::new()),
            order: Mutex::new(std::collections::VecDeque::new()),
        }
    }

    /// MapState frame observer: keep the newest RGB-D frames only when the
    /// producer reported normal tracking.
    pub fn offer_frame(&self, frame: &RgbdFrame) {
        if frame.header["tracking"].as_str() != Some("normal") {
            return;
        }
        let id = frame.header["frame_id"].as_i64().unwrap_or(-1);
        let held = HeldFrame {
            jpeg: frame.jpeg.clone(),
            depth: frame.depth.clone(),
            confidence: frame.confidence.clone(),
            width: frame.depth_width,
            height: frame.depth_height,
            calibration: frame.calibration,
            transform: frame.transform,
        };
        {
            let mut order = self.order.lock().unwrap();
            order.retain(|&x| x != id);
            order.push_back(id);
            while order.len() > HELD_FRAMES {
                if let Some(old) = order.pop_front() {
                    self.frames.lock().unwrap().remove(&old);
                }
            }
        }
        self.frames.lock().unwrap().insert(id, held);
    }

    /// Perception annotate hook: add `world` to tracked person boxes that
    /// were detected on a held RGB-D frame. Byte-compares the JPEG like the
    /// Python object-identity check — RGB-only or estimated-depth sources
    /// never get a 3D position. A mirrored IPHONE LIDAR frame counts as the
    /// same picture: its published JPEG was rotated, so the bytes differ.
    pub fn annotate(&self, boxes: &mut Vec<Value>, camera_frame: &CameraFrame) {
        let held = self.frames.lock().unwrap();
        let Some(f) = held.get(&camera_frame.frame_id) else {
            return;
        };
        let same_picture = f.jpeg == camera_frame.jpeg
            || (camera_frame.source == "IPHONE LIDAR" && camera_frame.mirrored);
        if !same_picture {
            return;
        }
        // Boxes live on the upright picture; sensor-native depth is viewed rotated to match.
        let rotated;
        let view = if camera_frame.mirrored {
            let (depth, confidence, calibration, transform) = crate::orientation::upright_grid(
                f.width,
                f.height,
                &f.depth,
                &f.confidence,
                &f.calibration,
                &f.transform,
            );
            rotated = HeldFrame {
                jpeg: f.jpeg.clone(),
                depth,
                confidence,
                width: f.width,
                height: f.height,
                calibration,
                transform,
            };
            &rotated
        } else {
            f
        };
        for b in boxes.iter_mut() {
            if b["cls"].as_str() == Some("person") && b["id"].is_i64() {
                if let Some(world) = torso_point(b, view) {
                    b["world"] = json!(world);
                }
            }
        }
    }
}

impl Default for PersonLocator {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::IDENTITY;

    fn held(width: usize, height: usize, depth: f32, conf: u8) -> HeldFrame {
        HeldFrame {
            jpeg: vec![1, 2, 3],
            depth: vec![depth; width * height],
            confidence: vec![conf; width * height],
            width,
            height,
            calibration: Calibration {
                width,
                height,
                fx: 100.0,
                fy: 100.0,
                cx: width as f64 / 2.0,
                cy: height as f64 / 2.0,
            },
            transform: IDENTITY,
        }
    }

    #[test]
    fn torso_point_uses_central_patch_and_pose() {
        // 100x100 grid at depth 2.0; box covers whole frame so the patch is
        // x 30..70, y 25..60 -> centre (50, 42.5).
        let mut f = held(100, 100, 2.0, 1);
        f.transform[0][3] = 1.0;
        let b = json!({"cls": "person", "id": 3, "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0});
        let world = torso_point(&b, &f).unwrap();
        let expect = [
            ((50.0 - 50.0) / 100.0 * 2.0 + 1.0),
            (-(42.5 - 50.0) / 100.0 * 2.0),
            -2.0,
        ];
        assert_eq!(world, expect.map(|v: f64| (v * 1000.0).round() / 1000.0));
        // Zero confidence -> no world.
        let bad = held(100, 100, 2.0, 0);
        assert!(torso_point(&b, &bad).is_none());
    }
}
