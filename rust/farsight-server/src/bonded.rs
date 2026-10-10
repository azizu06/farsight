//! Opt-in bonded upstream control — port of jetson/control/bonded.py.
//!
//! One logical controller shared by at most two already-attached upstream
//! paths. The first path claims the owner via `ControlState::begin`; a second
//! path joins it with the private 256-bit credential. The receiver keeps a
//! bounded canonical-payload history so an identical or obsolete copy that
//! crosses the link twice never re-enters the control state or renews a
//! lease, while a conflicting sequence fails closed. One logical 40 Hz
//! publisher issues each status once and copies it to every attached path.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use indexmap::IndexMap;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

use crate::control::ControlState;
use crate::state::now_monotonic;

pub const STATUS_INTERVAL: f64 = 0.025;
pub const RECORD_INTERVAL: f64 = 0.050;
pub const CACHE_LIMIT: usize = 256;
pub const MAX_PATHS: usize = 2;

enum SlotItem {
    Status(Value),
    End,
}

/// Single-slot status queue (asyncio.Queue(maxsize=1) parity): an offer to
/// an occupied slot fails and the publisher detaches that congested path.
/// Termination discards any pending status and installs the sentinel so a
/// waiting receiver exits instead of hiding stale consumption.
struct StatusSlot {
    cell: Mutex<Option<SlotItem>>,
    notify: tokio::sync::Notify,
}

impl StatusSlot {
    fn new() -> Self {
        Self { cell: Mutex::new(None), notify: tokio::sync::Notify::new() }
    }

    fn offer(&self, status: Value) -> bool {
        {
            let mut cell = self.cell.lock().unwrap();
            if cell.is_some() {
                return false;
            }
            *cell = Some(SlotItem::Status(status));
        }
        self.notify.notify_one();
        true
    }

    fn terminate(&self) {
        *self.cell.lock().unwrap() = Some(SlotItem::End);
        self.notify.notify_one();
    }

    async fn take(&self) -> Option<Value> {
        loop {
            if let Some(item) = self.cell.lock().unwrap().take() {
                return match item {
                    SlotItem::Status(status) => Some(status),
                    SlotItem::End => None,
                };
            }
            self.notify.notified().await;
        }
    }
}

struct PathState {
    slot: Arc<StatusSlot>,
}

/// One attached upstream path (jetson/control/bonded.py Attachment).
pub struct BondedAttachment {
    pub path_id: String,
    pub session_id: String,
    pub secret: String,
    slot: Arc<StatusSlot>,
}

impl BondedAttachment {
    /// Next queued status; None is the detach sentinel (queue.get() -> None).
    pub async fn next_status(&self) -> Option<Value> {
        self.slot.take().await
    }
}

struct HubInner {
    session_id: Option<String>,
    secret: Option<String>,
    paths: HashMap<String, PathState>,
    /// Canonical-payload digests keyed by command seq, FIFO-bounded at
    /// CACHE_LIMIT (OrderedDict history parity).
    cache: IndexMap<i64, [u8; 32]>,
    /// Ordinal stamped on every published status; never resets, so a relay
    /// can reject older copies even across owners.
    status_seq: i64,
    recorded_at: f64,
    publisher: Option<tokio::task::JoinHandle<()>>,
    recorder: Option<tokio::task::JoinHandle<()>>,
    /// #110 nav revision lane for the whole bonded session — one lane
    /// decision per status_seq keeps both paths byte-identical.
    lane: Option<crate::nav::NavLane>,
    closed: bool,
}

fn token_hex(bytes: usize) -> String {
    let mut raw = vec![0u8; bytes];
    rand::fill(raw.as_mut_slice());
    raw.iter().map(|b| format!("{b:02x}")).collect()
}

/// Constant-time compare (hmac.compare_digest parity): attachment secrets
/// and known payload digests must not leak through timing.
fn const_eq(a: &[u8], b: &[u8]) -> bool {
    a.ct_eq(b).into()
}

/// Canonical command payload for dedup: recursively sorted object keys with
/// compact separators, matching json.dumps(sort_keys=True,
/// separators=(",",":")) over the parsed message. serde_json only holds
/// finite numbers, matching allow_nan=False.
fn canonical(value: &Value) -> String {
    fn write(v: &Value, out: &mut String) {
        match v {
            Value::Object(map) => {
                out.push('{');
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for (i, key) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::to_string(key).unwrap());
                    out.push(':');
                    write(&map[*key], out);
                }
                out.push('}');
            }
            Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write(item, out);
                }
                out.push(']');
            }
            _ => out.push_str(&v.to_string()),
        }
    }
    let mut out = String::new();
    write(value, &mut out);
    out
}

pub struct BondedControlHub {
    state: Arc<ControlState>,
    on_status: Option<Arc<dyn Fn(&Value) + Send + Sync>>,
    inner: Mutex<HubInner>,
}

impl BondedControlHub {
    pub fn new(state: Arc<ControlState>, on_status: Option<Arc<dyn Fn(&Value) + Send + Sync>>) -> Self {
        Self {
            state,
            on_status,
            inner: Mutex::new(HubInner {
                session_id: None,
                secret: None,
                paths: HashMap::new(),
                cache: IndexMap::new(),
                status_seq: 0,
                recorded_at: f64::NEG_INFINITY,
                publisher: None,
                recorder: None,
                lane: None,
                closed: false,
            }),
        }
    }

    pub fn control(&self) -> &Arc<ControlState> {
        &self.state
    }

    /// Attach one upstream path. Without a live owner the first path claims
    /// it (a supplied secret is rejected as retired); a later path must
    /// present the current credential. Rejections never revoke the owner.
    pub fn attach(self: &Arc<Self>, secret: Option<&str>) -> Result<BondedAttachment, String> {
        let mut inner = self.inner.lock().unwrap();
        if inner.closed {
            return Err("Bonded controller is closed".into());
        }
        if inner.session_id.is_none() {
            if secret.is_some() {
                return Err("Attachment session is no longer active".into());
            }
            let session = self.state.begin().ok_or("Another controller owns the rover")?;
            inner.session_id = Some(session);
            inner.secret = Some(token_hex(32));
            inner.cache.clear();
            inner.recorded_at = f64::NEG_INFINITY;
        } else {
            let expected = inner.secret.clone().unwrap_or_default();
            let valid = secret.map(|s| const_eq(s.as_bytes(), expected.as_bytes())).unwrap_or(false);
            if !valid {
                return Err("Invalid attachment secret".into());
            }
        }
        if inner.paths.len() >= MAX_PATHS {
            return Err("Two bonded paths are already attached".into());
        }
        let path_id = token_hex(16);
        let slot = Arc::new(StatusSlot::new());
        inner.paths.insert(path_id.clone(), PathState { slot: slot.clone() });
        // A newly attached path gets the nav arrays in its first status
        // (bonded.py #110): refresh the lane so revision-matching doesn't
        // starve the newcomer.
        if inner.lane.is_some() {
            inner.lane = Some(crate::nav::NavLane::new());
        }
        if inner.publisher.is_none() {
            let me = Arc::clone(self);
            inner.publisher = Some(tokio::spawn(async move { me.publish_loop().await }));
        }
        Ok(BondedAttachment {
            path_id,
            session_id: inner.session_id.clone().unwrap(),
            secret: inner.secret.clone().unwrap(),
            slot,
        })
    }

    /// Drop one path. Losing an attachment preserves the owner; losing the
    /// last ends it and retires the credential (detach() parity).
    pub fn detach(&self, path_id: &str) {
        let mut inner = self.inner.lock().unwrap();
        let Some(path) = inner.paths.remove(path_id) else { return };
        path.slot.terminate();
        if !inner.paths.is_empty() {
            return;
        }
        let session_id = inner.session_id.take();
        inner.secret = None;
        inner.cache.clear();
        if let Some(publisher) = inner.publisher.take() {
            publisher.abort();
        }
        if let Some(recorder) = inner.recorder.take() {
            recorder.abort();
        }
        drop(inner);
        if let Some(session_id) = session_id {
            self.state.end(&session_id);
        }
    }

    fn detach_all(&self) {
        let ids: Vec<String> = self.inner.lock().unwrap().paths.keys().cloned().collect();
        for id in ids {
            self.detach(&id);
        }
    }

    /// One inbound command copy from an attached path (bonded.py accept).
    /// Identical and obsolete copies return without touching control state;
    /// a conflicting known seq stops with conflicting_duplicate.
    pub fn accept(&self, message: &Value, path_id: &str) -> bool {
        let mut inner = self.inner.lock().unwrap();
        if !inner.paths.contains_key(path_id) || inner.session_id.is_none() {
            return false; // unauthenticated/detached paths cannot affect an owner
        }
        let session_id = inner.session_id.clone().unwrap();
        self.state.output(); // duplicates never postpone an existing safety expiry
        if !message.is_object() {
            // Parsed JSON carries no binary type; the ws layer rejects binary.
            self.state.stop("invalid_json", true);
            return false;
        }
        if message.get("session_id").and_then(Value::as_str) != Some(session_id.as_str()) {
            self.state.stop("wrong_session", true);
            return false;
        }
        if inner.lane.is_none()
            && message.get("nav_lane").and_then(Value::as_str) == Some("revision")
        {
            inner.lane = Some(crate::nav::NavLane::new());
        }
        let seq = match message.get("seq").and_then(|v| v.as_i64()) {
            Some(seq) if (0..=(1i64 << 53) - 1).contains(&seq) => seq,
            _ => {
                self.state.stop("invalid_sequence", true);
                return false;
            }
        };
        let encoded = canonical(message);
        if encoded.len() > 2048 {
            self.state.stop("oversized_command", true);
            return false;
        }
        let digest: [u8; 32] = Sha256::digest(encoded.as_bytes()).into();
        if let Some(previous) = inner.cache.get(&seq) {
            if !const_eq(previous, &digest) {
                self.state.stop("conflicting_duplicate", true);
            }
            return false;
        }
        // bonded.py `with state.lock:` — the obsolete check, history insert
        // and state.accept must not interleave with another path's accept.
        let mut s = self.state.inner.lock().unwrap();
        if seq <= s.seq {
            return false; // obsolete copy, including an already-applied release
        }
        inner.cache.insert(seq, digest);
        while inner.cache.len() > CACHE_LIMIT {
            inner.cache.shift_remove_index(0);
        }
        if message.get("deadman") == Some(&Value::Bool(true)) {
            let permit = message.get("permit").and_then(Value::as_str);
            match permit {
                Some(p) if !p.is_empty() => {}
                _ => {
                    drop(s);
                    self.state.stop("expired_permit", true);
                    return false;
                }
            }
            let expiry = s
                .permits
                .iter()
                .find(|(k, _)| k.as_str() == permit.unwrap())
                .map(|(_, e)| *e)
                .unwrap_or(0.0);
            if expiry <= (self.state.clock)() {
                return false; // a delayed copy supplies no new lease or motion
            }
        }
        self.state.accept_locked(&mut s, message, &session_id)
    }

    /// One logical publisher tick: a single status() issues the permit once,
    /// then the same packet fans out to every attached path.
    fn tick(&self, started: f64) -> bool {
        let mut status = self.state.status(true);
        let mut inner = self.inner.lock().unwrap();
        if inner.paths.is_empty() {
            return false;
        }
        if status.get("session_id").and_then(Value::as_str) != inner.session_id.as_deref() {
            let ids: Vec<String> = inner.paths.keys().cloned().collect();
            drop(inner);
            for id in ids {
                self.detach(&id);
            }
            return false;
        }
        inner.status_seq += 1;
        status["status_seq"] = json!(inner.status_seq);
        // Attachment credentials are never status or recorded data.
        if let Some(obj) = status.as_object_mut() {
            obj.remove("secret");
            obj.remove("attach_secret");
        }
        // One lane decision per status_seq: both paths carry identical
        // copies (bonded.py #110).
        let sent = inner
            .lane
            .as_mut()
            .map(|l| l.apply(&status, started))
            .unwrap_or_else(|| status.clone());
        let mut congested = Vec::new();
        for (path_id, path) in inner.paths.iter() {
            if !path.slot.offer(sent.clone()) {
                congested.push(path_id.clone());
            }
        }
        // Evidence recording stays once per logical stream at 20 Hz and must
        // never change motion authority; a faulting hook cannot stop motion.
        // Python gates on self.paths after the congested detaches above.
        if inner.paths.len() > congested.len()
            && self.on_status.is_some()
            && started - inner.recorded_at >= RECORD_INTERVAL
            && inner.recorder.as_ref().map(|r| r.is_finished()).unwrap_or(true)
        {
            inner.recorded_at = started;
            if let Some(hook) = self.on_status.clone() {
                let packet = status.clone();
                inner.recorder = Some(tokio::task::spawn_blocking(move || {
                    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| hook(&packet)));
                }));
            }
        }
        drop(inner);
        for path_id in congested {
            self.detach(&path_id);
        }
        !self.inner.lock().unwrap().paths.is_empty()
    }

    async fn publish_loop(self: &Arc<Self>) {
        loop {
            if self.inner.lock().unwrap().paths.is_empty() {
                return;
            }
            let started = now_monotonic();
            // A publisher fault cannot leave a controller alive without
            // status; tear down every path like _publish's generic except.
            let keep_going = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.tick(started)))
                .unwrap_or_else(|_| {
                    self.detach_all();
                    false
                });
            if !keep_going {
                return;
            }
            let wait = STATUS_INTERVAL - (now_monotonic() - started);
            if wait > 0.0 {
                tokio::time::sleep(Duration::from_secs_f64(wait)).await;
            }
        }
    }

    /// Shutdown hook (hub.close() parity): closed hubs reject new paths and
    /// every attached path is terminated.
    pub fn close(&self) {
        self.inner.lock().unwrap().closed = true;
        self.detach_all();
    }

    #[cfg(test)]
    pub(crate) fn path_count(&self) -> usize {
        self.inner.lock().unwrap().paths.len()
    }
}
