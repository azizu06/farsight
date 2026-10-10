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
    /// True when the frame echoed a live receiver-issued capture permit.
    pub capture_verified: bool,
    /// The permit issuer the frame was bound to; drive eligibility requires
    /// it to still be active (jetson/camera.py parity).
    pub capture_binding: Option<Arc<crate::capture_permits::CapturePermits>>,
}

impl CameraFrame {
    pub fn age_ms(&self) -> i64 {
        ((now_monotonic() - self.received) * 1000.0).round().max(0.0) as i64
    }
}

struct Inner {
    frame: Option<CameraFrame>,
    phone_rgb: Option<CameraFrame>,
    phone_capture_diagnostics: serde_json::Map<String, serde_json::Value>,
    error: Option<String>,
}

/// Mirrors camera.adjusted_receipt: receipt time minus the producer's own
/// encoding delay, so frame age reflects true capture-to-now latency.
pub fn adjusted_receipt(header: &serde_json::Value, received_at: f64) -> Result<f64, String> {
    let delay_ms = match header.get("producer_delay_ms") {
        None => 0.0,
        Some(v) => v
            .as_f64()
            .filter(|d| d.is_finite() && (0.0..=60000.0).contains(d))
            .ok_or("Producer encoding delay must be finite milliseconds in 0..60000")?,
    };
    Ok(received_at - delay_ms / 1000.0)
}

/// Bounded producer clock observations, never a server qualification
/// decision. Port of camera.capture_diagnostics.
pub fn capture_diagnostics(
    header: &serde_json::Value,
) -> serde_json::Map<String, serde_json::Value> {
    let mut result = serde_json::Map::new();
    if let Some(clock) = header
        .get("capture_clock")
        .and_then(|v| v.as_str())
        .filter(|c| matches!(*c, "avcapture_sync_to_host" | "arkit_unverified"))
    {
        result.insert("clock".into(), serde_json::json!(clock));
    }
    for key in [
        "t_capture",
        "callback_host_t",
        "sensor_timestamp_raw_t",
        "sensor_timestamp_host_t",
        "sensor_host_delta_ms",
        "sensor_age_ms",
    ] {
        if let Some(v) = header.get(key).and_then(|v| v.as_f64()).filter(|v| v.is_finite()) {
            result.insert(key.into(), serde_json::json!(v));
        }
    }
    result
}

pub struct CameraService {
    pub mapping: Arc<MapState>,
    pub device: Option<String>,
    /// Opt-in (--lidar-drive): fresh /phone ARKit frames with normal tracking may
    /// drive without a capture-permit binding. Leases and stale stops still apply.
    pub lidar_drive: bool,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub quality: u32,
    inner: Mutex<Inner>,
    stop: Arc<AtomicBool>,
}

impl CameraService {
    pub fn new(mapping: Arc<MapState>, device: Option<String>, lidar_drive: bool) -> Self {
        Self {
            mapping,
            device,
            lidar_drive,
            width: 640,
            height: 480,
            fps: 15,
            quality: 55,
            inner: Mutex::new(Inner {
                frame: None,
                phone_rgb: None,
                phone_capture_diagnostics: serde_json::Map::new(),
                error: None,
            }),
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
            return jpeg.map(|jpeg| CameraFrame {
                jpeg,
                received,
                frame_id,
                source: source.into(),
                capture_verified: false,
                capture_binding: None,
            });
        }
        self.inner.lock().unwrap().frame.clone()
    }

    /// Validate + store a phone RGB packet. Mirrors accept_phone_rgb.
    pub fn accept_phone_rgb(
        &self,
        packet: &[u8],
        session_id: &str,
        previous_frame: i64,
        permits: Option<&Arc<crate::capture_permits::CapturePermits>>,
    ) -> Result<i64, String> {
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
        let origin = permits.and_then(|p| p.verified_origin(&header));
        let mut received = adjusted_receipt(&header, now_monotonic())?;
        if let Some(origin) = origin {
            received = received.min(origin);
        }
        let mut inner = self.inner.lock().unwrap();
        inner.phone_capture_diagnostics = capture_diagnostics(&header);
        inner.phone_rgb = Some(CameraFrame {
            jpeg: jpeg.to_vec(),
            received,
            frame_id: fid,
            source: "PHONE RGB".into(),
            capture_verified: origin.is_some(),
            capture_binding: permits.cloned(),
        });
        Ok(fid)
    }

    pub fn set_phone_rgb(&self, frame: Option<CameraFrame>) {
        self.inner.lock().unwrap().phone_rgb = frame;
    }

    pub fn status(&self) -> serde_json::Value {
        let frame = self.snapshot();
        let inner = self.inner.lock().unwrap();
        let error = inner.error.clone();
        let fresh = frame.as_ref().map(|f| f.age_ms() < 500).unwrap_or(false);
        let bound = frame
            .as_ref()
            .map(|f| {
                f.capture_verified
                    && f.capture_binding.as_ref().map(|p| p.is_active()).unwrap_or(false)
            })
            .unwrap_or(false);
        // Drive eligibility: verified phone RGB, local USB, or a known-safe
        // source; legacy/unverified streams are viewable but cannot drive.
        let lidar = self.lidar_drive
            && fresh
            && frame.as_ref().map(|f| f.source == "IPHONE LIDAR").unwrap_or(false)
            && self.mapping.lidar_drive_eligible();
        let drive_ready = fresh
            && (self.device.is_some()
                || bound
                || lidar
                || frame.as_ref().map(|f| {
                    f.source == "SIMULATED" || f.source.starts_with("REPLAY")
                })
                .unwrap_or(false));
        let diagnostics = if inner.phone_rgb.is_some() {
            inner.phone_capture_diagnostics.clone()
        } else {
            self.mapping.capture_diagnostics()
        };
        serde_json::json!({
            "camera": fresh,
            "camera_drive_ready": drive_ready,
            "camera_capture_verified": bound,
            "camera_capture_diagnostics": diagnostics,
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
                        while let Some(jpeg) = next_jpeg(&mut buf) {
                            let mut inner = self.inner.lock().unwrap();
                            inner.frame = Some(CameraFrame {
                                jpeg,
                                received: now_monotonic(),
                                frame_id,
                                source: "USB CAMERA".into(),
                                capture_verified: false,
                                capture_binding: None,
                            });
                            inner.error = None;
                            drop(inner);
                            frame_id += 1;
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

/// Pull the next complete JPEG out of the MJPEG byte stream: resync on the
/// SOI marker, cut through the EOI marker. Returns None when the buffer
/// holds only a partial frame.
fn next_jpeg(buf: &mut Vec<u8>) -> Option<Vec<u8>> {
    loop {
        if buf.len() < 4 {
            return None;
        }
        let soi = match buf.windows(2).position(|w| w == [0xFF, 0xD8]) {
            Some(p) => p,
            None => {
                buf.clear();
                return None;
            }
        };
        if soi > 0 {
            buf.drain(..soi);
        }
        // skip(1) drops the SOI window, so e is the EOI marker index minus
        // one: the marker sits at byte e + 1 and the frame ends at e + 3.
        let eoi = buf.windows(2).skip(1).position(|w| w == [0xFF, 0xD9]);
        match eoi {
            Some(e) => return Some(buf.drain(..e + 3).collect()),
            None => return None,
        }
    }
}

#[cfg(test)]
mod jpeg_tests {
    use super::next_jpeg;

    fn jpeg(payload: usize, tag: u8) -> Vec<u8> {
        let mut v = vec![0xFF, 0xD8];
        v.extend(std::iter::repeat(tag).take(payload));
        v.extend([0xFF, 0xD9]);
        v
    }

    #[test]
    fn single_frame_cuts_through_eoi() {
        let mut buf = jpeg(627, 0x11); // 631 bytes: the drain must not over-run
        let j = next_jpeg(&mut buf).unwrap();
        assert_eq!(j.len(), 631);
        assert_eq!(j[..2], [0xFF, 0xD8]);
        assert_eq!(j[629..], [0xFF, 0xD9]);
        assert!(buf.is_empty());
        assert!(next_jpeg(&mut buf).is_none());
    }

    #[test]
    fn fragmented_frame_waits_for_the_rest() {
        let full = jpeg(10, 0x22);
        let mut buf = full[..4].to_vec();
        assert!(next_jpeg(&mut buf).is_none());
        buf.extend_from_slice(&full[4..]);
        assert_eq!(next_jpeg(&mut buf).unwrap(), full);
    }

    #[test]
    fn back_to_back_frames_each_cut() {
        let a = jpeg(3, 0xAA);
        let b = jpeg(5, 0xBB);
        let mut buf = [a.clone(), b.clone()].concat();
        assert_eq!(next_jpeg(&mut buf).unwrap(), a);
        assert_eq!(next_jpeg(&mut buf).unwrap(), b);
        assert!(next_jpeg(&mut buf).is_none());
    }

    #[test]
    fn garbage_resyncs_on_soi() {
        let mut buf = b"noise\xFF\xD9more".to_vec();
        let frame = jpeg(2, 0x33);
        buf.extend_from_slice(&frame);
        assert_eq!(next_jpeg(&mut buf).unwrap(), frame);
    }
}
