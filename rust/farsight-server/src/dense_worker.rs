//! Best-effort visual accumulation; never wait for it on the camera path.
//! Port of jetson/mapping/dense_worker.py.

use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use serde_json::{json, Value};

use crate::dense::DenseMap;
use crate::phone::RgbdFrame;

pub const DENSE_HZ: f64 = 2.0;
pub const DENSE_FRAME_POINTS: usize = 4096;

/// FARSIGHT_DENSE opt-out: "0"/"false"/"off"/"no" disable at construction.
pub fn env_enabled(value: Option<&str>) -> bool {
    !matches!(
        value.map(|v| v.trim().to_ascii_lowercase()).as_deref(),
        Some("0") | Some("false") | Some("off") | Some("no")
    )
}

/// generation + cloud-identity + session gate evaluated inside the worker
/// before sampling and again before integrating (dense_worker.current parity).
pub type Current = Arc<dyn Fn(u64, &Arc<DenseMap>) -> bool + Send + Sync>;

struct Job {
    frame: RgbdFrame,
    cloud: Arc<DenseMap>,
    generation: u64,
}

struct Shared {
    state: Mutex<State>,
    cond: Condvar,
}

#[derive(Default)]
struct State {
    /// Latest-only slot: a newer offer replaces the queued frame whole.
    pending: Option<Job>,
    stopped: bool,
    offered: u64,
    processed: u64,
    dropped: u64,
    processing_ms: f64,
    error: Option<String>,
}

/// Outcome of one worker iteration (dense.py try/continue/except parity).
enum Outcome {
    /// First current() check failed: `continue` — error stays untouched.
    Skipped,
    /// Job ran; bool = whether the second check still passed and integrated.
    Ran(bool),
}

/// Bounded latest-only frame worker: one active job plus one replaceable
/// pending job, ~2 Hz cadence, <= DENSE_FRAME_POINTS samples per job with a
/// rotating phase stride. Only ever takes the dense cloud lock — the camera
/// path never blocks on it.
pub struct DenseWorker {
    pub enabled: bool,
    current: Current,
    shared: Arc<Shared>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl DenseWorker {
    /// Reads FARSIGHT_DENSE like the Python constructor.
    pub fn new(current: Current) -> Self {
        Self::with_enabled(env_enabled(std::env::var("FARSIGHT_DENSE").ok().as_deref()), current)
    }

    pub fn with_enabled(enabled: bool, current: Current) -> Self {
        Self {
            enabled,
            current,
            shared: Arc::new(Shared { state: Mutex::new(State::default()), cond: Condvar::new() }),
            thread: Mutex::new(None),
        }
    }

    pub fn offer(&self, frame: RgbdFrame, cloud: Arc<DenseMap>, generation: u64) {
        if !self.enabled {
            return;
        }
        {
            let mut s = self.shared.state.lock().unwrap();
            if s.stopped {
                return;
            }
            s.offered += 1;
            if s.pending.is_some() {
                s.dropped += 1;
            }
            s.pending = Some(Job { frame, cloud, generation });
            let mut thread = self.thread.lock().unwrap();
            if thread.is_none() {
                let shared = self.shared.clone();
                let current = self.current.clone();
                *thread = Some(
                    std::thread::Builder::new()
                        .name("dense-visual".into())
                        .spawn(move || run(shared, current))
                        .expect("dense worker spawn"),
                );
            }
        }
        self.shared.cond.notify_one();
    }

    /// Independent of camera/coarse frame timing (status parity).
    pub fn status(&self) -> Value {
        let s = self.shared.state.lock().unwrap();
        json!({
            "enabled": self.enabled,
            "hz": DENSE_HZ as u64,
            "frame_points": DENSE_FRAME_POINTS,
            "queued": if s.pending.is_some() { 1 } else { 0 },
            "offered": s.offered,
            "processed": s.processed,
            "dropped": s.dropped,
            "processing_ms": s.processing_ms,
            "error": s.error,
        })
    }

    /// Diagnostics for tests: (offered, processed, dropped).
    pub fn counters(&self) -> (u64, u64, u64) {
        let s = self.shared.state.lock().unwrap();
        (s.offered, s.processed, s.dropped)
    }

    pub fn close(&self) {
        {
            let mut s = self.shared.state.lock().unwrap();
            s.stopped = true;
            s.pending = None;
            self.shared.cond.notify_all();
        }
        if let Some(thread) = self.thread.lock().unwrap().take() {
            let _ = thread.join();
        }
    }
}

impl Drop for DenseWorker {
    fn drop(&mut self) {
        self.close();
    }
}

fn run(shared: Arc<Shared>, current: Current) {
    let mut next_at = 0.0f64;
    loop {
        let job = {
            let mut s = shared.state.lock().unwrap();
            loop {
                if s.stopped {
                    return;
                }
                let delay = next_at - crate::state::now_monotonic();
                if s.pending.is_some() && delay <= 0.0 {
                    break;
                }
                s = if s.pending.is_some() {
                    shared
                        .cond
                        .wait_timeout(s, Duration::from_secs_f64(delay.max(0.0)))
                        .unwrap()
                        .0
                } else {
                    shared.cond.wait(s).unwrap()
                };
            }
            s.pending.take().unwrap()
        };
        let started = crate::state::now_monotonic();
        next_at = started + 1.0 / DENSE_HZ;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            if !current(job.generation, &job.cloud) {
                return Outcome::Skipped;
            }
            let phase = shared.state.lock().unwrap().processed as usize;
            let (xyz, rgb) =
                crate::phone::measured_points(&job.frame, Some(DENSE_FRAME_POINTS), phase);
            Outcome::Ran(if current(job.generation, &job.cloud) {
                // Only the visual cloud lock; never acquire MapState's lock.
                job.cloud.integrate(&xyz, &rgb);
                true
            } else {
                false
            })
        }));
        let mut s = shared.state.lock().unwrap();
        match result {
            Ok(Outcome::Skipped) => {}
            Ok(Outcome::Ran(integrated)) => {
                if integrated {
                    s.processed += 1;
                }
                s.error = None;
            }
            Err(panic) => {
                let detail = panic
                    .downcast_ref::<&str>()
                    .map(|s| s.to_string())
                    .or_else(|| panic.downcast_ref::<String>().cloned())
                    .unwrap_or_else(|| "panic".into());
                s.error = Some(format!("Panic: {detail}"));
            }
        }
        s.processing_ms = ((crate::state::now_monotonic() - started) * 1000.0 * 10.0).round() / 10.0;
    }
}
