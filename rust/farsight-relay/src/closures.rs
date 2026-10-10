//! Bounded, payload-free diagnostics for the relay's single-route control WS.
//! Port of tools/control_closures.py (eab0c88): the bonded control route is
//! excluded — only plain /ws/control proxy sessions are counted.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::Instant;

use serde_json::{json, Value};

struct ClosuresInner {
    attempts: u64,
    opened: u64,
    active: u64,
    ended: u64,
    connect_failures: u64,
    prepare_failures: u64,
    history: VecDeque<Value>,
}

pub struct ControlClosures {
    inner: Mutex<ClosuresInner>,
}

pub struct Attempt {
    owner: std::sync::Arc<ControlClosures>,
    started: Instant,
    state: Mutex<AttemptState>,
}

struct AttemptState {
    is_open: bool,
    finished: bool,
}

fn valid_side(side: &str) -> &str {
    if side == "quest" { "quest" } else { "upstream" }
}

fn valid_operation(op: &str) -> &str {
    match op {
        "receive" | "send" | "connect" | "prepare" => op,
        _ => "receive",
    }
}

fn valid_reason(reason: &str) -> &str {
    match reason {
        "normal" | "error" | "send_timeout" | "invalid_control" | "prepare_failure"
        | "connect_failure" => reason,
        _ => "error",
    }
}

fn code(value: Option<u16>) -> Value {
    match value {
        Some(c) => json!(c as i64),
        None => Value::Null,
    }
}

impl ControlClosures {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(ClosuresInner {
                attempts: 0,
                opened: 0,
                active: 0,
                ended: 0,
                connect_failures: 0,
                prepare_failures: 0,
                history: VecDeque::new(),
            }),
        }
    }

    pub fn begin(self: &std::sync::Arc<Self>) -> std::sync::Arc<Attempt> {
        self.inner.lock().unwrap().attempts += 1;
        std::sync::Arc::new(Attempt {
            owner: self.clone(),
            started: Instant::now(),
            state: Mutex::new(AttemptState { is_open: false, finished: false }),
        })
    }

    pub fn status(&self, enabled: bool) -> Value {
        let i = self.inner.lock().unwrap();
        json!({
            "scope": "single-route-control",
            "enabled": enabled,
            "excluded": if enabled { Value::Null } else { json!("bonded-control") },
            "attempts": i.attempts,
            "opened": i.opened,
            "active": i.active,
            "ended": i.ended,
            "connect_failures": i.connect_failures,
            "prepare_failures": i.prepare_failures,
            "history": i.history.iter().cloned().collect::<Vec<_>>(),
        })
    }
}

impl Attempt {
    pub fn opened(&self) {
        let mut st = self.state.lock().unwrap();
        if !st.is_open && !st.finished {
            st.is_open = true;
            let mut i = self.owner.inner.lock().unwrap();
            i.opened += 1;
            i.active += 1;
        }
    }

    /// First completion wins, including simultaneously finished pipe tasks.
    pub fn end(
        &self,
        side: &str,
        reason: &str,
        local_code: Option<u16>,
        upstream_code: Option<u16>,
        exception_class: Option<&str>,
        operation: &str,
    ) {
        let mut st = self.state.lock().unwrap();
        if st.finished {
            return;
        }
        st.finished = true;
        let is_open = st.is_open;
        drop(st);

        let reason = valid_reason(reason);
        let mut i = self.owner.inner.lock().unwrap();
        if is_open {
            i.active -= 1;
            i.ended += 1;
        }
        if reason == "connect_failure" {
            i.connect_failures += 1;
        } else if reason == "prepare_failure" {
            i.prepare_failures += 1;
        }
        let name = exception_class
            .filter(|n| {
                !n.is_empty()
                    && n.len() <= 80
                    && n.chars().next().unwrap().is_ascii_alphabetic() || *n == "_"
            })
            .map(|n| {
                if n.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
                    n.to_string()
                } else {
                    "Exception".to_string()
                }
            });
        let timestamp = {
            let now = chrono::Utc::now();
            now.format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
        };
        i.history.push_back(json!({
            "timestamp_utc": timestamp,
            "duration_ms": (self.started.elapsed().as_secs_f64() * 1000.0 * 1000.0).round() / 1000.0,
            "first_ended_side": valid_side(side),
            "operation": valid_operation(operation),
            "reason": reason,
            "local_close_code": code(local_code),
            "upstream_close_code": code(upstream_code),
            "exception_class": name.map(Value::String).unwrap_or(Value::Null),
        }));
        while i.history.len() > 8 {
            i.history.pop_front();
        }
    }
}
