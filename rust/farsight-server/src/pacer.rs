//! Aggregate egress limiter for bulk (non-control) bytes leaving the jetson.
//!
//! On a shared constrained link (the cellular hop) every bulk byte the server
//! emits can queue ahead of control/permit frames. Pacing the server's own
//! sends is the only point where real headroom is created: bulk traffic is
//! capped at `--bulk-limit-mbps`, leaving the remainder of the link for
//! control traffic, which is never paced. Rate 0 disables pacing.

use std::sync::OnceLock;
use std::time::{Duration, Instant};

use tokio::sync::Mutex;

struct Bucket {
    /// Scheduled finish time of the last committed send. Idle credit is the
    /// amount this timestamp lags behind now, clamped to `burst` — so a send
    /// may start early only after real idle time, and slept time can never be
    /// re-credited as fresh tokens.
    next: Option<Instant>,
}

pub struct BulkPacer {
    rate: f64, // bytes/sec
    bucket: Mutex<Bucket>,
}

impl BulkPacer {
    fn new(rate_mbps: f64) -> Self {
        // NaN/negative/infinite caps mean "no pacing"; a near-zero cap is
        // floored so the per-chunk delay can never overflow an Instant.
        let rate = if rate_mbps.is_finite() { rate_mbps.max(0.0) } else { 0.0 };
        Self { rate: rate * 1_000_000.0 / 8.0, bucket: Mutex::new(Bucket { next: None }) }
    }

    async fn take(&self, n: usize) {
        if self.rate <= 0.0 {
            return;
        }
        let mut b = self.bucket.lock().await;
        let now = Instant::now();
        // Small bursts only: up to 25ms of accumulated idle credit lets a bulk
        // payload start early; anything beyond is scheduled (GCRA-style).
        let burst = Duration::from_millis(25);
        let earliest = now.checked_sub(burst).unwrap_or(now);
        let start = match b.next {
            Some(t) => t.max(earliest),
            None => earliest,
        };
        // Cap the per-chunk delay: beyond an hour the request is stalled
        // anyway, and unbounded durations can overflow Instant's add.
        let wake = start + Duration::from_secs_f64((n as f64 / self.rate).min(3600.0));
        b.next = Some(wake);
        if wake > now {
            tokio::time::sleep(wake - now).await;
        }
    }
}

static PACER: OnceLock<BulkPacer> = OnceLock::new();

pub fn init(rate_mbps: f64) {
    let _ = PACER.set(BulkPacer::new(rate_mbps));
}

/// Wait for `n` bulk bytes of link credit before sending. Call only for
/// non-control traffic; control/permit paths must stay unpaced.
pub async fn pace(n: usize) {
    PACER.get_or_init(|| BulkPacer::new(0.0)).take(n).await;
}
