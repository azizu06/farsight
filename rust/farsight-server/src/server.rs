//! Axum application — every endpoint of the Python FastAPI mapping server.
//! Port of jetson/mapping/server.py + jetson/control/service.py routing.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tower_http::services::ServeDir;

use crate::bonded::BondedControlHub;
use crate::camera::CameraService;
use crate::control::{ControlState, SerialPump};
use crate::identity::IdentityService;
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

/// Bounded-1 latest-wins slot — asyncio.Queue(maxsize=1) parity for the
/// phone lanes: `offer` displaces an unprocessed item and returns it so the
/// caller can count the drop; `wait` yields items until `close`.
struct LatestSlot<T> {
    state: Mutex<LatestSlotState<T>>,
    notify: tokio::sync::Notify,
}

struct LatestSlotState<T> {
    item: Option<T>,
    closed: bool,
}

impl<T> LatestSlot<T> {
    fn new() -> Self {
        Self {
            state: Mutex::new(LatestSlotState { item: None, closed: false }),
            notify: tokio::sync::Notify::new(),
        }
    }

    /// Queue `item`, displacing an unprocessed one if present (returned).
    fn offer(&self, item: T) -> Option<T> {
        let displaced = {
            let mut g = self.state.lock().unwrap();
            g.item.replace(item)
        };
        self.notify.notify_one();
        displaced
    }

    /// Next queued item, or None once closed and empty.
    async fn wait(&self) -> Option<T> {
        loop {
            {
                let mut g = self.state.lock().unwrap();
                if g.item.is_some() {
                    return g.item.take();
                }
                if g.closed {
                    return None;
                }
            }
            self.notify.notified().await;
        }
    }

    fn close(&self) {
        self.state.lock().unwrap().closed = true;
        self.notify.notify_waiters();
    }
}

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
    /// Physical camera pan installed (--scan-pan); additive /health field
    /// the viewer uses to pick the fixed-vs-servo mount default.
    pub camera_pan: bool,
    /// Read-only control packet source when --replay is active.
    pub replay_control: Option<Arc<ReplayControl>>,
    /// Per-status-emission telemetry sink (control.on_status in Python).
    pub control_on_status: Option<Arc<dyn Fn(&Value) + Send + Sync>>,
    /// Opt-in bonded upstream hub (--bonded-control); owns /ws/control/bonded
    /// when enabled. The legacy /ws/control owner path is unchanged.
    pub bonded: Option<Arc<BondedControlHub>>,
    /// --nav2 NavService seam; the /nav/* contract still answers without it.
    pub nav: Option<Arc<crate::nav::NavService>>,
    /// --sim-world feed; drives camera/depth/range/detections from the sim.
    pub sim: Option<Arc<crate::sim::SimFeed>>,
    /// The loaded sim world name for /health (`sim_world` key).
    pub sim_world: Option<String>,
    /// Opt-in enrolled-face identity (jetson identity_service.py port);
    /// created unconditionally like Python, silently disabled with no models.
    pub identity: Arc<IdentityService>,
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
    min_pwm: i64,
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
    drive_profile: Option<crate::drive_profile::DriveProfile>,
    allow_calibration: bool,
    proximity_stop: bool,
    pan_mount: &str,
    autonomy_max_pwm: Option<i64>,
    bonded: bool,
    nav2: bool,
    nav_speed_mps: f64,
    nav_turn_radps: f64,
    nav_max_v: f64,
    nav_max_w: f64,
    nav_camera_height: f64,
    sim_world: Option<String>,
    lidar_drive: bool,
    visual_caps: (usize, usize),
    fusion_bypass: bool,
    demo_mode: bool,
    // (yunet, sface, gallery) — IdentityService::from_environment inputs.
    identity_config: (Option<String>, Option<String>, Option<String>),
) -> Arc<AppState> {
    // create_app validation order: sim-world, replay, simulated+serial,
    // nav2+replay, then pan mount.
    if sim_world.is_some()
        && (serial_port.is_some() || camera_device.is_some() || replay.is_some())
    {
        panic!("--sim-world is simulated; it cannot drive real motors, cameras or replays");
    }
    // A sim world is simulated hardware: the demo camera path and the
    // simulated rover drive it, like create_app's `demo = simulate_rover = True`.
    let demo = demo || sim_world.is_some();
    let simulate_rover = simulate_rover || sim_world.is_some();
    if replay.is_some()
        && (serial_port.is_some() || camera_device.is_some() || demo || simulate_rover || record)
    {
        panic!("Replay cannot be combined with serial, live/simulated sources, or recording");
    }
    if (demo || simulate_rover) && serial_port.is_some() {
        panic!("Simulated sources cannot be combined with a physical motor serial port");
    }
    if nav2 && replay.is_some() {
        panic!("Replay is read-only; navigation cannot be combined with it");
    }
    if scan_pan && pan_mount == "none" {
        panic!("--scan-pan needs a physical pan servo (--pan-mount servo)");
    }
    let mut mapping_state = MapState::new(demo);
    mapping_state.points_cap = visual_caps.0;
    mapping_state.triangles_cap = visual_caps.1;
    mapping_state.fusion_bypass = fusion_bypass;
    let mapping = Arc::new(mapping_state);
    let sim = sim_world.as_ref().map(|name| {
        let world = crate::sim::load(name).unwrap_or_else(|e| panic!("{e}"));
        Arc::new(crate::sim::SimFeed::new(crate::sim::RoverSim::new(
            world,
            Some(nav_speed_mps),
            Some(nav_turn_radps),
        )))
    });
    let camera = Arc::new(CameraService::new(
        mapping.clone(),
        camera_device.clone(),
        lidar_drive,
    ));
    let nav = if nav2 {
        let camera_snapshot = camera.clone();
        Some(Arc::new(
            crate::nav::NavService::new(
                mapping.clone(),
                nav_speed_mps,
                nav_turn_radps,
                nav_max_v,
                nav_max_w,
                nav_camera_height,
                None,
                Some(Arc::new(move || camera_snapshot.snapshot())),
            )
            .unwrap_or_else(|e| panic!("{e}")),
        ))
    } else {
        None
    };
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
    // Tracked-person 3D markers (f1b883c): MapState offers RGB-D frames to
    // the locator; perception annotates tracked boxes detected on held
    // frames. Replay path skips it like Python (ReplayPerception has no
    // annotate hook).
    let people = Arc::new(crate::people::PersonLocator::new());
    if replay.is_none() {
        let pl = people.clone();
        perception.set_annotate(Box::new(move |boxes, frame| pl.annotate(boxes, frame)));
    }
    let cam = camera.clone();
    let perc = perception.clone();
    // Physical serial requires verified drive eligibility (camera_drive_ready);
    // simulated/USB-free setups keep plain freshness (camera.py parity).
    let drive_key = if serial_port.is_some() { "camera_drive_ready" } else { "camera" };
    // Ground-truth sim detections replace the perception snapshot with
    // --sim-world (create_app `detections=sim.detections if sim else ...`).
    let detections: crate::control::Detections = if let Some(feed) = &sim {
        let feed = feed.clone();
        Arc::new(move || feed.detections())
    } else {
        Arc::new(move || perc.snapshot())
    };
    let control = Arc::new(
        ControlState::new(
            simulate_rover,
            min_pwm,
            max_pwm,
            Some(Arc::new(move || {
                cam.status()[drive_key].as_bool().unwrap_or(false)
            })),
            Some(detections),
            None,
            drive_profile,
            allow_calibration,
            proximity_stop,
            pan_mount,
            autonomy_max_pwm,
            nav.clone(),
            demo_mode,
        )
        .expect("control config"),
    );
    // The sim's ultrasonic feeds the range input like a Uno echo.
    let simulated_range: Option<Arc<dyn Fn() -> Option<i64> + Send + Sync>> =
        sim.as_ref().map(|feed| {
            let feed = feed.clone();
            Arc::new(move || feed.sim.ultrasonic_cm(206.0)) as _
        });
    let pump = Arc::new(SerialPump::new(
        control.clone(),
        serial_port,
        simulate_rover,
        simulated_range,
    ));
    if let Some(nav) = &nav {
        nav.attach(&control);
        let observer = nav.clone();
        mapping
            .frame_observers
            .lock()
            .unwrap()
            .push(Arc::new(move |frame: &crate::phone::RgbdFrame| {
                observer.offer_frame(frame)
            }));
    }
    {
        let pl = people.clone();
        mapping
            .frame_observers
            .lock()
            .unwrap()
            .push(Arc::new(move |frame: &crate::phone::RgbdFrame| {
                pl.offer_frame(frame)
            }));
    }
    let replay_control = replay.as_ref().map(|_| Arc::new(ReplayControl::new()));
    let replay_session = match (&replay, &replay_control) {
        (Some(dir), Some(rc)) => Some(Arc::new(
            ReplaySession::new(dir, camera.clone(), perception.clone(), rc.clone())
                .unwrap_or_else(|e| panic!("{e}")),
        )),
        _ => None,
    };
    // Live control loops feed the drill scorer; recorded replays have no
    // live loop (hasattr(control, "state") in Python). The sim's apply() is
    // chained after drill_motion like create_app's `applied` observer list.
    if replay_session.is_none() {
        let m2 = mission.clone();
        let sim_apply = sim.clone();
        *control.on_output.lock().unwrap() = Some(Box::new(move |v, w, blocked| {
            m2.drill_motion(v, w, blocked);
            if let Some(feed) = &sim_apply {
                feed.sim.apply(v, w, blocked);
            }
        }));
    }
    // #79/#81: /mission/drill carries the face-detector status, and the face
    // pass only costs CPU while a drill is running.
    {
        let p = perception.clone();
        mission.set_face_status(Box::new(move || p.face_status()));
        let m3 = mission.clone();
        perception.set_face_wanted(Box::new(move || m3.drill_score.running()));
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
    // Python creates IdentityService.from_environment(camera) for every
    // server flavor, replay included; start() is a no-op without models.
    let (identity_yunet, identity_sface, identity_gallery) = identity_config;
    let identity = Arc::new(IdentityService::new(
        camera.clone(),
        identity_yunet,
        identity_sface,
        identity_gallery,
        3.0,
    ));
    Arc::new(AppState {
        mapping,
        camera,
        video,
        control: control.clone(),
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
        camera_pan: scan_pan,
        replay: replay_session,
        replay_control,
        control_on_status: control_on_status.clone(),
        bonded: bonded.then(|| Arc::new(BondedControlHub::new(control.clone(), control_on_status))),
        nav,
        sim,
        sim_world,
        identity,
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
    state.identity.clone().start();
    {
        // sample_mission: read-only control telemetry at <=10 Hz; never
        // issues permits or commands. Replays sample the ReplayControl
        // read-only packet, same as Python.
        let mission = state.mission.clone();
        let control = state.control.clone();
        let replay_control = state.replay_control.clone();
        let camera = state.camera.clone();
        std::thread::spawn(move || loop {
            let status = replay_control
                .as_ref()
                .map(|rc| rc.status())
                .unwrap_or_else(|| control.status(false));
            let cam = camera.status()["camera"].as_bool();
            mission.observe_control(&status, cam);
            std::thread::sleep(Duration::from_millis(100));
        });
    }
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
    if let Some(feed) = state.sim.clone() {
        // --sim-world: poses at 30 Hz, frames at 10 Hz (sim/stream.py).
        crate::sim::spawn_world_stream(state.mapping.clone(), state.camera.clone(), feed);
    } else if state.demo {
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
                let mut frame = crate::demo::simulated_frame(
                    captured - started,
                    frame_id,
                    &("simulated-room".to_string(), 1),
                );
                camera.set_phone_rgb(Some(crate::camera::CameraFrame {
                    jpeg: frame.jpeg.clone(),
                    received: captured,
                    frame_id,
                    capture_verified: false,
                    capture_binding: None,
                            tracking: None,
                            mirrored: false,
                            socket_received: None,
                            published: None,
                            native_jpeg: None,
                            sensor_key: None,
                    source: "SIMULATED".into(),
                }));
                if captured >= next_fusion
                    && !busy.swap(true, std::sync::atomic::Ordering::SeqCst)
                {
                    let (mapping, busy) = (mapping.clone(), busy.clone());
                    next_fusion = captured + 0.2;
                    std::thread::spawn(move || {
                        let _ = mapping.process(&mut frame);
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
    obj.insert("camera_pan".into(), json!(s.camera_pan));
    for (k, v) in s.perception.health().as_object().unwrap() {
        obj.insert(k.clone(), v.clone());
    }
    obj.insert("rover".into(), rover["rover"].clone());
    obj.insert("estop".into(), rover["estop"].clone());
    obj.insert("control".into(), rover);
    obj.insert(
        "video".into(),
        json!({"enabled": s.h264, "ready": s.video.ready(), "error": video_err,
               "fps": s.video_fps, "bitrate": s.video_bitrate,
               "encoded_frames": s.video.encoded.load(std::sync::atomic::Ordering::Relaxed),
               "jetson_encode_ms": *s.video.encode_ms.lock().unwrap(),
               "camera_ingest_ms": *s.video.ingest_ms.lock().unwrap()}),
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
    if let Some(nav) = &s.nav {
        // Absent without --nav2, exactly like the Python health payload.
        obj.insert(
            "nav".into(),
            json!({"enabled": true, "available": nav.available()}),
        );
    }
    if let Some(name) = &s.sim_world {
        obj.insert("sim_world".into(), json!(name));
    }
    Json(status)
}

/// GET /video/status — camera status plus encoder timing and the map
/// processing duration. Lock-free w.r.t. the map mutex: camera diagnostics
/// remain observable while fusion is busy (state.processing_ms lives in an
/// atomic for exactly this).
async fn video_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    let mut status = s.camera.status();
    let obj = status.as_object_mut().unwrap();
    obj.insert("mapping_processing_ms".into(), json!(s.mapping.processing_ms()));
    obj.insert("enabled".into(), json!(s.h264));
    obj.insert("ready".into(), json!(s.video.ready()));
    obj.insert("error".into(), json!(s.video.error.lock().unwrap().clone()));
    obj.insert(
        "jetson_encode_ms".into(),
        json!(*s.video.encode_ms.lock().unwrap()),
    );
    obj.insert(
        "encoded_frames".into(),
        json!(s.video.encoded.load(std::sync::atomic::Ordering::Relaxed)),
    );
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

/// Bulk bodies leave in 16KiB credit-sized pieces so a large snapshot can't
/// occupy the link ahead of control frames for the whole write.
const PACE_CHUNK: usize = 16 * 1024;

fn paced_body(body: impl Into<bytes::Bytes>) -> Body {
    let all = body.into();
    let stream = futures_util::stream::unfold((all, 0usize), |(all, off)| async move {
        if off >= all.len() {
            return None;
        }
        let end = (off + PACE_CHUNK).min(all.len());
        crate::pacer::pace(end - off).await;
        let chunk = all.slice(off..end);
        Some((Ok::<_, std::convert::Infallible>(chunk), (all, end)))
    });
    Body::from_stream(stream)
}

/// Same chunked pacing on the live-video lane (#124): video must not queue
/// behind map bulk — an unpaced (cap 0) live lane returns immediately.
fn paced_body_live(body: impl Into<bytes::Bytes>) -> Body {
    let all = body.into();
    let stream = futures_util::stream::unfold((all, 0usize), |(all, off)| async move {
        if off >= all.len() {
            return None;
        }
        let end = (off + PACE_CHUNK).min(all.len());
        crate::pacer::pace_live(end - off).await;
        let chunk = all.slice(off..end);
        Some((Ok::<_, std::convert::Infallible>(chunk), (all, end)))
    });
    Body::from_stream(stream)
}

async fn map_points(State(s): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    map_packet_response(&s, crate::state::PacketKind::Points, &headers, false)
}

async fn map_mesh(State(s): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    map_packet_response(&s, crate::state::PacketKind::Mesh, &headers, false)
}

/// Revision-keyed ETag + If-None-Match: `?points=http` subscribers poll these
/// at their own pace, and an unchanged map must not re-download the full
/// compressed blob over cellular — a 304 costs ~100 bytes. `no-cache` keeps
/// the client revalidating (the same freshness `no-store` enforced).
fn map_packet_response(s: &Arc<AppState>, which: crate::state::PacketKind, headers: &HeaderMap, attachment: bool) -> Response {
    let (body, coding, tag) = s.mapping.packet(which, accept_encoding(headers));
    let fresh = headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(',')
        .map(str::trim)
        .any(|t| t == "*" || t == tag);
    let mut response = if fresh {
        StatusCode::NOT_MODIFIED.into_response()
    } else {
        let mut r = packet_response(body, coding);
        if attachment {
            r.headers_mut().insert(header::CONTENT_DISPOSITION, "attachment; filename=\"farsight-scan.ply\"".parse().unwrap());
        }
        r
    };
    response.headers_mut().insert(header::ETAG, tag.parse().unwrap());
    response.headers_mut().insert(header::VARY, "Accept-Encoding".parse().unwrap());
    response.headers_mut().insert(header::CACHE_CONTROL, "no-cache".parse().unwrap());
    response
}

fn accept_encoding(headers: &HeaderMap) -> &str {
    headers.get(header::ACCEPT_ENCODING).and_then(|v| v.to_str().ok()).unwrap_or("")
}

fn packet_response(body: impl Into<bytes::Bytes>, coding: &'static str) -> Response {
    let mut response = Response::new(paced_body(body));
    response.headers_mut().insert(header::CONTENT_TYPE, "application/octet-stream".parse().unwrap());
    response.headers_mut().insert(header::VARY, "Accept-Encoding".parse().unwrap());
    if !coding.is_empty() {
        response.headers_mut().insert(header::CONTENT_ENCODING, coding.parse().unwrap());
    }
    response
}

async fn map_export(State(s): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    map_packet_response(&s, crate::state::PacketKind::Ply, &headers, true)
}

/// `/map/dense.bin` query surface: epoch/after/snapshot/anchor/limit
/// (jetson/mapping/server.py parity).
#[derive(Debug)]
pub struct DenseParams {
    pub epoch: Option<String>,
    pub after: i64,
    pub snapshot: bool,
    pub anchor: Option<i64>,
    pub limit: i64,
}

fn query_i64(q: &std::collections::HashMap<String, String>, key: &str) -> Result<Option<i64>, Response> {
    match q.get(key) {
        None => Ok(None),
        Some(v) => v.trim().parse::<i64>().map(Some).map_err(|_| {
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail": [{"loc": ["query", key], "msg": "value is not a valid integer", "type": "type_error.integer"}]})),
            )
                .into_response()
        }),
    }
}

fn query_bool(q: &std::collections::HashMap<String, String>, key: &str) -> Result<Option<bool>, Response> {
    match q.get(key).map(|v| v.trim().to_ascii_lowercase()) {
        None => Ok(None),
        Some(v) => match v.as_str() {
            "true" | "t" | "yes" | "y" | "on" | "1" => Ok(Some(true)),
            "false" | "f" | "no" | "n" | "off" | "0" => Ok(Some(false)),
            _ => Err((
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail": [{"loc": ["query", key], "msg": "value is not a valid boolean", "type": "type_error.bool"}]})),
            )
                .into_response()),
        },
    }
}

impl DenseParams {
    pub fn parse(q: &std::collections::HashMap<String, String>) -> Result<Self, Response> {
        Ok(Self {
            epoch: q.get("epoch").cloned(),
            after: query_i64(q, "after")?.unwrap_or(0),
            snapshot: query_bool(q, "snapshot")?.unwrap_or(false),
            anchor: query_i64(q, "anchor")?,
            limit: query_i64(q, "limit")?.unwrap_or(8192),
        })
    }
}

/// Build the dense delta response. Fresh bytes every call (no packet cache —
/// the delta/journal moves per request), Cache-Control: no-store, and the
/// same Accept-Encoding negotiation + Vary as the other .bin routes.
pub fn dense_response(mapping: &MapState, accept_encoding: &str, q: &DenseParams) -> Response {
    if q.after < 0 || q.limit < 1 {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "Invalid dense cursor or limit"})),
        )
            .into_response();
    }
    let body = match mapping
        .dense()
        .delta(q.epoch.as_deref(), q.after, q.snapshot, q.limit, q.anchor)
    {
        Ok(b) => b,
        Err(e) => {
            return (StatusCode::BAD_REQUEST, Json(json!({"detail": e}))).into_response()
        }
    };
    let mut response = match crate::state::negotiate_encoding(accept_encoding) {
        Some(coding) => {
            let encoded = match coding {
                crate::state::Coding::Zstd => crate::state::zstd_compress(&body),
                crate::state::Coding::Br => crate::state::brotli_compress(&body),
                crate::state::Coding::Gzip => crate::state::gzip_compress(&body),
            };
            packet_response(encoded, coding.name())
        }
        None => packet_response(body, ""),
    };
    response.headers_mut().insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response
}

async fn map_dense(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(q): Query<std::collections::HashMap<String, String>>,
) -> Response {
    let params = match DenseParams::parse(&q) {
        Ok(p) => p,
        Err(r) => return r,
    };
    let encoding = accept_encoding(&headers).to_string();
    let mapping = s.mapping.clone();
    tokio::task::spawn_blocking(move || dense_response(&mapping, &encoding, &params))
        .await
        .unwrap_or_else(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response())
}

/// Read-only map state for geometry relays/renderers (real-backend contract;
/// Codex's Python state.status mirror).
async fn map_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(s.mapping.status())
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
    let started = now_monotonic();
    let camera = s.camera.clone();
    // A first-frame JPEG encode must not stall control/health on an async worker.
    match tokio::task::spawn_blocking(move || camera.preview()).await.ok().flatten() {
        Some(frame) => {
            let fid = frame.frame_id;
            let age = frame.age_ms();
            let source = frame.source.clone();
            // The frame age already includes preview/executor residence.
            // Report only completed handler time, rounded down, so clients
            // can avoid counting that same interval again without
            // subtracting network time.
            let wait_ms = ((now_monotonic() - started) * 1000.0).max(0.0) as i64;
            let mut resp = Response::new(paced_body_live(frame.jpeg));
            let h = resp.headers_mut();
            h.insert(header::CONTENT_TYPE, "image/jpeg".parse().unwrap());
            h.insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
            h.insert("x-frame-id", fid.to_string().parse().unwrap());
            h.insert("x-frame-age-ms", age.to_string().parse().unwrap());
            h.insert("x-frame-server-wait-ms", wait_ms.to_string().parse().unwrap());
            h.insert("x-frame-source", source.parse().unwrap());
            resp
        }
        None => (StatusCode::SERVICE_UNAVAILABLE, "Waiting for a camera frame").into_response(),
    }
}

async fn depth_jpg(State(s): State<Arc<AppState>>) -> Response {
    let jpeg = s.mapping.inner.lock().unwrap().depth_jpeg.clone();
    match jpeg {
        Some(j) => {
            let mut resp = Response::new(paced_body_live(j));
            let h = resp.headers_mut();
            h.insert(header::CONTENT_TYPE, "image/jpeg".parse().unwrap());
            h.insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
            resp
        }
        None => (StatusCode::SERVICE_UNAVAILABLE, "Waiting for measured depth").into_response(),
    }
}

async fn stream_mjpg(State(s): State<Arc<AppState>>) -> Response {
    let camera = s.camera.clone();
    let stream = async_stream::stream! {
        let mut last: Option<(String, i64)> = None;
        loop {
            let worker_camera = camera.clone();
            let frame = tokio::task::spawn_blocking(move || worker_camera.preview()).await.ok().flatten();
            let identity = frame.as_ref().map(|f| (f.source.clone(), f.frame_id));
            if let Some(f) = frame {
                if f.age_ms() < 500 && identity != last {
                    let mut part = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n".to_vec();
                    part.extend_from_slice(&f.jpeg);
                    part.extend_from_slice(b"\r\n");
                    let all = bytes::Bytes::from(part);
                    let mut off = 0usize;
                    while off < all.len() {
                        let end = (off + PACE_CHUNK).min(all.len());
                        crate::pacer::pace_live(end - off).await;
                        yield Ok::<_, std::io::Error>(all.slice(off..end));
                        off = end;
                    }
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

/// The disabled_router message (jetson/nav.py): the contract answers, but
/// nothing navigates without --nav2.
const NAV_DISABLED_MESSAGE: &str =
    "Navigation disabled: start jetson.server with --nav2 (FARSIGHT_NAV2=1)";

/// GoalRequest field: pydantic's lax float takes numbers, bools and numeric
/// strings; non-finite values are legal JSON here and fail inside set_goal.
fn goal_float(body: &Value, key: &str) -> Result<f64, ()> {
    match body.get(key) {
        Some(Value::Number(n)) => n.as_f64().ok_or(()),
        Some(Value::Bool(b)) => Ok(if *b { 1.0 } else { 0.0 }),
        Some(Value::String(t)) => t.trim().parse::<f64>().map_err(|_| ()),
        _ => Err(()),
    }
}

/// POST /nav/goal — 202 nav object, 409 refusal with the nav object, 422 for
/// bad input (the FastAPI GoalRequest contract).
async fn nav_goal(
    State(s): State<Arc<AppState>>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let Some(nav) = s.nav.clone() else {
        return (
            StatusCode::CONFLICT,
            Json(json!({"nav": crate::nav::empty_nav("unavailable", NAV_DISABLED_MESSAGE),
                        "message": NAV_DISABLED_MESSAGE})),
        )
            .into_response();
    };
    let body = match body {
        Ok(Json(b)) if b.is_object() => b,
        _ => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail":[{"type":"json_invalid","loc":["body"],
                            "msg":"JSON decode error","input":{}}]})),
            )
                .into_response();
        }
    };
    let mut errors = Vec::new();
    let x = match body.get("x") {
        None => {
            errors.push(json!({"type":"missing","loc":["body","x"],"msg":"Field required","input":body}));
            None
        }
        Some(_) => match goal_float(&body, "x") {
            Ok(v) => Some(v),
            Err(()) => {
                errors.push(json!({"type":"float_parsing","loc":["body","x"],
                    "msg":"Input should be a valid number, unable to parse value as a number",
                    "input":body["x"]}));
                None
            }
        },
    };
    let z = match body.get("z") {
        None => {
            errors.push(json!({"type":"missing","loc":["body","z"],"msg":"Field required","input":body}));
            None
        }
        Some(_) => match goal_float(&body, "z") {
            Ok(v) => Some(v),
            Err(()) => {
                errors.push(json!({"type":"float_parsing","loc":["body","z"],
                    "msg":"Input should be a valid number, unable to parse value as a number",
                    "input":body["z"]}));
                None
            }
        },
    };
    let session_id = match body.get("session_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(t)) => Some(t.clone()),
        Some(_) => {
            errors.push(json!({"type":"string_type","loc":["body","session_id"],
                "msg":"Input should be a valid string","input":body["session_id"]}));
            None
        }
    };
    let permit = match body.get("permit") {
        None | Some(Value::Null) => None,
        Some(Value::String(t)) => Some(t.clone()),
        Some(_) => {
            errors.push(json!({"type":"string_type","loc":["body","permit"],
                "msg":"Input should be a valid string","input":body["permit"]}));
            None
        }
    };
    if !errors.is_empty() {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": errors})),
        )
            .into_response();
    }
    match nav.set_goal(
        x.unwrap_or(f64::NAN),
        z.unwrap_or(f64::NAN),
        session_id.as_deref(),
        permit.as_deref(),
    ) {
        Ok(nav_obj) => (StatusCode::ACCEPTED, Json(nav_obj)).into_response(),
        Err(crate::nav::GoalError::Refused { nav, message }) => (
            StatusCode::CONFLICT,
            Json(json!({"nav": nav, "message": message})),
        )
            .into_response(),
        Err(crate::nav::GoalError::Unprocessable(message)) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": message})),
        )
            .into_response(),
    }
}

/// POST /nav/cancel — disarms go-to-point; returns the nav object.
async fn nav_cancel(State(s): State<Arc<AppState>>) -> Response {
    match &s.nav {
        Some(nav) => Json(nav.cancel()).into_response(),
        None => Json(crate::nav::empty_nav("idle", "")).into_response(),
    }
}

/// GET /nav/status — the nav object plus the diagnostic detail object.
async fn nav_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    match &s.nav {
        Some(nav) => {
            let mut out = nav.status_object();
            out.as_object_mut()
                .unwrap()
                .insert("detail".into(), nav.detail());
            Json(out)
        }
        None => {
            let mut out = crate::nav::empty_nav("unavailable", NAV_DISABLED_MESSAGE);
            out.as_object_mut()
                .unwrap()
                .insert("detail".into(), json!({"enabled": false}));
            Json(out)
        }
    }
}

/// GET /ws/nav — loopback bridge socket (NavService.websocket); disabled: the
/// contract accepts and immediately closes 1008.
async fn ws_nav(
    ws: WebSocketUpgrade,
    axum::extract::ConnectInfo(addr): axum::extract::ConnectInfo<PeerAddr>,
    State(s): State<Arc<AppState>>,
) -> Response {
    if let Some(nav) = s.nav.clone() {
        let host = addr.0.ip().to_string();
        ws.on_upgrade(move |socket| async move {
            nav.run_ws(socket, host).await;
        })
    } else {
        ws.on_upgrade(move |mut socket| async move {
            let reason: String = NAV_DISABLED_MESSAGE.chars().take(100).collect();
            let _ = socket
                .send(axum::extract::ws::Message::Close(Some(
                    axum::extract::ws::CloseFrame {
                        code: 1008,
                        reason: reason.into(),
                    },
                )))
                .await;
        })
    }
}

/// GET /sim/state — sim-only ground truth (pose, collisions, coverage).
async fn sim_state(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(
        s.sim
            .as_ref()
            .map(|feed| feed.sim.state())
            .unwrap_or_else(|| json!({})),
    )
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

async fn ws_map(
    ws: WebSocketUpgrade,
    State(s): State<Arc<AppState>>,
    Query(q): Query<std::collections::HashMap<String, String>>,
) -> Response {
    // Opt-in (#67): `?points=http` subscribers get status only and pull
    // compressed HTTP snapshots at their own pace; legacy subscribers keep
    // binary pushes.
    let http_points = q.get("points").is_some_and(|v| v == "http");
    ws.on_upgrade(move |socket| handle_ws_map(socket, s, http_points))
}

async fn handle_ws_map(socket: WebSocket, s: Arc<AppState>, http_points: bool) {
    let (mut tx, mut rx) = socket.split();
    let mapping = s.mapping.clone();
    let sender = tokio::spawn(async move {
        let mut revision: i64 = -1;
        // Wake on a fresh map revision to push the new packet at fusion
        // time; the 500 ms tick still drives the periodic status frame.
        let mut watch = mapping.revision_watch();
        loop {
            let mut status = mapping.status();
            if http_points {
                status["points_transport"] = json!("http");
                if tx.send(Message::Text(status.to_string().into())).await.is_err() {
                    return;
                }
            } else if status["revision"].as_i64().unwrap_or(-1) != revision {
                let (packet, ..) = mapping.point_packet("");
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
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_millis(500)) => {}
                r = watch.changed() => { if r.is_err() { return; } }
            }
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
    // #110 opt-in nav revision lane, latched per socket (control/service.py).
    let lane = Arc::new(std::sync::Mutex::new(None::<crate::nav::NavLane>));
    let send_lane = lane.clone();
    let sender_sid = session_id.clone();
    let sender = tokio::spawn(async move {
        // Mission recording keeps its 20 Hz control sampling
        // (control/service.py RECORD_INTERVAL), while status publishes at 40 Hz.
        let mut recorded = f64::NEG_INFINITY;
        loop {
            // #139: a newer controller replaced this session after it went
            // silent — stop publishing so the socket closes (service.py).
            let started = now_monotonic();
            let status = control.status(true);
            if status["session_id"].as_str() != Some(sender_sid.as_str()) {
                return;
            }
            let status = send_lane
                .lock()
                .unwrap()
                .as_mut()
                .map(|l| l.apply(&status, started))
                .unwrap_or(status);
            match tokio::time::timeout(Duration::from_millis(100), tx.send(Message::Text(status.to_string().into()))).await {
                Ok(Ok(_)) => {}
                _ => return,
            }
            if let Some(hook) = &on_status {
                if started - recorded >= 0.045 {
                    recorded = started;
                    hook(&status);
                }
            }
            // 40 Hz target accounting for emit cost, per control/service.py.
            tokio::time::sleep(Duration::from_secs_f64((0.025 - (now_monotonic() - started)).max(0.0))).await;
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
                        if v.get("nav_lane").and_then(Value::as_str) == Some("revision") {
                            let mut lane = lane.lock().unwrap();
                            if lane.is_none() {
                                *lane = Some(crate::nav::NavLane::new());
                            }
                        }
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

/// Opt-in bonded upstream (jetson/control/service.py bonded_websocket):
/// the first frame, within 3 s, is a v1 JSON attach; a second path joins the
/// live owner with the private credential. The relay strips attachment data.
async fn ws_control_bonded(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_control_bonded(socket, s))
}

async fn handle_ws_control_bonded(socket: WebSocket, s: Arc<AppState>) {
    let hub = s.bonded.clone().expect("/ws/control/bonded requires --bonded-control");
    let (tx, rx) = socket.split();
    run_bonded_session(tx, rx, hub).await
}

async fn run_bonded_session<TxErr, RxErr, Tx, Rx>(mut tx: Tx, mut rx: Rx, hub: Arc<BondedControlHub>)
where
    Tx: futures_util::Sink<Message, Error = TxErr> + Unpin + Send + 'static,
    Rx: futures_util::Stream<Item = Result<Message, RxErr>> + Unpin + Send + 'static,
    TxErr: Send + 'static,
{
    let close = |code: u16| {
        Message::Close(Some(axum::extract::ws::CloseFrame { code, reason: "".into() }))
    };
    // Attach frame: text JSON within 3 s, {"type":"attach","version":1} with
    // an optional 64-character path secret for joining the live owner.
    let attachment = async {
        let text = match tokio::time::timeout(Duration::from_secs(3), rx.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => t,
            _ => return Err(()),
        };
        if text.chars().count() > 2048 {
            return Err(());
        }
        let message: Value = serde_json::from_str(&text).map_err(|_| ())?;
        let valid = message
            .as_object()
            .map(|o| {
                o.get("type").and_then(Value::as_str) == Some("attach")
                    && o.get("version").and_then(Value::as_i64) == Some(1)
                    && o.keys().all(|k| matches!(k.as_str(), "type" | "version" | "secret"))
            })
            .unwrap_or(false);
        if !valid {
            return Err(());
        }
        let secret = match message.get("secret") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) if s.len() == 64 => Some(s.clone()),
            _ => return Err(()),
        };
        hub.attach(secret.as_deref()).map_err(|_| ())
    }
    .await;
    let Ok(attachment) = attachment else {
        // Rejected attachments close without affecting the existing owner.
        let _ = tx.send(close(1000)).await;
        return;
    };
    let reply = json!({
        "type": "attached",
        "version": 1,
        "session_id": attachment.session_id.clone(),
        "secret": attachment.secret.clone(),
        "path_id": attachment.path_id.clone(),
    });
    let sent = tokio::time::timeout(
        Duration::from_millis(100),
        tx.send(Message::Text(reply.to_string().into())),
    )
    .await;
    if sent.map(|r| r.is_err()).unwrap_or(true) {
        hub.detach(&attachment.path_id);
        let _ = tx.send(close(1000)).await;
        return;
    }
    let shared_tx = Arc::new(tokio::sync::Mutex::new(tx));
    // Publisher drains this path's single-slot status queue; a send past the
    // 100 ms deadline or the detach sentinel ends the session. Whichever
    // side finishes first tears the session down (FIRST_COMPLETED parity).
    let writer = shared_tx.clone();
    let path_id = attachment.path_id.clone();
    let publish = tokio::spawn(async move {
        while let Some(status) = attachment.next_status().await {
            let mut out = writer.lock().await;
            let ok = tokio::time::timeout(
                Duration::from_millis(100),
                out.send(Message::Text(status.to_string().into())),
            )
            .await
            .map(|r| r.is_ok())
            .unwrap_or(false);
            drop(out);
            if !ok {
                return;
            }
        }
    });
    let control = hub.control().clone();
    let hub_recv = hub.clone();
    let recv_path = path_id.clone();
    let receive = async move {
        while let Some(Ok(message)) = rx.next().await {
            match message {
                Message::Text(text) => {
                    if text.chars().count() > 2048 {
                        control.stop("oversized_command", true);
                        return;
                    }
                    match serde_json::from_str::<Value>(&text) {
                        Ok(v) => {
                            hub_recv.accept(&v, &recv_path);
                        }
                        Err(_) => control.stop("invalid_json", true),
                    }
                }
                Message::Binary(_) => {
                    control.stop("binary_control_rejected", true);
                    return;
                }
                Message::Close(_) => return,
                _ => {}
            }
        }
    };
    tokio::pin!(publish);
    tokio::pin!(receive);
    tokio::select! {
        _ = &mut publish => {}
        _ = &mut receive => {}
    }
    publish.abort();
    hub.detach(&path_id);
    {
        let mut out = shared_tx.lock().await;
        let _ = tokio::time::timeout(Duration::from_millis(250), out.send(close(1000))).await;
    }
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
    let queue = s.video.subscribe();
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
            // Never put an already stale picture on the wire: the client must
            // reconnect and resume at a fresh keyframe (send_fragment parity).
            if now_monotonic() - fragment.received >= 0.5 {
                return;
            }
            let mut metadata = fragment.metadata();
            metadata["type"] = json!("video_segment");
            metadata["request_id"] = json!(request_id);
            metadata["server_wait_ms"] = json!((now_monotonic() - waiting_at) * 1000.0);
            metadata["age_ms"] = json!(((now_monotonic() - fragment.received) * 1000.0).round().max(0.0) as i64);
            let mut out = tx2.lock().await;
            if tokio::time::timeout(Duration::from_millis(500), out.send(Message::Text(metadata.to_string().into()))).await.is_err() {
                return;
            }
            crate::pacer::pace_live(fragment.payload.len()).await;
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
    let (tx, rx) = socket.split();
    run_phone_session(tx, rx, s).await
}

async fn run_phone_session<TxErr, RxErr, Tx, Rx>(mut tx: Tx, mut rx: Rx, s: Arc<AppState>)
where
    Tx: futures_util::Sink<Message, Error = TxErr> + Unpin + Send + 'static,
    Rx: futures_util::Stream<Item = Result<Message, RxErr>> + Unpin + Send + 'static,
    TxErr: Send + 'static,
{
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
    let mut permits_opt: Option<Arc<crate::capture_permits::CapturePermits>> = None;
    let mut writer_opt: Option<tokio::task::JoinHandle<()>> = None;
    let mut out_tx_opt: Option<tokio::sync::mpsc::Sender<Message>> = None;
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
        // ARKit RGB-D has no supported sensor-to-host clock contract yet. In
        // particular, claiming the RGB-only AVCapture clock on this endpoint
        // cannot make an ARKit frame eligible for physical drive.
        let permits = Arc::new(crate::capture_permits::CapturePermits::with_clocks(
            &hello.session_id,
            hello.map_epoch,
            hello.supports_capture_permits,
            &[],
        ));
        s.camera.begin_phone(Some(permits.clone()));
        // Finish the socket state mutation before the endpoint continues
        // (server.py _session_thread parity): spawn_blocking work is never
        // cancelled by a dropped await, so a late start_session cannot
        // overwrite the next socket's state while ownership stays held.
        let (mapping, ident, device) = (s.mapping.clone(), identity.clone(), hello.device.clone());
        let socket_generation = tokio::task::spawn_blocking(move || {
            mapping.start_session(ident, device)
        })
        .await
        .map_err(|_| "Session start worker failed".to_string())?;
        // Outbound traffic goes through one writer task: the permit issuer
        // ticks at 20 Hz (capture_permits.run parity; issue() returns None
        // when the client did not opt in), the handler sends closes/acks.
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<Message>(16);
        let permits_writer = permits.clone();
        let writer = tokio::spawn(async move {
            let mut tick =
                tokio::time::interval(Duration::from_secs_f64(crate::capture_permits::PERIOD));
            loop {
                tokio::select! {
                    _ = tick.tick() => {
                        if let Some(packet) = permits_writer.issue() {
                            if tokio::time::timeout(
                                Duration::from_millis(100),
                                tx.send(Message::Text(packet.to_string().into())),
                            )
                            .await
                            .map(|r| r.is_err())
                            .unwrap_or(true)
                            {
                                return;
                            }
                        }
                    }
                    msg = out_rx.recv() => {
                        match msg {
                            Some(m) => {
                                if tx.send(m).await.is_err() { return; }
                            }
                            None => return,
                        }
                    }
                }
            }
        });
        out_tx_opt = Some(out_tx);
        writer_opt = Some(writer);
        permits_opt = Some(permits.clone());
        // Three bounded latest-wins lanes (#157): the camera fast-lane
        // publishes the phone picture independently of fusion, pose
        // heartbeats update tracking independently of fusion, and fusion
        // itself stays a bounded queue. Backpressure on any lane replaces
        // the unprocessed item instead of holding up the socket reader.
        // Bytes queues: the socket reader hands the frame buffer by
        // refcount instead of copying every RGB-D packet.
        let fusion_q = Arc::new(LatestSlot::<(Bytes, f64)>::new());
        let camera_q = Arc::new(LatestSlot::<(Bytes, f64)>::new());
        let pose_q = Arc::new(LatestSlot::<(Value, crate::geometry::Mat4, f64)>::new());
        // Camera fast-lane: validate + publish the JPEG BEFORE offering the
        // same packet to fusion, so detection work can never hold up the
        // camera. A decoding error here kills the session (parity).
        let mut cam_worker = {
            let (q, fusion, cam, mapping, ident, permits) = (
                camera_q.clone(),
                fusion_q.clone(),
                s.camera.clone(),
                s.mapping.clone(),
                identity.clone(),
                permits.clone(),
            );
            tokio::spawn(async move {
                let mut previous: i64 = -1;
                while let Some((packet, received_at)) = q.wait().await {
                    let (cam2, ident2, permits2) = (cam.clone(), ident.clone(), permits.clone());
                    let r = tokio::task::spawn_blocking(move || {
                        (
                            cam2.accept_phone_rgbd(packet.as_ref(), &ident2, previous, Some(received_at), Some(&permits2), Some(socket_generation)),
                            packet,
                        )
                    })
                    .await;
                    match r {
                        Ok((Ok(v), packet)) => {
                            previous = v;
                            // Offer only after camera publication, without
                            // waiting for fusion.
                            if fusion.offer((packet, received_at)).is_some() {
                                mapping.note_dropped();
                            }
                        }
                        Ok((Err(e), _)) => return Err(e),
                        Err(_) => return Err("Camera fast-lane worker failed".into()),
                    }
                }
                Ok(())
            })
        };
        // Pose heartbeats: observe_pose without waiting behind fusion work.
        let mut pose_worker = {
            let (q, mapping) = (pose_q.clone(), s.mapping.clone());
            tokio::spawn(async move {
                while let Some((header, transform, received_at)) = q.wait().await {
                    let m = mapping.clone();
                    match tokio::task::spawn_blocking(move || {
                        crate::camera::adjusted_receipt(&header, received_at)
                            .and_then(|receipt| {
                                m.observe_pose(&header, &transform, Some(receipt), Some(socket_generation))
                            })
                    })
                    .await
                    {
                        Ok(Ok(())) => {}
                        Ok(Err(e)) => return Err(e),
                        Err(_) => return Err("Pose worker failed".into()),
                    }
                }
                Ok(())
            })
        };
        // Fusion lane: decode + process on a blocking worker thread; rejects
        // count and keep flowing (consume parity).
        let mut fusion_worker = {
            let (q, mapping, ident, permits) =
                (fusion_q.clone(), s.mapping.clone(), identity.clone(), permits.clone());
            tokio::spawn(async move {
                while let Some((packet, received_at)) = q.wait().await {
                    let (m, i, p) = (mapping.clone(), ident.clone(), permits.clone());
                    let m2 = m.clone();
                    let r = tokio::task::spawn_blocking(move || {
                        phone::decode_frame(&packet, &i).and_then(|mut f| {
                            f.header["_received_monotonic"] = json!(received_at);
                            m2.process_permits(&mut f, Some(&p), Some(socket_generation))
                        })
                    })
                    .await;
                    match r {
                        Ok(Ok(())) => {}
                        Ok(Err(e)) => m.reject(e),
                        Err(_) => {}
                    }
                }
                Ok(())
            })
        };
        loop {
            tokio::select! {
                msg = rx.next() => match msg {
                    Some(Ok(Message::Binary(bytes))) => {
                        if camera_q.offer((bytes, now_monotonic())).is_some() {
                            s.camera.note_ingest_dropped();
                        }
                    }
                    Some(Ok(Message::Text(text))) => {
                        let header: Value = match serde_json::from_str::<Value>(&text) {
                            Ok(v) if v.is_object() => v,
                            _ => return Err("Expected a pose message or binary RGB-D frame".into()),
                        };
                        if header.get("type").and_then(Value::as_str) != Some("pose") {
                            return Err("Expected a pose message or binary RGB-D frame".into());
                        }
                        let transform = phone::validate_pose(&header, &identity)?;
                        // Tracking loss must revoke opt-in LiDAR drive without
                        // waiting for the pose/map worker's fusion lock.
                        s.camera.observe_phone_tracking(&header);
                        pose_q.offer((header, transform, now_monotonic()));
                    }
                    Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                    _ => {}
                },
                // FIRST_COMPLETED parity: any worker finishing (cleanly or
                // with the error it propagates) tears the session down.
                r = &mut cam_worker => match r {
                    Ok(Err(e)) => return Err(e),
                    Ok(Ok(())) => break,
                    Err(e) => return Err(format!("Camera fast-lane worker failed: {e}")),
                },
                r = &mut pose_worker => match r {
                    Ok(Err(e)) => return Err(e),
                    Ok(Ok(())) => break,
                    Err(e) => return Err(format!("Pose worker failed: {e}")),
                },
                r = &mut fusion_worker => match r {
                    Ok(Err(e)) => return Err(e),
                    Ok(Ok(())) => break,
                    Err(e) => return Err(format!("Fusion worker failed: {e}")),
                },
                // First failure wins (capture_permits.run parity): a dead
                // writer ends the session even while the receiver stays
                // open, or the issuer stays bound and owns the camera.
                _ = writer_opt.as_mut().unwrap() => break,
            }
        }
        fusion_q.close();
        camera_q.close();
        pose_q.close();
        cam_worker.abort();
        pose_worker.abort();
        fusion_worker.abort();
        Ok(())
    }
    .await;
    if let Some(permits) = &permits_opt {
        permits.close();
    }
    if let Err(reason) = result {
        if let Some(out_tx) = &out_tx_opt {
            let _ = out_tx
                .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                    code: 1008,
                    reason: reason.chars().take(100).collect::<String>().into(),
                })))
                .await;
        }
    }
    if let Some(writer) = writer_opt {
        writer.abort();
    }
    // Withdraw lock-free live eligibility immediately, then finish the
    // generation bumps before ownership is released (server.py parity:
    // disconnect_sensor runs to completion before phone_owner clears).
    s.mapping.inner.lock().unwrap().connected = false;
    s.mapping.disconnect();
    *s.phone_owner.lock().unwrap() = false;
}

async fn ws_phone_rgb(ws: WebSocketUpgrade, State(s): State<Arc<AppState>>) -> Response {
    ws.on_upgrade(move |socket| handle_ws_phone_rgb(socket, s))
}

async fn handle_ws_phone_rgb(socket: WebSocket, s: Arc<AppState>) {
    let (tx, rx) = socket.split();
    run_phone_rgb_session(tx, rx, s).await
}

async fn run_phone_rgb_session<TxErr, RxErr, Tx, Rx>(mut tx: Tx, mut rx: Rx, s: Arc<AppState>)
where
    Tx: futures_util::Sink<Message, Error = TxErr> + Unpin + Send + 'static,
    Rx: futures_util::Stream<Item = Result<Message, RxErr>> + Unpin + Send + 'static,
    TxErr: Send + 'static,
{
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
    let mut permits_opt: Option<Arc<crate::capture_permits::CapturePermits>> = None;
    let mut writer_opt: Option<tokio::task::JoinHandle<()>> = None;
    let mut out_tx_opt: Option<tokio::sync::mpsc::Sender<Message>> = None;
    let result = async {
        let hello_text = match tokio::time::timeout(Duration::from_secs(10), rx.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => t,
            _ => return Err("Expected phone hello".to_string()),
        };
        let hello = Hello::parse(&hello_text)?;
        let permits = Arc::new(crate::capture_permits::CapturePermits::new(
            &hello.session_id,
            hello.map_epoch,
            hello.supports_capture_permits,
        ));
        s.camera.begin_phone(Some(permits.clone()));
        // One writer task owns tx: the 20 Hz permit issuer ticks alongside
        // frame receipt (capture_permits.run parity).
        let (out_tx, mut out_rx) = tokio::sync::mpsc::channel::<Message>(16);
        let permits_writer = permits.clone();
        let writer = tokio::spawn(async move {
            let mut tick =
                tokio::time::interval(Duration::from_secs_f64(crate::capture_permits::PERIOD));
            loop {
                tokio::select! {
                    _ = tick.tick() => {
                        if let Some(packet) = permits_writer.issue() {
                            if tokio::time::timeout(
                                Duration::from_millis(100),
                                tx.send(Message::Text(packet.to_string().into())),
                            )
                            .await
                            .map(|r| r.is_err())
                            .unwrap_or(true)
                            {
                                return;
                            }
                        }
                    }
                    msg = out_rx.recv() => {
                        match msg {
                            Some(m) => {
                                if tx.send(m).await.is_err() { return; }
                            }
                            None => return,
                        }
                    }
                }
            }
        });
        out_tx_opt = Some(out_tx);
        writer_opt = Some(writer);
        permits_opt = Some(permits.clone());
        // Bounded latest-wins queue: the decoder always works on the newest
        // packet while the socket reader keeps flowing (#157 parity).
        let queue = Arc::new(LatestSlot::<(Bytes, f64)>::new());
        let mut consumer = {
            let (q, cam, session, permits) = (
                queue.clone(),
                s.camera.clone(),
                hello.session_id.clone(),
                permits.clone(),
            );
            tokio::spawn(async move {
                let mut previous: i64 = -1;
                while let Some((packet, received)) = q.wait().await {
                    let (c, sn, p) = (cam.clone(), session.clone(), permits.clone());
                    previous = tokio::task::spawn_blocking(move || {
                        c.accept_phone_rgb(packet.as_ref(), &sn, previous, Some(received), Some(&p))
                    })
                    .await
                    .map_err(|_| "worker failed".to_string())??;
                }
                Ok(())
            })
        };
        loop {
            tokio::select! {
                message = rx.next() => match message {
                    Some(Ok(Message::Binary(packet))) => {
                        if queue.offer((packet, now_monotonic())).is_some() {
                            s.camera.note_ingest_dropped();
                        }
                    }
                    // receive_bytes parity: a non-binary message ends the session.
                    Some(Ok(_)) => break,
                    _ => break,
                },
                r = &mut consumer => match r {
                    Ok(Err(e)) => return Err(e),
                    Ok(Ok(())) => break,
                    Err(e) => return Err(format!("RGB decode worker failed: {e}")),
                },
                // Same lifecycle race as handle_ws_phone: writer failure
                // revokes the issuer instead of stranding the session.
                _ = writer_opt.as_mut().unwrap() => break,
            }
        }
        queue.close();
        consumer.abort();
        Ok(())
    }
    .await;
    if let Some(permits) = &permits_opt {
        permits.close();
    }
    if let Err(reason) = result {
        if let Some(out_tx) = &out_tx_opt {
            let _ = out_tx
                .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                    code: 1008,
                    reason: reason.chars().take(100).collect::<String>().into(),
                })))
                .await;
        }
    }
    if let Some(writer) = writer_opt {
        writer.abort();
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
        .route("/map/status", get(map_status))
        .route("/map/points.bin", get(map_points))
        .route("/map/dense.bin", get(map_dense))
        .route("/map/mesh.bin", get(map_mesh))
        .route("/map/export.ply", get(map_export))
        .route("/map/reset", post(map_reset))
        .route("/map/pause", post(map_pause))
        .route("/camera.jpg", get(camera_jpg))
        .route("/depth.jpg", get(depth_jpg))
        .route("/stream.mjpg", get(stream_mjpg))
        .route("/video/status", get(video_status))
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
        .route("/tts/{name}", get(tts))
        .route("/nav/goal", post(nav_goal))
        .route("/nav/cancel", post(nav_cancel))
        .route("/nav/status", get(nav_status))
        .route("/ws/nav", get(ws_nav))
        // identity_router mounted unconditionally like the Python app
        // (replay servers included — the service stays disabled w/o models).
        .merge(crate::identity::router(state.identity.clone()));
    // Opt-in bonded upstream (experimental v1): only mounted with
    // --bonded-control; /ws/control keeps its single-owner behaviour.
    let app = if state.bonded.is_some() {
        app.route("/ws/control/bonded", get(ws_control_bonded))
    } else {
        app
    };
    let app = if state.sim.is_some() {
        app.route("/sim/state", get(sim_state))
    } else {
        app
    };
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

/// TcpListener wrapper that sets TCP_NODELAY on every accepted socket
/// (control/service.py parity for both plain and TLS paths).
pub struct NodelayListener(pub tokio::net::TcpListener);

impl axum::serve::Listener for NodelayListener {
    type Io = tokio::net::TcpStream;
    type Addr = SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match self.0.accept().await {
                Ok((io, addr)) => {
                    let _ = io.set_nodelay(true);
                    return (io, addr);
                }
                Err(_) => tokio::time::sleep(Duration::from_millis(10)).await,
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.0.local_addr()
    }
}

/// Peer address of the accepted socket; any listener yielding SocketAddr
/// (plain TCP and the axum_server TLS acceptor alike) feeds /ws/nav's
/// loopback check the way ws.client.host does in Python.
#[derive(Clone, Debug)]
pub struct PeerAddr(pub SocketAddr);

impl axum::extract::connect_info::Connected<axum::serve::IncomingStream<'_, NodelayListener>>
    for PeerAddr
{
    fn connect_info(target: axum::serve::IncomingStream<'_, NodelayListener>) -> Self {
        PeerAddr(*target.remote_addr())
    }
}

/// Serve the app over plain HTTP (TLS handled in main via axum-server).
pub async fn serve_http(app: Router, addr: SocketAddr) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(
        NodelayListener(listener),
        app.into_make_service_with_connect_info::<PeerAddr>(),
    )
    .await
}

#[cfg(test)]
mod phone_lifecycle_tests {
    //! The failed-send/open-receiver path: when the permit writer dies while
    //! the client socket stays open, the session must end — revoke the
    //! issuer and release the camera owner (Python races writer/reader
    //! FIRST_COMPLETED and closes the issuer on either failure).

    use super::*;
    use futures_util::stream::{SplitSink, SplitStream};
    use futures_util::{Sink, Stream};
    use std::pin::Pin;
    use std::task::{Context, Poll};
    use tokio::io::DuplexStream;
    use tokio_tungstenite::tungstenite::protocol::Role;
    use tokio_tungstenite::tungstenite::Message as TsMsg;
    use tokio_tungstenite::WebSocketStream;

    fn test_state() -> Arc<AppState> {
        build_state(
            false,      // demo
            None,       // camera_device
            None,       // serial_port
            false,      // simulate_rover
            "off",      // detector
            None, None, // detector_model, face_model
            false,      // h264
            None,       // mission_dir
            100, 0,     // max_pwm, min_pwm
            None, None, None, None, None, // assistant/whisper/piper
            None,       // tts_dir
            None,       // whisper_url
            None,       // piper_model_es
            None,       // speaker_cmd
            5.0,        // triage_interval
            None,       // depth_model
            false, 120.0, None, 90.0, // scan_pan, scan_fov, scan_intrinsics, scan_turn_dps
            15, 500_000, // video_fps, video_bitrate
            false, None, // record, replay
            None,       // drive_profile
            false,      // allow_calibration
            false,      // proximity_stop
            "servo",    // pan_mount
            None,       // autonomy_max_pwm
            false,      // bonded
            false,      // nav2
            0.45,       // nav_speed_mps
            3.0,        // nav_turn_radps
            0.7,        // nav_max_v
            0.8,        // nav_max_w
            0.15,       // nav_camera_height
            None,       // sim_world
            false,      // lidar_drive
            (0, 0),     // visual_caps: uncapped in tests
            false,      // fusion_bypass
            false,      // demo_mode
            (None, None, None), // identity_config
        )
    }

    fn hello_text(depth: bool) -> String {
        format!(
            "{{\"version\":1,\"type\":\"hello\",\"session_id\":\"t-{depth}\",\"map_epoch\":0,\
             \"supports_scene_depth\":{depth},\"supports_capture_permits\":true}}"
        )
    }

    pub(super) fn to_ts(m: Message) -> TsMsg {
        match m {
            Message::Text(t) => TsMsg::Text(t.as_str().to_string().into()),
            Message::Binary(b) => TsMsg::Binary(b),
            Message::Ping(p) => TsMsg::Ping(p),
            Message::Pong(p) => TsMsg::Pong(p),
            Message::Close(cf) => TsMsg::Close(cf.map(|f| {
                tokio_tungstenite::tungstenite::protocol::CloseFrame {
                    code: f.code.into(),
                    reason: f.reason.as_str().to_string().into(),
                }
            })),
        }
    }

    pub(super) fn from_ts(m: TsMsg) -> Option<Message> {
        Some(match m {
            TsMsg::Text(t) => Message::Text(t.as_str().to_string().into()),
            TsMsg::Binary(b) => Message::Binary(b),
            TsMsg::Ping(p) => Message::Ping(p),
            TsMsg::Pong(p) => Message::Pong(p),
            TsMsg::Close(cf) => Message::Close(cf.map(|f| {
                axum::extract::ws::CloseFrame {
                    code: f.code.into(),
                    reason: f.reason.as_str().to_string().into(),
                }
            })),
            TsMsg::Frame(_) => return None,
        })
    }

    pub(super) struct TxA(pub(super) SplitSink<WebSocketStream<DuplexStream>, TsMsg>);
    impl Sink<Message> for TxA {
        type Error = tokio_tungstenite::tungstenite::Error;
        fn poll_ready(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
            Pin::new(&mut self.0).poll_ready(cx)
        }
        fn start_send(mut self: Pin<&mut Self>, item: Message) -> Result<(), Self::Error> {
            Pin::new(&mut self.0).start_send(to_ts(item))
        }
        fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
            Pin::new(&mut self.0).poll_flush(cx)
        }
        fn poll_close(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
            Pin::new(&mut self.0).poll_close(cx)
        }
    }

    pub(super) struct RxA(pub(super) SplitStream<WebSocketStream<DuplexStream>>);
    impl Stream for RxA {
        type Item = Result<Message, tokio_tungstenite::tungstenite::Error>;
        fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
            loop {
                match Pin::new(&mut self.0).poll_next(cx) {
                    Poll::Ready(Some(Ok(m))) => match from_ts(m) {
                        Some(am) => return Poll::Ready(Some(Ok(am))),
                        None => continue,
                    },
                    Poll::Ready(Some(Err(e))) => return Poll::Ready(Some(Err(e))),
                    Poll::Ready(None) => return Poll::Ready(None),
                    Poll::Pending => return Poll::Pending,
                }
            }
        }
    }

    /// A client that completes the hello then never reads again: the first
    /// permit write saturates the tiny duplex pipe, the writer's send
    /// budget expires, and the session must end — the old code left the
    /// receiver open, the issuer bound, and the camera owner held.
    async fn dead_writer_session(
        depth: bool,
        run: fn(TxA, RxA, Arc<AppState>) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>,
    ) -> Arc<AppState> {
        let s = test_state();
        // 512-byte pipe: the hello fits in one write, but 20 Hz
        // permit packets saturate it within a few ticks when the
        // client never drains, reproducing a stalled writer.
        let (client_io, server_io) = tokio::io::duplex(512);
        let server_ws = WebSocketStream::from_raw_socket(server_io, Role::Server, None).await;
        let mut client = WebSocketStream::from_raw_socket(client_io, Role::Client, None).await;
        use futures_util::SinkExt;
        client.send(TsMsg::Text(hello_text(depth).into())).await.unwrap();
        // Never read the client side again; just hold it open.
        let _hold = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs(60)).await;
            drop(client);
        });
        let (tx, rx) = server_ws.split();
        let done = tokio::time::timeout(
            Duration::from_secs(10),
            run(TxA(tx), RxA(rx), s.clone()),
        )
        .await;
        assert!(
            done.is_ok(),
            "a failed writer must end the session while the receiver stays open"
        );
        assert!(
            !*s.phone_owner.lock().unwrap(),
            "camera owner still held after writer death"
        );
        s
    }

    #[tokio::test]
    async fn phone_rgb_dead_writer_ends_session_and_frees_owner() {
        dead_writer_session(false, |tx, rx, s| {
            Box::pin(run_phone_rgb_session(tx, rx, s))
        })
        .await;
    }

    #[tokio::test]
    async fn phone_depth_dead_writer_ends_session_and_frees_owner() {
        dead_writer_session(true, |tx, rx, s| {
            Box::pin(run_phone_session(tx, rx, s))
        })
        .await;
    }
}

#[cfg(test)]
mod bonded_ws_tests {
    //! Opt-in bonded upstream over real tungstenite WS pairs, mirroring
    //! phone_lifecycle_tests. Mirrors jetson/control/tests/test_bonded.py +
    //! test_bonded_transport.py: attach flows, credential rejects without
    //! owner revocation, canonical dedup, stale copies, conflicting seq,
    //! per-path congestion detach, and last-detach owner teardown.

    use super::phone_lifecycle_tests::{RxA, TxA};
    use super::*;
    use std::time::Instant;
    use tokio::io::DuplexStream;
    use tokio_tungstenite::tungstenite::protocol::Role;
    use tokio_tungstenite::tungstenite::Message as TsMsg;
    use tokio_tungstenite::WebSocketStream;

    fn bonded_setup() -> (Arc<ControlState>, Arc<BondedControlHub>) {
        let control = Arc::new(
            ControlState::new(false, 0, 100, None, None, None, None, false, false, "servo", None, None, false)
                .unwrap(),
        );
        control.set_connected(true, None);
        control.update_range(100.0);
        (
            control.clone(),
            Arc::new(BondedControlHub::new(control, None)),
        )
    }

    async fn bonded_ws_cap(hub: &Arc<BondedControlHub>, cap: usize) -> WebSocketStream<DuplexStream> {
        let (client_io, server_io) = tokio::io::duplex(cap);
        let server = WebSocketStream::from_raw_socket(server_io, Role::Server, None).await;
        let client = WebSocketStream::from_raw_socket(client_io, Role::Client, None).await;
        let (tx, rx) = server.split();
        tokio::spawn(run_bonded_session(TxA(tx), RxA(rx), hub.clone()));
        client
    }

    async fn bonded_ws(hub: &Arc<BondedControlHub>) -> WebSocketStream<DuplexStream> {
        bonded_ws_cap(hub, 128 * 1024).await
    }

    async fn send_text(client: &mut WebSocketStream<DuplexStream>, text: String) {
        client.send(TsMsg::Text(text.into())).await.unwrap();
    }

    /// Next incoming text frame as (parsed JSON, raw text); panics if a
    /// non-text frame or the stream end arrives first.
    async fn recv_json(client: &mut WebSocketStream<DuplexStream>) -> (Value, String) {
        match tokio::time::timeout(Duration::from_secs(5), client.next()).await {
            Ok(Some(Ok(TsMsg::Text(t)))) => {
                (serde_json::from_str(&t).unwrap(), t.to_string())
            }
            other => panic!("expected a text frame, got {other:?}"),
        }
    }

    async fn expect_closed(client: &mut WebSocketStream<DuplexStream>) {
        let frame = tokio::time::timeout(Duration::from_secs(5), client.next()).await;
        assert!(
            matches!(frame, Ok(Some(Ok(TsMsg::Close(_))))),
            "expected a close frame, got {frame:?}"
        );
    }

    async fn attach(client: &mut WebSocketStream<DuplexStream>, secret: Option<&str>) -> Value {
        let mut req = json!({"type": "attach", "version": 1});
        if let Some(s) = secret {
            req["secret"] = json!(s);
        }
        send_text(client, req.to_string()).await;
        let (v, _) = recv_json(client).await;
        assert_eq!(v["type"], "attached");
        assert_eq!(v["version"], 1);
        v
    }

    /// First status frame matching `pred` (checked on parsed status objects
    /// only; attached replies are skipped).
    async fn status_until(
        client: &mut WebSocketStream<DuplexStream>,
        pred: impl Fn(&Value) -> bool,
    ) -> Value {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let (v, _) = recv_json(client).await;
            if v.get("type").and_then(Value::as_str) != Some("control_status") {
                continue;
            }
            if pred(&v) {
                return v;
            }
            assert!(Instant::now() < deadline, "timed out waiting for status");
        }
    }

    /// Control command text; `shuffled` puts the keys in a different order —
    /// a different on-wire encoding of the same canonical payload.
    fn command_text(
        status: &Value,
        seq: i64,
        held: bool,
        v: f64,
        permit: Option<&str>,
        shuffled: bool,
    ) -> String {
        let session = status["session_id"].as_str().unwrap_or("");
        let permit = permit
            .map(str::to_string)
            .unwrap_or_else(|| status["permit"].as_str().unwrap_or("").to_string());
        let parts = [
            format!("\"seq\":{seq}"),
            format!("\"session_id\":\"{session}\""),
            format!("\"permit\":\"{permit}\""),
            format!("\"deadman\":{held}"),
            format!("\"v\":{v}"),
            "\"w\":0".to_string(),
            "\"pan\":0".to_string(),
            "\"mode\":\"teleop\"".to_string(),
            "\"video_fresh\":true".to_string(),
            "\"video_age_ms\":0".to_string(),
        ];
        let mut body = parts.to_vec();
        if shuffled {
            body.rotate_left(4);
        }
        format!("{{{}}}", body.join(","))
    }

    #[tokio::test]
    async fn bonded_two_paths_share_one_owner_and_status_hides_credential() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att_a = attach(&mut a, None).await;
        assert_eq!(att_a["secret"].as_str().unwrap().len(), 64);
        assert!(att_a["secret"]
            .as_str()
            .unwrap()
            .chars()
            .all(|c| c.is_ascii_hexdigit()));
        let mut b = bonded_ws(&hub).await;
        let att_b = attach(&mut b, Some(att_a["secret"].as_str().unwrap())).await;
        assert_eq!(att_a["session_id"], att_b["session_id"]);
        assert_ne!(att_a["path_id"], att_b["path_id"]);
        // One logical stream: once b is attached both paths carry the same
        // status_seq values with identical payloads.
        let (vb, _) = recv_json(&mut b).await;
        let mut va = recv_json(&mut a).await.0;
        while va["status_seq"].as_i64().unwrap() < vb["status_seq"].as_i64().unwrap() {
            va = recv_json(&mut a).await.0;
        }
        assert_eq!(va, vb);
        assert!(va["status_seq"].as_i64().unwrap() >= 1);
        assert!(!va.to_string().contains(att_a["secret"].as_str().unwrap()));
        assert!(va.get("path_id").is_none());
        assert_eq!(va["session_id"], att_a["session_id"]);
        assert_eq!(hub.path_count(), 2);
        assert_eq!(
            control.inner.lock().unwrap().session_id.as_deref(),
            Some(att_a["session_id"].as_str().unwrap())
        );
        drop((a, b));
    }

    #[tokio::test]
    async fn bonded_wrong_secret_rejected_without_revoking_owner() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att = attach(&mut a, None).await;
        let mut bad = bonded_ws(&hub).await;
        send_text(
            &mut bad,
            json!({"type": "attach", "version": 1, "secret": "0".repeat(64)}).to_string(),
        )
        .await;
        expect_closed(&mut bad).await;
        assert_eq!(hub.path_count(), 1);
        assert_eq!(
            control.inner.lock().unwrap().session_id.as_deref(),
            Some(att["session_id"].as_str().unwrap())
        );
        let s = status_until(&mut a, |_| true).await;
        assert_eq!(s["session_id"], att["session_id"]);
    }

    #[tokio::test]
    async fn bonded_third_path_rejected_without_revoking_owner() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att = attach(&mut a, None).await;
        let mut b = bonded_ws(&hub).await;
        attach(&mut b, Some(att["secret"].as_str().unwrap())).await;
        // Even the correct credential is refused once both paths are live.
        let mut c = bonded_ws(&hub).await;
        send_text(
            &mut c,
            json!({"type": "attach", "version": 1, "secret": att["secret"]}).to_string(),
        )
        .await;
        expect_closed(&mut c).await;
        assert_eq!(hub.path_count(), 2);
        assert_eq!(
            control.inner.lock().unwrap().session_id.as_deref(),
            Some(att["session_id"].as_str().unwrap())
        );
        let s = status_until(&mut a, |_| true).await;
        assert_eq!(s["session_id"], att["session_id"]);
    }

    #[tokio::test]
    async fn bonded_attach_must_be_a_valid_first_frame() {
        let (control, hub) = bonded_setup();
        let mut c = bonded_ws(&hub).await;
        send_text(&mut c, "{\"type\":\"attach\",\"version\":2}".to_string()).await;
        expect_closed(&mut c).await;
        assert!(control.inner.lock().unwrap().session_id.is_none());
        assert_eq!(hub.path_count(), 0);
    }

    #[tokio::test]
    async fn bonded_identical_copy_is_deduplicated_without_state_reentry() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att_a = attach(&mut a, None).await;
        let mut b = bonded_ws(&hub).await;
        attach(&mut b, Some(att_a["secret"].as_str().unwrap())).await;
        let s0 = status_until(&mut b, |s| s["permit"].is_string()).await;
        send_text(&mut a, command_text(&s0, 0, false, 0.0, None, false)).await;
        let s = status_until(&mut b, |v| v["seq"] == 0).await;
        send_text(&mut a, command_text(&s, 1, true, 0.5, None, false)).await;
        let s1 = status_until(&mut b, |v| v["seq"] == 1).await;
        assert_eq!(s1["left_pwm"], 50);
        assert_eq!(s1["right_pwm"], 50);
        // Identical canonical copy with a different key order on path B:
        // deduped before control state, so no stale_sequence stop.
        send_text(&mut b, command_text(&s, 1, true, 0.5, None, true)).await;
        tokio::time::sleep(Duration::from_millis(150)).await;
        let inner = control.inner.lock().unwrap();
        assert_eq!(inner.seq, 1);
        assert_eq!(inner.reason, "active");
        assert!(!inner.requires_release);
        drop(inner);
        let s2 = status_until(&mut b, |v| {
            v["status_seq"].as_i64() > s1["status_seq"].as_i64()
        })
        .await;
        assert_eq!(s2["reason"], "active");
        assert_eq!(s2["requires_release"], false);
        assert_eq!(s2["left_pwm"], 50);
    }

    #[tokio::test]
    async fn bonded_stale_release_copy_is_ignored() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att_a = attach(&mut a, None).await;
        let mut b = bonded_ws(&hub).await;
        attach(&mut b, Some(att_a["secret"].as_str().unwrap())).await;
        let s0 = status_until(&mut b, |s| s["permit"].is_string()).await;
        let release0 = command_text(&s0, 0, false, 0.0, None, false);
        send_text(&mut a, release0.clone()).await;
        let s = status_until(&mut b, |v| v["seq"] == 0).await;
        send_text(&mut a, command_text(&s, 1, true, 0.5, None, false)).await;
        status_until(&mut b, |v| v["seq"] == 1).await;
        // A stale copy of the earlier release arriving on the other path
        // must not stop the newer hold.
        send_text(&mut b, release0).await;
        tokio::time::sleep(Duration::from_millis(150)).await;
        let inner = control.inner.lock().unwrap();
        assert_eq!(inner.seq, 1);
        assert_eq!(inner.v, 0.5);
        assert_eq!(inner.reason, "active");
        assert!(!inner.requires_release);
    }

    #[tokio::test]
    async fn bonded_conflicting_duplicate_stops_and_needs_fresh_release() {
        let (_control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att_a = attach(&mut a, None).await;
        let mut b = bonded_ws(&hub).await;
        attach(&mut b, Some(att_a["secret"].as_str().unwrap())).await;
        let s0 = status_until(&mut b, |s| s["permit"].is_string()).await;
        send_text(&mut a, command_text(&s0, 0, false, 0.0, None, false)).await;
        let s = status_until(&mut b, |v| v["seq"] == 0).await;
        send_text(&mut a, command_text(&s, 1, true, 0.5, None, false)).await;
        status_until(&mut b, |v| v["seq"] == 1).await;
        // Same seq with a different payload: conflicting_duplicate stop.
        send_text(&mut b, command_text(&s, 1, true, -0.5, None, false)).await;
        let sc = status_until(&mut b, |v| v["reason"] == "conflicting_duplicate").await;
        assert_eq!(sc["requires_release"], true);
        assert_eq!(sc["left_pwm"], 0);
        // A fresh press while release is required is refused.
        let s_cur = status_until(&mut b, |v| v["permit"].is_string()).await;
        send_text(&mut a, command_text(&s_cur, 2, true, 0.5, None, false)).await;
        status_until(&mut b, |v| v["reason"] == "release_required").await;
        // A fresh release unlocks and new presses apply again.
        send_text(&mut a, command_text(&s_cur, 3, false, 0.0, None, false)).await;
        status_until(&mut b, |v| v["seq"] == 3).await;
        let s3 = status_until(&mut b, |v| v["requires_release"] == false).await;
        send_text(&mut a, command_text(&s3, 4, true, 0.5, None, false)).await;
        let s4 = status_until(&mut b, |v| v["seq"] == 4).await;
        assert_eq!(s4["left_pwm"], 50);
        assert_eq!(s4["reason"], "active");
    }

    #[tokio::test]
    async fn bonded_stalled_path_is_detached_while_owner_survives() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws_cap(&hub, 4 * 1024).await;
        let att_a = attach(&mut a, None).await;
        // a never reads again: its socket buffer fills, the writer misses the
        // 100 ms deadline, and only this path is torn down.
        let mut b = bonded_ws(&hub).await;
        attach(&mut b, Some(att_a["secret"].as_str().unwrap())).await;
        let deadline = Instant::now() + Duration::from_secs(5);
        while hub.path_count() == 2 {
            assert!(Instant::now() < deadline, "stalled path was not detached");
            let _ = status_until(&mut b, |_| true).await;
        }
        assert_eq!(
            control.inner.lock().unwrap().session_id.as_deref(),
            Some(att_a["session_id"].as_str().unwrap())
        );
        let s = status_until(&mut b, |_| true).await;
        assert_eq!(s["session_id"], att_a["session_id"]);
    }

    #[tokio::test]
    async fn bonded_last_detach_ends_owner_and_retires_credential() {
        let (control, hub) = bonded_setup();
        let mut a = bonded_ws(&hub).await;
        let att_a = attach(&mut a, None).await;
        let session = att_a["session_id"].as_str().unwrap().to_string();
        let mut b = bonded_ws(&hub).await;
        attach(&mut b, Some(att_a["secret"].as_str().unwrap())).await;
        // Losing one path keeps the owner live.
        drop(a);
        let deadline = Instant::now() + Duration::from_secs(3);
        while hub.path_count() != 1 {
            assert!(Instant::now() < deadline, "first detach never landed");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert_eq!(
            control.inner.lock().unwrap().session_id.as_deref(),
            Some(session.as_str())
        );
        let s = status_until(&mut b, |_| true).await;
        assert_eq!(s["session_id"], session);
        // Losing the last path ends ownership and retires the credential.
        drop(b);
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            {
                let inner = control.inner.lock().unwrap();
                if inner.session_id.is_none() {
                    assert!(inner.requires_release);
                    break;
                }
            }
            assert!(Instant::now() < deadline, "last detach never ended owner");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert_eq!(hub.path_count(), 0);
        // The retired secret cannot mint a new owner.
        let mut c = bonded_ws(&hub).await;
        send_text(
            &mut c,
            json!({"type": "attach", "version": 1, "secret": att_a["secret"]}).to_string(),
        )
        .await;
        expect_closed(&mut c).await;
        // A fresh owner still has to release before driving.
        let mut d = bonded_ws(&hub).await;
        let att_d = attach(&mut d, None).await;
        assert_ne!(att_d["session_id"], session);
        let s = status_until(&mut d, |_| true).await;
        assert_eq!(s["requires_release"], true);
    }
}
