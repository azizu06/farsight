//! Metric indoor depth and pure camera-to-scan geometry (Three.js: Y up, -Z ahead).
//! Port of jetson/depth.py. The ONNX path requires the `detector` feature
//! (ort `load-dynamic`); TensorRT `.engine` exports stay Python-side.

#[cfg(feature = "detector")]
use image::imageops::FilterType;

pub const DEPTH_INPUT: [i64; 4] = [1, 3, 308, 406];
pub const DEPTH_OUTPUT: [i64; 3] = [1, 308, 406];

#[cfg(feature = "detector")]
const MODEL_W: u32 = 406;
#[cfg(feature = "detector")]
const MODEL_H: u32 = 308;
#[cfg(feature = "detector")]
const MEAN: [f32; 3] = [0.485, 0.456, 0.406];
#[cfg(feature = "detector")]
const STD: [f32; 3] = [0.229, 0.224, 0.225];

/// Local HF metric-indoor-small export; no downloads or relative-depth scaling.
pub struct MetricDepth {
    #[cfg(feature = "detector")]
    session: ort::session::Session,
    #[cfg(feature = "detector")]
    input_name: String,
    pub backend: String,
}

impl MetricDepth {
    pub fn new(model: &str) -> Result<Self, String> {
        let path = std::path::Path::new(model);
        let suffix = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
        if suffix == "engine" || suffix == "plan" {
            return Err("TensorRT depth engines have no Rust runtime; use the ONNX export".into());
        }
        #[cfg(feature = "detector")]
        {
            let session = ort::session::Session::builder()
                .map_err(|e| e.to_string())?
                .commit_from_file(model)
                .map_err(|e| e.to_string())?;
            let inputs = session.inputs();
            let shape_ok = inputs.len() == 1
                && inputs[0].input_type.tensor_shape().map(|s| s.iter().copied().collect::<Vec<i64>>())
                    == Some(DEPTH_INPUT.to_vec());
            if !shape_ok {
                return Err(format!("Depth model must accept {:?}", DEPTH_INPUT));
            }
            let input_name = inputs[0].name().to_string();
            return Ok(Self { session, input_name, backend: "depth-onnx-cpu".into() });
        }
        #[cfg(not(feature = "detector"))]
        {
            let _ = path;
            Err("Depth model inference requires the `detector` build feature".into())
        }
    }

    /// RGB pixels (row-major, width*height*3) -> metric depth at the same
    /// pixel grid. Port of predict(): resize to the fixed export input,
    /// ImageNet-normalized RGB, then restore depth to the original grid.
    pub fn predict(&self, rgb: &[u8], width: usize, height: usize) -> Result<Vec<f32>, String> {
        if width == 0 || height == 0 || rgb.len() != width * height * 3 {
            return Err("Depth input must be nonempty BGR uint8".into());
        }
        #[cfg(feature = "detector")]
        {
            let image = image::RgbImage::from_raw(width as u32, height as u32, rgb.to_vec())
                .ok_or("Depth input must be nonempty BGR uint8")?;
            let small = image::imageops::resize(&image, MODEL_W, MODEL_H, FilterType::CatmullRom);
            let mut blob = vec![0f32; 3 * (MODEL_H * MODEL_W) as usize];
            for y in 0..MODEL_H {
                for x in 0..MODEL_W {
                    let px = small.get_pixel(x, y);
                    for c in 0..3 {
                        blob[c * (MODEL_H * MODEL_W) as usize + (y * MODEL_W + x) as usize] =
                            (px[c] as f32 / 255.0 - MEAN[c]) / STD[c];
                    }
                }
            }
            let shape = [1usize, 3, MODEL_H as usize, MODEL_W as usize];
            let outputs = self
                .session
                .run(ort::inputs![self.input_name.as_str() =>
                    ort::value::Tensor::from_array((shape, blob)).map_err(|e| e.to_string())?])
                .map_err(|e| e.to_string())?;
            let out_name = self.session.outputs()[0].name().to_string();
            let output = outputs.get(&out_name).ok_or("Depth model must return finite metric [1,308,406] output")?;
            let (oshape, data) = output.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
            let dims: Vec<i64> = oshape.iter().copied().collect();
            if dims != DEPTH_OUTPUT || !data.iter().all(|v| v.is_finite()) {
                return Err("Depth model must return finite metric [1,308,406] output".into());
            }
            let depth_img = image::ImageBuffer::<image::Luma<f32>, Vec<f32>>::from_raw(
                MODEL_W,
                MODEL_H,
                data.to_vec(),
            )
            .ok_or("Depth model must return finite metric [1,308,406] output")?;
            let restored = image::imageops::resize(&depth_img, width as u32, height as u32, FilterType::CatmullRom);
            return Ok(restored.into_raw());
        }
        #[cfg(not(feature = "detector"))]
        {
            let _ = (rgb, width, height);
            Err("Depth model inference requires the `detector` build feature".into())
        }
    }
}

/// FOV fallback intrinsics: square pixels, centred principal point.
pub fn intrinsics_from_fov(width: u32, height: u32, horizontal_fov: f64) -> Result<[f64; 4], String> {
    if width.min(height) < 1 || !(1.0 < horizontal_fov && horizontal_fov < 179.0) {
        return Err("Invalid image dimensions or horizontal FOV".into());
    }
    let focal = width as f64 / (2.0 * (horizontal_fov.to_radians() / 2.0).tan());
    Ok([focal, focal, (width - 1) as f64 / 2.0, (height - 1) as f64 / 2.0])
}

/// Depth is optical-axis metres. Positive commanded yaw turns right.
/// Returns world-frame points (X right, Y up, Z negative ahead) and uint8 colors.
pub fn back_project(
    depth: &[f32],
    width: usize,
    height: usize,
    rgb: &[u8],
    intrinsics: [f64; 4],
    yaw_deg: f64,
    max_depth: f64,
) -> Result<(Vec<[f32; 3]>, Vec<[u8; 3]>), String> {
    if depth.len() != width * height || rgb.len() != width * height * 3 {
        return Err("Depth and RGB must have matching image dimensions".into());
    }
    let [fx, fy, cx, cy] = intrinsics;
    if ![fx, fy, cx, cy, yaw_deg].iter().all(|v| v.is_finite()) || fx.min(fy) <= 0.0 {
        return Err("Intrinsics and yaw must be finite with positive focal lengths".into());
    }
    let angle = yaw_deg.to_radians();
    let (c, s) = (angle.cos(), angle.sin());
    let mut points = Vec::new();
    let mut colors = Vec::new();
    for v in 0..height {
        for u in 0..width {
            let d = depth[v * width + u] as f64;
            if !(d.is_finite() && d > 0.15 && d <= max_depth) {
                continue;
            }
            let (px, py, pz) = ((u as f64 - cx) * d / fx, -(v as f64 - cy) * d / fy, -d);
            // rotation [[c,0,-s],[0,1,0],[s,0,c]] applied to the point.
            points.push([(c * px - s * pz) as f32, py as f32, (s * px + c * pz) as f32]);
            let base = (v * width + u) * 3;
            colors.push([rgb[base], rgb[base + 1], rgb[base + 2]]);
        }
    }
    Ok((points, colors))
}
