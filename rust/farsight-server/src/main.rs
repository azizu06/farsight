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
    /// Bundled CPU person detector
    #[arg(long, default_value = "hog", value_parser = ["hog", "none"])]
    detector: String,
    /// YOLOv8/11 detection ONNX path; overrides bundled detector
    #[arg(long)]
    detector_model: Option<String>,
    /// Enable ffmpeg H264 fragmented MP4 WebSocket
    #[arg(long)]
    h264: bool,
    /// Persist detection evidence beneath this directory
    #[arg(long)]
    mission_dir: Option<PathBuf>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    if args.simulate_rover && args.serial_port.is_some() {
        panic!("--simulate-rover cannot be combined with --serial-port");
    }
    if args.demo && args.serial_port.is_some() {
        panic!("A simulated camera cannot be used to drive physical motors");
    }
    if args.demo && args.camera.is_some() {
        panic!("Choose --demo or a real --camera, not both");
    }
    let _ = args.detector_model; // ONNX path only used with `detector` feature
    let state = server::build_state(
        args.demo,
        args.camera,
        args.serial_port,
        args.simulate_rover,
        if args.detector_model.is_some() { "onnx" } else { &args.detector },
        args.h264,
        args.mission_dir,
        args.max_pwm,
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
