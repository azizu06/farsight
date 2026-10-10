//! Axum application — every endpoint of the Python FastAPI mapping server.
//! Port of jetson/mapping/server.py + jetson/control/service.py routing.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tower_http::services::ServeDir;

use crate::camera::CameraService;
use crate::control::{ControlState, SerialPump};
use crate::mission::Mission;
use crate::perception::PerceptionService;
use crate::phone::{self, Hello};
use crate::assistant::SceneAssistant;
use crate::phrases::{PhraseSpeaker, SpeechError};
use crate::replay::{ReplayControl, ReplaySession};
use crate::scan::ScanService;
use crate::state::{now_monotonic, MapState};
use crate::triage::{TriageError, TriageService};
use crate::video::VideoService;

pub struct AppState {
    pub mapping: Arc<MapState>,
    pub camera: Arc<CameraService>,
    pub video: Arc<VideoService>,
    pub control: Arc<ControlState>,
    pub pump: Arc<SerialPump>,
    pub perception: Arc<PerceptionService>,
    pub mission: Arc<Mission>,
    pub demo: bool,
    pub h264: bool,
    pub video_fps: u32,
    pub video_bitrate: u32,
    pub phone_owner: Mutex<bool>,
    pub assistant: Option<Arc<SceneAssistant>>,
    pub assistant_whisper: bool,
    pub assistant_piper: bool,
    pub triage: Arc<TriageService>,
    pub phrases: Arc<PhraseSpeaker>,
    pub scanner: Arc<ScanService>,
    pub record: bool,
    pub replay: Option<Arc<ReplaySession>>,
    /// Read-only control packet source when --replay is active.
    pub replay_control: Option<Arc<ReplayControl>>,
    /// Per-status-emission telemetry sink (control.on_status in Python).
    pub control_on_status: Option<Arc<dyn Fn(&Value) + Send + Sync>>,
}

pub fn build_state(
    demo: bool,
    camera_device: Option<String>,
    serial_port: Option<String>,
    simulate_rover: bool,
    detector: &str,
    detector_model: Option<String>,
    face_model: Option<String>,
    h264: bool,
    mission_dir: Option<PathBuf>,
    max_pwm: i64,
    assistant_url: Option<String>,
    whisper_bin: Option<String>,
    whisper_model: Option<String>,
    piper_bin: Option<String>,
    piper_model: Option<String>,
    tts_dir: Option<PathBuf>,
    whisper_url: Option<String>,
    piper_model_es: Option<String>,
    speaker_cmd: Option<String>,
    triage_interval: f64,
    depth_model: Option<String>,
    scan_pan: bool,
    scan_fov: f64,
    scan_intrinsics: Option<Vec<f64>>,
    scan_turn_dps: f64,
    video_fps: u32,
    video_bitrate: u32,
    record: bool,
    replay: Option<PathBuf>,
) -> Arc<AppState> {
    if replay.is_some()
        && (serial_port.is_some() || camera_device.is_some() || demo || simulate_rover || record)
    {
        panic!("Replay cannot be combined with serial, live/simulated sources, or recording");
    }
    if (demo || simulate_rover) && serial_port.is_some() {
        panic!("Simulated sources cannot be combined with a physical motor serial port");
    }
    let mapping = Arc::new(MapState::new(demo));
    let camera = Arc::new(CameraService::new(mapping.clone(), camera_device.clone()));
    let mut video = VideoService::new(camera.clone(), h264);
    video.fps = video_fps;
    video.bitrate = video_bitrate;
    let video = Arc::new(video);
    let mission = Arc::new(Mission::new(mission_dir, record).unwrap_or_else(|e| panic!("{e}")));
    let m = mission.clone();
    let on_detection: Option<Box<dyn Fn(&Value, &crate::camera::CameraFrame) + Send + Sync>> = if replay.is_some() {
        None // ReplayPerception never calls back into the mission.
    } else {
        Some(Box::new(move |packet, frame| m.detect(packet, frame)))
    };
    let perception = Arc::new(PerceptionService::new(
        camera.clone(),
        if replay.is_some() { "replay" } else { detector },
        on_detection,
        if replay.is_some() { None } else { detector_model.as_deref() },
        if replay.is_some() { None } else { face_model.as_deref() },
    ));
    let cam = camera.clone();
    let perc = perception.clone();
    let control = Arc::new(
        ControlState::new(
            simulate_rover,
            0,
            max_pwm,
            Some(Arc::new(move || {
                cam.status()["camera"].as_bool().unwrap_or(false)
            })),
            Some(Arc::new(move || perc.snapshot())),
            None,
        )
        .expect("PWM bounds"),
    );
    let pump = Arc::new(SerialPump::new(control.clone(), serial_port, simulate_rover));
    let replay_control = replay.as_ref().map(|_| Arc::new(ReplayControl::new()));
    let replay_session = match (&replay, &replay_control) {
        (Some(dir), Some(rc)) => Some(Arc::new(
            ReplaySession::new(dir, camera.clone(), perception.clone(), rc.clone())
                .unwrap_or_else(|e| panic!("{e}")),
        )),
        _ => None,
    };
    // Live control loops feed the drill scorer; recorded replays have no
    // live loop (hasattr(control, "state") in Python).
    if replay_session.is_none() {
        let m2 = mission.clone();
        *control.on_output.lock().unwrap() =
            Some(Box::new(move |v, w, blocked| m2.drill_motion(v, w, blocked)));
    }
    let control_on_status: Option<Arc<dyn Fn(&Value) + Send + Sync>> = if record {
        let m3 = mission.clone();
        Some(Arc::new(move |packet| m3.record("control", packet, None)))
    } else {
        None
    };
    let assistant_whisper = whisper_url.is_some() || (whisper_bin.is_some() && whisper_model.is_some());
    let assistant_piper = piper_model.is_some();
    let assistant = SceneAssistant::new(
        camera.clone(),
        perception.clone(),
        control.clone(),
        assistant_url,
        whisper_bin,
        whisper_model,
        piper_bin.clone(),
        piper_model.clone(),
        tts_dir.clone(),
        whisper_url,
    )
    .ok()
    .map(Arc::new);
    let range = control.clone();
    let triage = TriageService::new(
        mission.clone(),
        assistant.as_ref().and_then(|a| a.server_url.clone()),
        Arc::new(move || range.status(false)["range_cm"].as_f64()),
        triage_interval,
    );
    let voices = [("en", piper_model), ("es", piper_model_es)]
        .into_iter()
        .filter_map(|(lang, p)| p.map(|p| (lang.to_string(), PathBuf::from(p))))
        .collect();
    let phrases = Arc::new(PhraseSpeaker::new(tts_dir, voices, piper_bin, speaker_cmd));
    let scanner = Arc::new(
        ScanService::new(
            mapping.clone(),
            camera.clone(),
            control.clone(),
            perception.clone(),
            depth_model,
            scan_pan,
            scan_fov,
            scan_intrinsics,
            scan_turn_dps,
        )
        .expect("scan configuration"),
    );
    Arc::new(AppState {
        mapping,
        camera,
        video,
        control,
        pump,
        perception,
        mission,
        demo,
        h264,
        video_fps,
        video_bitrate,
        phone_owner: Mutex::new(false),
        assistant,
        assistant_whisper,
        assistant_piper,
        triage,
        phrases,
        scanner,
        record,
        replay: replay_session,
        replay_control,
        control_on_status,
    })
}

/// Lifespan start: kicks off control pump, camera, video, perception, and
/// (in demo mode) the simulated frame feeder.
pub fn start_services(state: &Arc<AppState>) {
    state.pump.clone().start();
    state.camera.clone().start();
    state.video.clone().start();
    state.perception.clone().start();
    state.triage.clone().start();
    if let Some(session) = &state.replay {
        let session = session.clone();
        std::thread::spawn(move || loop {
            let started = now_monotonic();
            session.publish(None); // Clear all streams at the loop boundary.
            for event in &session.run.events {
                let wait = started + event["t"].as_f64().unwrap_or(0.0) - now_monotonic();
                if wait > 0.0 {
                    std::thread::sleep(Duration::from_secs_f64(wait));
                }
                session.publish(Some(event));
            }
            let tail = started + session.run.duration - now_monotonic();
            if tail > 0.0 {
                std::thread::sleep(Duration::from_secs_f64(tail));
            }
        });
    }
    if state.record {
        let state = state.clone();
        std::thread::spawn(move || {
            let mut previous: Option<(String, i64, i64)> = None;
            let mut next_status = 0.0f64;
            loop {
                let frame = state.camera.snapshot();
                let identity = frame
                    .as_ref()
                    .map(|f| (f.source.clone(), f.frame_id, f.received.to_bits() as i64));
                if let Some(f) = &frame {
                    if identity != previous {
                        state.mission.record("frame", &json!(null), Some(f));
                        previous = identity;
                    }
                }
                if now_monotonic() >= next_status {
                    let status = if let Some(rc) = &state.replay_control {
                        rc.status()
                    } else {
                        state.control.status(false)
                    };
                    state.mission.record("control", &status, None);
                    next_status = now_monotonic() + 0.1;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        });
    }
    if state.demo {
        // #59 parity: 15 Hz synthetic camera with bounded 5 Hz fusion —
        // capture cadence is independent of map work; busy fusion skips
        // candidates so queued geometry never goes stale.
        let mapping = state.mapping.clone();
        let camera = state.camera.clone();
        let busy = Arc::new(std::sync::atomic::AtomicBool::new(false));
        std::thread::spawn(move || {
            let started = now_monotonic();
            let mut frame_id = 0i64;
            let mut next_fusion = 0.0f64;
            loop {
                let captured = now_monotonic();
                let frame = crate::demo::simulated_frame(
                    captured - started,
                    frame_id,
                    &("simulated-room".to_string(), 1),
                );
                camera.set_phone_rgb(Some(crate::camera::CameraFrame {
                    jpeg: frame.jpeg.clone(),
                    received: captured,
                    frame_id,
                    source: "SIMULATED".into(),
                }));
                if captured >= next_fusion
                    && !busy.swap(true, std::sync::atomic::Ordering::SeqCst)
                {
                    let (mapping, busy) = (mapping.clone(), busy.clone());
                    next_fusion = captured + 0.2;
                    std::thread::spawn(move || {
                        let _ = mapping.process(&frame);
                        busy.store(false, std::sync::atomic::Ordering::SeqCst);
                    });
                }
                frame_id += 1;
                let elapsed = now_monotonic() - captured;
                let wait = 1.0 / 15.0 - elapsed;
                if wait > 0.0 {
                    std::thread::sleep(Duration::from_secs_f64(wait));
                }
            }
        });
    }
}

// ---------- HTTP routes ----------

async fn health(State(s): State<Arc<AppState>>) -> Json<Value> {
    let mut status = s.mapping.status();
    let cam = s.camera.status();
    let rover = s
        .replay_control
        .as_ref()
        .map(|rc| rc.status())
        .unwrap_or_else(|| s.control.status(false));
    let video_err = s.video.error.lock().unwrap().clone();
    let obj = status.as_object_mut().unwrap();
    for (k, v) in cam.as_object().unwrap() {
        obj.insert(k.clone(), v.clone());
    }
    obj.insert("replay".into(), json!(s.replay.is_some()));
    obj.insert("recording".into(), json!(s.record));
    obj.insert("scan".into(), s.scanner.result());
    for (k, v) in s.perception.health().as_object().unwrap() {
        obj.insert(k.clone(), v.clone());
    }
    obj.insert("rover".into(), rover["rover"].clone());
    obj.insert("estop".into(), rover["estop"].clone());
    obj.insert("control".into(), rover);
    obj.insert(
        "video".into(),
        json!({"enabled": s.h264, "ready": s.video.ready(), "error": video_err,
               "fps": s.video_fps, "bitrate": s.video_bitrate}),
    );
    obj.insert(
        "assistant".into(),
        json!({
            "vision_model": s.assistant.as_ref().map(|a| a.server_url.is_some()).unwrap_or(false),
            "whisper": s.assistant_whisper,
            "piper": s.assistant_piper,
        }),
    );
    obj.insert("speech".into(), s.phrases.status());
    obj.insert("triage".into(), s.triage.status());
    Json(status)
}

async fn scan_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(s.scanner.result())
}

async fn scan_start(State(s): State<Arc<AppState>>, Json(body): Json<Value>) -> Response {
    let session_id = body["session_id"].as_str();
    let permit = body["permit"].as_str();
    if session_id.is_none() || permit.is_none() {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": [{"loc": ["body", "permit"], "msg": "Field required", "type": "missing"}]})),
        )
            .into_response();
    }
    if s.replay.is_some() {
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "Replay is read-only; live scans are disabled"})),
        )
            .into_response();
    }
    let session_id = session_id.map(str::to_string);
    let permit = permit.map(str::to_string);
    let scanner = s.scanner.clone();
    let result = tokio::task::spawn_blocking(move || {
        scanner.start(session_id.as_deref(), permit.as_deref())
    })
    .await
    .map_err(|e| e.to_string())
    .and_then(|r| r);
    match result {
        Ok(v) => (StatusCode::ACCEPTED, Json(v)).into_response(),
        Err(e) => (StatusCode::CONFLICT, Json(json!({"detail": e}))).into_response(),
    }
}

async fn map_points(State(s): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let (body, coding) = s.mapping.point_packet(accept_encoding(&headers));
    crate::pacer::pace(body.len()).await;
    packet_response(body, coding, false)
}

async fn map_mesh(State(s): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let (body, coding) = s.mapping.mesh_packet(accept_encoding(&headers));
    crate::pacer::pace(body.len()).await;
    packet_response(body, coding, true)
}

fn accept_encoding(headers: &HeaderMap) -> &str {
    headers.get(header::ACCEPT_ENCODING).and_then(|v| v.to_str().ok()).unwrap_or("")
}

fn packet_response(body: Vec<u8>, coding: &'static str, no_store: bool) -> Response {
    let mut response = ([(header::CONTENT_TYPE, "application/octet-stream"), (header::VARY, "Accept-Encoding")], body).into_response();
    if !coding.is_empty() {
        response.headers_mut().insert(header::CONTENT_ENCODING, coding.parse().unwrap());
    }
    if no_store {
        response.headers_mut().insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    }
    response
}

async fn map_export(State(s): State<Arc<AppState>>) -> Response {
    (
        [
            (header::CONTENT_TYPE, "application/octet-stream"),
            (header::CONTENT_DISPOSITION, "attachment; filename=\"farsight-scan.ply\""),
        ],
        s.mapping.ply(),
    )
        .into_response()
}

async fn map_reset(State(s): State<Arc<AppState>>) -> Json<Value> {
    s.mapping.clear();
    Json(s.mapping.status())
}

/// Pydantic-style coercion: bool fields accept true/false, 0/1 and the usual
/// truthy/falsey strings; int fields accept ints, integral floats and numeric
/// strings. Anything else (including a missing bool) is a 422, matching
/// FastAPI's request validation.
fn pydantic_bool(v: &Value) -> Option<bool> {
    match v {
        Value::Bool(b) => Some(*b),
        Value::Number(n) => n.as_i64().and_then(|i| match i {
            0 => Some(false),
            1 => Some(true),
            _ => None,
        }),
        Value::String(s) => match s.to_ascii_lowercase().as_str() {
            "true" | "t" | "yes" | "y" | "on" | "1" => Some(true),
            "false" | "f" | "no" | "n" | "off" | "0" => Some(false),
            _ => None,
        },
        _ => None,
    }
}

fn pydantic_int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Some(i)
            } else {
                n.as_f64().and_then(|f| {
                    if f.fract() == 0.0 && f.abs() <= i64::MAX as f64 {
                        Some(f as i64)
                    } else {
                        None
                    }
                })
            }
        }
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

async fn map_pause(State(s): State<Arc<AppState>>, Json(body): Json<Value>) -> Response {
    let Some(paused) = pydantic_bool(&body["paused"]) else {
        return (StatusCode::UNPROCESSABLE_ENTITY, "paused must be a boolean").into_response();
    };
    // Resuming replaces a camera-only scan snapshot with live fusion.
    if !paused && s.mapping.inner.lock().unwrap().scan.is_some() {
        s.mapping.clear();
    }
    s.mapping.set_paused(paused);
    Json(s.mapping.status()).into_response()
}

async fn camera_jpg(State(s): State<Arc<AppState>>) -> Response {
    match s.camera.preview() {
        Some(frame) => {
            crate::pacer::pace(frame.jpeg.len()).await;
            let fid = frame.frame_id;
            let age = frame.age_ms();
            let source = frame.source.clone();
            let mut resp = Response::new(Body::from(frame.jpeg));
            let h = resp.headers_mut();
            h.insert(header::CONTENT_TYPE, "image/jpeg".parse().unwrap());
            h.insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
            h.insert("x-frame-id", fid.to_string().parse().unwrap());
            h.insert("x-frame-age-ms", age.to_string().parse().unwrap());
            h.insert("x-frame-source", source.parse().unwrap());
            resp
        }
        None => (StatusCode::SERVICE_UNAVAILABLE, "Waiting for a camera frame").into_response(),
    }
}

async fn depth_jpg(State(s): State<Arc<AppState>>) -> Response {
    let jpeg = s.mapping.inner.lock().unwrap().depth_jpeg.clone();
    match jpeg {
        Some(j) => (
            [(header::CONTENT_TYPE, "image/jpeg"), (header::CACHE_CONTROL, "no-store")],
            j,
        )
            .into_response(),
        None => (StatusCode::SERVICE_UNAVAILABLE, "Waiting for measured depth").into_response(),
    }
}

async fn stream_mjpg(State(s): State<Arc<AppState>>) -> Response {
    let camera = s.camera.clone();
    let stream = async_stream::stream! {
        let mut last: Option<(String, i64)> = None;
        loop {
            let frame = camera.preview();
            let identity = frame.as_ref().map(|f| (f.source.clone(), f.frame_id));
            if let Some(f) = frame {
                if f.age_ms() < 500 && identity != last {
                    let mut part = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n".to_vec();
                    part.extend_from_slice(&f.jpeg);
                    part.extend_from_slice(b"\r\n");
                    crate::pacer::pace(part.len()).await;
                    yield Ok::<_, std::io::Error>(part);
                    last = identity;
                }
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    };
    Response::builder()
        .header(header::CONTENT_TYPE, "multipart/x-mixed-replace; boundary=frame")
        .body(Body::from_stream(stream))
        .unwrap()
}

async fn mission_report(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(s.mission.report())
}

async fn mission_snapshot(State(s): State<Arc<AppState>>, Path(name): Path<String>) -> Response {
    match s.mission.snapshot_file(&name) {
        Some(bytes) => ([(header::CONTENT_TYPE, "image/jpeg")], bytes).into_response(),
        None => (StatusCode::NOT_FOUND, "Mission snapshot unavailable").into_response(),
    }
}

async fn mission_drill_start(State(s): State<Arc<AppState>>, Json(body): Json<Value>) -> Response {
    let duration = match &body["duration_s"] {
        Value::Null => 90,
        v => match pydantic_int(v) {
            Some(i) => i,
            None => {
                return (StatusCode::UNPROCESSABLE_ENTITY, "duration_s must be an integer").into_response();
            }
        },
    };
    if !(10..=300).contains(&duration) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Drill duration must be 10..300 seconds").into_response();
    }
    let victims = match &body["victims"] {
        Value::Null => 3,
        v => match pydantic_int(v) {
            Some(i) => i,
            None => {
                return (StatusCode::UNPROCESSABLE_ENTITY, "victims must be an integer").into_response();
            }
        },
    };
    if !(1..=20).contains(&victims) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Drill victims must be 1..20").into_response();
    }
    Json(s.mission.start_drill(duration as f64, victims)).into_response()
}

async fn mission_drill_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(s.mission.drill())
}

async fn mission_triage(State(s): State<Arc<AppState>>, Path(key): Path<String>) -> Response {
    let triage = s.triage.clone();
    match tokio::task::spawn_blocking(move || triage.generate(&key)).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(TriageError::Unknown(m))) => {
            (StatusCode::NOT_FOUND, Json(json!({"detail": m}))).into_response()
        }
        Ok(Err(TriageError::Busy(m))) => {
            (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"detail": m}))).into_response()
        }
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn speak_options(State(s): State<Arc<AppState>>) -> Json<Value> {
    let mut out = json!({"phrases": s.phrases.phrases()});
    for (k, v) in s.phrases.status().as_object().unwrap() {
        out[k.clone()] = v.clone();
    }
    Json(out)
}

async fn speak(State(s): State<Arc<AppState>>, Json(body): Json<Value>) -> Response {
    let phrase = body["phrase"].as_str();
    let lang = body["lang"].as_str();
    if phrase.is_none() || lang.is_none() {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": [{"loc": ["body", "lang"], "msg": "Field required", "type": "missing"}]})),
        )
            .into_response();
    }
    match s.phrases.speak(phrase.unwrap(), lang.unwrap()).await {
        Ok(v) => Json(v).into_response(),
        Err(SpeechError { status_code, message }) => (
            StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
            Json(json!({"detail": message})),
        )
            .into_response(),
    }
}

async fn ask(State(s): State<Arc<AppState>>, headers: HeaderMap, body: Bytes) -> Response {
    let content_type = headers
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim();
    let Some(assistant) = s.assistant.as_ref() else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Assistant is not configured").into_response();
    };
    let result = if content_type == "application/json" {
        let body: Value = match serde_json::from_slice::<Value>(&body) {
            Ok(v) if v.is_object() => v,
            _ => return (StatusCode::BAD_REQUEST, "Expected a text question object").into_response(),
        };
        assistant.ask(body["text"].as_str().map(String::from), None).await
    } else if content_type == "multipart/form-data" {
        let ct = headers.get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("");
        let boundary = ct.rsplit("boundary=").next().unwrap_or("");
        // minimal multipart parse: last file part named "audio"
        let audio = extract_audio(&body[..], boundary);
        match audio {
            Some(a) => assistant.ask(None, Some(a)).await,
            None => return (StatusCode::BAD_REQUEST, "Expected an audio file").into_response(),
        }
    } else {
        return (StatusCode::UNSUPPORTED_MEDIA_TYPE, "Use JSON text or multipart audio").into_response();
    };
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => (StatusCode::from_u16(e.status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR), e.message).into_response(),
    }
}

fn extract_audio(body: &[u8], boundary: &str) -> Option<Vec<u8>> {
    let marker = format!("--{boundary}").into_bytes();
    let mut start = 0;
    while let Some(pos) = find_subslice(&body[start..], &marker).map(|p| p + start) {
        let mut p = pos + marker.len();
        if body.get(p..p+2) == Some(b"\r\n") { p += 2; }
        let hdr_end = find_subslice(&body[p..], b"\r\n\r\n").map(|e| e + p)?;
        let headers = String::from_utf8_lossy(&body[p..hdr_end]);
        let data_start = hdr_end + 4;
        let next = find_subslice(&body[data_start..], &marker).map(|e| e + data_start);
        let data_end = next.map(|e| e.saturating_sub(2)).unwrap_or(body.len());
        if headers.contains("name=\"audio\"") {
            return Some(body[data_start..data_end].to_vec());
        }
        start = data_start;
    }
    None
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

async fn tts(State(s): State<Arc<AppState>>, Path(name): Path<String>) -> Response {
    let dir = s.assistant.as_ref().and_then(|a| a.output_dir.clone());
    if !((name.starts_with("reply-") || name.starts_with("phrase-")) && name.ends_with(".wav") && !name.contains('/') && !name.contains('\\')) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Some(dir) = dir else { return StatusCode::NOT_FOUND.into_response(); };
    let path = dir.join(&name);
    match std::fs::read(&path) {
        Ok(bytes) => ([("content-type", "audio/wav")], bytes).into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn control_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    if let Some(rc) = &s.replay_control {
        return Json(rc.public_status());
    }
    Json(s.control.public_status())
}

// ---------- WebSocket routes ----------

async fn ws_map(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_map(socket, s))
}

async fn handle_ws_map(socket: WebSocket, s: Arc<AppState>) {
    let (mut tx, mut rx) = socket.split();
    let mapping = s.mapping.clone();
    let sender = tokio::spawn(async move {
        let mut revision: i64 = -1;
        loop {
            let status = mapping.status();
            if status["revision"].as_i64().unwrap_or(-1) != revision {
                let (packet, _) = mapping.point_packet("");
                if tx.send(Message::Text(status.to_string().into())).await.is_err() {
                    return;
                }
                crate::pacer::pace(packet.len()).await;
                if tx.send(Message::Binary(packet.into())).await.is_err() {
                    return;
                }
                revision = status["revision"].as_i64().unwrap_or(-1);
            } else if tx.send(Message::Text(status.to_string().into())).await.is_err() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    });
    while rx.next().await.is_some() {}
    sender.abort();
}

async fn ws_control(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_control(socket, s))
}

async fn handle_ws_control(socket: WebSocket, s: Arc<AppState>) {
    // ReplayControl.websocket: replays the recorded packet at ~10 Hz and
    // intentionally never executes commands.
    if let Some(rc) = s.replay_control.clone() {
        let (mut tx, mut rx) = socket.split();
        let sender = tokio::spawn(async move {
            loop {
                let status = rc.status();
                match tokio::time::timeout(
                    Duration::from_millis(500),
                    tx.send(Message::Text(status.to_string().into())),
                )
                .await
                {
                    Ok(Ok(_)) => {}
                    _ => return,
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        });
        while rx.next().await.is_some() {}
        sender.abort();
        return;
    }
    let Some(session_id) = s.control.begin() else {
        let mut socket = socket;
        let _ = socket
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1008,
                reason: "Another controller owns the rover".into(),
            })))
            .await;
        return;
    };
    let (mut tx, mut rx) = socket.split();
    let control = s.control.clone();
    let on_status = s.control_on_status.clone();
    let sender = tokio::spawn(async move {
        loop {
            // A congested status path makes continued control unsafe; a send
            // timeout closes the socket rather than queueing permits.
            let started = now_monotonic();
            let status = control.status(true);
            match tokio::time::timeout(Duration::from_millis(100), tx.send(Message::Text(status.to_string().into()))).await {
                Ok(Ok(_)) => {}
                _ => return,
            }
            if let Some(hook) = &on_status {
                hook(&status);
            }
            // 20 Hz target accounting for emit cost, per control/service.py.
            tokio::time::sleep(Duration::from_secs_f64((0.05 - (now_monotonic() - started)).max(0.0))).await;
        }
    });
    while let Some(Ok(message)) = rx.next().await {
        match message {
            Message::Text(text) => {
                if text.len() > 2048 {
                    s.control.stop("oversized_command", false);
                    break;
                }
                match serde_json::from_str::<Value>(&text) {
                    Ok(v) => {
                        s.control.accept(&v, &session_id);
                    }
                    Err(_) => s.control.stop("invalid_json", false),
                }
            }
            Message::Binary(_) => {
                s.control.stop("binary_control_rejected", false);
                break;
            }
            _ => {}
        }
    }
    sender.abort();
    s.control.end(&session_id);
}

async fn ws_detections(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| async move {
        let (mut tx, _) = socket.split();
        loop {
            if tx
                .send(Message::Text(s.perception.snapshot().to_string().into()))
                .await
                .is_err()
            {
                return;
            }
            tokio::time::sleep(Duration::from_millis(300)).await;
        }
    })
}

async fn ws_video(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_video(socket, s))
}

async fn handle_ws_video(socket: WebSocket, s: Arc<AppState>) {
    let (mut tx, mut rx) = socket.split();
    macro_rules! close {
        ($code:expr, $reason:expr) => {{
            let _ = tx
                .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                    code: $code,
                    reason: $reason.into(),
                })))
                .await;
            return;
        }};
    }
    if !s.h264 {
        let _ = tx.send(Message::Text(json!({"type":"video_error","reason":"H264 disabled; use camera.jpg"}).to_string().into())).await;
        close!(1008, "");
    }
    for _ in 0..100 {
        if s.video.ready() || s.video.error.lock().unwrap().is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    if !s.video.ready() {
        let err = s.video.error.lock().unwrap().clone().unwrap_or_else(|| "Waiting for camera".into());
        let _ = tx.send(Message::Text(json!({"type":"video_error","reason":err}).to_string().into())).await;
        close!(1013, "");
    }
    let init = s.video.init.lock().unwrap().clone().unwrap_or_default();
    let mut queue = s.video.subscribe();
    let frame = s.camera.snapshot();
    let _ = tx
        .send(Message::Text(
            json!({"type":"video_init","mime":s.video.mime.lock().unwrap().clone(),"source":frame.map(|f| f.source).unwrap_or_else(||"WAITING".into())})
                .to_string()
                .into(),
        ))
        .await;
    let _ = tx.send(Message::Binary(init.into())).await;
    // Pipelined requests: bounded request window of 4 (Python requests Queue).
    let shared_tx = Arc::new(tokio::sync::Mutex::new(tx));
    let (req_tx, mut req_rx) = tokio::sync::mpsc::channel::<(i64, f64)>(4);
    let receiver = tokio::spawn(async move {
        loop {
            let valid = match tokio::time::timeout(Duration::from_secs(10), rx.next()).await {
                Ok(Some(Ok(Message::Text(t)))) => serde_json::from_str::<Value>(&t)
                    .ok()
                    .and_then(|v| {
                        let id = v["request_id"].as_i64()?;
                        if v.is_object() && v["type"] == "next" && (0..=(1i64 << 53) - 1).contains(&id) {
                            Some(id)
                        } else {
                            None
                        }
                    }),
                _ => None,
            };
            match valid {
                Some(id) if req_tx.try_send((id, now_monotonic())).is_ok() => {}
                _ => return, // Invalid or excessive video request window
            }
        }
    });
    let tx2 = shared_tx.clone();
    let sender = tokio::spawn(async move {
        while let Some((request_id, waiting_at)) = req_rx.recv().await {
            let fragment = match tokio::time::timeout(Duration::from_secs(2), queue.recv()).await {
                Ok(Some(Some(f))) => f,
                Ok(Some(None)) | Ok(None) | Err(_) => return,
            };
            let mut metadata = fragment.metadata();
            metadata["type"] = json!("video_segment");
            metadata["request_id"] = json!(request_id);
            metadata["server_wait_ms"] = json!((now_monotonic() - waiting_at) * 1000.0);
            metadata["age_ms"] = json!(((now_monotonic() - fragment.received) * 1000.0).round().max(0.0) as i64);
            let mut out = tx2.lock().await;
            if tokio::time::timeout(Duration::from_millis(500), out.send(Message::Text(metadata.to_string().into()))).await.is_err() {
                return;
            }
            crate::pacer::pace(fragment.payload.len()).await;
            if tokio::time::timeout(Duration::from_millis(500), out.send(Message::Binary(fragment.payload.into()))).await.is_err() {
                return;
            }
            drop(out);
        }
    });
    // FIRST_COMPLETED: whichever loop ends first tears down the session (1013).
    tokio::pin!(receiver);
    tokio::pin!(sender);
    tokio::select! {
        _ = &mut receiver => {}
        _ = &mut sender => {}
    }
    receiver.abort();
    sender.abort();
    {
        let mut out = shared_tx.lock().await;
        let _ = out
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1013,
                reason: "".into(),
            })))
            .await;
    }
}

async fn ws_phone(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_phone(socket, s))
}

async fn handle_ws_phone(socket: WebSocket, s: Arc<AppState>) {
    let (mut tx, mut rx) = socket.split();
    let reject = {
        let mut owned = s.phone_owner.lock().unwrap();
        if s.demo || *owned {
            true
        } else {
            *owned = true;
            false
        }
    };
    if reject {
        let _ = tx
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1008,
                reason: "Demo is active or another phone owns the stream".into(),
            })))
            .await;
        return;
    }
    let result = async {
        let hello_text = match tokio::time::timeout(Duration::from_secs(10), rx.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => t,
            _ => return Err("Expected phone hello".to_string()),
        };
        let hello = Hello::parse(&hello_text)?;
        if !hello.supports_scene_depth {
            return Err("This mapper requires measured scene depth".into());
        }
        let identity: phone::Identity = (hello.session_id.clone(), hello.map_epoch);
        s.camera.set_phone_rgb(None);
        s.mapping.start_session(identity.clone(), hello.device.clone());
        // Frame queue of depth 1, processed on a blocking worker thread.
        let (qtx, mut qrx) = tokio::sync::mpsc::channel::<Vec<u8>>(1);
        let mapping = s.mapping.clone();
        let ident = identity.clone();
        let worker = tokio::spawn(async move {
            while let Some(packet) = qrx.recv().await {
                let mapping2 = mapping.clone();
                let ident2 = ident.clone();
                let r = tokio::task::spawn_blocking(move || {
                    phone::decode_frame(&packet, &ident2).and_then(|f| mapping2.process(&f))
                })
                .await;
                match r {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => mapping.reject(e),
                    Err(_) => {}
                }
            }
        });
        loop {
            match rx.next().await {
                Some(Ok(Message::Binary(bytes))) => {
                    if qtx.try_send(bytes.to_vec()).is_err() {
                        s.mapping.inner.lock().unwrap().dropped += 1;
                    }
                }
                Some(Ok(Message::Text(text))) => {
                    let header: Value = match serde_json::from_str(&text) {
                        Ok(v) => v,
                        Err(_) => return Err("Expected a pose message or binary RGB-D frame".into()),
                    };
                    if header.get("type").and_then(Value::as_str) != Some("pose") {
                        return Err("Expected a pose message or binary RGB-D frame".into());
                    }
                    let transform = phone::validate_pose(&header, &identity)?;
                    s.mapping.observe_pose(&header, &transform)?;
                }
                Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                _ => {}
            }
        }
        worker.abort();
        Ok(())
    }
    .await;
    if let Err(reason) = result {
        let _ = tx
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1008,
                reason: reason.chars().take(100).collect::<String>().into(),
            })))
            .await;
    }
    *s.phone_owner.lock().unwrap() = false;
    s.mapping.disconnect();
}

async fn ws_phone_rgb(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_phone_rgb(socket, s))
}

async fn handle_ws_phone_rgb(socket: WebSocket, s: Arc<AppState>) {
    let (mut tx, mut rx) = socket.split();
    let reject = {
        let mut owned = s.phone_owner.lock().unwrap();
        if s.demo || *owned || s.camera.device.is_some() {
            true
        } else {
            *owned = true;
            false
        }
    };
    if reject {
        let _ = tx
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1008,
                reason: "Another camera source is active".into(),
            })))
            .await;
        return;
    }
    let result = async {
        let hello_text = match tokio::time::timeout(Duration::from_secs(10), rx.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => t,
            _ => return Err("Expected phone hello".to_string()),
        };
        let hello = Hello::parse(&hello_text)?;
        s.camera.set_phone_rgb(None);
        let mut previous: i64 = -1;
        while let Some(Ok(message)) = rx.next().await {
            if let Message::Binary(packet) = message {
                let packet = packet.to_vec();
                let cam = s.camera.clone();
                let session = hello.session_id.clone();
                previous = tokio::task::spawn_blocking(move || cam.accept_phone_rgb(&packet, &session, previous))
                    .await
                    .map_err(|_| "worker failed".to_string())??;
            }
        }
        Ok(())
    }
    .await;
    if let Err(reason) = result {
        let _ = tx
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1008,
                reason: reason.chars().take(100).collect::<String>().into(),
            })))
            .await;
    }
    *s.phone_owner.lock().unwrap() = false;
}

async fn index_fallback() -> Json<Value> {
    Json(json!({"message": "Build the viewer: npm --prefix quest install && npm --prefix quest run build"}))
}

pub fn router(state: Arc<AppState>, dist: Option<PathBuf>) -> Router {
    let app = Router::new()
        .route("/health", get(health))
        // Raw 16 B/point payloads crossing a cellular link; compressed
        // variants are cached per map version inside MapState.
        .route("/map/points.bin", get(map_points))
        .route("/map/mesh.bin", get(map_mesh))
        .route("/map/export.ply", get(map_export))
        .route("/map/reset", post(map_reset))
        .route("/map/pause", post(map_pause))
        .route("/camera.jpg", get(camera_jpg))
        .route("/depth.jpg", get(depth_jpg))
        .route("/stream.mjpg", get(stream_mjpg))
        .route("/ws/video", get(ws_video))
        .route("/ws/detections", get(ws_detections))
        .route("/ws/map", get(ws_map))
        .route("/ws/control", get(ws_control))
        .route("/phone", get(ws_phone))
        .route("/phone/rgb", get(ws_phone_rgb))
        .route("/control/status", get(control_status))
        .route("/scan", get(scan_status))
        .route("/scan", post(scan_start))
        .route("/mission/report", get(mission_report))
        .route("/mission/snapshots/{name}", get(mission_snapshot))
        .route("/mission/drill", post(mission_drill_start))
        .route("/mission/drill", get(mission_drill_status))
        .route("/mission/triage/{key}", post(mission_triage))
        .route("/ask", post(ask))
        .route("/speak", get(speak_options))
        .route("/speak", post(speak))
        .route("/tts/{name}", get(tts));
    let app = if let Some(dist) = dist {
        if dist.is_dir() {
            app.fallback_service(ServeDir::new(dist).append_index_html_on_directories(true))
        } else {
            app.route("/", get(index_fallback))
        }
    } else {
        app.route("/", get(index_fallback))
    };
    app.with_state(state)
}

/// Serve the app over plain HTTP (TLS handled in main via axum-server).
pub async fn serve_http(app: Router, addr: SocketAddr) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await
}
