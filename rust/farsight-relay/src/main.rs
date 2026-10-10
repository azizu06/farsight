//! farsight-relay — Rust port of tools/operator_relay.py.
//! Serves the Quest build over localhost (ADB reverse) and proxies the rover
//! upstream over a private network. The deadman/video-fresh contract and the
//! JSON URL rewrite are byte-compatible with the Python relay.


use std::collections::{HashMap, VecDeque};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::body::Body;
mod closures;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode, Uri};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use url::Url;

mod bonded;

const HOP_HEADERS: &[&str] = &[
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
];
const PROXY_PREFIXES: &[&str] = &["/ws/", "/map/", "/geometry/", "/dense/", "/tts/", "/video/", "/mission/", "/phone/"];
const PROXY_PATHS: &[&str] = &["/health", "/control/status", "/camera.jpg", "/depth.jpg", "/stream.mjpg", "/ask", "/phone", "/scan", "/speak"];
/// HTTP requests on these paths are control-plane traffic: they use the
/// dedicated upstream pool so bulk downloads can never take their slots.
const CONTROL_PATHS: &[&str] = &["/control/status", "/scan", "/ask"];
/// Only binary map downloads keep the client's Accept-Encoding across the
/// cellular leg; everything else asks upstream for identity because the
/// relay decodes and rewrites JSON (operator_relay.py parity).
const ENCODED_PATHS: &[&str] = &["/map/points.bin", "/map/mesh.bin", "/map/export.ply", "/geometry/mesh.bin", "/dense/chunk.bin", "/dense/delta.bin"];

pub(crate) type RemoteSink = Arc<
    tokio::sync::Mutex<
        futures_util::stream::SplitSink<
            tokio_tungstenite::WebSocketStream<
                tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
            >,
            tokio_tungstenite::tungstenite::Message,
        >,
    >,
>;
pub(crate) type LocalSink = Arc<tokio::sync::Mutex<futures_util::stream::SplitSink<WebSocket, Message>>>;

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Bounded local close frame (operator_relay.py wait_for(close, 0.25)).
async fn local_close(local_tx: &LocalSink, code: u16, reason: &'static str) {
    let _ = tokio::time::timeout(Duration::from_millis(250), async {
        let _ = local_tx
            .lock()
            .await
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code,
                reason: reason.into(),
            })))
            .await;
    })
    .await;
}

#[derive(Parser)]
#[command(about = "Serve Quest over USB and relay the rover through a private network")]
struct Args {
    /// Jetson http(s) origin on Tailscale
    #[arg(long)]
    upstream: String,
    #[arg(long, default_value_t = 8080)]
    port: u16,
    #[arg(long)]
    dist: Option<PathBuf>,
    /// Trusted CA PEM for a private upstream certificate
    #[arg(long)]
    ca_file: Option<PathBuf>,
    /// Optional farsight-geometry origin. When set, /geometry/* and
    /// /map/mesh.bin proxy to it instead of the Jetson (board-agreed
    /// Mac-assembly split: phone mesh -> Mac assembler -> Quest).
    #[arg(long)]
    geometry_upstream: Option<String>,
    /// Opt-in second route to the same --bonded-control Jetson; JPEG only
    #[arg(long)]
    bonded_upstream: Option<String>,
    /// Explicit single-route demo configuration (17d3b2d): skip the relay's
    /// displayed-video zeroing so the server sees the actual freshness
    /// fields. Cannot combine with --bonded-upstream.
    #[arg(long)]
    demo_mode: bool,
}

/// /ws/video last-hop diagnostics (48bb941 / operator_relay.py video_stats):
/// request round-trip, bounded network estimate, relay residence, send
/// duration, completed picture fragments, and stale closes.
#[derive(Default)]
pub(crate) struct VideoStats {
    pub fragments: u64,
    pub stale_disconnects: u64,
    pub request_ms: Option<f64>,
    pub network_bound_ms: Option<f64>,
    pub residence_ms: Option<f64>,
    pub send_ms: Option<f64>,
}

struct AppState {
    origin: Url,
    /// farsight-geometry origin for assembled-mesh routes (None = Jetson).
    geometry: Option<Url>,
    dist: PathBuf,
    http: reqwest::Client,     // general pool (limit ~16)
    control: reqwest::Client,  // control pool (limit ~4)
    /// Camera never waits for the map/bulk connection slots (#157).
    video: reqwest::Client,    // camera pool (limit ~4)
    video_stats: Arc<Mutex<VideoStats>>,
    tls: Arc<rustls::ClientConfig>,
    /// Opt-in bonded two-path mode (tools/bonded_relay.py parity).
    bonded: Option<Arc<bonded::BondedRelay>>,
    send_timeout: Duration,
    /// WebSocket heartbeat: ping each end this often (aiohttp heartbeat=10).
    ping_interval: Duration,
    /// A direction with no inbound traffic (including pong replies) for this
    /// long is declared dead — bounds a silently dropped cellular link.
    stale_after: Duration,
    /// tools/control_closures.py parity: bounded single-route /ws/control
    /// connection diagnostics surfaced at /relay/health.
    closures: Arc<closures::ControlClosures>,
    /// Explicit single-route demo mode: deadman commands keep the reported
    /// video freshness untouched instead of being zeroed (17d3b2d).
    demo_mode: bool,
}

pub(crate) fn upstream_origin(value: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|e| e.to_string())?;
    if !(url.scheme() == "http" || url.scheme() == "https")
        || url.host_str().is_none()
        || url.username() != ""
        || url.password().is_some()
        || !(url.path() == "" || url.path() == "/")
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("Upstream must be an http(s) origin without credentials, path, or query".into());
    }
    Ok(url.origin().ascii_serialization().parse::<Url>().unwrap())
}

fn local_url(value: &Value, upstream: &Url) -> Value {
    if let Some(s) = value.as_str() {
        if let Ok(url) = Url::parse(s) {
            if url.origin().ascii_serialization() == upstream.origin().ascii_serialization() {
                let rel = url.path().to_string()
                    + &url.query().map(|q| format!("?{q}")).unwrap_or_default()
                    + &url.fragment().map(|f| format!("#{f}")).unwrap_or_default();
                return Value::from(if rel.is_empty() { "/".into() } else { rel });
            }
        }
    }
    value.clone()
}

pub(crate) fn rewrite_json(value: &Value, upstream: &Url) -> Value {
    match value {
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), rewrite_json(v, upstream))).collect()),
        Value::Array(a) => Value::Array(a.iter().map(|v| rewrite_json(v, upstream)).collect()),
        _ => local_url(value, upstream),
    }
}

pub(crate) fn forward_headers(headers: &HeaderMap) -> HeaderMap {
    let mut extra: Vec<String> = Vec::new();
    if let Some(conn) = headers.get("connection").and_then(|v| v.to_str().ok()) {
        extra = conn.split(',').map(|s| s.trim().to_lowercase()).collect();
    }
    let mut out = HeaderMap::new();
    for (k, v) in headers.iter() {
        let kl = k.as_str().to_lowercase();
        if !HOP_HEADERS.contains(&kl.as_str()) && !extra.contains(&kl) {
            out.insert(k.clone(), v.clone());
        }
    }
    out
}

/// localhost-only + same-origin guard (DNS rebinding defense).
async fn local_only(req: Request, next: Next) -> Result<Response, StatusCode> {
    let uri: &Uri = req.uri();
    let host = uri.host().unwrap_or("");
    // Host header may live in headers rather than the URI on HTTP/1.
    let host = if host.is_empty() {
        req.headers()
            .get("host")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(':')
            .next()
            .unwrap_or("")
    } else {
        host
    };
    if !(host == "localhost" || host == "127.0.0.1" || host == "::1" || host.is_empty()) {
        return Ok((StatusCode::FORBIDDEN, "Relay accepts localhost only").into_response());
    }
    if let Some(origin) = req.headers().get("origin").and_then(|v| v.to_str().ok()) {
        let parsed = Url::parse(origin);
        let own = format!("http://{}", req.headers().get("host").and_then(|v| v.to_str().ok()).unwrap_or(""));
        match (parsed, Url::parse(&own)) {
            (Ok(o), Ok(own)) if o.origin() == own.origin() => {}
            (Ok(_), _) => return Ok((StatusCode::FORBIDDEN, "Cross-origin relay request rejected").into_response()),
            _ => return Ok((StatusCode::FORBIDDEN, "Invalid browser origin").into_response()),
        }
    }
    Ok(next.run(req).await)
}

async fn relay_health(State(s): State<Arc<AppState>>) -> Response {
    let video = {
        let st = s.video_stats.lock().unwrap();
        json!({"fragments": st.fragments, "stale_disconnects": st.stale_disconnects,
               "request_ms": st.request_ms, "network_bound_ms": st.network_bound_ms,
               "residence_ms": st.residence_ms, "send_ms": st.send_ms})
    };
    let mut packet = json!({
        "relay": true,
        "upstream": s.origin.as_str().trim_end_matches('/'),
        "geometry_upstream": s.geometry.as_ref().map(|u| u.as_str().trim_end_matches('/')),
        "quest_built": s.dist.join("index.html").is_file(),
        // eab0c88: enabled only while the single upstream route exists;
        // bonded-control is excluded from these counters by contract.
        "control_closures": s.closures.status(s.bonded.is_none()),
        "demo_mode": s.demo_mode,
        "video": video,
    });
    if let Some(bond) = &s.bonded {
        packet["transport"] = bond.status().await;
    }
    ([(header::CACHE_CONTROL, "no-store")], Json(packet)).into_response()
}

/// Bonded-mode guards (operator_relay.py dispatch parity): the private
/// upstream attachment endpoint is never proxied, and video streams are
/// rejected while the bonded JPEG-only profile is active.
async fn bonded_guards(
    State(s): State<Arc<AppState>>,
    req: Request,
    next: Next,
) -> Response {
    let path = req.uri().path();
    if path == "/ws/control/bonded" {
        return (StatusCode::FORBIDDEN, "Private upstream attachment endpoint").into_response();
    }
    if s.bonded.is_some()
        && (path == "/ws/video" || path == "/stream.mjpg" || path.starts_with("/video/"))
    {
        return (
            StatusCode::NOT_IMPLEMENTED,
            "Bonded prototype supports complete JPEG frames only",
        )
            .into_response();
    }
    next.run(req).await
}

/// The HTTP route for geometry paths when a --geometry-upstream origin is
/// configured (None = the Jetson origin serves everything).
fn geometry_route(s: &AppState, path: &str) -> Option<Url> {
    let g = s.geometry.as_ref()?;
    (path.starts_with("/geometry/") || path == "/map/mesh.bin").then(|| g.clone())
}

fn upstream_target(s: &AppState, req: &Request) -> String {
    let path_and_query = req.uri().path_and_query().map(|pq| pq.as_str()).unwrap_or("/");
    let path = req.uri().path();
    // Board-agreed split: assembled geometry comes from the Mac service,
    // everything else from the Jetson.
    if let Some(g) = &s.geometry {
        if path.starts_with("/geometry/") || path == "/map/mesh.bin" {
            return format!("{}{}", g.as_str().trim_end_matches('/'), path_and_query);
        }
    }
    format!("{}{}", s.origin.as_str().trim_end_matches('/'), path_and_query)
}

async fn ws_upgrade(
    ws: WebSocketUpgrade,
    State(s): State<Arc<AppState>>,
    req: Request,
) -> Response {
    let path = req.uri().path().to_string();
    if let Some(bond) = &s.bonded {
        if path == "/ws/control" {
            // Owner claim + upstream attach happen before the handshake is
            // accepted, exactly like the Python relay: a validation failure
            // is an HTTP error, not an accepted-then-closed socket.
            return match bond.begin_control().await {
                Ok(session) => ws.on_upgrade(move |socket| session.run(socket)),
                Err(resp) => resp,
            };
        }
    }
    let control = path == "/ws/control";
    let camera_video = path == "/ws/video";
    // Non-control sockets still ride the surviving validated route.
    let target = match &s.bonded {
        Some(bond) => match bond.preferred_origin().await {
            Ok(route) => format!(
                "{}{}",
                route.as_str().trim_end_matches('/'),
                req.uri().path_and_query().map(|pq| pq.as_str()).unwrap_or("/")
            ),
            Err(resp) => return resp,
        },
        None => upstream_target(&s, &req),
    };
    // Switch ws/wss for the tungstenite handshake.
    let target = target.replacen("http", "ws", 1);
    let send_timeout = s.send_timeout;
    ws.on_upgrade(move |socket| async move {
        relay_ws(socket, s, &target, control, camera_video, send_timeout).await;
    })
}

fn exc_name(e: &tokio_tungstenite::tungstenite::Error) -> &'static str {
    use tokio_tungstenite::tungstenite::Error as E;
    match e {
        E::Io(_) => "IoError",
        E::Tls(_) => "TlsError",
        E::Http(_) | E::HttpFormat(_) => "HttpError",
        E::ConnectionClosed | E::AlreadyClosed => "ConnectionClosed",
        E::Protocol(_) => "ProtocolError",
        E::Capacity(_) => "CapacityError",
        E::Url(_) => "UrlError",
        _ => "Error",
    }
}

async fn relay_ws(local: WebSocket, s: Arc<AppState>, target: &str, control: bool, camera_video: bool, send_timeout: Duration) {
    // Do not accept the Quest socket until the upstream socket exists.
    let max_msg = if control { 16 * 1024 } else { 8 * 1024 * 1024 };
    // control_closures parity: only the single-route /ws/control proxy is
    // diagnosed (bonded control is excluded).
    let attempt = control.then(|| s.closures.begin());
    let connector = tokio_tungstenite::Connector::Rustls(s.tls.clone());
    let mut ws_config = tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default();
    ws_config.max_message_size = Some(max_msg);
    let (remote, _) = match tokio_tungstenite::connect_async_tls_with_config(
        target,
        Some(ws_config),
        false,
        Some(connector),
    )
    .await
    {
        Ok(r) => r,
        Err(e) => {
            if let Some(a) = &attempt {
                a.end("upstream", "connect_failure", None, None, Some(exc_name(&e)), "connect");
            }
            let mut local = local;
            let _ = local
                .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                    code: 1011,
                    reason: format!("Rover unavailable: {e}").chars().take(120).collect::<String>().into(),
                })))
                .await;
            return;
        }
    };
    if let Some(a) = &attempt {
        a.opened();
    }
    let (local_tx, mut local_rx) = local.split();
    let (remote_tx, mut remote_rx) = remote.split();
    // Sinks are shared so the heartbeat and the bounded teardown can write
    // without owning the pipe tasks (operator_relay.py: cancel sibling, then
    // remote.close() and local.close() inside a 0.25 s deadline each).
    let remote_tx: RemoteSink = Arc::new(tokio::sync::Mutex::new(remote_tx));
    let local_tx: LocalSink = Arc::new(tokio::sync::Mutex::new(local_tx));
    // Last inbound traffic ms on each side; pongs count, so a quiet but
    // healthy link stays alive while a blackholed one is reaped.
    let remote_seen = Arc::new(AtomicU64::new(now_ms()));
    let local_seen = Arc::new(AtomicU64::new(now_ms()));
    // Camera sends carry a 150 ms backpressure deadline: a slow consumer is
    // reset rather than accumulating seconds of video (#157).
    let deadline = if control {
        send_timeout.min(Duration::from_millis(100))
    } else if camera_video {
        send_timeout.min(Duration::from_millis(150))
    } else {
        send_timeout
    };
    // Per-connection `next` request ledger (operator_relay.py: bounded 4,
    // oldest evicted) shared between the two pipe directions.
    let video_requests: Arc<Mutex<(HashMap<i64, Instant>, VecDeque<i64>)>> =
        Arc::new(Mutex::new((HashMap::new(), VecDeque::new())));
    let origin = s.origin.clone();

    // Quest -> rover. On control sockets the deadman is never relayed with a
    // stale video stamp: a modified client still produces a stop.
    let mut c2r = {
        let remote_tx = remote_tx.clone();
        let local_tx = local_tx.clone();
        let local_seen = local_seen.clone();
        let attempt = attempt.clone();
        // Explicit demo mode leaves reported video freshness untouched
        // (operator_relay.py --demo-mode); default still zeros it.
        let demo_mode = s.demo_mode;
        let video_requests_c2r = video_requests.clone();
        tokio::spawn(async move {
            while let Some(Ok(message)) = local_rx.next().await {
                local_seen.store(now_ms(), Ordering::Relaxed);
                match message {
                    Message::Text(text) => {
                        let mut data = text.to_string();
                        if camera_video {
                            if let Ok(packet) = serde_json::from_str::<Value>(&data) {
                                if packet.is_object()
                                    && packet.get("type").and_then(|v| v.as_str()) == Some("next")
                                    && packet.get("request_id").map(|v| v.is_i64()).unwrap_or(false)
                                {
                                    let rid = packet["request_id"].as_i64().unwrap();
                                    let mut reqs = video_requests_c2r.lock().unwrap();
                                    reqs.0.insert(rid, Instant::now());
                                    reqs.1.push_back(rid);
                                    while reqs.0.len() > 4 {
                                        if let Some(old) = reqs.1.pop_front() {
                                            reqs.0.remove(&old);
                                        } else {
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                        if control {
                            match serde_json::from_str::<Value>(&data) {
                                Ok(mut command) if command.is_object() => {
                                    if command["deadman"] == Value::Bool(true) && !demo_mode {
                                        let age = command["video_age_ms"].as_f64();
                                        if command["video_fresh"] != Value::Bool(true)
                                            || !age.map(|a| (0.0..=500.0).contains(&a)).unwrap_or(false)
                                        {
                                            command["deadman"] = json!(false);
                                            command["v"] = json!(0);
                                            command["w"] = json!(0);
                                            data = command.to_string();
                                        }
                                    }
                                }
                                _ => {
                                    if let Some(a) = &attempt {
                                        a.end("quest", "invalid_control", Some(1008), None, None, "receive");
                                    }
                                    local_close(&local_tx, 1008, "Invalid control JSON").await;
                                    return;
                                }
                            }
                        }
                        let rtx = remote_tx.clone();
                        if tokio::time::timeout(deadline, async move {
                            rtx.lock()
                                .await
                                .send(tokio_tungstenite::tungstenite::Message::Text(data.into()))
                                .await
                        })
                        .await
                        .is_err()
                        {
                            if let Some(a) = &attempt {
                                a.end("quest", "send_timeout", None, None, None, "send");
                            }
                            return;
                        }
                    }
                    Message::Binary(bytes) => {
                        if control {
                            if let Some(a) = &attempt {
                                a.end("quest", "invalid_control", Some(1008), None, None, "receive");
                            }
                            local_close(&local_tx, 1008, "Control requires JSON").await;
                            return;
                        }
                        let rtx = remote_tx.clone();
                        if tokio::time::timeout(deadline, async move {
                            rtx.lock()
                                .await
                                .send(tokio_tungstenite::tungstenite::Message::Binary(bytes.to_vec().into()))
                                .await
                        })
                        .await
                        .is_err()
                        {
                            if let Some(a) = &attempt {
                                a.end("quest", "send_timeout", None, None, None, "send");
                            }
                            return;
                        }
                    }
                    Message::Close(frame) => {
                        if let Some(a) = &attempt {
                            a.end("quest", "normal", frame.map(|f| f.code), None, None, "receive");
                        }
                        return;
                    }
                    _ => {}
                }
            }
            if let Some(a) = &attempt {
                a.end("quest", "normal", None, None, None, "receive");
            }
            let rtx = remote_tx.clone();
            let _ = tokio::time::timeout(Duration::from_millis(250), async move {
                let _ = rtx
                    .lock()
                    .await
                    .send(tokio_tungstenite::tungstenite::Message::Close(None))
                    .await;
            })
            .await;
        })
    };

    // Rover -> Quest. JSON telemetry may carry absolute upstream URLs; rewrite.
    // Bulk sockets are paced so control keeps its share of the upstream link.
    let mut r2c = {
        let local_tx = local_tx.clone();
        let remote_seen = remote_seen.clone();
        let attempt = attempt.clone();
        let video_stats = s.video_stats.clone();
        let video_requests_r2c = video_requests.clone();
        let mut pending_picture = false;
        tokio::spawn(async move {
            while let Some(Ok(message)) = remote_rx.next().await {
                remote_seen.store(now_ms(), Ordering::Relaxed);
                let arrived = Instant::now();
                match message {
                    tokio_tungstenite::tungstenite::Message::Text(text) => {
                        let mut data = text.to_string();
                        if camera_video {
                            match serde_json::from_str::<Value>(&data) {
                                Ok(mut packet) if packet.is_object()
                                    && packet.get("type").and_then(|v| v.as_str()) == Some("video_segment") =>
                                {
                                    pending_picture = true;
                                    let began = packet
                                        .get("request_id")
                                        .and_then(|v| v.as_i64())
                                        .and_then(|rid| video_requests_r2c.lock().unwrap().0.remove(&rid));
                                    let elapsed = began.map(|b| arrived.duration_since(b).as_secs_f64() * 1000.0);
                                    let wait = packet
                                        .get("server_wait_ms")
                                        .and_then(|v| v.as_f64())
                                        .filter(|w| w.is_finite() && *w >= 0.0)
                                        .unwrap_or(0.0);
                                    // Round-trip bound minus the server's own
                                    // measured wait — never a claimed one-way
                                    // transit or cross-host clock subtraction.
                                    let bound = elapsed.map(|e| (e - wait.min(e)).max(0.0));
                                    let residence = arrived.elapsed().as_secs_f64() * 1000.0;
                                    packet["relay_request_ms"] = elapsed.map_or(Value::Null, |v| json!(v));
                                    packet["relay_network_bound_ms"] = bound.map_or(Value::Null, |v| json!(v));
                                    packet["relay_residence_ms"] = json!(residence);
                                    {
                                        let mut st = video_stats.lock().unwrap();
                                        st.request_ms = elapsed;
                                        st.network_bound_ms = bound;
                                        st.residence_ms = Some(residence);
                                    }
                                    data = packet.to_string();
                                    let age = packet
                                        .get("age_ms")
                                        .and_then(|v| v.as_f64())
                                        .filter(|a| a.is_finite() && *a >= 500.0);
                                    if age.is_some() {
                                        video_stats.lock().unwrap().stale_disconnects += 1;
                                        let ltx = local_tx.clone();
                                        let _ = ltx.lock().await.send(Message::Text(data.into())).await;
                                        local_close(&ltx, 1013, "Stale camera; reconnect for latest keyframe").await;
                                        return;
                                    }
                                }
                                _ => {
                                    if let Ok(v) = serde_json::from_str::<Value>(&data) {
                                        data = rewrite_json(&v, &origin).to_string();
                                    }
                                }
                            }
                        } else if let Ok(v) = serde_json::from_str::<Value>(&data) {
                            data = rewrite_json(&v, &origin).to_string();
                        }
                        let ltx = local_tx.clone();
                        if tokio::time::timeout(deadline, async move {
                            ltx.lock().await.send(Message::Text(data.into())).await
                        })
                        .await
                        .is_err()
                        {
                            if let Some(a) = &attempt {
                                a.end("upstream", "send_timeout", None, None, None, "send");
                            }
                            return;
                        }
                    }
                    tokio_tungstenite::tungstenite::Message::Binary(bytes) => {
                        let ltx = local_tx.clone();
                        let send_began = Instant::now();
                        if tokio::time::timeout(deadline, async move {
                            ltx.lock().await.send(Message::Binary(bytes.to_vec().into())).await
                        })
                        .await
                        .is_err()
                        {
                            if let Some(a) = &attempt {
                                a.end("upstream", "send_timeout", None, None, None, "send");
                            }
                            return;
                        }
                        if camera_video {
                            let mut st = video_stats.lock().unwrap();
                            st.send_ms = Some(send_began.elapsed().as_secs_f64() * 1000.0);
                            // Initialization is not a picture; only matched
                            // requests count as completed fragments.
                            if pending_picture {
                                st.fragments += 1;
                                pending_picture = false;
                            }
                        }
                    }
                    tokio_tungstenite::tungstenite::Message::Close(frame) => {
                        if let Some(a) = &attempt {
                            let code = frame.and_then(|f| u16::try_from(f.code).ok());
                            a.end("upstream", "normal", None, code, None, "receive");
                        }
                        return;
                    }
                    _ => {}
                }
            }
            if let Some(a) = &attempt {
                a.end("upstream", "normal", None, None, None, "receive");
            }
            local_close(&local_tx, 1011, "Relay link ended; reconnect stopped").await;
        })
    };

    // Heartbeat + staleness watchdog (aiohttp heartbeat=10): a quiet link is
    // kept alive by pong replies; a blackholed one is reaped after
    // `stale_after` without any inbound traffic.
    let mut hb = {
        let remote_tx = remote_tx.clone();
        let local_tx = local_tx.clone();
        let remote_seen = remote_seen.clone();
        let local_seen = local_seen.clone();
        let attempt = attempt.clone();
        let (ping, stale) = (s.ping_interval, s.stale_after);
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(ping);
            tick.tick().await; // first tick is immediate
            loop {
                tick.tick().await;
                let now = now_ms();
                let stale_ms = stale.as_millis() as u64;
                if now.saturating_sub(remote_seen.load(Ordering::Relaxed)) > stale_ms
                    || now.saturating_sub(local_seen.load(Ordering::Relaxed)) > stale_ms
                {
                    if let Some(a) = &attempt {
                        let side = if now.saturating_sub(remote_seen.load(Ordering::Relaxed)) > stale_ms {
                            "upstream"
                        } else {
                            "quest"
                        };
                        a.end(side, "error", None, None, Some("HeartbeatTimeout"), "receive");
                    }
                    return;
                }
                let rtx = remote_tx.clone();
                let _ = tokio::time::timeout(Duration::from_millis(250), async move {
                    let _ = rtx
                        .lock()
                        .await
                        .send(tokio_tungstenite::tungstenite::Message::Ping(Vec::new().into()))
                        .await;
                })
                .await;
                let ltx = local_tx.clone();
                let _ = tokio::time::timeout(Duration::from_millis(250), async move {
                    let _ = ltx.lock().await.send(Message::Ping(Vec::new().into())).await;
                })
                .await;
            }
        })
    };

    tokio::select! {
        _ = &mut c2r => {}
        _ = &mut r2c => {}
        _ = &mut hb => {}
    }
    // First pipe to finish tears the whole session down: Python cancels the
    // sibling and closes both ends under a 0.25 s deadline.
    c2r.abort();
    r2c.abort();
    hb.abort();
    {
        let _ = tokio::time::timeout(Duration::from_millis(250), async {
            let _ = remote_tx
                .lock()
                .await
                .send(tokio_tungstenite::tungstenite::Message::Close(None))
                .await;
        })
        .await;
    }
    local_close(&local_tx, 1011, "Relay link ended; reconnect stopped").await;
    let _ = c2r.await;
    let _ = r2c.await;
    let _ = hb.await;
}

async fn proxy_or_static(State(s): State<Arc<AppState>>, req: Request) -> Response {
    let path = req.uri().path().to_string();
    let proxied = PROXY_PATHS.contains(&path.as_str())
        || PROXY_PREFIXES.iter().any(|p| path.starts_with(p));
    if proxied {
        if let Some(bond) = &s.bonded {
            if req.method() == axum::http::Method::GET
                && (path == "/camera.jpg" || path == "/health")
            {
                return bonded_get(s.clone(), bond.clone(), &path).await;
            }
            match bond.preferred_origin().await {
                Ok(route) => return proxy_http(s, req, route).await,
                Err(resp) => return resp,
            }
        }
        let route = geometry_route(&s, &path).unwrap_or_else(|| s.origin.clone());
        return proxy_http(s, req, route).await;
    }
    if req.method() != axum::http::Method::GET && req.method() != axum::http::Method::HEAD {
        return (StatusCode::METHOD_NOT_ALLOWED, [("allow", "GET, HEAD")]).into_response();
    }
    // Path traversal defense: strip prefix segments, only serve under dist.
    let rel = path.trim_start_matches('/');
    let candidate = s.dist.join(rel);
    let canonical = match candidate.canonicalize() {
        Ok(c) => c,
        Err(_) => return (StatusCode::NOT_FOUND, "Build Quest first: npm --prefix quest run build").into_response(),
    };
    let dist_canon = s.dist.canonicalize().unwrap_or_else(|_| s.dist.clone());
    if !canonical.starts_with(&dist_canon) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let file = if canonical.is_dir() { canonical.join("index.html") } else { canonical };
    if !file.is_file() {
        return (StatusCode::NOT_FOUND, "Build Quest first: npm --prefix quest run build").into_response();
    }
    let mime = mime_guess::from_path(&file).first_or_octet_stream();
    match tokio::fs::read(&file).await {
        Ok(bytes) => (
            [(header::CONTENT_TYPE, mime.to_string()), (header::CACHE_CONTROL, "no-cache".to_string())],
            bytes,
        )
            .into_response(),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

/// Bonded-mode /health and /camera.jpg: race complete responses across the
/// validated routes; /health additionally gets the transport block and the
/// usual upstream-URL rewrite (operator_relay.py proxy() parity).
async fn bonded_get(s: Arc<AppState>, bond: Arc<bonded::BondedRelay>, path: &str) -> Response {
    let reply = match bond.race_get(path).await {
        Ok(r) => r,
        Err(resp) => return resp,
    };
    let mut headers = reply.headers;
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if path != "/health" {
        return (StatusCode::OK, headers, reply.body).into_response();
    }
    let mut packet = match serde_json::from_slice::<Value>(&reply.body) {
        Ok(v) if v.is_object() => rewrite_json(&v, &reply.origin),
        _ => return (StatusCode::BAD_GATEWAY, "Rover unavailable: invalid JSON").into_response(),
    };
    packet["relay_transport"] = bond.status().await;
    headers.remove(header::CONTENT_ENCODING);
    headers.remove(header::CONTENT_LENGTH);
    let _ = s; // AppState kept in the signature for symmetry with proxy_http
    (StatusCode::OK, headers, Json(packet)).into_response()
}

async fn proxy_http(s: Arc<AppState>, req: Request, route: Url) -> Response {
    let target = {
        let path_and_query = req.uri().path_and_query().map(|pq| pq.as_str()).unwrap_or("/");
        format!("{}{}", route.as_str().trim_end_matches('/'), path_and_query)
    };
    let is_ws = req
        .headers()
        .get("upgrade")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("websocket"))
        .unwrap_or(false);
    if is_ws {
        // WebSocket upgrades are routed separately by the router.
        return (StatusCode::BAD_REQUEST, "Expected WebSocket upgrade").into_response();
    }
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    // Binary map downloads keep the client's Accept-Encoding: the jetson leg
    // is the cellular hop and encoded bodies pass straight through. Every
    // other path asks upstream for identity — the relay decodes and rewrites
    // JSON, so a compressed JSON body would be unmungeable here.
    let mut headers = forward_headers(req.headers());
    let ae = if ENCODED_PATHS.contains(&path.as_str()) {
        req.headers()
            .get(header::ACCEPT_ENCODING)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("identity")
            .to_string()
    } else {
        "identity".to_string()
    };
    headers.insert(header::ACCEPT_ENCODING, HeaderValue::from_str(&ae).unwrap());
    let body = req.into_body();
    let body_bytes = match axum::body::to_bytes(body, 8 * 1024 * 1024).await {
        Ok(b) => b,
        Err(_) => return (StatusCode::BAD_GATEWAY, "Rover unavailable").into_response(),
    };
    // Camera paths get their own four-slot pool so bulk downloads can never
    // starve the camera feed (48bb941 / operator_relay.py).
    let camera_path = path == "/camera.jpg" || path == "/stream.mjpg" || path.starts_with("/video/");
    let client = if camera_path {
        &s.video
    } else if CONTROL_PATHS.contains(&path.as_str()) {
        &s.control
    } else {
        &s.http
    };
    let remote = match client
        .request(method, &target)
        .headers(headers)
        .body(body_bytes)
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => return (StatusCode::BAD_GATEWAY, format!("Rover unavailable: {e:?}")).into_response(),
    };
    let status = StatusCode::from_u16(remote.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut out_headers = forward_headers(remote.headers());
    out_headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    // Rebind upstream redirects to this origin; never let them bypass USB.
    if let Some(loc) = out_headers.get(header::LOCATION).and_then(|v| v.to_str().ok()) {
        match route.join(loc) {
            Ok(redirect) if redirect.origin() == route.origin() => {
                let rel = redirect.path().to_string()
                    + &redirect.query().map(|q| format!("?{q}")).unwrap_or_default();
                out_headers.insert(header::LOCATION, HeaderValue::from_str(&rel).unwrap());
            }
            _ => return (StatusCode::BAD_GATEWAY, "Upstream redirect would bypass USB").into_response(),
        }
    }
    let content_type = remote
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    if content_type.starts_with("application/json") {
        let bytes = match remote.bytes().await {
            Ok(b) if b.len() <= 2 * 1024 * 1024 => b,
            _ => return (StatusCode::BAD_GATEWAY, "Upstream JSON exceeds relay limit").into_response(),
        };
        // JSON must be decoded before URL replacement, never string-replaced.
        let data = match serde_json::from_slice::<Value>(&bytes) {
            Ok(v) => rewrite_json(&v, &route).to_string(),
            Err(_) => return (StatusCode::BAD_GATEWAY, "Rover unavailable: invalid JSON").into_response(),
        };
        out_headers.remove(header::CONTENT_ENCODING);
        out_headers.remove(header::CONTENT_LENGTH);
        return (status, out_headers, data).into_response();
    }
    // Streaming proxy for everything else (MJPEG, binaries).
    let stream = remote.bytes_stream();
    let body = Body::from_stream(stream);
    (status, out_headers, body).into_response()
}

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[tokio::main]
async fn main() {
    rustls::crypto::CryptoProvider::install_default(rustls::crypto::aws_lc_rs::default_provider()).ok();
    tracing_subscriber::fmt::init();
    let args = Args::parse();
    let origin = match upstream_origin(&args.upstream) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    if args.demo_mode && args.bonded_upstream.is_some() {
        panic!("--demo-mode supports single-route control only; cannot combine with --bonded-upstream");
    }
    let second = match &args.bonded_upstream {
        Some(value) => match upstream_origin(value) {
            Ok(o) if o == origin => {
                eprintln!("Bonded upstreams must be distinct routes to the same Jetson");
                std::process::exit(2);
            }
            Ok(o) => Some(o),
            Err(e) => {
                eprintln!("{e}");
                std::process::exit(2);
            }
        },
        None => None,
    };
    let dist = args
        .dist
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../quest/dist"));
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_native_certs::load_native_certs().certs {
        let _ = roots.add(cert);
    }
    let mut extra_certs: Vec<reqwest::Certificate> = Vec::new();
    if let Some(ca_file) = &args.ca_file {
        if let Ok(pem) = std::fs::read(ca_file) {
            let mut rd = std::io::BufReader::new(&pem[..]);
            for cert in rustls_pemfile::certs(&mut rd).flatten() {
                if let Ok(c) = reqwest::Certificate::from_der(&cert) {
                    extra_certs.push(c);
                }
                let _ = roots.add(cert);
            }
        }
    }
    let tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let tls = Arc::new(tls);
    let make_client = |certs: &Vec<reqwest::Certificate>, idle: usize| {
        let mut b = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .pool_max_idle_per_host(idle);
        for c in certs {
            b = b.add_root_certificate(c.clone());
        }
        b.build().unwrap()
    };
    let http_client = make_client(&extra_certs, 16);
    let control_client = make_client(&extra_certs, 4);
    let video_client = make_client(&extra_certs, 4);
    let geometry = match args.geometry_upstream.as_deref().map(upstream_origin) {
        Some(Ok(u)) => Some(u),
        Some(Err(e)) => {
            eprintln!("error: --geometry-upstream: {e}");
            std::process::exit(2);
        }
        None => None,
    };
    let bonded = second.map(|second| {
        // Bonded GETs never follow redirects (aiohttp allow_redirects=False)
        // and ride the same CA trust as the shared pools.
        let mut b = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .pool_max_idle_per_host(16)
            .redirect(reqwest::redirect::Policy::none());
        for c in &extra_certs {
            b = b.add_root_certificate(c.clone());
        }
        Arc::new(
            bonded::BondedRelay::new([origin.clone(), second], b.build().unwrap(), tls.clone())
                .unwrap(),
        )
    });
    let state = Arc::new(AppState {
        origin,
        geometry,
        dist,
        http: http_client,
        control: control_client,
        video: video_client,
        video_stats: Arc::new(Mutex::new(VideoStats::default())),
        tls,
        bonded,
        send_timeout: Duration::from_millis(500),
        ping_interval: Duration::from_secs(10),
        stale_after: Duration::from_secs(20),
        closures: Arc::new(closures::ControlClosures::new()),
        demo_mode: args.demo_mode,
    });
    let app = build_router(state.clone());
    let addr = SocketAddr::from(([127, 0, 0, 1], args.port));
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    tracing::info!("farsight-relay listening on http://{addr} -> {}", args.upstream);
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let ctrl_c = tokio::signal::ctrl_c();
            #[cfg(unix)]
            let term = async {
                let mut sig = tokio::signal::unix::signal(
                    tokio::signal::unix::SignalKind::terminate(),
                )
                .unwrap();
                sig.recv().await;
            };
            #[cfg(not(unix))]
            let term = std::future::pending::<()>();
            tokio::select! {
                _ = ctrl_c => {}
                _ = term => {}
            }
            if let Some(bond) = &state.bonded {
                bond.close().await;
            }
        })
        .await
        .unwrap();
}

fn build_router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/relay/health", get(relay_health))
        .route("/ws/{*path}", get(ws_upgrade).post(proxy_or_static))
        .fallback(proxy_or_static)
        .layer(middleware::from_fn_with_state(state.clone(), bonded_guards))
        .layer(middleware::from_fn(local_only))
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn upstream() -> Url {
        upstream_origin("https://rover.tailnet.ts.net").unwrap()
    }

    #[test]
    fn upstream_validation() {
        for bad in [
            "https://rover/path",
            "https://u:pw@rover",
            "ftp://rover",
            "rover",
            "https://rover?q=1",
        ] {
            assert!(upstream_origin(bad).is_err(), "{bad}");
        }
        assert_eq!(upstream().as_str(), "https://rover.tailnet.ts.net/");
    }

    #[test]
    fn local_url_rewrites_same_origin_only() {
        let u = upstream();
        assert_eq!(
            local_url(&Value::from("https://rover.tailnet.ts.net/tts/42.wav?x=1"), &u),
            Value::from("/tts/42.wav?x=1")
        );
        assert_eq!(
            local_url(&Value::from("https://evil.com/tts/42.wav"), &u),
            Value::from("https://evil.com/tts/42.wav")
        );
        assert_eq!(local_url(&Value::from(5), &u), Value::from(5));
        assert_eq!(local_url(&Value::from("/already/relative"), &u), Value::from("/already/relative"));
    }

    #[test]
    fn rewrite_json_nested() {
        let u = upstream();
        let input = serde_json::json!({
            "speech": {"url": "https://rover.tailnet.ts.net/tts/7.wav"},
            "list": ["https://rover.tailnet.ts.net/a", "other"],
        });
        let out = rewrite_json(&input, &u);
        assert_eq!(out["speech"]["url"], "/tts/7.wav");
        assert_eq!(out["list"][0], "/a");
        assert_eq!(out["list"][1], "other");
    }

    #[test]
    fn forward_headers_drops_hops() {
        let mut h = HeaderMap::new();
        h.insert("host", "relay".parse().unwrap());
        h.insert("connection", "keep-alive, x-junk".parse().unwrap());
        h.insert("x-junk", "1".parse().unwrap());
        h.insert("te", "trailers".parse().unwrap());
        h.insert("keep-alive", "timeout=5".parse().unwrap());
        h.insert("x-custom", "yes".parse().unwrap());
        let out = forward_headers(&h);
        assert!(out.get("x-custom").is_some());
        for dropped in ["host", "connection", "x-junk", "te", "keep-alive"] {
            assert!(out.get(dropped).is_none(), "{dropped} leaked");
        }
    }
}

#[cfg(test)]
mod contract_tests {
    /// Port of tools/tests/test_relay_contract.py (#73): every rover path the
    /// Quest source calls must be proxied by this relay.
    #[test]
    fn every_quest_path_is_proxied() {
        let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../quest/src");
        let call = regex::Regex::new(
            r#"(?:fetch|fetchImpl|api|endpoint|URL)\(\s*["'`](/[A-Za-z0-9_./-]*)"#,
        )
        .unwrap();
        let local = ["/relay/health"];
        let mut missing = Vec::new();
        if !src.is_dir() {
            return; // quest/ not present in a rust-only checkout
        }
        for entry in walkdir(&src) {
            let path = entry.to_string_lossy().replace('\\', "/");
            if !(path.ends_with(".ts") || path.ends_with(".js")) {
                continue;
            }
            let text = std::fs::read_to_string(&entry).unwrap_or_default();
            for cap in call.captures_iter(&text) {
                let p = cap[1].to_string();
                if local.contains(&p.as_str())
                    || !super::PROXY_PATHS.contains(&p.as_str())
                        && !super::PROXY_PREFIXES.iter().any(|pre| p.starts_with(pre))
                {
                    missing.push((p, path.clone()));
                }
            }
        }
        assert!(missing.is_empty(), "Quest calls paths the relay does not proxy: {missing:?}");
    }

    fn walkdir(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
        let mut out = Vec::new();
        if let Ok(rd) = std::fs::read_dir(dir) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() {
                    out.extend(walkdir(&p));
                } else {
                    out.push(p);
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod recovery_tests {
    //! Relay recovery audit: upstream disconnect, blackholed upstream, and
    //! reconnect freshness (Python: cancel sibling, 0.25 s bounded closes,
    //! heartbeat liveness). relay never buffers or replays frames by
    //! construction — each recv is forwarded once or dropped.

    use super::*;
    use std::sync::atomic::AtomicUsize;
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;
    use tokio_tungstenite::tungstenite::Message as TMsg;

    pub(crate) fn test_state(upstream_port: u16, ping_ms: u64, stale_ms: u64) -> Arc<AppState> {
        rustls::crypto::CryptoProvider::install_default(
            rustls::crypto::aws_lc_rs::default_provider(),
        )
        .ok();
        let tls = Arc::new(
            rustls::ClientConfig::builder()
                .with_root_certificates(rustls::RootCertStore::empty())
                .with_no_client_auth(),
        );
        Arc::new(AppState {
            origin: upstream_origin(&format!("http://127.0.0.1:{upstream_port}")).unwrap(),
            geometry: None,
            dist: PathBuf::from("/nonexistent-dist"),
            http: reqwest::Client::new(),
            control: reqwest::Client::new(),
            video: reqwest::Client::new(),
            video_stats: Arc::new(Mutex::new(VideoStats::default())),
            tls,
            bonded: None,
            send_timeout: Duration::from_millis(300),
            ping_interval: Duration::from_millis(ping_ms),
            stale_after: Duration::from_millis(stale_ms),
            closures: Arc::new(closures::ControlClosures::new()),
            demo_mode: false,
        })
    }

    pub(crate) async fn start_relay(state: Arc<AppState>) -> (u16, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let h = tokio::spawn(async move {
            axum::serve(listener, build_router(state)).await.unwrap();
        });
        (port, h)
    }

    fn command(seq: i64) -> TMsg {
        TMsg::Text(
            json!({
                "session_id": "s", "permit": "p", "seq": seq, "deadman": true,
                "v": 0.5, "w": 0.0, "pan": 0, "mode": "teleop",
                "video_fresh": true, "video_age_ms": 0,
            })
            .to_string()
            .into(),
        )
    }

    /// Control client that counts how long until the socket ends.
    async fn client_close_secs(port: u16, heartbeats: usize) -> f64 {
        let url = format!("ws://127.0.0.1:{port}/ws/control");
        let (ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
        let (mut tx, mut rx) = ws.split();
        let writer = tokio::spawn(async move {
            for i in 0..heartbeats {
                if tx.send(command(i as i64)).await.is_err() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            tx
        });
        let start = std::time::Instant::now();
        while let Some(Ok(m)) = rx.next().await {
            if matches!(m, TMsg::Close(_)) {
                break;
            }
            if start.elapsed() > Duration::from_secs(5) {
                panic!("client socket never closed");
            }
        }
        let secs = start.elapsed().as_secs_f64();
        let _ = writer.await;
        secs
    }

    /// Upstream that dies mid-session: the local socket must close promptly
    /// and the sibling pipe must not linger as a zombie.
    #[tokio::test]
    async fn upstream_disconnect_closes_local_and_upstream() {
        let up_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let up_port = up_listener.local_addr().unwrap().port();
        let upstream_open = Arc::new(AtomicUsize::new(0));
        let open = upstream_open.clone();
        tokio::spawn(async move {
            let (sock, _) = up_listener.accept().await.unwrap();
            let ws = accept_async(sock).await.unwrap();
            let (tx, mut rx) = ws.split();
            open.fetch_add(1, Ordering::Relaxed);
            // Read two commands then die abruptly (drop the socket).
            let mut seen = 0;
            while let Some(Ok(m)) = rx.next().await {
                if matches!(m, TMsg::Text(_)) {
                    seen += 1;
                    if seen >= 2 {
                        break;
                    }
                }
            }
            drop(tx);
            drop(rx);
            open.fetch_sub(1, Ordering::Relaxed);
        });
        let state = test_state(up_port, 100, 500);
        let (port, _h) = start_relay(state).await;
        let secs = client_close_secs(port, 40).await;
        assert!(secs < 2.0, "local socket stayed open {secs}s after upstream died");
        assert_eq!(upstream_open.load(Ordering::Relaxed), 0, "upstream side saw a zombie connection");
    }

    /// Blackholed upstream (socket accepted, never reads or writes): the
    /// staleness watchdog must bound teardown — previously unbounded.
    #[tokio::test]
    async fn blackholed_upstream_is_bounded() {
        let up_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let up_port = up_listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (sock, _) = up_listener.accept().await.unwrap();
            let ws = accept_async(sock).await.unwrap();
            // Handshake done; now say nothing and never read, forever.
            std::future::pending::<()>().await;
            drop(ws);
        });
        let state = test_state(up_port, 100, 400);
        let (port, _h) = start_relay(state).await;
        let secs = client_close_secs(port, 60).await;
        assert!(secs < 2.0, "blackholed upstream bound teardown at {secs}s");
    }

    /// Reconnect is a fresh upstream connection; nothing is replayed — each
    /// accepted upstream socket sees only frames sent while it lived.
    #[tokio::test]
    async fn reconnect_gets_fresh_upstream_with_no_replay() {
        let up_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let up_port = up_listener.local_addr().unwrap().port();
        let seqs_seen = Arc::new(std::sync::Mutex::new(Vec::<(usize, Vec<i64>)>::new()));
        let conns = Arc::new(AtomicUsize::new(0));
        {
            let seqs_seen = seqs_seen.clone();
            let conns = conns.clone();
            tokio::spawn(async move {
                loop {
                    let (sock, _) = match up_listener.accept().await {
                        Ok(v) => v,
                        Err(_) => break,
                    };
                    let mine = conns.fetch_add(1, Ordering::Relaxed);
                    let seqs_seen = seqs_seen.clone();
                    tokio::spawn(async move {
                        let ws = match accept_async(sock).await {
                            Ok(w) => w,
                            Err(_) => return,
                        };
                        let (_tx, mut rx) = ws.split();
                        let mut mine_seqs = Vec::new();
                        while let Some(Ok(m)) = rx.next().await {
                            if let TMsg::Text(t) = m {
                                if let Ok(v) = serde_json::from_str::<Value>(&t) {
                                    if let Some(s) = v["seq"].as_i64() {
                                        mine_seqs.push(s);
                                        if s >= 2 {
                                            break; // pretend upstream dies mid-session
                                        }
                                    }
                                }
                            }
                        }
                        seqs_seen.lock().unwrap().push((mine, mine_seqs));
                    });
                }
            });
        }
        let state = test_state(up_port, 100, 800);
        let (port, _h) = start_relay(state).await;
        // First client: sends until the socket drops.
        client_close_secs(port, 40).await;
        // Second client: a fresh relay session opens a fresh upstream socket.
        client_close_secs(port, 3).await;
        assert_eq!(conns.load(Ordering::Relaxed), 2, "each relay session must own its upstream socket");
        let seen = seqs_seen.lock().unwrap();
        // Upstream socket #2 only ever saw seqs from client 2 (starting at 0) —
        // nothing queued from client 1's session leaked across.
        let second = seen.iter().find(|(i, _)| *i == 1).unwrap();
        assert!(
            second.1.iter().all(|&s| s <= 2),
            "reconnect replayed stale commands: {:?}",
            second.1
        );
    }
}

#[cfg(test)]
mod video_lane_tests {
    //! Port of tools/tests/test_camera_fastlane.py (#157): /ws/video segments
    //! get relay hop diagnostics, stale segments close the socket, and
    //! /relay/health reports the video counters.
    use super::*;
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;
    use tokio_tungstenite::tungstenite::Message as TMsg;

    /// Fake rover /ws/video: init + binary, then for each "next" request a
    /// video_segment + binary fragment. `age` controls the segment's age_ms.
    async fn video_upstream(age_ms: i64) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            while let Ok((sock, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let ws = accept_async(sock).await.unwrap();
                    let (mut tx, mut rx) = ws.split();
                    tx.send(TMsg::Text(r#"{"type":"video_init","mime":"fixture"}"#.into())).await.ok();
                    tx.send(TMsg::Binary(b"init".to_vec().into())).await.ok();
                    while let Some(Ok(TMsg::Text(t))) = rx.next().await {
                        if let Ok(v) = serde_json::from_str::<Value>(&t) {
                            if v.get("type").and_then(|x| x.as_str()) == Some("next") {
                                tokio::time::sleep(Duration::from_millis(20)).await;
                                let seg = json!({"type":"video_segment","request_id":v["request_id"],
                                    "server_wait_ms":10,"age_ms":age_ms,"jetson_encode_ms":8,
                                    "camera_ingest_ms":2});
                                tx.send(TMsg::Text(seg.to_string().into())).await.ok();
                                tx.send(TMsg::Binary(b"fragment".to_vec().into())).await.ok();
                            }
                        }
                    }
                });
            }
        });
        port
    }

    #[tokio::test]
    async fn video_segment_gets_relay_hops_and_health_counts() {
        let up_port = video_upstream(30).await;
        let state = recovery_tests::test_state(up_port, 100, 5000);
        let (port, _h) = recovery_tests::start_relay(state.clone()).await;
        let url = format!("ws://127.0.0.1:{port}/ws/video");
        let (ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
        let (mut tx, mut rx) = ws.split();
        assert_eq!(
            serde_json::from_str::<Value>(&rx.next().await.unwrap().unwrap().into_text().unwrap()).unwrap()["type"],
            "video_init"
        );
        assert_eq!(rx.next().await.unwrap().unwrap().into_data(), b"init".to_vec());
        tx.send(TMsg::Text(r#"{"type":"next","request_id":1}"#.into())).await.unwrap();
        let packet: Value =
            serde_json::from_str(&rx.next().await.unwrap().unwrap().into_text().unwrap()).unwrap();
        assert_eq!(packet["age_ms"], 30);
        // ~20 ms upstream sleep minus the reported 10 ms server wait.
        assert!(packet["relay_request_ms"].as_f64().unwrap() >= 15.0, "{}", packet);
        assert!(packet["relay_network_bound_ms"].as_f64().unwrap() >= 5.0, "{}", packet);
        assert!(packet["relay_residence_ms"].as_f64().unwrap() >= 0.0);
        assert_eq!(rx.next().await.unwrap().unwrap().into_data(), b"fragment".to_vec());
        let health: Value = serde_json::from_str(
            &reqwest::get(format!("http://127.0.0.1:{port}/relay/health"))
                .await
                .unwrap()
                .text()
                .await
                .unwrap(),
        )
        .unwrap();
        let stats = &health["video"];
        assert_eq!(stats["fragments"], 1, "{stats}");
        assert!(stats["send_ms"].is_number());
        assert!(stats["request_ms"].is_number());
        drop(tx);
        drop(rx);
        drop(state);
    }

    #[tokio::test]
    async fn stale_video_segment_closes_socket() {
        let up_port = video_upstream(900).await;
        let state = recovery_tests::test_state(up_port, 100, 5000);
        let (port, _h) = recovery_tests::start_relay(state.clone()).await;
        let url = format!("ws://127.0.0.1:{port}/ws/video");
        let (ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
        let (mut tx, mut rx) = ws.split();
        let _ = rx.next().await;
        let _ = rx.next().await;
        tx.send(TMsg::Text(r#"{"type":"next","request_id":1}"#.into())).await.unwrap();
        let mut closed = false;
        while let Some(Ok(m)) = rx.next().await {
            if let TMsg::Close(frame) = m {
                assert_eq!(u16::from(frame.unwrap().code), 1013);
                closed = true;
                break;
            }
        }
        assert!(closed, "stale segment did not close the socket");
        let health: Value = serde_json::from_str(
            &reqwest::get(format!("http://127.0.0.1:{port}/relay/health"))
                .await
                .unwrap()
                .text()
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(health["video"]["stale_disconnects"], 1);
        drop(tx);
        drop(state);
    }
}
