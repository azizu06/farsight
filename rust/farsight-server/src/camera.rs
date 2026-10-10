//! One latest camera frame, shared by video, inference, and mission capture.
//! Port of jetson/camera.py. USB capture goes through ffmpeg v4l2/MJPEG since
//! we do not link OpenCV; with no device the mapping camera (or phone RGB)
//! supplies frames instead.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::state::{now_monotonic, MapState};

#[derive(Clone)]
pub struct CameraFrame {
    pub jpeg: Vec<u8>,
    pub received: f64, // monotonic seconds
    pub frame_id: i64,
    pub source: String, // "USB CAMERA" | "PHONE RGB" | "IPHONE LIDAR" | "SIMULATED"
}

impl CameraFrame {
    pub fn age_ms(&self) -> i64 {
        ((now_monotonic() - self.received) * 1000.0).round().max(0.0) as i64
    }
}

struct Inner {
    frame: Option<CameraFrame>,
    phone_rgb: Option<CameraFrame>,
    error: Option<String>,
}

pub struct CameraService {
    pub mapping: Arc<MapState>,
    pub device: Option<String>,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub quality: u32,
    inner: Mutex<Inner>,
    stop: Arc<AtomicBool>,
}

impl CameraService {
    pub fn new(mapping: Arc<MapState>, device: Option<String>) -> Self {
        Self {
            mapping,
            device,
            width: 640,
            height: 480,
            fps: 15,
            quality: 55,
            inner: Mutex::new(Inner { frame: None, phone_rgb: None, error: None }),
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn snapshot(&self) -> Option<CameraFrame> {
        if self.device.is_none() {
            let inner = self.inner.lock().unwrap();
            if inner.phone_rgb.is_some() {
                return inner.phone_rgb.clone();
            }
            drop(inner);
            let (jpeg, received, frame_id, source) = self.mapping.camera_snapshot();
            return jpeg.map(|jpeg| CameraFrame { jpeg, received, frame_id, source: source.into() });
        }
        self.inner.lock().unwrap().frame.clone()
    }

    /// Validate + store a phone RGB packet. Mirrors accept_phone_rgb.
    pub fn accept_phone_rgb(&self, packet: &[u8], session_id: &str, previous_frame: i64) -> Result<i64, String> {
        if packet.len() <= 4 || packet.len() > 2 * 1024 * 1024 {
            return Err("RGB packet length is invalid".into());
        }
        let length = u32::from_le_bytes(packet[..4].try_into().unwrap()) as usize;
        if length == 0 || length > 65536 || length + 4 >= packet.len() {
            return Err("RGB header length is invalid".into());
        }
        let header: serde_json::Value =
            serde_json::from_slice(&packet[4..4 + length]).map_err(|_| "Expected RGB frame version 1")?;
        if !header.is_object()
            || header.get("version").and_then(|v| v.as_i64()) != Some(1)
            || header.get("type").and_then(|v| v.as_str()) != Some("rgb_frame")
        {
            return Err("Expected RGB frame version 1".into());
        }
        let frame_id = header.get("frame_id");
        let capture = header.get("t_capture");
        let fid = match frame_id {
            Some(serde_json::Value::Number(n)) => n.as_i64().unwrap_or(-1),
            _ => -1,
        };
        if header.get("session_id").and_then(|v| v.as_str()) != Some(session_id) || fid <= previous_frame {
            return Err("RGB frame identity or ordering is invalid".into());
        }
        match capture.and_then(|c| c.as_f64()) {
            Some(c) if c.is_finite() && c >= 0.0 => {}
            _ => return Err("RGB capture timestamp is invalid".into()),
        }
        let jpeg = &packet[4 + length..];
        if header.get("jpeg_len").and_then(|v| v.as_u64()) != Some(jpeg.len() as u64)
            || !jpeg.starts_with(&[0xFF, 0xD8])
        {
            return Err("RGB JPEG length or format is invalid".into());
        }
        let img = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg)
            .map_err(|_| "RGB image cannot be decoded or is excessive")?;
        if img.width().max(img.height()) > 4096 {
            return Err("RGB image cannot be decoded or is excessive".into());
        }
        self.inner.lock().unwrap().phone_rgb =
            Some(CameraFrame { jpeg: jpeg.to_vec(), received: now_monotonic(), frame_id: fid, source: "PHONE RGB".into() });
        Ok(fid)
    }

    pub fn set_phone_rgb(&self, frame: Option<CameraFrame>) {
        self.inner.lock().unwrap().phone_rgb = frame;
    }

    pub fn status(&self) -> serde_json::Value {
        let frame = self.snapshot();
        let error = self.inner.lock().unwrap().error.clone();
        serde_json::json!({
            "camera": frame.as_ref().map(|f| f.age_ms() < 500).unwrap_or(false),
            "camera_age_ms": frame.as_ref().map(|f| f.age_ms()),
            "camera_frame": frame.as_ref().map(|f| f.frame_id),
            "camera_source": frame.as_ref().map(|f| f.source.clone())
                .unwrap_or_else(|| if self.device.is_some() { "USB CAMERA".into() } else { "WAITING".into() }),
            "camera_error": error,
        })
    }

    /// Start the USB capture thread (device mode only).
    pub fn start(self: Arc<Self>) {
        let Some(device) = self.device.clone() else { return };
        std::thread::spawn(move || self.capture_loop(device));
    }

    fn capture_loop(&self, device: String) {
        use std::process::{Command, Stdio};
        let mut frame_id = 0i64;
        while !self.stop.load(Ordering::Relaxed) {
            let dev = if device.chars().all(|c| c.is_ascii_digit()) {
                format!("/dev/video{}", device)
            } else {
                device.clone()
            };
            let child = Command::new("ffmpeg")
                .args([
                    "-hide_banner", "-loglevel", "error",
                    "-f", "v4l2", "-input_format", "mjpeg",
                    "-video_size", &format!("{}x{}", self.width, self.height),
                    "-framerate", &self.fps.to_string(),
                    "-i", &dev,
                    "-f", "mjpeg", "-q:v", "8", "-",
                ])
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn();
            match child {
                Ok(mut child) => {
                    let mut stdout = child.stdout.take().unwrap();
                    let mut buf: Vec<u8> = Vec::with_capacity(1 << 20);
                    let mut chunk = [0u8; 16384];
                    loop {
                        if self.stop.load(Ordering::Relaxed) {
                            let _ = child.kill();
                            return;
                        }
                        match std::io::Read::read(&mut stdout, &mut chunk) {
                            Ok(0) => {
                                self.inner.lock().unwrap().error = Some("Camera capture failed; retrying".into());
                                let _ = child.kill();
                                break;
                            }
                            Ok(n) => buf.extend_from_slice(&chunk[..n]),
                            Err(_) => {
                                self.inner.lock().unwrap().error = Some("Camera capture failed; retrying".into());
                                let _ = child.kill();
                                break;
                            }
                        }
                        while buf.len() >= 4 {
                            let soi = match buf.windows(2).position(|w| w == [0xFF, 0xD8]) {
                                Some(p) => p,
                                None => {
                                    buf.clear();
                                    break;
                                }
                            };
                            if soi > 0 {
                                buf.drain(..soi);
                            }
                            let eoi = buf.windows(2).skip(1).position(|w| w == [0xFF, 0xD9]);
                            match eoi {
                                Some(e) => {
                                    let jpeg: Vec<u8> = buf.drain(..(e + 1) * 2).collect();
                                    let mut inner = self.inner.lock().unwrap();
                                    inner.frame = Some(CameraFrame {
                                        jpeg,
                                        received: now_monotonic(),
                                        frame_id,
                                        source: "USB CAMERA".into(),
                                    });
                                    inner.error = None;
                                    drop(inner);
                                    frame_id += 1;
                                }
                                None => break,
                            }
                        }
                    }
                }
                Err(e) => {
                    self.inner.lock().unwrap().error = Some(format!(
                        "Camera {} cannot be opened (check: v4l2-ctl --list-devices; another process may hold it): {e}", dev));
                }
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    }

    /// Re-encode snapshot JPEG at preview size/quality (max_width=640, q=50).
    pub fn preview(&self) -> Option<CameraFrame> {
        let frame = self.snapshot()?;
        let img = image::load_from_memory_with_format(&frame.jpeg, image::ImageFormat::Jpeg).ok()?;
        let img = if img.width() > 640 {
            let h = (img.height() as u64 * 640 / img.width() as u64) as u32;
            img.resize_exact(640, h, image::imageops::FilterType::Triangle)
        } else {
            img
        };
        let mut buf = Vec::new();
        let enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 50);
        image::ImageEncoder::write_image(
            enc,
            img.to_rgb8().as_raw(),
            img.width(),
            img.height(),
            image::ExtendedColorType::Rgb8,
        )
        .ok()?;
        Some(CameraFrame { jpeg: buf, ..frame })
    }

    pub fn close(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
