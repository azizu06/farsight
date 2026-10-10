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
use crate::state::{now_monotonic, MapState};
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
    pub phone_owner: Mutex<bool>,
    pub assistant: Option<Arc<SceneAssistant>>,
}

pub fn build_state(
    demo: bool,
    camera_device: Option<String>,
    serial_port: Option<String>,
    simulate_rover: bool,
    detector: &str,
    detector_model: Option<String>,
    h264: bool,
    mission_dir: Option<PathBuf>,
    max_pwm: i64,
    assistant_url: Option<String>,
    whisper_bin: Option<String>,
    whisper_model: Option<String>,
    piper_bin: Option<String>,
    piper_model: Option<String>,
    tts_dir: Option<PathBuf>,
) -> Arc<AppState> {
    if (demo || simulate_rover) && serial_port.is_some() {
        panic!("Simulated sources cannot be combined with a physical motor serial port");
    }
    let mapping = Arc::new(MapState::new(demo));
    let camera = Arc::new(CameraService::new(mapping.clone(), camera_device.clone()));
    let video = Arc::new(VideoService::new(camera.clone(), h264));
    let mission = Arc::new(Mission::new(mission_dir));
    let m = mission.clone();
    let perception = Arc::new(PerceptionService::new(
        camera.clone(),
        detector,
        Some(Box::new(move |packet, frame| m.detect(packet, frame))),
        detector_model.as_deref(),
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
    let assistant = SceneAssistant::new(
        camera.clone(),
        perception.clone(),
        control.clone(),
        assistant_url,
        whisper_bin,
        whisper_model,
        piper_bin,
        piper_model,
        tts_dir,
    )
    .ok();
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
        phone_owner: Mutex::new(false),
        assistant: assistant.map(Arc::new),
    })
}

/// Lifespan start: kicks off control pump, camera, video, perception, and
/// (in demo mode) the simulated frame feeder.
pub fn start_services(state: &Arc<AppState>) {
    state.pump.clone().start();
    state.camera.clone().start();
    state.video.clone().start();
    state.perception.clone().start();
    if state.demo {
        let mapping = state.mapping.clone();
        std::thread::spawn(move || {
            let started = now_monotonic();
            let mut frame_id = 0i64;
            loop {
                let frame = crate::demo::simulated_frame(
                    now_monotonic() - started,
                    frame_id,
                    &("simulated-room".to_string(), 1),
                );
                let _ = mapping.process(&frame);
                frame_id += 1;
                std::thread::sleep(Duration::from_millis(160));
            }
        });
    }
}

// ---------- HTTP routes ----------

async fn health(State(s): State<Arc<AppState>>) -> Json<Value> {
    let mut status = s.mapping.status();
    let cam = s.camera.status();
    let rover = s.control.status(false);
    let video_err = s.video.error.lock().unwrap().clone();
    let init_ready = s.video.init.lock().unwrap().is_some();
    let obj = status.as_object_mut().unwrap();
    for (k, v) in cam.as_object().unwrap() {
        obj.insert(k.clone(), v.clone());
    }
    obj.insert("rover".into(), rover["rover"].clone());
    obj.insert("estop".into(), rover["estop"].clone());
    obj.insert("control".into(), rover);
    obj.insert("detector".into(), json!(s.perception.mode));
    obj.insert("detector_error".into(), s.perception.snapshot()["error"].clone());
    obj.insert("detector_fps".into(), s.perception.snapshot()["fps"].clone());
    obj.insert(
        "assistant".into(),
        json!({
            "vision_model": s.assistant.as_ref().map(|a| a.server_url.is_some()).unwrap_or(false),
            "tts": s.assistant.as_ref().map(|a| a.output_dir.is_some()).unwrap_or(false),
        }),
    );
    obj.insert(
        "video".into(),
        json!({"enabled": s.h264, "ready": init_ready, "error": video_err}),
    );
    Json(status)
}

async fn map_points(State(s): State<Arc<AppState>>) -> Response {
    (
        [(header::CONTENT_TYPE, "application/octet-stream")],
        s.mapping.point_packet(),
    )
        .into_response()
}

async fn map_mesh(State(s): State<Arc<AppState>>) -> Response {
    (
        [
            (header::CONTENT_TYPE, "application/octet-stream"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        s.mapping.mesh_packet(),
    )
        .into_response()
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

async fn map_pause(State(s): State<Arc<AppState>>, Json(body): Json<Value>) -> Json<Value> {
    s.mapping.set_paused(body["paused"].as_bool().unwrap_or(false));
    Json(s.mapping.status())
}

async fn camera_jpg(State(s): State<Arc<AppState>>) -> Response {
    match s.camera.preview() {
        Some(frame) => {
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
    let duration = body["duration_s"].as_f64().unwrap_or(90.0);
    if !(10.0..=300.0).contains(&duration) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Drill duration must be 10..300 seconds").into_response();
    }
    Json(s.mission.start_drill(duration)).into_response()
}

async fn mission_drill_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(s.mission.drill())
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
    if !(name.starts_with("reply-") && name.ends_with(".wav") && !name.contains('/') && !name.contains('\\')) {
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
                let packet = mapping.point_packet();
                if tx.send(Message::Text(status.to_string().into())).await.is_err() {
                    return;
                }
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
    let sender = tokio::spawn(async move {
        loop {
            // A congested status path makes continued control unsafe; a send
            // timeout closes the socket rather than queueing permits.
            let status = control.status(true);
            match tokio::time::timeout(Duration::from_millis(100), tx.send(Message::Text(status.to_string().into()))).await {
                Ok(Ok(_)) => {}
                _ => return,
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
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
        if s.video.init.lock().unwrap().is_some() || s.video.error.lock().unwrap().is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let Some(init) = s.video.init.lock().unwrap().clone() else {
        let err = s.video.error.lock().unwrap().clone().unwrap_or_else(|| "Waiting for camera".into());
        let _ = tx.send(Message::Text(json!({"type":"video_error","reason":err}).to_string().into())).await;
        close!(1013, "");
    };
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
    loop {
        let request = match tokio::time::timeout(Duration::from_secs(10), rx.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => match serde_json::from_str::<Value>(&t) {
                Ok(v) => v,
                Err(_) => close!(1008, "Expected next video fragment request"),
            },
            Ok(Some(Ok(Message::Close(_)))) | Ok(None) | Ok(Some(Err(_))) | Err(_) => return,
            _ => close!(1008, "Expected next video fragment request"),
        };
        let request_id = request["request_id"].as_i64();
        if request["type"] != "next" || request_id.map(|i| !(0..=(1i64 << 53) - 1).contains(&i)).unwrap_or(true) {
            close!(1008, "Expected next video fragment request");
        }
        let fragment = match tokio::time::timeout(Duration::from_secs(2), queue.recv()).await {
            Ok(Some(Some(f))) => f,
            Ok(Some(None)) | Err(_) => close!(1013, "Video consumer fell behind; reconnect for fresh video"),
            Ok(None) => return,
        };
        let mut metadata = fragment.metadata();
        metadata["type"] = json!("video_segment");
        metadata["request_id"] = json!(request_id.unwrap());
        metadata["age_ms"] = json!(((now_monotonic() - fragment.received) * 1000.0).round().max(0.0) as i64);
        if tokio::time::timeout(Duration::from_millis(500), tx.send(Message::Text(metadata.to_string().into()))).await.is_err() {
            return;
        }
        if tokio::time::timeout(Duration::from_millis(500), tx.send(Message::Binary(fragment.payload.into()))).await.is_err() {
            return;
        }
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
        .route("/mission/report", get(mission_report))
        .route("/mission/snapshots/{name}", get(mission_snapshot))
        .route("/mission/drill", post(mission_drill_start))
        .route("/mission/drill", get(mission_drill_status))
        .route("/ask", post(ask))
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
