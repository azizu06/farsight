//! Standalone Mac-side geometry service: PULLs the rover phone's geometry
//! lane (tailnet or USB loopback upstream), assembles measured mesh-anchor
//! chunks, and serves the assembled `mesh.bin` to local consumers (Quest via
//! the Python relay or directly). Runs beside the existing relay — no relay
//! swap required.

use std::net::SocketAddr;

use clap::Parser;
use farsight_geometry as geom;

#[derive(Parser)]
struct Args {
    /// Local bind for the served geometry endpoints.
    #[arg(long, default_value = "127.0.0.1:8484")]
    listen: String,
    /// WebSocket URL of the phone geometry lane to pull, e.g.
    /// ws://rover:8083/geometry/stream or ws://127.0.0.1:18445/geometry/stream.
    /// May be repeated for tailnet-primary + USB-fallback.
    #[arg(long)]
    upstream: Vec<String>,
    /// Assembled triangle budget (least-recently-written anchors evict first).
    /// Default 100k faces: under the Quest decodeMesh ceiling (480k scalar
    /// indices = 160k faces) with headroom — board-agreed bound.
    #[arg(long, default_value_t = 100_000)]
    budget: usize,
    /// Reconnect backoff bounds (seconds) when the lane drops.
    #[arg(long, default_value_t = 0.5)]
    retry_min: f64,
    #[arg(long, default_value_t = 5.0)]
    retry_max: f64,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    let shared = geom::Shared::new(args.budget);
    tokio::spawn(geom::pull_loop(
        shared.clone(),
        args.upstream,
        args.retry_min,
        args.retry_max,
    ));
    let app = geom::router(shared);
    let addr: SocketAddr = args.listen.parse().expect("bind address");
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind");
    tracing::info!("farsight-geometry serving on {addr}");
    axum::serve(listener, app).await.expect("serve");
}
