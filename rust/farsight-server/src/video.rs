//! H.264 fragmented-MP4 transport. Port of jetson/video.py — ffmpeg produces
//! an fMP4 stream; each moof+mdat fragment is published to WebSocket
//! subscribers with capture-matched timing.

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::process::{ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::camera::{CameraFrame, CameraService};

#[derive(Clone)]
pub struct Fragment {
    pub payload: Vec<u8>,
    pub media_start_s: f64,
    pub media_end_s: f64,
    pub received: f64,
    pub frame: i64,
    pub source: String,
    pub samples: Vec<Value>,
}

impl Fragment {
    pub fn metadata(&self) -> Value {
        json!({
            "media_start_s": self.media_start_s,
            "media_end_s": self.media_end_s,
            "frame": self.frame,
            "source": self.source,
            "samples": self.samples,
        })
    }
}

pub fn codec_from_init(data: &[u8]) -> Result<String, String> {
    let offset = data.windows(4).position(|w| w == b"avcC").ok_or("missing avcC")?;
    if data.len() < offset + 8 {
        return Err("Encoder initialization is missing H.264 codec metadata".into());
    }
    Ok(format!("avc1.{:02X}{:02X}{:02X}", data[offset + 5], data[offset + 6], data[offset + 7]))
}

/// (first_sample, sample_count, media_start_s, media_end_s)
pub fn fragment_timing(data: &[u8], fps: u32) -> Result<(u64, u64, f64, f64), String> {
    let tfdt = data.windows(4).position(|w| w == b"tfdt").ok_or("Fragment missing sample timing")?;
    let trun = data.windows(4).position(|w| w == b"trun").ok_or("Fragment missing sample timing")?;
    let version = data[tfdt + 4];
    let start = if version == 1 {
        u64::from_be_bytes(data[tfdt + 8..tfdt + 16].try_into().map_err(|_| "bad tfdt")?)
    } else {
        u32::from_be_bytes(data[tfdt + 8..tfdt + 12].try_into().map_err(|_| "bad tfdt")?) as u64
    } as f64
        / 1_000_000.0;
    let count = u32::from_be_bytes(data[trun + 8..trun + 12].try_into().map_err(|_| "bad trun")?) as u64;
    let first = (start * fps as f64).round() as u64;
    Ok((first, count, start, (first + count) as f64 / fps as f64))
}

pub type Subscriber = mpsc::Receiver<Option<Fragment>>;

pub struct VideoService {
    pub camera: Arc<CameraService>,
    pub enabled: bool,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub bitrate: u32,
    pub init: Mutex<Option<Vec<u8>>>,
    pub mime: Mutex<Option<String>>,
    pub error: Mutex<Option<String>>,
    samples: Mutex<BTreeMap<u64, CameraFrame>>,
    subscribers: Mutex<Vec<mpsc::Sender<Option<Fragment>>>>,
    running: AtomicBool,
    alive: AtomicBool,
}

impl VideoService {
    pub fn new(camera: Arc<CameraService>, enabled: bool) -> Self {
        Self {
            camera,
            enabled,
            width: 640,
            height: 480,
            fps: 15,
            bitrate: 1_000_000,
            init: Mutex::new(None),
            mime: Mutex::new(None),
            error: Mutex::new(None),
            samples: Mutex::new(BTreeMap::new()),
            subscribers: Mutex::new(Vec::new()),
            running: AtomicBool::new(false),
            alive: AtomicBool::new(false),
        }
    }

    /// Mirrors ready: init received, supervisor and ffmpeg alive, no error.
    pub fn ready(&self) -> bool {
        self.init.lock().unwrap().is_some()
            && self.running.load(Ordering::Relaxed)
            && self.alive.load(Ordering::Relaxed)
            && self.error.lock().unwrap().as_deref().map(|e| e.is_empty()).unwrap_or(true)
    }

    pub fn subscribe(&self) -> Subscriber {
        let (tx, rx) = mpsc::channel(8);
        self.subscribers.lock().unwrap().push(tx);
        rx
    }

    /// Mirrors publish(): full consumer queues are dropped entirely — the
    /// channel closes, which the WS handler turns into a 1013 reconnect.
    fn publish(&self, fragment: Option<Fragment>) {
        self.subscribers.lock().unwrap().retain(|tx| tx.try_send(fragment.clone()).is_ok());
    }

    pub fn start(self: Arc<Self>) {
        if !self.enabled {
            return;
        }
        let ok = Command::new("ffmpeg")
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !ok {
            *self.error.lock().unwrap() = Some("ffmpeg unavailable; use camera.jpg until installed".into());
            return;
        }
        self.running.store(true, Ordering::Relaxed);
        std::thread::spawn(move || self.run());
    }

    /// Supervisor: restart ffmpeg forever on any failure (Python _supervise).
    fn run(self: Arc<Self>) {
        while self.running.load(Ordering::Relaxed) {
            *self.init.lock().unwrap() = None;
            *self.mime.lock().unwrap() = None;
            self.samples.lock().unwrap().clear();
            *self.error.lock().unwrap() = None;
            self.run_once();
            *self.init.lock().unwrap() = None;
            *self.mime.lock().unwrap() = None;
            if self.running.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_secs_f64(0.5));
            }
        }
    }

    fn run_once(self: &Arc<Self>) {
        let args = [
            "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
            "-pix_fmt", "bgr24", "-s", &format!("{}x{}", self.width, self.height),
            "-r", &self.fps.to_string(),
            "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-profile:v", "baseline", "-level", "3.0",
            "-pix_fmt", "yuv420p", "-b:v", &self.bitrate.to_string(),
            // ~200ms VBV like Python: tighter buffer keeps latency down.
            "-maxrate", &self.bitrate.to_string(), "-bufsize", &std::cmp::max(64000, self.bitrate / 5).to_string(),
            "-g", &self.fps.to_string(), "-keyint_min", &self.fps.to_string(),
            "-sc_threshold", "0", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-video_track_timescale", "1000000",
            "-frag_duration", "100000", "-f", "mp4", "pipe:1",
        ];
        let mut child = match Command::new("ffmpeg").args(args).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn() {
            Ok(c) => c,
            Err(e) => {
                *self.error.lock().unwrap() = Some(e.to_string());
                self.publish(None);
                return;
            }
        };
        self.alive.store(true, Ordering::Relaxed);
        let mut stdin = child.stdin.take().unwrap();
        let stdout: ChildStdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();

        // ffmpeg's own error text is the latest failure line (Python stderr task).
        {
            let svc = self.clone();
            std::thread::spawn(move || {
                let mut buf: Vec<u8> = Vec::new();
                let mut byte = [0u8; 1];
                let mut stderr = stderr;
                loop {
                    match stderr.read(&mut byte) {
                        Ok(0) | Err(_) => return,
                        Ok(_) => {
                            if byte[0] == b'\n' {
                                let line = String::from_utf8_lossy(&buf).trim().chars().take(300).collect::<String>();
                                *svc.error.lock().unwrap() = Some(line);
                                buf.clear();
                            } else if buf.len() < 4096 {
                                buf.push(byte[0]);
                            }
                        }
                    }
                }
            });
        }

        let stop_reader = Arc::new(AtomicBool::new(false));
        let r = Arc::clone(&stop_reader);
        let svc = self.clone();
        let reader = std::thread::spawn(move || svc.read_boxes(stdout, r));

        let mut sample_id = 0u64;
        while self.running.load(Ordering::Relaxed) {
            let frame = self.camera.snapshot();
            if let Some(frame) = frame {
                if frame.age_ms() < 500 {
                    if let Ok(img) = image::load_from_memory_with_format(&frame.jpeg, image::ImageFormat::Jpeg) {
                        let resized = img
                            .resize_exact(self.width, self.height, image::imageops::FilterType::Triangle)
                            .to_rgb8();
                        let mut bgr = resized.into_raw();
                        for px in bgr.chunks_exact_mut(3) {
                            px.swap(0, 2);
                        }
                        {
                            let mut samples = self.samples.lock().unwrap();
                            samples.insert(sample_id, frame.clone());
                            while samples.len() > 600 {
                                samples.pop_first();
                            }
                        }
                        sample_id += 1;
                        if stdin.write_all(&bgr).is_err() {
                            break;
                        }
                    }
                }
            }
            std::thread::sleep(Duration::from_secs_f64(1.0 / self.fps as f64));
        }
        stop_reader.store(true, Ordering::Relaxed);
        drop(stdin);
        let _ = child.kill();
        let _ = reader.join();
        self.alive.store(false, Ordering::Relaxed);
        self.publish(None);
    }

    fn read_boxes(&self, mut stdout: ChildStdout, running: Arc<AtomicBool>) {
        let mut initialization: Vec<u8> = Vec::new();
        let mut fragment: Vec<u8> = Vec::new();
        loop {
            if !running.load(Ordering::Relaxed) {
                return;
            }
            let mut header = [0u8; 8];
            if stdout.read_exact(&mut header).is_err() {
                return;
            }
            let size = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
            if !(8..=4 * 1024 * 1024).contains(&size) {
                *self.error.lock().unwrap() = Some("Invalid or excessive MP4 box from encoder".into());
                return;
            }
            let kind = [header[4], header[5], header[6], header[7]];
            let mut body = vec![0u8; size - 8];
            if stdout.read_exact(&mut body).is_err() {
                return;
            }
            let mut bx = header.to_vec();
            bx.extend_from_slice(&body);
            if self.init.lock().unwrap().is_none() {
                initialization.extend_from_slice(&bx);
                if &kind == b"moov" {
                    let init = initialization.clone();
                    match codec_from_init(&init) {
                        Ok(codec) => {
                            *self.init.lock().unwrap() = Some(init);
                            *self.mime.lock().unwrap() = Some(format!("video/mp4; codecs=\"{codec}\""));
                        }
                        Err(e) => {
                            *self.error.lock().unwrap() = Some(e);
                            return;
                        }
                    }
                }
            } else {
                fragment.extend_from_slice(&bx);
                if &kind == b"mdat" {
                    let data = std::mem::take(&mut fragment);
                    match fragment_timing(&data, self.fps) {
                        Ok((first, count, start, end)) => {
                            if let Some(frag) = self.build_fragment(data, first, count, start, end) {
                                self.publish(Some(frag));
                            }
                        }
                        Err(e) => {
                            *self.error.lock().unwrap() = Some(e);
                            return;
                        }
                    }
                }
            }
        }
    }

    fn build_fragment(&self, payload: Vec<u8>, first: u64, count: u64, start: f64, end: f64) -> Option<Fragment> {
        let samples = self.samples.lock().unwrap();
        let captures: Vec<&CameraFrame> = (first..first + count).filter_map(|i| samples.get(&i)).collect();
        if captures.len() as u64 != count || captures.is_empty() {
            *self.error.lock().unwrap() = Some("Encoded fragment cannot be matched to capture receipt times".into());
            return None;
        }
        Some(Fragment {
            payload,
            media_start_s: start,
            media_end_s: end,
            received: captures.iter().map(|f| f.received).fold(f64::INFINITY, f64::min),
            frame: captures.last().unwrap().frame_id,
            source: captures.last().unwrap().source.clone(),
            samples: captures
                .iter()
                .enumerate()
                .map(|(offset, frame)| {
                    json!({
                        "media_start_s": (first + offset as u64) as f64 / self.fps as f64,
                        "media_end_s": (first + offset as u64 + 1) as f64 / self.fps as f64,
                        "frame": frame.frame_id,
                    })
                })
                .collect(),
        })
    }

    pub fn close(&self) {
        self.running.store(false, Ordering::Relaxed);
    }
}
