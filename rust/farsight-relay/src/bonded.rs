//! Opt-in two-path bonded control — port of tools/bonded_relay.py.
//!
//! One Quest owner and at most two authenticated upstream paths; secrets stay
//! private. No command retries, queues or reconnects. Precontroller health is
//! read-only; camera GETs are rejected until an owner validates at least one
//! route. A restored path is only used by a new explicitly stopped controller
//! session: there is no silent new owner after the last-path fault.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::ws::{Message, WebSocket};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use futures_util::stream::FuturesUnordered;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Map, Value};
use tokio::sync::Mutex;
use tokio::task::{AbortHandle, JoinHandle};
use tokio::time::{interval, timeout};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message as TMsg;
use url::Url;

use crate::{forward_headers, LocalSink, RemoteSink};

pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
pub const ATTACH_TIMEOUT: Duration = Duration::from_secs(3);
pub const SEND_TIMEOUT: Duration = Duration::from_millis(100);
pub const CLOSE_TIMEOUT: Duration = Duration::from_millis(250);
pub const MAX_GET_BODY: usize = 2 * 1024 * 1024;
pub const MAX_HTTP_RACES: u64 = 4;
pub const MAX_COMMAND: usize = 2048;
pub const SAFE_INTEGER: i64 = (1 << 53) - 1;
/// aiohttp heartbeat=10 on upstream path sockets.
const PING_INTERVAL: Duration = Duration::from_secs(10);
/// A path with no inbound traffic (pong replies count) this long is dead.
const STALE_AFTER: Duration = Duration::from_secs(20);

fn http_err(code: StatusCode, text: &'static str) -> Response {
    (code, text).into_response()
}

/// The upstream fetch result that can win a race (Python CompleteReply).
pub struct CompleteReply {
    pub body: Bytes,
    pub headers: HeaderMap,
    pub origin: Url,
}

struct PathHandle {
    sink: RemoteSink,
    reader: JoinHandle<()>,
    writer: Option<JoinHandle<()>>,
}

#[derive(Default)]
struct Inner {
    paths: HashMap<Url, PathHandle>,
    local: Option<LocalSink>,
    /// Token identifying the live controller session (Python stores the
    /// owner task; a u64 survives the "same session_id, new owner" case).
    owner: Option<u64>,
    session_id: Option<String>,
    secret: Option<String>,
    /// (compact JSON, receipt instant, status_seq) of the newest status.
    latest: Option<(String, Instant, i64)>,
    last_status_seq: i64,
    last_command_seq: i64,
    last_delivered_seq: i64,
    disconnected: bool,
    closed: bool,
    path_faults: u64,
    http_races: u64,
    get_tasks: HashMap<u64, AbortHandle>,
    cleanup_tasks: HashMap<u64, AbortHandle>,
    /// Test-only fault injection (mirrors the Python fixtures monkey-patching
    /// send_str): writers bound for a gated route wait on the gate first, and
    /// fail_local makes every downstream status delivery fail.
    stall_sends: HashMap<Url, Arc<tokio::sync::Notify>>,
    fail_local: bool,
}

impl Inner {
    fn new() -> Self {
        let mut inner = Inner::default();
        inner.last_status_seq = -1;
        inner.last_command_seq = -1;
        inner.last_delivered_seq = -1;
        inner
    }

    /// Python _detach: remove the path if present, count the fault, and end
    /// the owner when the last authenticated path goes away.
    fn detach(&mut self, origin: &Url) -> Option<PathHandle> {
        let path = self.paths.remove(origin)?;
        self.path_faults += 1;
        if self.paths.is_empty() {
            self.disconnected = true;
        }
        Some(path)
    }
}

pub struct BondedRelay {
    pub origins: [Url; 2],
    http: reqwest::Client,
    tls: Arc<rustls::ClientConfig>,
    inner: Arc<Mutex<Inner>>,
    ids: AtomicU64,
}

impl BondedRelay {
    pub fn new(origins: [Url; 2], http: reqwest::Client, tls: Arc<rustls::ClientConfig>) -> Result<Self, String> {
        if origins[0] == origins[1] {
            return Err("Bonded relay requires two distinct origins".into());
        }
        Ok(Self {
            origins,
            http,
            tls,
            inner: Arc::new(Mutex::new(Inner::new())),
            ids: AtomicU64::new(1),
        })
    }

    pub async fn status(&self) -> Value {
        let g = self.inner.lock().await;
        json!({
            "configured_paths": 2,
            "attached_paths": g.paths.len(),
            "controller_active": g.owner.is_some(),
            "disconnected": g.disconnected,
            "path_faults": g.path_faults,
            "active_http_races": g.http_races,
            "automatic_reconnect": false,
            "profile": "bonded-jpeg",
        })
    }

    /// Route for newly initiated non-raced requests: first surviving attached
    /// origin while an owner lives, otherwise the configured primary.
    pub async fn preferred_origin(&self) -> Result<Url, Response> {
        let g = self.inner.lock().await;
        if g.closed || g.disconnected {
            return Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "Bonded controller disconnected"));
        }
        if g.owner.is_some() {
            for route in &self.origins {
                if g.paths.contains_key(route) {
                    return Ok(route.clone());
                }
            }
            return Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "No validated bonded route"));
        }
        Ok(self.origins[0].clone())
    }

    fn next_id(&self) -> u64 {
        self.ids.fetch_add(1, Ordering::Relaxed)
    }

    /// Track a background cleanup job so close()/session teardown can drain it.
    async fn track_cleanup(&self, task: JoinHandle<()>) {
        let id = self.next_id();
        self.inner.lock().await.cleanup_tasks.insert(id, task.abort_handle());
    }

    /// Bounded upstream socket close (aiohttp wait_for(close, .25) parity).
    async fn close_sink(sink: RemoteSink) {
        let _ = timeout(CLOSE_TIMEOUT, async {
            let _ = sink.lock().await.send(TMsg::Close(None)).await;
        })
        .await;
    }

    async fn close_local(local: LocalSink) {
        let _ = timeout(CLOSE_TIMEOUT, async {
            let _ = local.lock().await.send(Message::Close(None)).await;
        })
        .await;
    }

    fn ws_target(route: &Url) -> String {
        let base = route.as_str().trim_end_matches('/');
        format!("{}/ws/control/bonded", base.replacen("http", "ws", 1))
    }

    /// Connect one route to /ws/control/bonded (3 s deadline, 16 KiB cap).
    async fn connect(&self, route: &Url) -> Option<(RemoteSink, futures_util::stream::SplitStream<UpstreamStream>)> {
        let mut cfg = WebSocketConfig::default();
        cfg.max_message_size = Some(16384);
        let connector = tokio_tungstenite::Connector::Rustls(self.tls.clone());
        match timeout(
            CONNECT_TIMEOUT,
            tokio_tungstenite::connect_async_tls_with_config(
                Self::ws_target(route),
                Some(cfg),
                false,
                Some(connector),
            ),
        )
        .await
        {
            Ok(Ok((stream, _))) => {
                let (tx, rx) = stream.split();
                Some((Arc::new(Mutex::new(tx)), rx))
            }
            _ => None,
        }
    }

    /// Attach hello exchange on a connected socket; on success the path is
    /// registered and its reader is running. Returns false on any fault.
    async fn attach(self: &Arc<Self>, route: Url, sink: RemoteSink, mut rx: futures_util::stream::SplitStream<UpstreamStream>) -> bool {
        let relay = self.clone();
        let inner = self.inner.clone();
        let block_sink = sink.clone();
        let result = timeout(ATTACH_TIMEOUT, async move {
            let mut hello = json!({"type": "attach", "version": 1});
            {
                let g = inner.lock().await;
                if let Some(secret) = &g.secret {
                    hello["secret"] = json!(secret);
                }
            }
            timeout(SEND_TIMEOUT, async {
                block_sink.lock().await.send(TMsg::Text(hello.to_string().into())).await
            })
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
            let message = match rx.next().await {
                Some(Ok(m)) => m,
                _ => return Err(()),
            };
            let text = match message {
                TMsg::Text(t) => t,
                _ => return Err(()),
            };
            if text.len() > MAX_COMMAND {
                return Err(());
            }
            let reply = decode(&text)?;
            let (session_id, secret) = validate_attach(&reply)?;
            let mut g = inner.lock().await;
            if let (Some(sid), Some(sec)) = (&g.session_id, &g.secret) {
                if session_id != *sid || secret != *sec {
                    return Err(()); // different logical controller
                }
            }
            if g.disconnected {
                return Err(()); // logical controller already ended
            }
            g.session_id = Some(session_id);
            g.secret = Some(secret);
            let reader = tokio::spawn(read_loop(relay.clone(), route.clone(), rx, block_sink.clone()));
            g.paths.insert(route, PathHandle { sink: block_sink, reader, writer: None });
            Ok(())
        })
        .await;
        match result {
            Ok(Ok(())) => true,
            _ => {
                self.inner.lock().await.path_faults += 1;
                Self::close_sink(sink).await;
                false
            }
        }
    }

    /// Remove a live path and finish its socket; from_reader avoids aborting
    /// the calling reader task's own handle.
    async fn retire_path(self: &Arc<Self>, origin: &Url, from_reader: bool) {
        let (path, local) = {
            let mut g = self.inner.lock().await;
            let p = g.detach(origin);
            let local = if g.paths.is_empty() { g.local.clone() } else { None };
            (p, local)
        };
        if let Some(path) = path {
            if !from_reader {
                path.reader.abort();
            }
            if let Some(w) = path.writer {
                w.abort();
            }
            self.track_cleanup(tokio::spawn(Self::close_sink(path.sink))).await;
            if let Some(local) = local {
                self.track_cleanup(tokio::spawn(Self::close_local(local))).await;
            }
        }
    }

    /// A downstream status delivery failed: every authenticated path ends so
    /// the server-side owner and its lease die with the last attachment.
    async fn end_controller(self: &Arc<Self>) {
        let (paths, local) = {
            let mut g = self.inner.lock().await;
            let n = g.paths.len() as u64;
            let paths: Vec<(Url, PathHandle)> = g.paths.drain().collect();
            g.path_faults += n;
            if g.paths.is_empty() {
                g.disconnected = true;
            }
            (paths, g.local.clone())
        };
        for (_route, path) in paths {
            path.reader.abort();
            if let Some(w) = path.writer {
                w.abort();
            }
            self.track_cleanup(tokio::spawn(Self::close_sink(path.sink))).await;
        }
        if let Some(local) = local {
            self.track_cleanup(tokio::spawn(Self::close_local(local))).await;
        }
    }

    /// End-of-session teardown (Python control() finally): every surviving
    /// path closes, the server-side owner dies, controller state resets.
    async fn finish_session(self: &Arc<Self>) {
        let (paths, cleanups, local) = {
            let mut g = self.inner.lock().await;
            let paths: Vec<(Url, PathHandle)> = g.paths.drain().collect();
            let cleanups: Vec<AbortHandle> = g.cleanup_tasks.drain().map(|(_, h)| h).collect();
            (paths, cleanups, g.local.take())
        };
        for (_route, path) in paths {
            path.reader.abort();
            if let Some(w) = path.writer {
                w.abort();
            }
            Self::close_sink(path.sink).await;
        }
        // Cleanup jobs are bounded closes; wait briefly for them to land.
        let _ = timeout(Duration::from_millis(500), async {
            for h in cleanups {
                while !h.is_finished() {
                    tokio::task::yield_now().await;
                }
            }
        })
        .await;
        if let Some(local) = local {
            Self::close_local(local).await;
        }
        let mut g = self.inner.lock().await;
        g.disconnected = true;
        g.session_id = None;
        g.secret = None;
        g.latest = None;
        g.local = None;
        g.owner = None;
    }

    /// Claim the owner slot, connect and attach both routes, and wait for the
    /// first upstream status. Mirrors BondedRelay.control() before prepare():
    /// an error here is an HTTP failure, never an accepted socket.
    pub async fn begin_control(self: &Arc<Self>) -> Result<ControlSession, Response> {
        {
            let mut g = self.inner.lock().await;
            if g.closed {
                return Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "Bonded relay closed"));
            }
            if g.owner.is_some() {
                return Err(http_err(StatusCode::CONFLICT, "Another controller owns this relay"));
            }
            g.owner = Some(self.next_id());
            g.disconnected = false;
            g.last_status_seq = -1;
            g.last_command_seq = -1;
            g.last_delivered_seq = -1;
            g.latest = None;
        }
        // Each connected socket is attached immediately: leaving an already
        // connected secondary idle behind a slow first handshake would spend
        // its server-side attachment deadline. First-route preference.
        for configured in self.origins.clone() {
            if let Some((sink, rx)) = self.connect(&configured).await {
                self.attach(configured.clone(), sink, rx).await;
            }
            if self.inner.lock().await.disconnected {
                break; // never silently create a replacement owner after last loss
            }
        }
        {
            let g = self.inner.lock().await;
            if g.paths.is_empty() || g.disconnected {
                drop(g);
                self.finish_session().await;
                return Err(http_err(StatusCode::BAD_GATEWAY, "No validated bonded path"));
            }
        }
        // Startup readers retain one latest status, not seconds of permits.
        for _ in 0..100 {
            {
                let g = self.inner.lock().await;
                if g.latest.is_some() || g.paths.is_empty() {
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        {
            let g = self.inner.lock().await;
            if g.latest.is_none() || g.paths.is_empty() {
                drop(g);
                self.finish_session().await;
                return Err(http_err(StatusCode::BAD_GATEWAY, "No bonded status"));
            }
        }
        Ok(ControlSession { relay: self.clone() })
    }

    /// One complete bounded GET per route, first valid body wins.
    async fn fetch(&self, route: &Url, path: &str) -> Result<CompleteReply, ()> {
        timeout(CONNECT_TIMEOUT, async {
            let url = format!("{}{}", route.as_str().trim_end_matches('/'), path);
            let response = self
                .http
                .get(&url)
                .header(header::ACCEPT_ENCODING, "identity")
                .header(header::CACHE_CONTROL, "no-store")
                .send()
                .await
                .map_err(|_| ())?;
            if response.status() != StatusCode::OK {
                return Err(());
            }
            let headers = forward_headers(response.headers());
            let content_type = response
                .headers()
                .get(header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.split(';').next())
                .map(|v| v.trim().to_string())
                .unwrap_or_default();
            let mut body = Vec::new();
            let mut stream = response.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(|_| ())?;
                if body.len() + chunk.len() > MAX_GET_BODY {
                    return Err(());
                }
                body.extend_from_slice(&chunk);
            }
            let payload = Bytes::from(body);
            if path == "/health" {
                decode(std::str::from_utf8(&payload).map_err(|_| ())?)?;
            } else if content_type != "image/jpeg"
                || !payload.starts_with(b"\xff\xd8")
                || !payload.ends_with(b"\xff\xd9")
            {
                return Err(()); // incomplete JPEG
            }
            Ok(CompleteReply { body: payload, headers, origin: route.clone() })
        })
        .await
        .map_err(|_| ())?
    }

    /// Race GET /health or /camera.jpg across validated routes. Camera replies
    /// require the same owner that validated the route — a complete image can
    /// never cross an owner/session change while in flight.
    pub async fn race_get(self: &Arc<Self>, path: &str) -> Result<CompleteReply, Response> {
        if path != "/health" && path != "/camera.jpg" {
            return Err(http_err(StatusCode::BAD_REQUEST, "Only GET health/camera can be raced"));
        }
        let (owner, session_id, routes) = {
            let mut g = self.inner.lock().await;
            if g.closed || g.disconnected {
                return Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "Bonded controller disconnected"));
            }
            let owner = g.owner;
            let session_id = g.session_id.clone();
            if path == "/camera.jpg" && (owner.is_none() || session_id.is_none() || g.paths.is_empty()) {
                return Err(http_err(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Camera requires an owner-validated bonded path",
                ));
            }
            let routes: Vec<Url> = if owner.is_some() {
                self.origins.iter().filter(|r| g.paths.contains_key(*r)).cloned().collect()
            } else {
                self.origins.to_vec()
            };
            if routes.is_empty() {
                return Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "No validated bonded HTTP path"));
            }
            if g.http_races >= MAX_HTTP_RACES {
                return Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "Bonded GET capacity reached"));
            }
            g.http_races += 1;
            (owner, session_id, routes)
        };
        let mut futs = FuturesUnordered::new();
        for route in routes {
            let id = self.next_id();
            let inner = self.inner.clone();
            let relay = self.clone();
            let path = path.to_string();
            let handle = tokio::spawn(async move {
                let result = relay.fetch(&route, &path).await;
                inner.lock().await.get_tasks.remove(&id);
                result
            });
            self.inner.lock().await.get_tasks.insert(id, handle.abort_handle());
            futs.push(handle);
        }
        let mut outcome: Result<CompleteReply, Response> =
            Err(http_err(StatusCode::BAD_GATEWAY, "No complete bonded response"));
        while let Some(done) = futs.next().await {
            let Ok(Ok(reply)) = done else { continue };
            let g = self.inner.lock().await;
            if g.closed
                || g.disconnected
                || (owner.is_some() && (g.owner != owner || g.session_id != session_id))
            {
                drop(g);
                outcome = Err(http_err(StatusCode::SERVICE_UNAVAILABLE, "Bonded owner ended during GET"));
                break;
            }
            if g.owner.is_some() && !g.paths.contains_key(&reply.origin) {
                drop(g);
                continue; // route lost validation while its body was in flight
            }
            drop(g);
            outcome = Ok(reply);
            break;
        }
        for task in &futs {
            task.abort();
        }
        while futs.next().await.is_some() {}
        self.inner.lock().await.http_races -= 1;
        outcome
    }

    /// Shutdown: cancel in-flight GETs, end the owner session, drop paths.
    pub async fn close(&self) {
        let (jobs, paths, local) = {
            let mut g = self.inner.lock().await;
            g.closed = true;
            g.disconnected = true;
            let jobs: Vec<AbortHandle> = g.get_tasks.drain().map(|(_, h)| h).collect();
            let paths: Vec<(Url, PathHandle)> = g.paths.drain().collect();
            (jobs, paths, g.local.take())
        };
        for job in &jobs {
            job.abort();
        }
        for (_route, path) in paths {
            path.reader.abort();
            if let Some(w) = path.writer {
                w.abort();
            }
            Self::close_sink(path.sink).await;
        }
        if let Some(local) = local {
            Self::close_local(local).await;
        }
        let mut g = self.inner.lock().await;
        g.owner = None;
        g.session_id = None;
        g.secret = None;
        g.latest = None;
    }

    #[cfg(test)]
    pub(crate) async fn debug_counts(&self) -> (usize, usize, u64) {
        let g = self.inner.lock().await;
        (g.get_tasks.len(), g.cleanup_tasks.len(), g.path_faults)
    }
}

type UpstreamStream = tokio_tungstenite::WebSocketStream<
    tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
>;

/// Per-path receive loop: dedup statuses by ordinal, forward fresh ones to
/// Quest under the status lock, and fault this path on any protocol break.
/// A 10 s ping / 20 s staleness watchdog mirrors aiohttp heartbeat=10.
async fn read_loop(
    relay: Arc<BondedRelay>,
    route: Url,
    mut rx: futures_util::stream::SplitStream<UpstreamStream>,
    sink: RemoteSink,
) {
    let mut tick = interval(PING_INTERVAL);
    let mut last_seen = Instant::now();
    loop {
        tokio::select! {
            _ = tick.tick() => {
                if last_seen.elapsed() > STALE_AFTER {
                    break;
                }
                let pinger = sink.clone();
                let _ = timeout(CLOSE_TIMEOUT, async {
                    let _ = pinger.lock().await.send(TMsg::Ping(Vec::new().into())).await;
                })
                .await;
            }
            message = rx.next() => {
                let text = match message {
                    Some(Ok(TMsg::Text(t))) => {
                        last_seen = Instant::now();
                        t
                    }
                    Some(Ok(TMsg::Ping(_))) | Some(Ok(TMsg::Pong(_))) => {
                        last_seen = Instant::now();
                        continue;
                    }
                    _ => break,
                };
                let mut g = relay.inner.lock().await;
                let status = match validate_status(&text, g.session_id.as_deref()) {
                    Ok(s) => s,
                    Err(()) => {
                        drop(g);
                        break;
                    }
                };
                let seq = status["status_seq"].as_i64().unwrap();
                if seq <= g.last_status_seq {
                    drop(g);
                    continue;
                }
                g.last_status_seq = seq;
                let encoded = match py_dumps(&status) {
                    Ok(e) => e,
                    Err(()) => {
                        drop(g);
                        break;
                    }
                };
                g.latest = Some((encoded.clone(), Instant::now(), seq));
                if let Some(local) = g.local.clone() {
                    let fail = g.fail_local;
                    let send = async {
                        if fail {
                            Err(axum::Error::new("injected downstream fault"))
                        } else {
                            local.lock().await.send(Message::Text(encoded.into())).await
                        }
                    };
                    match timeout(SEND_TIMEOUT, send).await {
                        Ok(Ok(())) => {
                            g.last_delivered_seq = seq;
                        }
                        _ => {
                            drop(g);
                            relay.end_controller().await;
                            relay.retire_path(&route, true).await;
                            return;
                        }
                    }
                }
                drop(g);
            }
        }
    }
    relay.retire_path(&route, true).await;
}

/// The accepted local control session; runs after begin_control succeeds.
pub struct ControlSession {
    relay: Arc<BondedRelay>,
}

impl ControlSession {
    pub async fn run(self, socket: WebSocket) {
        let (tx, mut rx) = socket.split();
        let local: LocalSink = Arc::new(Mutex::new(tx));
        self.relay.inner.lock().await.local = Some(local.clone());
        // Deliver the retained fresh status if it is still younger than a
        // send deadline (Python: latest[2] > last_delivered and age < .1).
        {
            let mut g = self.relay.inner.lock().await;
            let fresh = g
                .latest
                .as_ref()
                .map(|(enc, at, seq)| (*seq > g.last_delivered_seq && at.elapsed() < SEND_TIMEOUT).then(|| enc.clone()))
                .flatten();
            if let Some(enc) = fresh {
                let seq = g.latest.as_ref().unwrap().2;
                let send = async { local.lock().await.send(Message::Text(enc.into())).await };
                if let Ok(Ok(())) = timeout(SEND_TIMEOUT, send).await {
                    g.last_delivered_seq = seq;
                }
            }
        }
        while let Some(message) = rx.next().await {
            match message {
                Ok(Message::Text(text)) => {
                    let encoded = {
                        let mut g = self.relay.inner.lock().await;
                        match validate_command(text.as_str(), &mut g) {
                            Ok(e) => e,
                            Err(()) => break,
                        }
                    };
                    // Each fresh command is attempted exactly once per live
                    // path. A still-pending writer retires the path rather
                    // than delaying or queueing the survivor's next heartbeat.
                    let retired = {
                        let mut g = self.relay.inner.lock().await;
                        let mut retired = Vec::new();
                        let origins: Vec<Url> = g.paths.keys().cloned().collect();
                        for origin in origins {
                            let stall = g.stall_sends.get(&origin).cloned();
                            let path = match g.paths.get_mut(&origin) {
                                Some(p) => p,
                                None => continue,
                            };
                            if path.writer.as_ref().map(|w| !w.is_finished()).unwrap_or(false) {
                                retired.push(origin);
                                continue;
                            }
                            let sink = path.sink.clone();
                            let relay = self.relay.clone();
                            let dest = origin.clone();
                            let data = encoded.clone();
                            path.writer = Some(tokio::spawn(async move {
                                let send = async {
                                    if let Some(gate) = stall {
                                        gate.notified().await;
                                    }
                                    sink.lock().await.send(TMsg::Text(data.into())).await
                                };
                                if !matches!(timeout(SEND_TIMEOUT, send).await, Ok(Ok(()))) {
                                    relay.retire_path(&dest, false).await;
                                }
                            }));
                        }
                        retired
                    };
                    for origin in retired {
                        self.relay.retire_path(&origin, false).await;
                    }
                    if self.relay.inner.lock().await.paths.is_empty() {
                        break;
                    }
                    // Buffered client reads can hold several commands; let
                    // healthy handoffs run before checking the next packet.
                    tokio::task::yield_now().await;
                }
                Ok(Message::Ping(_)) | Ok(Message::Pong(_)) => continue,
                _ => break,
            }
        }
        self.relay.finish_session().await;
    }
}

// ---------- canonical validation / encoding (pure functions) ----------

/// Python decode(): a JSON object; NaN/Infinity literals never parse under
/// serde_json, matching parse_constant rejection.
fn decode(data: &str) -> Result<Value, ()> {
    let value: Value = serde_json::from_str(data).map_err(|_| ())?;
    if !value.is_object() {
        return Err(());
    }
    Ok(value)
}

/// Python safe_int: a real int in [0, 2^53-1]; bools and floats excluded.
fn safe_int(value: &Value) -> bool {
    match value.as_i64() {
        Some(v) => (0..=SAFE_INTEGER).contains(&v),
        None => false,
    }
}

fn forbidden_keys(obj: &Map<String, Value>) -> bool {
    obj.contains_key("secret") || obj.contains_key("attach_secret") || obj.contains_key("path_id")
}

/// Attached-reply contract: exactly {type, version, session_id, secret,
/// path_id} with a 256-bit hex credential and bounded identifiers.
fn validate_attach(reply: &Value) -> Result<(String, String), ()> {
    let obj = reply.as_object().ok_or(())?;
    if obj.len() != 5
        || !obj.contains_key("type")
        || !obj.contains_key("version")
        || !obj.contains_key("session_id")
        || !obj.contains_key("secret")
        || !obj.contains_key("path_id")
    {
        return Err(());
    }
    // Python `type(version) is int and == 1`: serde PosInt(1) parses via
    // as_i64 while bool/1.0/string all fail it.
    if reply["type"] != "attached" || reply["version"].as_i64() != Some(1) {
        return Err(());
    }
    let session_id = reply["session_id"].as_str().ok_or(())?;
    if session_id.is_empty() || session_id.len() > 256 {
        return Err(());
    }
    let path_id = reply["path_id"].as_str().ok_or(())?;
    if path_id.is_empty() || path_id.len() > 128 {
        return Err(());
    }
    let secret = reply["secret"].as_str().ok_or(())?;
    if secret.len() != 64 || !secret.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()) {
        return Err(());
    }
    Ok((session_id.to_string(), secret.to_string()))
}

/// Status contract (Python _status): session match, safe-int increasing
/// ordinal, bounded permit, fixed lease, boolean flags, no private keys.
fn validate_status(data: &str, session_id: Option<&str>) -> Result<Value, ()> {
    let status = decode(data)?;
    let sid_matches = match (status.get("session_id").and_then(|v| v.as_str()), session_id) {
        (Some(got), Some(expected)) => got == expected,
        _ => false,
    };
    if !sid_matches
        || !safe_int(status.get("status_seq").unwrap_or(&Value::Null))
        || !matches!(status.get("permit").and_then(|v| v.as_str()), Some(p) if !p.is_empty() && p.len() <= 256)
        || status["permit_ms"].as_i64() != Some(250)
        || !status["rover"].is_boolean()
        || !status["requires_release"].is_boolean()
        || forbidden_keys(status.as_object().unwrap())
    {
        return Err(());
    }
    Ok(status)
}

/// Command contract (Python _command): session match, strictly increasing
/// safe-int seq, required boolean deadman, no private keys, not an attach.
/// A deadman command with a stale/missing video stamp is rewritten into a
/// stop, exactly like the Python relay.
fn validate_command(text: &str, inner: &mut Inner) -> Result<String, ()> {
    if text.len() > MAX_COMMAND {
        return Err(());
    }
    let mut command = decode(text)?;
    let sid_matches = match (command.get("session_id").and_then(|v| v.as_str()), inner.session_id.as_deref()) {
        (Some(got), Some(expected)) => got == expected,
        _ => false,
    };
    let seq = command.get("seq").cloned().unwrap_or(Value::Null);
    if !sid_matches
        || !safe_int(&seq)
        || seq.as_i64().unwrap() <= inner.last_command_seq
        || !command["deadman"].is_boolean()
        || forbidden_keys(command.as_object().unwrap())
        || command.get("type") == Some(&Value::from("attach"))
    {
        return Err(());
    }
    if command["deadman"] == Value::Bool(true) {
        let age_ok = command
            .get("video_age_ms")
            .and_then(|v| v.as_f64())
            .map(|a| a.is_finite() && (0.0..=500.0).contains(&a))
            .unwrap_or(false);
        if command.get("video_fresh") != Some(&Value::Bool(true)) || !age_ok {
            command["deadman"] = json!(false);
            command["v"] = json!(0);
            command["w"] = json!(0);
        }
    }
    let encoded = py_dumps_sorted(&command)?;
    if encoded.len() > MAX_COMMAND {
        return Err(());
    }
    inner.last_command_seq = seq.as_i64().unwrap();
    Ok(encoded)
}

/// json.dumps(value, allow_nan=False, separators=(",",":"), ensure_ascii=True):
/// non-finite floats fail, non-ASCII escapes to \uXXXX.
fn py_dumps(value: &Value) -> Result<String, ()> {
    let mut out = String::new();
    write_py(value, &mut out, false)?;
    Ok(out)
}

/// The sorted-keys variant used for command canonicalization (sort_keys=True).
fn py_dumps_sorted(value: &Value) -> Result<String, ()> {
    let mut out = String::new();
    write_py(value, &mut out, true)?;
    Ok(out)
}

fn write_py(value: &Value, out: &mut String, sort: bool) -> Result<(), ()> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if n.as_f64().map(|f| !f.is_finite()).unwrap_or(false) {
                return Err(());
            }
            out.push_str(&n.to_string());
        }
        Value::String(s) => write_py_str(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_py(item, out, sort)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            if sort {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for (i, key) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_py_str(key, out);
                    out.push(':');
                    write_py(&map[*key], out, sort)?;
                }
            } else {
                for (i, (key, item)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_py_str(key, out);
                    out.push(':');
                    write_py(item, out, sort)?;
                }
            }
            out.push('}');
        }
    }
    Ok(())
}

/// Python ensure_ascii escaping: " \\ and everything outside [ -~].
fn write_py_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let u = c as u32;
                if u > 0xffff {
                    let v = u - 0x10000;
                    out.push_str(&format!("\\u{:04x}\\u{:04x}", 0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff)));
                } else {
                    out.push_str(&format!("\\u{:04x}", u));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod unit_tests {
    use super::*;

    fn inner_with_session() -> Inner {
        let mut i = Inner::new();
        i.session_id = Some("fixture-owner".to_string());
        i
    }

    #[test]
    fn attach_reply_exact_shape() {
        let good = json!({"type":"attached","version":1,"session_id":"s","secret":"a".repeat(64),"path_id":"p"});
        assert!(validate_attach(&good).is_ok());
        // missing / extra key
        let mut extra = good.clone();
        extra["x"] = json!(1);
        assert!(validate_attach(&extra).is_err());
        let mut missing = good.clone();
        missing.as_object_mut().unwrap().remove("path_id");
        assert!(validate_attach(&missing).is_err());
        // version must be int 1
        let mut v = good.clone();
        v["version"] = json!(true);
        assert!(validate_attach(&v).is_err());
        let mut v = good.clone();
        v["version"] = json!(1.0);
        assert!(validate_attach(&v).is_err());
        // secret must be 64 lowercase hex
        for secret in ["a".repeat(63), "A".repeat(64), "g".repeat(64)] {
            let mut r = good.clone();
            r["secret"] = json!(secret);
            assert!(validate_attach(&r).is_err());
        }
        // bounded identifiers
        let mut r = good.clone();
        r["session_id"] = json!("");
        assert!(validate_attach(&r).is_err());
        let mut r = good.clone();
        r["path_id"] = json!("x".repeat(129));
        assert!(validate_attach(&r).is_err());
    }

    #[test]
    fn status_contract() {
        let ok = r#"{"session_id":"fixture-owner","status_seq":7,"permit":"p","permit_ms":250,"rover":true,"requires_release":false}"#;
        assert!(validate_status(ok, Some("fixture-owner")).is_ok());
        // bool status_seq rejected (type(value) is int, not bool)
        assert!(validate_status(
            r#"{"session_id":"fixture-owner","status_seq":true,"permit":"p","permit_ms":250,"rover":true,"requires_release":false}"#,
            Some("fixture-owner")
        )
        .is_err());
        // 250.0 is a float, not int
        assert!(validate_status(
            r#"{"session_id":"fixture-owner","status_seq":1,"permit":"p","permit_ms":250.0,"rover":true,"requires_release":false}"#,
            Some("fixture-owner")
        )
        .is_err());
        // private keys never cross
        for key in ["secret", "attach_secret", "path_id"] {
            let s = format!(
                r#"{{"session_id":"fixture-owner","status_seq":1,"permit":"p","permit_ms":250,"rover":true,"requires_release":false,"{key}":"x"}}"#
            );
            assert!(validate_status(&s, Some("fixture-owner")).is_err(), "{key}");
        }
        // wrong / missing session
        assert!(validate_status(ok, Some("other")).is_err());
        assert!(validate_status(ok, None).is_err());
        // negative / oversized / float ordinals rejected
        for seq in ["-1", "9007199254740992", "1.0", "\"7\""] {
            let s = format!(
                r#"{{"session_id":"fixture-owner","status_seq":{seq},"permit":"p","permit_ms":250,"rover":true,"requires_release":false}}"#
            );
            assert!(validate_status(&s, Some("fixture-owner")).is_err(), "{seq}");
        }
        // permit bounds
        assert!(validate_status(
            r#"{"session_id":"fixture-owner","status_seq":1,"permit":"","permit_ms":250,"rover":true,"requires_release":false}"#,
            Some("fixture-owner")
        )
        .is_err());
    }

    #[test]
    fn command_contract_and_stale_view_stop() {
        let mut inner = inner_with_session();
        let ok = r#"{"session_id":"fixture-owner","seq":0,"deadman":false}"#;
        assert!(validate_command(ok, &mut inner).is_ok());
        assert_eq!(inner.last_command_seq, 0);
        // non-increasing seq
        assert!(validate_command(ok, &mut inner).is_err());
        // missing deadman / non-bool deadman
        assert!(validate_command(r#"{"session_id":"fixture-owner","seq":1}"#, &mut inner).is_err());
        assert!(validate_command(r#"{"session_id":"fixture-owner","seq":1,"deadman":"yes"}"#, &mut inner).is_err());
        // private keys + attach spoof rejected
        assert!(validate_command(
            r#"{"session_id":"fixture-owner","seq":1,"deadman":false,"secret":"x"}"#,
            &mut inner
        )
        .is_err());
        assert!(validate_command(
            r#"{"session_id":"fixture-owner","seq":1,"deadman":false,"type":"attach"}"#,
            &mut inner
        )
        .is_err());
        // oversized raw command
        let big = format!(
            r#"{{"session_id":"fixture-owner","seq":1,"deadman":false,"pad":"{}"}}"#,
            "x".repeat(2100)
        );
        assert!(validate_command(&big, &mut inner).is_err());
    }

    #[test]
    fn deadman_with_stale_view_becomes_stop() {
        let mut inner = inner_with_session();
        for age in ["501", "-1", "\"12\""] {
            inner.last_command_seq = -1;
            let cmd = format!(
                r#"{{"session_id":"fixture-owner","seq":0,"deadman":true,"v":0.5,"w":0.2,"video_fresh":true,"video_age_ms":{age}}}"#
            );
            let out = validate_command(&cmd, &mut inner).unwrap();
            let v: Value = serde_json::from_str(&out).unwrap();
            assert_eq!(v["deadman"], json!(false), "{age}");
            assert_eq!(v["v"], json!(0));
            assert_eq!(v["w"], json!(0));
        }
        let mut inner = inner_with_session();
        inner.last_command_seq = -1;
        // video_fresh absent -> stop
        let out = validate_command(
            r#"{"session_id":"fixture-owner","seq":0,"deadman":true,"v":0.5,"w":0,"video_age_ms":10}"#,
            &mut inner,
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["deadman"], json!(false));
        // fresh view survives unchanged except canonical encoding
        let mut inner = inner_with_session();
        inner.last_command_seq = -1;
        let out = validate_command(
            r#"{"session_id":"fixture-owner","seq":0,"deadman":true,"v":0.5,"w":0,"video_fresh":true,"video_age_ms":40}"#,
            &mut inner,
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["deadman"], json!(true));
        assert_eq!(v["v"], json!(0.5));
    }

    #[test]
    fn canonical_encoding_sorted_ascii_compact() {
        let v = json!({"b":1,"a":"x\ty†","deadman":false});
        assert_eq!(py_dumps_sorted(&v).unwrap(), r#"{"a":"x\ty\u2020","b":1,"deadman":false}"#);
        // serde_json::Value cannot hold NaN/Infinity at all — non-finite
        // literals never parse and Number::from_f64 rejects them, which is
        // exactly Python's allow_nan=False guarantee.
        assert!(serde_json::Number::from_f64(f64::NAN).is_none());
        assert!(serde_json::Number::from_f64(f64::INFINITY).is_none());
        assert!(serde_json::from_str::<Value>("NaN").is_err());
        assert!(serde_json::from_str::<Value>("Infinity").is_err());
        // astral plane surrogate pair, like Python ensure_ascii
        assert_eq!(py_dumps(&json!("😀")).unwrap(), "\"\\ud83d\\ude00\"");
    }
}

#[cfg(test)]
mod fixture_tests {
    //! Port of tools/tests/test_bonded_relay.py socket fixtures: two local
    //! simulated backends behind the real relay router. Loopback only.

    use super::*;
    use crate::{build_router, upstream_origin, AppState};
    use std::net::SocketAddr;
    use std::path::PathBuf;
    use std::sync::atomic::AtomicUsize;
    use std::sync::Mutex as StdMutex;
    use axum::extract::ws::WebSocketUpgrade;
    use axum::extract::State;
    use axum::routing::get;
    use axum::{Json, Router};
    use futures_util::stream::{SplitSink, SplitStream};
    use tokio::net::TcpListener;
    use tokio::sync::Notify;

    const SECRET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    #[derive(Default, Clone)]
    struct UpOpts {
        bad_second: bool,
        old_copies: bool,
        binary_first: bool,
        malformed_first: bool,
        partial_first: bool,
        oversized_first: bool,
        blocked_health: bool,
        blocked_camera: bool,
    }

    /// A released gate stays open (Python's asyncio.Event): later requests
    /// must not re-block the way Notify::notify_waiters would let them.
    struct Gate {
        open: std::sync::atomic::AtomicBool,
        notify: Notify,
    }
    impl Gate {
        async fn wait(&self) {
            while !self.open.load(Ordering::Relaxed) {
                self.notify.notified().await;
            }
        }
        fn release(&self) {
            self.open.store(true, Ordering::Relaxed);
            self.notify.notify_waiters();
        }
    }

    struct Gates {
        health: Gate,
        camera: Gate,
    }

    struct Fixture {
        idx: usize,
        opts: UpOpts,
        gates: Arc<Gates>,
        commands: StdMutex<Vec<String>>,
        times: StdMutex<Vec<Instant>>,
        images: AtomicUsize,
    }

    fn empty_tls() -> Arc<rustls::ClientConfig> {
        rustls::crypto::CryptoProvider::install_default(rustls::crypto::aws_lc_rs::default_provider())
            .ok();
        Arc::new(
            rustls::ClientConfig::builder()
                .with_root_certificates(rustls::RootCertStore::empty())
                .with_no_client_auth(),
        )
    }

    async fn serve_app(app: Router) -> Url {
        let listener = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0))).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });
        upstream_origin(&format!("http://127.0.0.1:{port}")).unwrap()
    }

    /// Simulated bonded backend: /ws/control/bonded attach + 40 Hz status
    /// publisher, /camera.jpg and /health like the Python fixtures.
    async fn spawn_upstream(idx: usize, opts: UpOpts, gates: Arc<Gates>) -> (Url, Arc<Fixture>) {
        let fixture = Arc::new(Fixture {
            idx,
            opts,
            gates,
            commands: StdMutex::new(Vec::new()),
            times: StdMutex::new(Vec::new()),
            images: AtomicUsize::new(0),
        });
        let app = Router::new()
            .route("/ws/control/bonded", get(up_control))
            .route("/camera.jpg", get(up_camera))
            .route("/health", get(up_health))
            .with_state(fixture.clone());
        (serve_app(app).await, fixture)
    }

    async fn up_control(ws: WebSocketUpgrade, State(f): State<Arc<Fixture>>) -> Response {
        ws.on_upgrade(move |socket| upstream_session(socket, f))
    }

    async fn upstream_session(socket: WebSocket, f: Arc<Fixture>) {
        let (tx, mut rx) = socket.split();
        let tx = Arc::new(Mutex::new(tx));
        // Attach hello, then the private attached reply.
        match rx.next().await {
            Some(Ok(Message::Text(hello))) => {
                let hello: Value = serde_json::from_str(&hello).unwrap();
                assert_eq!(hello["type"], "attach");
                assert_eq!(hello["version"], 1);
                if f.idx == 1 {
                    assert_eq!(hello["secret"], SECRET, "second path must carry the credential");
                }
                let sid = if f.opts.bad_second && f.idx == 1 { "wrong" } else { "fixture-owner" };
                let reply = json!({
                    "type": "attached", "version": 1, "session_id": sid,
                    "secret": SECRET, "path_id": format!("path-{}", f.idx),
                });
                if tx.lock().await.send(Message::Text(reply.to_string().into())).await.is_err() {
                    return;
                }
            }
            _ => return,
        }
        // 40 Hz logical publisher, copied per path by the hub normally; the
        // fixture issues matching ordinals on both paths.
        let pub_tx = tx.clone();
        let pub_f = f.clone();
        let publisher = tokio::spawn(async move {
            let f = pub_f;
            let mut count = 0i64;
            let mut tick = interval(Duration::from_millis(25));
            loop {
                tick.tick().await;
                count += 1;
                if count >= 3 && f.idx == 0 && f.opts.malformed_first {
                    let bad = json!({"session_id": "fixture-owner", "status_seq": true});
                    let _ = pub_tx.lock().await.send(Message::Text(bad.to_string().into())).await;
                    return;
                }
                if count >= 3 && f.idx == 0 && f.opts.binary_first {
                    let _ = pub_tx.lock().await.send(Message::Binary(b"invalid-control".to_vec().into())).await;
                    return;
                }
                let packet = json!({
                    "type": "control_status", "session_id": "fixture-owner",
                    "status_seq": count, "permit": "permit", "permit_ms": 250,
                    "rover": true, "simulated": true, "requires_release": false,
                    "seq": -1, "reason": "stopped",
                });
                let sent = pub_tx.lock().await.send(Message::Text(packet.to_string().into())).await;
                if sent.is_err() {
                    return;
                }
                if f.opts.old_copies {
                    let mut copy = packet.clone();
                    copy["status_seq"] = json!((count - 1).max(0));
                    copy["requires_release"] = json!(true);
                    let _ = pub_tx.lock().await.send(Message::Text(copy.to_string().into())).await;
                }
            }
        });
        while let Some(Ok(message)) = rx.next().await {
            if let Message::Text(t) = message {
                f.commands.lock().unwrap().push(t.to_string());
                f.times.lock().unwrap().push(Instant::now());
            }
        }
        publisher.abort();
    }

    async fn up_camera(State(f): State<Arc<Fixture>>) -> Response {
        f.images.fetch_add(1, Ordering::Relaxed);
        if f.opts.blocked_camera {
            f.gates.camera.wait().await;
        }
        let headers = [
            ("content-type", "image/jpeg"),
            ("x-frame-id", "7"),
            ("x-frame-age-ms", "10"),
            ("x-frame-source", "SIMULATED"),
        ];
        if f.idx == 0 && f.opts.partial_first {
            // A response that stalls mid-body: the complete route wins.
            let stream = futures_util::stream::unfold(0u8, |i| async move {
                match i {
                    0 => Some((Ok::<_, std::io::Error>(Bytes::from_static(b"\xff\xd8partial")), 1)),
                    1 => {
                        tokio::time::sleep(Duration::from_millis(300)).await;
                        Some((Ok(Bytes::from_static(b"\xff\xd9")), 2))
                    }
                    _ => None,
                }
            });
            return (headers, axum::body::Body::from_stream(stream)).into_response();
        }
        if f.opts.oversized_first {
            if f.idx == 0 {
                let mut body = b"\xff\xd8".to_vec();
                body.extend(std::iter::repeat(b'x').take(MAX_GET_BODY));
                body.extend(b"\xff\xd9");
                return (headers, body).into_response();
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        (headers, b"\xff\xd8complete\xff\xd9".to_vec()).into_response()
    }

    async fn up_health(State(f): State<Arc<Fixture>>) -> Response {
        if f.opts.blocked_health {
            f.gates.health.wait().await;
        }
        Json(json!({"camera": true})).into_response()
    }

    /// Real relay: bonded AppState behind the production router.
    async fn bonded_harness(routes: [Url; 2]) -> (Arc<BondedRelay>, u16) {
        let tls = empty_tls();
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap();
        let bond = Arc::new(
            BondedRelay::new(routes, client.clone(), tls.clone()).unwrap(),
        );
        let state = Arc::new(AppState {
            origin: bond.origins[0].clone(),
            geometry: None,
            dist: PathBuf::from("/nonexistent-dist"),
            http: client.clone(),
            control: client.clone(),
            video: client,
            video_stats: Arc::new(std::sync::Mutex::new(crate::VideoStats::default())),
            tls,
            bonded: Some(bond.clone()),
            send_timeout: Duration::from_millis(300),
            ping_interval: Duration::from_secs(10),
            stale_after: Duration::from_secs(20),
            closures: Arc::new(crate::closures::ControlClosures::new()),
            demo_mode: false,
        });
        let listener = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0))).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            axum::serve(listener, build_router(state)).await.ok();
        });
        (bond, port)
    }

    type QuestWs = SplitStream<
        tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    >;
    type QuestTx = SplitSink<
        tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
        TMsg,
    >;

    async fn quest(port: u16) -> (QuestTx, QuestWs) {
        let url = format!("ws://127.0.0.1:{port}/ws/control");
        let (ws, _) = tokio_tungstenite::connect_async(url).await.unwrap();
        ws.split()
    }

    async fn recv_json(rx: &mut QuestWs, secs: u64) -> Value {
        let deadline = std::time::Instant::now() + Duration::from_secs(secs);
        loop {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            match timeout(left, rx.next()).await {
                Ok(Some(Ok(TMsg::Text(t)))) => return serde_json::from_str(&t).unwrap(),
                Ok(Some(Ok(TMsg::Ping(_)))) | Ok(Some(Ok(TMsg::Pong(_)))) => continue,
                other => panic!("expected status, got {other:?}"),
            }
        }
    }

    async fn until<F, Fut, P>(mut call: F, pred: P, secs: u64) -> Fut::Output
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future,
        P: Fn(&Fut::Output) -> bool,
    {
        let end = std::time::Instant::now() + Duration::from_secs(secs);
        loop {
            let v = call().await;
            if pred(&v) {
                return v;
            }
            if std::time::Instant::now() > end {
                panic!("condition deadline");
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    struct Pair {
        bond: Arc<BondedRelay>,
        port: u16,
        upstreams: [Arc<Fixture>; 2],
        routes: [Url; 2],
        gates: Arc<Gates>,
    }

    async fn pair(opts: UpOpts, attach_control: bool) -> (Pair, Option<(QuestTx, QuestWs)>) {
        let gates = Arc::new(Gates {
            health: Gate { open: std::sync::atomic::AtomicBool::new(false), notify: Notify::new() },
            camera: Gate { open: std::sync::atomic::AtomicBool::new(false), notify: Notify::new() },
        });
        let (u0, f0) = spawn_upstream(0, opts.clone(), gates.clone()).await;
        let (u1, f1) = spawn_upstream(1, opts.clone(), gates.clone()).await;
        let (bond, port) = bonded_harness([u0.clone(), u1.clone()]).await;
        let pair = Pair {
            routes: [u0, u1],
            upstreams: [f0, f1],
            bond,
            port,
            gates,
        };
        let ws = if attach_control { Some(quest(port).await) } else { None };
        (pair, ws)
    }

    fn send_cmd(seq: i64) -> Value {
        json!({"session_id": "fixture-owner", "seq": seq, "deadman": false})
    }

    #[tokio::test]
    async fn dedup_ignores_old_stop_copy_and_identical_fanout_without_replay() {
        let (pair, ws) = pair(UpOpts { old_copies: true, ..Default::default() }, true).await;
        let bond = pair.bond.clone();
        let (mut tx, mut rx) = ws.unwrap();
        let mut seqs = Vec::new();
        for _ in 0..5 {
            let status = recv_json(&mut rx, 3).await;
            assert_eq!(status["requires_release"], false);
            seqs.push(status["status_seq"].as_i64().unwrap());
        }
        assert_eq!(seqs, {
            let mut s = seqs.clone();
            s.sort();
            s.dedup();
            s
        });
        tx.send(TMsg::Text(send_cmd(0).to_string().into())).await.unwrap();
        until(
            || async {
                (
                    pair.upstreams[0].commands.lock().unwrap().len(),
                    pair.upstreams[1].commands.lock().unwrap().len(),
                )
            },
            |(a, b)| *a == 1 && *b == 1,
            3,
        )
        .await;
        assert_eq!(
            *pair.upstreams[0].commands.lock().unwrap(),
            *pair.upstreams[1].commands.lock().unwrap()
        );
        tx.send(TMsg::Binary(b"malformed".to_vec().into())).await.unwrap();
        until(
            || async { bond.status().await["controller_active"] == false },
            |v| *v,
            3,
        )
        .await;
        assert_eq!(pair.upstreams[0].commands.lock().unwrap().len(), 1);
        assert_eq!(pair.upstreams[1].commands.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn wrong_backend_excluded_and_secret_never_reaches_quest() {
        let (pair, ws) = pair(UpOpts { bad_second: true, ..Default::default() }, true).await;
        let (mut _tx, mut rx) = ws.unwrap();
        let status = recv_json(&mut rx, 3).await;
        assert!(status.get("secret").is_none());
        assert!(status.get("path_id").is_none());
        until(
            || async { pair.bond.status().await["attached_paths"].as_u64().unwrap() },
            |n| *n == 1,
            3,
        )
        .await;
        let reply = pair.bond.race_get("/camera.jpg").await.unwrap();
        assert_eq!(reply.origin, pair.routes[0]);
        assert_eq!(pair.upstreams[0].images.load(Ordering::Relaxed), 1);
        assert_eq!(pair.upstreams[1].images.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn complete_jpeg_wins_never_partial_spliced_and_headers_survive() {
        let (pair, _ws) = pair(UpOpts { partial_first: true, ..Default::default() }, true).await;
        let reply = pair.bond.race_get("/camera.jpg").await.unwrap();
        assert_eq!(&reply.body[..], b"\xff\xd8complete\xff\xd9");
        assert_eq!(reply.origin, pair.routes[1]);
        assert_eq!(
            reply.headers.get("x-frame-age-ms").unwrap().to_str().unwrap(),
            "10"
        );
    }

    #[tokio::test]
    async fn binary_status_fault_loses_only_one_path_and_stale_view_becomes_stop() {
        let (pair, ws) = pair(UpOpts { binary_first: true, ..Default::default() }, true).await;
        let bond = pair.bond.clone();
        let (mut tx, mut rx) = ws.unwrap();
        until(
            || async { bond.status().await["attached_paths"].as_u64().unwrap() },
            |n| *n == 1,
            3,
        )
        .await;
        let status = recv_json(&mut rx, 3).await;
        assert_eq!(status["session_id"], "fixture-owner");
        tx.send(TMsg::Text(
            json!({"session_id":"fixture-owner","seq":0,"deadman":true,"v":0.5,"w":0,"video_fresh":true,"video_age_ms":501})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
        until(
            || async { pair.upstreams[1].commands.lock().unwrap().len() },
            |n| *n == 1,
            3,
        )
        .await;
        let got: Value =
            serde_json::from_str(&pair.upstreams[1].commands.lock().unwrap()[0]).unwrap();
        assert_eq!(got["deadman"], false);
        assert_eq!(got["v"], 0);
    }

    #[tokio::test]
    async fn malformed_status_is_a_path_fault_and_second_path_continues() {
        let (pair, ws) = pair(UpOpts { malformed_first: true, ..Default::default() }, true).await;
        let bond = pair.bond.clone();
        let (mut _tx, mut rx) = ws.unwrap();
        until(
            || async { bond.status().await["attached_paths"].as_u64().unwrap() },
            |n| *n == 1,
            3,
        )
        .await;
        let status = recv_json(&mut rx, 3).await;
        assert!(status["status_seq"].is_i64());
    }

    #[tokio::test]
    async fn oversized_complete_jpeg_is_rejected_before_winning() {
        let (pair, _ws) = pair(UpOpts { oversized_first: true, ..Default::default() }, true).await;
        let reply = pair.bond.race_get("/camera.jpg").await.unwrap();
        assert_eq!(&reply.body[..], b"\xff\xd8complete\xff\xd9");
        assert_eq!(reply.origin, pair.routes[1]);
        assert_eq!(pair.bond.debug_counts().await.0, 0, "loser tasks drained before return");
    }

    #[tokio::test]
    async fn precontroller_camera_is_503_without_fetching_unvalidated_backend() {
        let (pair, _ws) = pair(Default::default(), false).await;
        let client = reqwest::Client::new();
        let response = client
            .get(format!("http://127.0.0.1:{}/camera.jpg", pair.port))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(pair.upstreams[0].images.load(Ordering::Relaxed), 0);
        assert_eq!(pair.upstreams[1].images.load(Ordering::Relaxed), 0);
        let health = pair.bond.race_get("/health").await.unwrap();
        let body: Value = serde_json::from_slice(&health.body).unwrap();
        assert_eq!(body["camera"], true, "precontroller health stays read-only");
        assert_eq!(pair.bond.debug_counts().await.0, 0);
    }

    #[tokio::test]
    async fn three_stops_in_one_burst_do_not_false_fault_healthy_writers() {
        let (pair, ws) = pair(Default::default(), true).await;
        let (mut tx, _rx) = ws.unwrap();
        for seq in 0..3 {
            tx.send(TMsg::Text(send_cmd(seq).to_string().into())).await.unwrap();
        }
        until(
            || async {
                (
                    pair.upstreams[0].commands.lock().unwrap().len(),
                    pair.upstreams[1].commands.lock().unwrap().len(),
                )
            },
            |(a, b)| *a == 3 && *b == 3,
            3,
        )
        .await;
        assert_eq!(pair.bond.status().await["attached_paths"], 2);
        let a = pair.upstreams[0].commands.lock().unwrap().clone();
        let b = pair.upstreams[1].commands.lock().unwrap().clone();
        assert_eq!(a, b);
        let seqs: Vec<i64> = a
            .iter()
            .map(|c| serde_json::from_str::<Value>(c).unwrap()["seq"].as_i64().unwrap())
            .collect();
        assert_eq!(seqs, vec![0, 1, 2]);
    }

    #[tokio::test]
    async fn blocked_writer_is_retired_without_delaying_survivor_or_replay() {
        let (pair, ws) = pair(Default::default(), true).await;
        let bond = pair.bond.clone();
        let (mut tx, _rx) = ws.unwrap();
        // Stall every writer bound for the first route (Python: patched
        // slow.socket.send_str to wait on an event before sending).
        let gate = Arc::new(Notify::new());
        bond.inner
            .lock()
            .await
            .stall_sends
            .insert(pair.routes[0].clone(), gate.clone());
        for seq in 0..8 {
            tx.send(TMsg::Text(send_cmd(seq).to_string().into())).await.unwrap();
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        until(
            || async { pair.upstreams[1].commands.lock().unwrap().len() },
            |n| *n == 8,
            5,
        )
        .await;
        until(
            || async { bond.status().await["attached_paths"].as_u64().unwrap() },
            |n| *n == 1,
            5,
        )
        .await;
        // The survivor kept the cadence with no replay or deadline imposition.
        let seqs: Vec<i64> = pair.upstreams[1]
            .commands
            .lock()
            .unwrap()
            .iter()
            .map(|c| serde_json::from_str::<Value>(c).unwrap()["seq"].as_i64().unwrap())
            .collect();
        assert_eq!(seqs, (0..8).collect::<Vec<i64>>());
        let times = pair.upstreams[1].times.lock().unwrap().clone();
        let max_gap = times
            .windows(2)
            .map(|w| (w[1] - w[0]).as_secs_f64())
            .fold(0.0_f64, f64::max);
        assert!(max_gap < 0.15, "blocked path imposed its send deadline on the survivor: {max_gap}");
        // Releasing the gate must not replay the retired command: the pending
        // writer was cancelled, never queued.
        gate.notify_waiters();
        tokio::time::sleep(Duration::from_millis(50)).await;
        let after: Vec<i64> = pair.upstreams[0]
            .commands
            .lock()
            .unwrap()
            .iter()
            .filter_map(|c| serde_json::from_str::<Value>(c).ok()?["seq"].as_i64())
            .collect();
        assert!(after.is_empty(), "retired path replayed commands: {after:?}");
    }

    #[tokio::test]
    async fn quest_status_delivery_failure_ends_all_paths() {
        let (pair, ws) = pair(Default::default(), true).await;
        let bond = pair.bond.clone();
        let (_tx, mut rx) = ws.unwrap();
        // Every downstream send now fails (Python: broker.local.send_str = fail).
        bond.inner.lock().await.fail_local = true;
        until(
            || async { bond.status().await["attached_paths"].as_u64().unwrap() },
            |n| *n == 0,
            8,
        )
        .await;
        assert_eq!(bond.status().await["disconnected"], true);
        // The Quest socket is closed by the relay.
        let mut closed = false;
        for _ in 0..200 {
            match timeout(Duration::from_secs(2), rx.next()).await {
                Ok(Some(Ok(TMsg::Close(_)))) | Ok(None) | Ok(Some(Err(_))) => {
                    closed = true;
                    break;
                }
                Ok(Some(Ok(_))) => continue, // queued statuses, pings, pongs
                other => panic!("unexpected {other:?}"),
            }
        }
        assert!(closed, "Quest remained open after downstream delivery failure");
    }

    #[tokio::test]
    async fn concurrent_blocked_gets_are_capped_without_queue_and_close_drains() {
        let (pair, _ws) = pair(UpOpts { blocked_health: true, ..Default::default() }, true).await;
        let bond = pair.bond.clone();
        let mut races = Vec::new();
        for _ in 0..4 {
            let b = bond.clone();
            races.push(tokio::spawn(async move { b.race_get("/health").await }));
        }
        until(
            || async { bond.debug_counts().await.0 },
            |n| *n == 8,
            3,
        )
        .await;
        let fifth = bond.race_get("/health").await;
        assert!(fifth.is_err());
        assert_eq!(bond.status().await["active_http_races"], 4);
        assert_eq!(bond.debug_counts().await.0, 8);
        bond.close().await;
        for race in races {
            let _ = race.await;
        }
        assert_eq!(bond.debug_counts().await.0, 0);
        assert_eq!(bond.status().await["active_http_races"], 0);
    }

    #[tokio::test]
    async fn inflight_camera_cannot_cross_to_a_new_controller_even_if_session_repeats() {
        let (pair, ws) = pair(UpOpts { blocked_camera: true, ..Default::default() }, true).await;
        let bond = pair.bond.clone();
        let (tx, mut rx) = ws.unwrap();
        recv_json(&mut rx, 3).await;
        let old_bond = bond.clone();
        let old_image = tokio::spawn(async move { old_bond.race_get("/camera.jpg").await });
        until(
            || async {
                pair.upstreams[0].images.load(Ordering::Relaxed)
                    + pair.upstreams[1].images.load(Ordering::Relaxed)
            },
            |n| *n == 2,
            3,
        )
        .await;
        drop(tx);
        drop(rx);
        until(
            || async { bond.status().await["controller_active"] == false },
            |v| *v,
            3,
        )
        .await;
        let (_tx2, mut rx2) = quest(pair.port).await;
        recv_json(&mut rx2, 3).await;
        // The fixture repeats the session_id; the owner token still changed.
        assert_eq!(bond.inner.lock().await.session_id.as_deref(), Some("fixture-owner"));
        // Release the blocked upstreams: the stale race must fail, not win.
        // (Both fixtures share the same camera gate.)
        pair.gates.camera.release();
        let old = old_image.await.unwrap();
        assert!(old.is_err(), "in-flight image crossed the owner change");
        assert_eq!(bond.debug_counts().await.0, 0);
        let current = bond.race_get("/camera.jpg").await.unwrap();
        assert_eq!(&current.body[..], b"\xff\xd8complete\xff\xd9");
    }

    #[tokio::test]
    async fn dispatch_guards_and_health_transport() {
        let (pair, _ws) = pair(Default::default(), false).await;
        let client = reqwest::Client::new();
        let base = format!("http://127.0.0.1:{}", pair.port);
        // Private upstream endpoint is never proxied.
        let r = client.get(format!("{base}/ws/control/bonded")).send().await.unwrap();
        assert_eq!(r.status(), StatusCode::FORBIDDEN);
        // Video streams rejected under the JPEG-only bonded profile.
        for path in ["/ws/video", "/stream.mjpg", "/video/h264.mp4"] {
            let r = client.get(format!("{base}{path}")).send().await.unwrap();
            assert_eq!(r.status(), StatusCode::NOT_IMPLEMENTED, "{path}");
        }
        // /relay/health carries the transport block.
        let r = client.get(format!("{base}/relay/health")).send().await.unwrap();
        let body: Value = serde_json::from_slice(&r.bytes().await.unwrap()).unwrap();
        assert_eq!(body["transport"]["profile"], "bonded-jpeg");
        assert_eq!(body["transport"]["automatic_reconnect"], false);
        // GET /health races upstreams and gains relay_transport.
        let r = client.get(format!("{base}/health")).send().await.unwrap();
        assert_eq!(r.status(), StatusCode::OK);
        let body: Value = serde_json::from_slice(&r.bytes().await.unwrap()).unwrap();
        assert_eq!(body["camera"], true);
        assert_eq!(body["relay_transport"]["profile"], "bonded-jpeg");
    }
}
