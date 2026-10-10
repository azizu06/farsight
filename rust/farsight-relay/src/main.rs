//! farsight-relay — Rust port of tools/operator_relay.py.
//! Serves the Quest build over localhost (ADB reverse) and proxies the rover
//! upstream over a private network. The deadman/video-fresh contract and the
//! JSON URL rewrite are byte-compatible with the Python relay.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
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

const HOP_HEADERS: &[&str] = &[
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
];
const PROXY_PREFIXES: &[&str] = &["/ws/", "/map/", "/tts/", "/video/", "/mission/", "/phone/"];
const PROXY_PATHS: &[&str] = &["/health", "/control/status", "/camera.jpg", "/depth.jpg", "/stream.mjpg", "/ask", "/phone"];

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
}

struct AppState {
    origin: Url,
    dist: PathBuf,
    http: reqwest::Client,     // general pool (limit ~16)
    #[allow(dead_code)]
    control: reqwest::Client,  // control pool (limit ~4)
    tls: Arc<rustls::ClientConfig>,
    send_timeout: Duration,
}

fn upstream_origin(value: &str) -> Result<Url, String> {
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

fn rewrite_json(value: &Value, upstream: &Url) -> Value {
    match value {
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), rewrite_json(v, upstream))).collect()),
        Value::Array(a) => Value::Array(a.iter().map(|v| rewrite_json(v, upstream)).collect()),
        _ => local_url(value, upstream),
    }
}

fn forward_headers(headers: &HeaderMap) -> HeaderMap {
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
    (
        [(header::CACHE_CONTROL, "no-store")],
        Json(json!({
            "relay": true,
            "upstream": s.origin.as_str().trim_end_matches('/'),
            "quest_built": s.dist.join("index.html").is_file(),
        })),
    )
        .into_response()
}

fn upstream_target(s: &AppState, req: &Request) -> String {
    let path_and_query = req.uri().path_and_query().map(|pq| pq.as_str()).unwrap_or("/");
    format!("{}{}", s.origin.as_str().trim_end_matches('/'), path_and_query)
}

async fn ws_upgrade(
    ws: WebSocketUpgrade,
    State(s): State<Arc<AppState>>,
    req: Request,
) -> Response {
    let control = req.uri().path() == "/ws/control";
    let target = upstream_target(&s, &req);
    // Switch ws/wss for the tungstenite handshake.
    let target = target.replacen("http", "ws", 1);
    let send_timeout = s.send_timeout;
    ws.on_upgrade(move |socket| async move {
        relay_ws(socket, s, &target, control, send_timeout).await;
    })
}

async fn relay_ws(local: WebSocket, s: Arc<AppState>, target: &str, control: bool, send_timeout: Duration) {
    // Do not accept the Quest socket until the upstream socket exists.
    let max_msg = if control { 16 * 1024 } else { 8 * 1024 * 1024 };
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
    let (mut local_tx, mut local_rx) = local.split();
    let (mut remote_tx, mut remote_rx) = remote.split();
    let deadline = if control { send_timeout.min(Duration::from_millis(100)) } else { send_timeout };
    let origin = s.origin.clone();

    // Quest -> rover. On control sockets the deadman is never relayed with a
    // stale video stamp: a modified client still produces a stop.
    let c2r = {
        let _origin = origin.clone();
        tokio::spawn(async move {
            while let Some(Ok(message)) = local_rx.next().await {
                match message {
                    Message::Text(text) => {
                        let mut data = text.to_string();
                        if control {
                            match serde_json::from_str::<Value>(&data) {
                                Ok(mut command) if command.is_object() => {
                                    if command["deadman"] == Value::Bool(true) {
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
                                    let _ = remote_tx
                                        .send(tokio_tungstenite::tungstenite::Message::Close(Some(
                                            tokio_tungstenite::tungstenite::protocol::CloseFrame {
                                                code: 1008u16.into(),
                                                reason: "Invalid control JSON".into(),
                                            },
                                        )))
                                        .await;
                                    return;
                                }
                            }
                        }
                        if tokio::time::timeout(deadline, remote_tx.send(tokio_tungstenite::tungstenite::Message::Text(data.into()))).await.is_err() {
                            return;
                        }
                    }
                    Message::Binary(bytes) => {
                        if control {
                            return;
                        }
                        if tokio::time::timeout(deadline, remote_tx.send(tokio_tungstenite::tungstenite::Message::Binary(bytes.to_vec().into()))).await.is_err() {
                            return;
                        }
                    }
                    Message::Close(_) => return,
                    _ => {}
                }
            }
            let _ = remote_tx
                .send(tokio_tungstenite::tungstenite::Message::Close(None))
                .await;
        })
    };

    // Rover -> Quest. JSON telemetry may carry absolute upstream URLs; rewrite.
    let r2c = tokio::spawn(async move {
        while let Some(Ok(message)) = remote_rx.next().await {
            match message {
                tokio_tungstenite::tungstenite::Message::Text(text) => {
                    let mut data = text.to_string();
                    if let Ok(v) = serde_json::from_str::<Value>(&data) {
                        data = rewrite_json(&v, &origin).to_string();
                    }
                    if tokio::time::timeout(deadline, local_tx.send(Message::Text(data.into()))).await.is_err() {
                        return;
                    }
                }
                tokio_tungstenite::tungstenite::Message::Binary(bytes) => {
                    if tokio::time::timeout(deadline, local_tx.send(Message::Binary(bytes.to_vec().into()))).await.is_err() {
                        return;
                    }
                }
                tokio_tungstenite::tungstenite::Message::Close(_) => return,
                _ => {}
            }
        }
        let _ = local_tx
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: 1011,
                reason: "Relay link ended; reconnect stopped".into(),
            })))
            .await;
    });

    tokio::select! {
        _ = c2r => {}
        _ = r2c => {}
    }
}

async fn proxy_or_static(State(s): State<Arc<AppState>>, req: Request) -> Response {
    let path = req.uri().path().to_string();
    let proxied = PROXY_PATHS.contains(&path.as_str())
        || PROXY_PREFIXES.iter().any(|p| path.starts_with(p));
    if proxied {
        return proxy_http(s, req).await;
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

async fn proxy_http(s: Arc<AppState>, req: Request) -> Response {
    let target = upstream_target(&s, &req);
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
    let mut headers = forward_headers(req.headers());
    headers.remove("accept-encoding");
    headers.insert("accept-encoding", HeaderValue::from_static("identity"));
    let method = req.method().clone();
    let body = req.into_body();
    let body_bytes = match axum::body::to_bytes(body, 8 * 1024 * 1024).await {
        Ok(b) => b,
        Err(_) => return (StatusCode::BAD_GATEWAY, "Rover unavailable").into_response(),
    };
    let remote = match s
        .http
        .request(method, &target)
        .headers(headers)
        .body(body_bytes)
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => return (StatusCode::BAD_GATEWAY, format!("Rover unavailable: {e}")).into_response(),
    };
    let status = StatusCode::from_u16(remote.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut out_headers = forward_headers(remote.headers());
    out_headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    // Rebind upstream redirects to this origin; never let them bypass USB.
    if let Some(loc) = out_headers.get(header::LOCATION).and_then(|v| v.to_str().ok()) {
        match s.origin.join(loc) {
            Ok(redirect) if redirect.origin() == s.origin.origin() => {
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
            Ok(v) => rewrite_json(&v, &s.origin).to_string(),
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
    let connector = move || {
        let mut b = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .pool_max_idle_per_host(16);
        for c in &extra_certs {
            b = b.add_root_certificate(c.clone());
        }
        b.build().unwrap()
    };
    let state = Arc::new(AppState {
        origin,
        dist,
        http: connector(),
        control: connector(),
        tls,
        send_timeout: Duration::from_millis(500),
    });
    let app = Router::new()
        .route("/relay/health", get(relay_health))
        .route("/ws/{*path}", get(ws_upgrade).post(proxy_or_static))
        .fallback(proxy_or_static)
        .layer(middleware::from_fn(local_only))
        .with_state(state);
    let addr = SocketAddr::from(([127, 0, 0, 1], args.port));
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    tracing::info!("farsight-relay listening on http://{addr} -> {}", args.upstream);
    axum::serve(listener, app).await.unwrap();
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
