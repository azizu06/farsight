//! Optional per-phase GPU timing for the render pipeline.
//!
//! Enable with `BRUSH_PROFILE=1` in the environment or [`set_enabled`].
//! When enabled, each pipeline phase ends with a full device sync and the
//! wall-clock delta is recorded — on CPU backends (lavapipe) these are real
//! execution times; on discrete GPUs they include the sync bubble, so the
//! numbers are a *relative* profiling aid, not absolute truth.
//!
//! All data is thread-local; drain it with [`take`]. When disabled, every
//! entry point is a cheap atomic check.

use std::cell::RefCell;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

static ENABLED: AtomicBool = AtomicBool::new(false);

thread_local! {
    static LAST: RefCell<Option<Instant>> = const { RefCell::new(None) };
    static TIMES: RefCell<Vec<(String, f64)>> = const { RefCell::new(Vec::new()) };
    static COUNTS: RefCell<Vec<(String, u64)>> = const { RefCell::new(Vec::new()) };
}

/// Enable/disable profiling. `None` honours `BRUSH_PROFILE` once.
pub fn set_enabled(on: bool) {
    ENABLED.store(on, Ordering::Relaxed);
}

pub fn enabled() -> bool {
    #[cfg(not(target_family = "wasm"))]
    {
        static INIT: AtomicBool = AtomicBool::new(false);
        if !INIT.swap(true, Ordering::Relaxed) && std::env::var_os("BRUSH_PROFILE").is_some() {
            ENABLED.store(true, Ordering::Relaxed);
        }
    }
    ENABLED.load(Ordering::Relaxed)
}

/// Record a phase boundary. `sync` must flush the device queue and wait for
/// completion; it is only invoked when profiling is on.
#[inline]
pub fn mark(name: &str, sync: impl FnOnce()) {
    if !enabled() {
        return;
    }
    sync();
    let now = Instant::now();
    let prev = LAST.with(|l| l.borrow_mut().replace(now));
    if let Some(prev) = prev {
        TIMES.with(|t| {
            t.borrow_mut()
                .push((name.to_string(), (now - prev).as_secs_f64() * 1e3))
        });
    }
}

/// Record a scalar (counts, sizes) alongside the timings.
#[inline]
pub fn count(name: &str, v: u64) {
    if !enabled() {
        return;
    }
    COUNTS.with(|c| c.borrow_mut().push((name.to_string(), v)));
}

/// Mark the start of a profiled run (resets the phase clock).
pub fn begin() {
    if !enabled() {
        return;
    }
    LAST.with(|l| *l.borrow_mut() = Some(Instant::now()));
}

/// Drain all recorded phases and counts.
pub fn take() -> (Vec<(String, f64)>, Vec<(String, u64)>) {
    (
        TIMES.with(|t| std::mem::take(&mut *t.borrow_mut())),
        COUNTS.with(|c| std::mem::take(&mut *c.borrow_mut())),
    )
}
