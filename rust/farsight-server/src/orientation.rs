//! Upright camera picture for a rover phone mounted either way up.
//! Port of jetson/mapping/orientation.py.
//!
//! ARKit delivers sensor-native landscape-right images, depth and intrinsics
//! with a camera pose that already includes the phone's roll. Fusion, Nav2 and
//! the pose published to the Quest are therefore geometrically consistent in
//! either mount and are left untouched. Only pixels shown to people or run
//! through detectors see the sensor image upside down when the phone sits
//! rotated 180 degrees.
//!
//! `UprightNormalizer` decides the mount from gravity in the camera frame
//! (with hysteresis); the camera lane then rotates the published JPEG. Depth
//! lookups for a box detected on the upright picture mirror the box back
//! (see `people.rs`).

use crate::geometry::{Calibration, Mat4};
use crate::phone::RgbdFrame;

/// up-cosine below this => inverted mount (cos 110 deg).
pub const ENTER_INVERTED: f64 = -0.3420201433256687;
/// up-cosine above this => upright mount (cos 70 deg).
pub const EXIT_INVERTED: f64 = 0.3420201433256687;
/// Consecutive frames past a threshold before switching.
pub const CONFIRM_FRAMES: u32 = 3;
/// Camera pitched past ~69 deg from level: roll is undefined, hold.
pub const MIN_IN_PLANE: f64 = 0.35;

/// (cos of image roll, in-plane gravity magnitude) from a camera-to-world pose.
///
/// World is Y-up, so world-up in camera axes is row 1 of the rotation. Upright
/// landscape gives (+1), rotated 180 degrees gives (-1).
pub fn up_cosine(transform: &Mat4) -> (f64, f64) {
    let (x, y) = (transform[1][0], transform[1][1]);
    let in_plane = (x * x + y * y).sqrt();
    (if in_plane > 1e-9 { y / in_plane } else { 0.0 }, in_plane)
}

/// Per-session mount detector with hysteresis.
pub struct UprightNormalizer {
    inverted: bool,
    pending: u32,
}

impl UprightNormalizer {
    pub fn new() -> Self {
        Self { inverted: false, pending: 0 }
    }

    pub fn inverted(&self) -> bool {
        self.inverted
    }

    /// Observe one camera-to-world pose; returns the current mount decision.
    pub fn observe(&mut self, transform: &Mat4) -> bool {
        let (cosine, in_plane) = up_cosine(transform);
        if in_plane < MIN_IN_PLANE {
            self.pending = 0;
            return self.inverted;
        }
        // The band between the thresholds (about portrait) never switches.
        let wants = if self.inverted {
            cosine > EXIT_INVERTED
        } else {
            cosine < ENTER_INVERTED
        };
        self.pending = if wants { self.pending + 1 } else { 0 };
        if self.pending >= CONFIRM_FRAMES {
            self.inverted = !self.inverted;
            self.pending = 0;
        }
        self.inverted
    }
}

impl Default for UprightNormalizer {
    fn default() -> Self {
        Self::new()
    }
}

/// Upright JPEG for display, detectors and crops (re-encoded; only used while inverted).
pub fn rotate_jpeg_180(jpeg: &[u8]) -> Result<Vec<u8>, String> {
    let img = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg)
        .map_err(|_| "Upright JPEG re-encode failed".to_string())?;
    let rotated = image::imageops::rotate180(&img.to_rgb8());
    let mut buf = Vec::new();
    image::ImageEncoder::write_image(
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 85),
        &rotated,
        rotated.width(),
        rotated.height(),
        image::ExtendedColorType::Rgb8,
    )
    .map_err(|_| "Upright JPEG re-encode failed".to_string())?;
    Ok(buf)
}

/// 180°-rotated depth/confidence grids plus mirrored intrinsics and pose —
/// the non-RGB portion of upright_view, shared by people.rs's held frames.
/// Depth, confidence, principal point and pose rotate together, so a pixel on
/// the upright picture back-projects to the same world point as its sensor pixel.
pub fn upright_grid(
    width: usize,
    height: usize,
    depth: &[f32],
    confidence: &[u8],
    calibration: &Calibration,
    transform: &Mat4,
) -> (Vec<f32>, Vec<u8>, Calibration, Mat4) {
    let n = width * height;
    // Reversing both axes of a row-major grid is a flat reversal.
    let mut d = vec![0f32; n];
    let mut c = vec![0u8; n];
    for i in 0..n {
        d[i] = depth[n - 1 - i];
        c[i] = confidence[n - 1 - i];
    }
    let mut k = *calibration;
    k.cx = width as f64 - 1.0 - k.cx;
    k.cy = height as f64 - 1.0 - k.cy;
    // transform @ diag(-1,-1,1,1): negate the first two columns.
    let mut t = *transform;
    for row in t.iter_mut() {
        row[0] = -row[0];
        row[1] = -row[1];
    }
    (d, c, k, t)
}

/// Sensor-native RGB-D frame seen through a 180 degree rotation.
pub fn upright_view(frame: &RgbdFrame) -> RgbdFrame {
    let (w, h) = (frame.depth_width, frame.depth_height);
    let n = w * h;
    let (depth, confidence, calibration, transform) = upright_grid(
        w,
        h,
        &frame.depth,
        &frame.confidence,
        &frame.calibration,
        &frame.transform,
    );
    let mut rgb = vec![0u8; frame.rgb.len()];
    for i in 0..n {
        rgb[3 * i..3 * i + 3].copy_from_slice(&frame.rgb[3 * (n - 1 - i)..3 * (n - 1 - i) + 3]);
    }
    RgbdFrame {
        header: frame.header.clone(),
        rgb,
        depth,
        confidence,
        calibration,
        transform,
        jpeg: frame.jpeg.clone(),
        depth_width: w,
        depth_height: h,
    }
}
