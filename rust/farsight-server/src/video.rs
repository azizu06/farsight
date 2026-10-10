//! H.264 fragmented-MP4 transport. Port of jetson/video.py — ffmpeg produces
//! an fMP4 stream; each moof+mdat fragment is published to WebSocket
//! subscribers with capture-matched timing.

use std::collections::{BTreeMap, VecDeque};
use std::io::{Read, Write};
use std::process::{ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::camera::{CameraFrame, CameraService};

#[derive(Clone)]
pub struct Fragment {
    pub payload: Vec<u8>,
    pub media_start_s: f64,
    pub media_end_s: f64,
    /// Independently decodable: this fragment's mdat carries an IDR picture.
    pub keyframe: bool,
    /// ffmpeg raw-frame handoff to complete MP4 fragment, milliseconds.
    pub jetson_encode_ms: Option<f64>,
    /// Socket receipt to validated camera publication of the last capture, ms.
    pub camera_ingest_ms: Option<f64>,
    pub received: f64,
    pub frame: i64,
    pub source: String,
    pub samples: Vec<Value>,
}

impl Fragment {
    /// video_segment fields (payload and receipt stay server-side), matching
    /// the Python `metadata` dict — every key present, diagnostics nullable.
    pub fn metadata(&self) -> Value {
        json!({
            "media_start_s": self.media_start_s,
            "media_end_s": self.media_end_s,
            "keyframe": self.keyframe,
            "jetson_encode_ms": self.jetson_encode_ms,
            "camera_ingest_ms": self.camera_ingest_ms,
            "frame": self.frame,
            "source": self.source,
            "samples": self.samples,
        })
    }
}

/// A fragment whose mdat carries an IDR picture, so a viewer can start
/// decoding on it. ffmpeg writes MP4 samples as AVCC: 4-byte big-endian NAL
/// lengths, not Annex B start codes, and default-base-is-moof says nothing
/// about sync samples. (Ported from quest-hud's _is_keyframe.)
pub fn is_keyframe(data: &[u8]) -> bool {
    let Some(at) = data.windows(4).position(|w| w == b"mdat") else {
        return false;
    };
    if at < 4 {
        return false;
    }
    let mut size = u32::from_be_bytes(data[at - 4..at].try_into().unwrap()) as u64;
    let (end, mut pos) = if size == 1 {
        // 64-bit largesize between kind and payload
        if at + 12 > data.len() {
            return false;
        }
        size = u64::from_be_bytes(data[at + 4..at + 12].try_into().unwrap());
        ((at as u64 - 4 + size).min(data.len() as u64) as usize, at + 12)
    } else {
        ((at as u64 - 4 + size).min(data.len() as u64) as usize, at + 4)
    };
    while pos + 4 <= end {
        let length = u32::from_be_bytes(data[pos..pos + 4].try_into().unwrap()) as usize;
        pos += 4;
        if length == 0 || pos + length > end {
            return false;
        }
        if data[pos] & 0x1F == 5 {
            return true;
        }
        pos += length;
    }
    false
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

/// One subscriber's bounded fragment backlog (cap 2): a late join waits for
/// an IDR before seeing any output, a newer queued IDR supersedes older queued
/// fragments, and overflow of dependent fragments closes the stream for
/// recovery (video.py subscribe/publish parity).
struct SubQueue {
    items: VecDeque<Option<Fragment>>,
    waiting_keyframe: bool,
    dead: bool,
}

struct SubShared {
    queue: Mutex<SubQueue>,
    notify: tokio::sync::Notify,
}

/// Receiver side of a subscriber backlog; `recv` yields `None` once the
/// backlog bound dropped this subscriber (the WS handler turns it into a
/// 1013 reconnect).
pub struct Subscriber {
    shared: Arc<SubShared>,
}

impl Subscriber {
    /// `Some(None)` carries the stream-close fragment; outer `None` means the
    /// subscriber itself was dropped (queue overflow or service shutdown).
    pub async fn recv(&self) -> Option<Option<Fragment>> {
        loop {
            {
                let mut q = self.shared.queue.lock().unwrap();
                if let Some(item) = q.items.pop_front() {
                    return Some(item);
                }
                if q.dead {
                    return None;
                }
            }
            self.shared.notify.notified().await;
        }
    }
}

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
    /// Completed MP4 fragment count (video_service.encoded parity).
    pub encoded: AtomicU64,
    /// Last fragment's ffmpeg handoff-to-complete time, ms (encode_ms parity).
    pub encode_ms: Mutex<Option<f64>>,
    /// Last fragment's camera ingest time, ms (ingest_ms parity).
    pub ingest_ms: Mutex<Option<f64>>,
    samples: Mutex<BTreeMap<u64, (CameraFrame, f64)>>,
    subscribers: Mutex<Vec<Arc<SubShared>>>,
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
            encoded: AtomicU64::new(0),
            encode_ms: Mutex::new(None),
            ingest_ms: Mutex::new(None),
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

    /// Mirrors subscribe(): a bounded (2) backlog that starts out waiting for
    /// the first independently decodable fragment.
    pub fn subscribe(&self) -> Subscriber {
        let shared = Arc::new(SubShared {
            queue: Mutex::new(SubQueue {
                items: VecDeque::with_capacity(2),
                waiting_keyframe: true,
                dead: false,
            }),
            notify: tokio::sync::Notify::new(),
        });
        self.subscribers.lock().unwrap().push(shared.clone());
        Subscriber { shared }
    }

    /// Mirrors publish(): subscribers waiting for a keyframe skip dependent
    /// fragments; a keyframe drains the backlog and unlatches them; a full
    /// queue is dropped entirely — the channel closes, which the WS handler
    /// turns into a 1013 reconnect.
    fn publish(&self, fragment: Option<Fragment>) {
        let mut subs = self.subscribers.lock().unwrap();
        subs.retain(|shared| {
            let mut q = shared.queue.lock().unwrap();
            if let Some(frag) = &fragment {
                if q.waiting_keyframe && !frag.keyframe {
                    return true;
                }
                if frag.keyframe {
                    // Jump only at an independently decodable picture.
                    q.items.clear();
                    q.waiting_keyframe = false;
                }
            }
            if q.items.len() >= 2 {
                // Never drop dependent MP4 fragments and keep presenting them.
                q.dead = true;
                shared.notify.notify_one();
                return false;
            }
            q.items.push_back(fragment.clone());
            shared.notify.notify_one();
            true
        });
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
        let gop = std::cmp::max(1, self.fps / 4).to_string();
        let args = [
            "-hide_banner", "-loglevel", "error", "-probesize", "32",
            "-analyzeduration", "0", "-f", "rawvideo",
            "-pix_fmt", "bgr24", "-s", &format!("{}x{}", self.width, self.height),
            "-r", &self.fps.to_string(),
            "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-threads", "1", "-bf", "0",
            "-profile:v", "baseline", "-level", "3.0",
            "-pix_fmt", "yuv420p", "-b:v", &self.bitrate.to_string(),
            // ~200ms VBV like Python: tighter buffer keeps latency down.
            "-maxrate", &self.bitrate.to_string(), "-bufsize", &std::cmp::max(64000, self.bitrate / 5).to_string(),
            // Short IDR intervals (200 ms at the default 10 fps) let late
            // joins decode quickly and make stale backlog recovery cheap.
            "-g", &gop, "-keyint_min", &gop,
            "-sc_threshold", "0", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-video_track_timescale", "1000000",
            "-frag_duration", "1", "-flush_packets", "1", "-f", "mp4", "pipe:1",
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
                        // JPEG decode may have overlapped a newer receipt;
                        // never queue an already stale picture into ffmpeg's
                        // input pipe.
                        if frame.age_ms() >= 500 {
                            std::thread::sleep(Duration::from_secs_f64(1.0 / self.fps as f64));
                            continue;
                        }
                        let mut bgr = resized.into_raw();
                        for px in bgr.chunks_exact_mut(3) {
                            px.swap(0, 2);
                        }
                        {
                            let mut samples = self.samples.lock().unwrap();
                            samples.insert(sample_id, (frame.clone(), crate::state::now_monotonic()));
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
        let mut matched = Vec::with_capacity(count as usize);
        {
            let mut samples = self.samples.lock().unwrap();
            for i in first..first + count {
                if let Some(item) = samples.remove(&i) {
                    matched.push(item);
                }
            }
        }
        let captures: Vec<&CameraFrame> = matched.iter().map(|(f, _)| f).collect();
        if captures.len() as u64 != count || captures.is_empty() {
            *self.error.lock().unwrap() = Some("Encoded fragment cannot be matched to capture receipt times".into());
            return None;
        }
        // ffmpeg raw-frame handoff to complete fragment; socket receipt to
        // camera publication of the last capture in this fragment.
        let now = crate::state::now_monotonic();
        let encode_ms = ((now - matched.iter().map(|(_, t)| *t).fold(f64::INFINITY, f64::min))
            * 1000.0
            * 100.0)
            .round()
            / 100.0;
        let last = captures.last().unwrap();
        let ingest_ms = match (last.published, last.socket_received) {
            (Some(p), Some(s)) => Some(((p - s) * 1000.0 * 100.0).round() / 100.0),
            _ => None,
        };
        self.encoded.fetch_add(count, Ordering::Relaxed);
        *self.encode_ms.lock().unwrap() = Some(encode_ms);
        *self.ingest_ms.lock().unwrap() = ingest_ms;
        let keyframe = is_keyframe(&payload);
        Some(Fragment {
            payload,
            media_start_s: start,
            media_end_s: end,
            keyframe,
            jetson_encode_ms: Some(encode_ms),
            camera_ingest_ms: ingest_ms,
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

#[cfg(test)]
mod tests {
    //! Port of jetson/tests/test_video.py's meaningful assertions: the AVCC
    /// keyframe scan, the late-join/backlog resume behavior, and the
    /// diagnostics a real ffmpeg fragment carries.
    use super::*;
    use crate::camera::CameraService;
    use crate::state::MapState;

    fn mdat(nal_type: u8) -> Vec<u8> {
        // size(4) + "mdat" + one length-prefixed NAL unit.
        let nal = vec![0x60 | nal_type, 0xAB, 0xCD];
        let size = (8 + 4 + nal.len()) as u32;
        let mut data = Vec::new();
        data.extend_from_slice(&size.to_be_bytes());
        data.extend_from_slice(b"mdat");
        data.extend_from_slice(&(nal.len() as u32).to_be_bytes());
        data.extend_from_slice(&nal);
        data
    }

    #[test]
    fn is_keyframe_scans_the_avcc_sample_list() {
        assert!(is_keyframe(&mdat(5)));
        // Only an IDR picture (nal_type 5) restarts a decoder.
        assert!(!is_keyframe(&mdat(7)));
        assert!(!is_keyframe(&mdat(1)));
        assert!(!is_keyframe(&[]));
        assert!(!is_keyframe(b"moov"));
        // A truncated length prefix is malformed, not a keyframe.
        let mut bad = mdat(5);
        bad.truncate(bad.len() - 2);
        assert!(!is_keyframe(&bad));
    }

    fn fragment(keyframe: bool) -> Fragment {
        Fragment {
            payload: mdat(if keyframe { 5 } else { 1 }),
            media_start_s: 0.0,
            media_end_s: 0.1,
            keyframe,
            jetson_encode_ms: Some(0.0),
            camera_ingest_ms: None,
            received: 0.0,
            frame: 0,
            source: "TEST".into(),
            samples: Vec::new(),
        }
    }

    fn service() -> Arc<VideoService> {
        Arc::new(VideoService::new(
            Arc::new(CameraService::new(Arc::new(MapState::new(false)), None, false)),
            false,
        ))
    }

    #[tokio::test]
    async fn late_join_and_backlog_resume_only_at_newest_keyframe() {
        let svc = service();
        let sub = svc.subscribe();
        // Dependent fragments while waiting for an IDR never reach the queue.
        svc.publish(Some(fragment(false)));
        svc.publish(Some(fragment(false)));
        assert!(sub.shared.queue.lock().unwrap().items.is_empty());
        // The first IDR unlatches the subscriber.
        svc.publish(Some(fragment(true)));
        // Dependents queue behind it...
        svc.publish(Some(fragment(false)));
        // ...but a newer IDR jumps the whole backlog.
        svc.publish(Some(fragment(true)));
        let first = sub.recv().await.unwrap().unwrap();
        assert!(first.keyframe);
        assert!(sub.shared.queue.lock().unwrap().items.is_empty());
    }

    #[tokio::test]
    async fn dependent_fragment_overflow_closes_the_subscriber() {
        let svc = service();
        let sub = svc.subscribe();
        svc.publish(Some(fragment(true)));
        svc.publish(Some(fragment(false)));
        svc.publish(Some(fragment(false)));
        // Queue was at cap 2 -> the subscriber is dead. Already-queued items
        // still drain in order, then the stream closes (None sentinel).
        assert!(sub.recv().await.unwrap().unwrap().keyframe);
        assert!(!sub.recv().await.unwrap().unwrap().keyframe);
        assert!(sub.recv().await.is_none());
    }

    #[test]
    fn fragment_metadata_carries_diagnostics_and_samples() {
        let svc = service();
        // Seed two captures at media samples 0 and 1 (encoded parity).
        let frame = CameraFrame {
            jpeg: vec![1, 2, 3],
            received: crate::state::now_monotonic() - 0.1,
            frame_id: 7,
            source: "TEST".into(),
            capture_verified: true,
            capture_binding: None,
            tracking: Some("normal".into()),
            mirrored: false,
            socket_received: Some(crate::state::now_monotonic() - 0.2),
            published: Some(crate::state::now_monotonic()),
        };
        {
            let mut samples = svc.samples.lock().unwrap();
            samples.insert(0, (frame.clone(), crate::state::now_monotonic() - 0.05));
            samples.insert(1, (frame, crate::state::now_monotonic() - 0.05));
        }
        let frag = svc
            .build_fragment(mdat(5), 0, 2, 0.0, 2.0 / svc.fps as f64)
            .expect("matched fragment");
        assert!(frag.keyframe);
        assert_eq!(frag.samples.len(), 2);
        assert_eq!(frag.frame, 7);
        assert!(frag.jetson_encode_ms.unwrap() >= 0.0);
        assert!(frag.camera_ingest_ms.unwrap() >= 0.0);
        assert_eq!(svc.encoded.load(Ordering::Relaxed), 2);
        assert!(svc.encode_ms.lock().unwrap().is_some());
        assert!(svc.ingest_ms.lock().unwrap().is_some());
        // Single-sample fragment metadata (test_video's len(samples)==1).
        {
            let mut samples = svc.samples.lock().unwrap();
            samples.insert(2, (captures_frame(), crate::state::now_monotonic()));
        }
        let one = svc
            .build_fragment(mdat(1), 2, 1, 2.0 / svc.fps as f64, 3.0 / svc.fps as f64)
            .expect("one-sample fragment");
        assert_eq!(one.samples.len(), 1);
        assert!(!one.keyframe);
    }

    fn captures_frame() -> CameraFrame {
        CameraFrame {
            jpeg: vec![9],
            received: 0.0,
            frame_id: 1,
            source: "TEST".into(),
            capture_verified: false,
            capture_binding: None,
            tracking: None,
            mirrored: false,
            socket_received: None,
            published: None,
        }
    }
}
