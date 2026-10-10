//! Receiver-issued bounds on native sensor capture age, without clock offsets.
//! Port of jetson/capture_permits.py. The server issues short-lived tokens at
//! 20 Hz on the phone sockets; a frame that echoes a live token with a bound,
//! verified capture clock can roll its receipt back to issuance time.

use std::collections::VecDeque;
use std::sync::Mutex;

use base64::Engine;
use serde_json::{json, Value};

use crate::state::now_monotonic;

pub const TTL: f64 = 0.250;
pub const PERIOD: f64 = 0.050;
const MAX_RETAINED: usize = 8;

fn token() -> String {
    let mut bytes = [0u8; 18];
    rand::fill(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

struct Inner {
    active: bool,
    tokens: VecDeque<(String, f64)>,
}

pub struct CapturePermits {
    session_id: String,
    map_epoch: u64,
    enabled: bool,
    inner: Mutex<Inner>,
}

impl CapturePermits {
    pub fn new(session_id: &str, map_epoch: u64, enabled: bool) -> Self {
        Self {
            session_id: session_id.to_string(),
            map_epoch,
            enabled,
            inner: Mutex::new(Inner { active: true, tokens: VecDeque::new() }),
        }
    }

    pub fn issue(&self) -> Option<Value> {
        let mut inner = self.inner.lock().unwrap();
        if !inner.active || !self.enabled {
            return None;
        }
        let now = now_monotonic();
        inner
            .tokens
            .retain(|(_, issued)| (0.0..TTL).contains(&(now - *issued)));
        let t = token();
        inner.tokens.push_back((t.clone(), now));
        while inner.tokens.len() > MAX_RETAINED {
            inner.tokens.pop_front();
        }
        Some(json!({
            "type": "capture_permit",
            "version": 1,
            "session_id": self.session_id,
            "map_epoch": self.map_epoch,
            "token": t,
            "valid_for_ms": 250,
        }))
    }

    /// Server issuance time only for a current, bound RGB exposure.
    /// Client sensor/receipt times are compared only to each other; neither is
    /// compared to the server clock or used to fabricate a clock offset.
    pub fn verified_origin(&self, header: &Value) -> Option<f64> {
        let capture = header.get("t_capture")?;
        let receipt = header.get("capture_permit_received_t")?;
        let token = header.get("capture_permit")?.as_str()?;
        if header.get("capture_clock").and_then(Value::as_str) != Some("avcapture_sync_to_host")
            || header.get("capture_clock_verified") != Some(&json!(true))
            || header.get("capture_bound") != Some(&json!(true))
            || header.get("session_id").and_then(Value::as_str) != Some(self.session_id.as_str())
            || header.get("map_epoch").and_then(Value::as_u64) != Some(self.map_epoch)
        {
            return None;
        }
        let (capture, receipt) = (capture.as_f64()?, receipt.as_f64()?);
        if !capture.is_finite() || !receipt.is_finite() || !(0.0 <= receipt && receipt < capture) {
            return None;
        }
        let inner = self.inner.lock().unwrap();
        let issued = inner.tokens.iter().find(|(t, _)| t == token).map(|(_, i)| *i)?;
        if !inner.active || !self.enabled {
            return None;
        }
        let age = now_monotonic() - issued;
        (0.0 <= age && age < TTL).then_some(issued)
    }

    pub fn is_active(&self) -> bool {
        self.inner.lock().unwrap().active
    }

    pub fn close(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.active = false;
        inner.tokens.clear();
    }
}
