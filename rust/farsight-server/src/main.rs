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
}

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

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
    if args.demo && args.serial_port.is_some() {
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
            || args.simulate_rover
            || args.record)
    {
        panic!("Replay cannot be combined with serial, live/simulated sources, or recording");
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
            axum_server::bind_rustls(addr, config)
                .serve(app.into_make_service())
                .await
                .unwrap();
        }
        _ => {
            server::serve_http(app, addr).await.unwrap();
        }
    }
}
