//! farsight-server — Rust port of the Jetson mapping/control server.

use std::path::PathBuf;

use clap::Parser;
use farsight_server::server;

#[derive(Parser)]
#[command(about = "Farsight RGB-D mapper (Rust)")]
struct Args {
    /// Ray-cast room; no hardware needed
    #[arg(long)]
    demo: bool,
    #[arg(long, default_value = "0.0.0.0")]
    host: String,
    #[arg(long, default_value_t = 8443)]
    port: u16,
    #[arg(long)]
    ssl_certfile: Option<PathBuf>,
    #[arg(long)]
    ssl_keyfile: Option<PathBuf>,
    /// USB camera index or /dev/video path; default is phone RGB-D input
    #[arg(long)]
    camera: Option<String>,
    /// Uno serial path; omitted means physical motors disabled
    #[arg(long)]
    serial_port: Option<String>,
    /// Explicit virtual rover; never opens serial
    #[arg(long)]
    simulate_rover: bool,
    /// Initial motor PWM ceiling, 1..255
    #[arg(long, default_value_t = 100)]
    max_pwm: i64,
    /// Motor PWM floor to overcome stiction (FARSIGHT_MIN_PWM); 0 disables
    #[arg(long, default_value_t = 0)]
    min_pwm: i64,
    /// Opt-in measured drive profile (tools.calibrate_drive output); enables mode "velocity" in m/s, rad/s
    #[arg(long)]
    drive_profile: Option<PathBuf>,
    /// Accept raw-PWM mode "calibrate" from a held-deadman controller; for tools.calibrate_drive on a cleared floor only
    #[arg(long)]
    allow_calibration: bool,
    /// Ultrasonic stop: range < 25 cm or stale range blocks forward (default on); 'off' is an explicit opt-in for navigation runs that steer around obstacles
    #[arg(long, default_value = "on", value_parser = ["on", "off"])]
    proximity_stop: String,
    /// servo: a pan servo follows the commanded pan and forward drive is blocked while it is off-centre (default). none: no pan hardware; the headset pans digitally, the Uno never gets P and pan never blocks forward drive
    #[arg(long)]
    autonomy_max_pwm: Option<i64>,
    /// Opt-in: fresh /phone LiDAR frames with normal ARKit tracking may drive
    /// (FARSIGHT_LIDAR_DRIVE); default off — LiDAR is view-only.
    #[arg(long)]
    lidar_drive: bool,
    #[arg(long, default_value = "servo", value_parser = ["servo", "none"])]
    pan_mount: String,
    /// Bundled CPU person detector
    #[arg(long, default_value = "hog", value_parser = ["hog", "pose", "none"])]
    detector: String,
    /// YOLOv8/11 detection ONNX path; overrides bundled detector
    #[arg(long)]
    detector_model: Option<String>,
    /// UltraFace RFB-320 ONNX pass; omitted means off
    #[arg(long)]
    face_model: Option<String>,
    /// Metric Indoor Small 308x406 ONNX depth model
    #[arg(long)]
    depth_model: Option<String>,
    /// Physical camera pan installed; default is approximate rover turn
    #[arg(long)]
    scan_pan: bool,
    /// Camera horizontal FOV degrees; ignored with calibrated intrinsics
    #[arg(long, default_value_t = 70.0)]
    scan_fov: f64,
    /// Calibrated intrinsics for the transmitted RGB pixel grid: FX FY CX CY
    #[arg(long, num_args = 4)]
    scan_intrinsics: Option<Vec<f64>>,
    /// Measured turn degrees/sec at w=.3; approximate open-loop fallback
    #[arg(long, default_value_t = 90.0)]
    scan_turn_dps: f64,
    /// Enable ffmpeg H264 fragmented MP4 WebSocket
    #[arg(long)]
    h264: bool,
    #[arg(long, default_value_t = 10)]
    video_fps: u32,
    /// H264 bit/s; start low for cellular headroom
    #[arg(long, default_value_t = 500000)]
    video_bitrate: u32,
    /// Persist detection evidence beneath this directory
    #[arg(long)]
    mission_dir: Option<PathBuf>,
    /// Record JPEG, detections and control in --mission-dir
    #[arg(long)]
    record: bool,
    /// Loop a recorded mission directory; motors always disabled
    #[arg(long)]
    replay: Option<PathBuf>,
    /// Local llama-server origin, e.g. http://127.0.0.1:8082
    #[arg(long)]
    assistant_url: Option<String>,
    /// Running whisper-server origin (model stays loaded), e.g. http://127.0.0.1:8091
    #[arg(long)]
    whisper_url: Option<String>,
    #[arg(long)]
    whisper_bin: Option<String>,
    #[arg(long)]
    whisper_model: Option<String>,
    #[arg(long)]
    piper_bin: Option<String>,
    #[arg(long)]
    piper_model: Option<String>,
    /// Spanish Piper .onnx voice for survivor phrases
    #[arg(long)]
    piper_model_es: Option<String>,
    /// Rover speaker player, e.g. 'aplay -q' or 'paplay'; auto probes ALSA/Pulse; none disables
    #[arg(long, default_value = "auto")]
    speaker_cmd: String,
    /// Minimum seconds between SBAR model calls
    #[arg(long, default_value_t = 3.0)]
    triage_interval: f64,
    /// Directory for generated reply-*.wav speech
    #[arg(long)]
    tts_dir: Option<PathBuf>,
    /// Cap bulk (non-control) egress in Mbps so control/permit frames keep
    /// reserved headroom on a constrained link. 0 disables pacing.
    #[arg(long, default_value_t = 0.0)]
    bulk_limit_mbps: f64,
    /// Opt-in bonded upstream control: adds /ws/control/bonded for trusted
    /// operator-relay paths; /ws/control single-owner behaviour unchanged
    #[arg(long)]
    bonded_control: bool,
    /// Closed-loop indoor simulator: a moving ray-cast rover in a test floor
    /// plan feeding camera/depth/range/detections (SimFeed parity)
    #[arg(long, value_parser = ["deadend", "doorway", "floorplan", "popup", "seek"])]
    sim_world: Option<String>,
    /// Opt in to the ROS 2 Nav2 backend (ros2/farsight_nav bridge on /ws/nav); default off
    #[arg(long)]
    nav2: bool,
    /// Measured forward speed at v=1 (m/s)
    #[arg(long, default_value_t = 0.45)]
    nav_speed_mps: f64,
    /// Measured yaw rate at w=1 (rad/s)
    #[arg(long, default_value_t = 3.0)]
    nav_turn_radps: f64,
    /// Cap on Nav2 forward command, normalized 0..1
    #[arg(long, default_value_t = 0.7)]
    nav_max_v: f64,
    /// Cap on Nav2 turn command, normalized 0..1
    #[arg(long, default_value_t = 0.8)]
    nav_max_w: f64,
    /// Phone optical centre above the floor (m)
    #[arg(long, default_value_t = 0.15)]
    nav_camera_height: f64,
    /// Visual wire cap on exported map points (env FARSIGHT_MAP_POINTS_CAP,
    /// default 30000). Bounds only the served payload; the internal map
    /// keeps full resolution. 0 disables.
    #[arg(long)]
    map_points_cap: Option<usize>,
    /// Visual wire cap on exported mesh triangles (env
    /// FARSIGHT_MAP_TRIANGLES_CAP, default 40000). 0 disables.
    #[arg(long)]
    map_triangles_cap: Option<usize>,
    /// Skip the Jetson voxel/mesh fusion of /phone RGB-D — the mesh is
    /// built elsewhere (phone ARKit lane). Pose, depth for Nav2, camera
    /// and inference keep running. Env FARSIGHT_FUSION_BYPASS=1 also
    /// enables it (#109 contract).
    #[arg(long)]
    fusion_bypass: bool,
}

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .map(|v| v.parse().unwrap_or_else(|_| panic!("{name} must be a non-negative integer")))
        .unwrap_or(default)
}

#[tokio::main]
async fn main() {
    // Both rustls providers end up in the tree via axum-server + reqwest;
    // pin one or RustlsConfig panics on TLS startup.
    rustls::crypto::CryptoProvider::install_default(rustls::crypto::aws_lc_rs::default_provider()).ok();
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    farsight_server::pacer::init(args.bulk_limit_mbps);
    if args.simulate_rover && args.serial_port.is_some() {
        panic!("--simulate-rover cannot be combined with --serial-port");
    }
    if (args.demo || args.sim_world.is_some()) && args.serial_port.is_some() {
        panic!("A simulated camera cannot be used to drive physical motors");
    }
    if args.demo && args.camera.is_some() {
        panic!("Choose --demo or a real --camera, not both");
    }
    if args.record && args.mission_dir.is_none() {
        panic!("--record requires --mission-dir");
    }
    if args.detector == "pose" && args.detector_model.is_none() {
        panic!("--detector pose requires --detector-model (YOLO11-pose ONNX)");
    }
    let drive_profile = args
        .drive_profile
        .as_ref()
        .map(|p| {
            let profile = farsight_server::drive_profile::DriveProfile::load(p)
                .unwrap_or_else(|e| panic!("{e}"));
            if profile.meta.get("source").and_then(|v| v.as_str()) == Some("simulated") {
                eprintln!("warning: drive profile came from a SIMULATED rover; it does not describe real motors");
            }
            profile
        });
    if args.replay.is_some()
        && (args.serial_port.is_some()
            || args.camera.is_some()
            || args.demo
            || args.sim_world.is_some()
            || args.simulate_rover
            || args.record)
    {
        panic!("Replay cannot be combined with serial, live/simulated sources, or recording");
    }
    if args.sim_world.is_some() && args.camera.is_some() {
        panic!("--sim-world cannot be combined with a real --camera or --replay");
    }
    let state = server::build_state(
        args.demo,
        args.camera,
        args.serial_port,
        args.simulate_rover,
        // Model path overrides the bundled HOG backend, but an explicit
        // `--detector pose` keeps the pose decoder (R5).
        if args.detector == "hog" && args.detector_model.is_some() { "onnx" } else { &args.detector },
        args.detector_model.clone(),
        args.face_model.clone(),
        args.h264,
        args.mission_dir,
        args.max_pwm,
        args.min_pwm,
        args.assistant_url,
        args.whisper_bin,
        args.whisper_model,
        args.piper_bin,
        args.piper_model,
        args.tts_dir,
        args.whisper_url,
        args.piper_model_es,
        Some(args.speaker_cmd),
        args.triage_interval,
        args.depth_model,
        args.scan_pan,
        args.scan_fov,
        args.scan_intrinsics,
        args.scan_turn_dps,
        args.video_fps,
        args.video_bitrate,
        args.record,
        args.replay,
        drive_profile,
        args.allow_calibration,
        args.proximity_stop == "on",
        &args.pan_mount,
        args.autonomy_max_pwm,
        args.bonded_control,
        args.nav2,
        args.nav_speed_mps,
        args.nav_turn_radps,
        args.nav_max_v,
        args.nav_max_w,
        args.nav_camera_height,
        args.sim_world,
        args.lidar_drive,
        (
            args.map_points_cap.unwrap_or_else(|| env_usize("FARSIGHT_MAP_POINTS_CAP", 30_000)),
            args.map_triangles_cap.unwrap_or_else(|| env_usize("FARSIGHT_MAP_TRIANGLES_CAP", 40_000)),
        ),
        args.fusion_bypass
            || matches!(std::env::var("FARSIGHT_FUSION_BYPASS").ok().as_deref(), Some("1") | Some("true") | Some("on")),
    );
    server::start_services(&state);
    let dist = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../quest/dist");
    let dist = std::fs::canonicalize(&dist).ok();
    let app = server::router(state, dist);
    let addr: std::net::SocketAddr = format!("{}:{}", args.host, args.port)
        .parse()
        .expect("bind address");
    tracing::info!("farsight-server listening on {addr}");
    match (args.ssl_certfile, args.ssl_keyfile) {
        (Some(cert), Some(key)) => {
            let config = axum_server::tls_rustls::RustlsConfig::from_pem_file(cert, key)
                .await
                .expect("TLS config");
            // NoDelayAcceptor sets TCP_NODELAY on the accepted socket before
            // the TLS handshake (control/service.py parity).
            let acceptor = axum_server::tls_rustls::RustlsAcceptor::new(config)
                .acceptor(axum_server::accept::NoDelayAcceptor::new());
            // axum-server's TLS acceptor is a MakeService<SocketAddr>, not an
            // axum::serve::Listener, so ConnectInfo is injected per-request.
            use std::convert::Infallible;
            use std::future::{ready, Ready};
            use std::pin::Pin;
            use std::task::{Context, Poll};
            use tower_service::Service;

            #[derive(Clone)]
            struct ConnectMakeService {
                app: axum::Router,
            }
            impl Service<std::net::SocketAddr> for ConnectMakeService {
                type Response = ConnectService;
                type Error = Infallible;
                type Future = Ready<Result<ConnectService, Infallible>>;
                fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Infallible>> {
                    Poll::Ready(Ok(()))
                }
                fn call(&mut self, addr: std::net::SocketAddr) -> Self::Future {
                    ready(Ok(ConnectService { app: self.app.clone(), addr }))
                }
            }
            #[derive(Clone)]
            struct ConnectService {
                app: axum::Router,
                addr: std::net::SocketAddr,
            }
            impl Service<axum::http::Request<hyper::body::Incoming>> for ConnectService {
                type Response = axum::response::Response;
                type Error = Infallible;
                type Future = Pin<
                    Box<dyn std::future::Future<Output = Result<axum::response::Response, Infallible>> + Send>,
                >;
                fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Infallible>> {
                    Poll::Ready(Ok(()))
                }
                fn call(&mut self, mut req: axum::http::Request<hyper::body::Incoming>) -> Self::Future {
                    req.extensions_mut().insert(axum::extract::ConnectInfo(
                        farsight_server::server::PeerAddr(self.addr),
                    ));
                    Box::pin(self.app.call(req.map(axum::body::Body::new)))
                }
            }
            axum_server::Server::bind(addr)
                .acceptor(acceptor)
                .serve(ConnectMakeService { app })
                .await
                .unwrap();
        }
        _ => {
            server::serve_http(app, addr).await.unwrap();
        }
    }
}
