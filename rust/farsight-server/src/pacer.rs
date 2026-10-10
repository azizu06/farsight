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

#[derive(Default)]
struct Bucket {
    tokens: f64,
    last: Option<Instant>,
}

pub struct BulkPacer {
    rate: f64, // bytes/sec
    bucket: Mutex<Bucket>,
}

impl BulkPacer {
    fn new(rate_mbps: f64) -> Self {
        Self { rate: rate_mbps * 1_000_000.0 / 8.0, bucket: Mutex::new(Bucket::default()) }
    }

    async fn take(&self, n: usize) {
        if self.rate <= 0.0 {
            return;
        }
        let mut b = self.bucket.lock().await;
        let now = Instant::now();
        // Small bursts only: a big credit lets one bulk payload occupy the
        // shared link long enough to delay control frames behind it.
        let burst = self.rate / 40.0; // up to 25ms of credit
        let tokens = match b.last {
            Some(last) => (b.tokens + (now - last).as_secs_f64() * self.rate).min(burst),
            None => burst,
        };
        b.last = Some(now);
        let n = n as f64;
        if n > tokens {
            b.tokens = 0.0;
            tokio::time::sleep(Duration::from_secs_f64((n - tokens) / self.rate)).await;
        } else {
            b.tokens = tokens - n;
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
