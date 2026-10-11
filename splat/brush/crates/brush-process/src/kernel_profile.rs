//! splat-turbo T7: `--profile <file>` per-kernel GPU timing table.
//!
//! Installs a cubecl [`LaunchObserver`] asking for
//! [`TimingRequest::Resolved`] — every kernel launch is bracketed with a
//! device-timestamp window and the observer gets `(kernel, duration,
//! method)` back. Resolved timing waits for each kernel before the next
//! launches, so the training loop serializes: use this only for profiling
//! runs, never for a timed budget run. `method` distinguishes real device
//! timestamps from cubecl's host-clock fallback (`TimingMethod::System`,
//! e.g. once wgpu's timestamp-query budget is spent or on backends that
//! lack queries); JSON rows carry it so the table doesn't mix the two.

use std::{
    collections::BTreeMap,
    path::Path,
    sync::{Arc, Mutex},
    time::Duration,
};

use anyhow::Context;
use burn_cubecl::cubecl::logging::{
    LaunchObservation, LaunchObserver, TimingMethod, TimingRequest,
};

#[derive(Default)]
struct Row {
    launches: u64,
    total: Duration,
    methods: BTreeMap<String, u64>,
}

#[derive(Default)]
struct Shared {
    rows: BTreeMap<&'static str, Row>,
}

struct KernelTable {
    shared: Arc<Mutex<Shared>>,
}

impl LaunchObserver for KernelTable {
    fn launched(&self, _kernel: &'static str) {}

    fn timing(&self) -> TimingRequest {
        TimingRequest::Resolved
    }

    fn timed(&self, kernel: &'static str, duration: Duration, method: TimingMethod) {
        let mut g = self.shared.lock().expect("kernel profile lock");
        let row = g.rows.entry(kernel).or_default();
        row.launches += 1;
        row.total += duration;
        *row.methods
            .entry(match method {
                TimingMethod::Device => "device".to_string(),
                _ => "system".to_string(),
            })
            .or_default() += 1;
    }
}

/// Installed kernel profiler. Hold the guard for the span to measure, then
/// call [`KernelProfile::write_json`]. Dropping without writing just detaches
/// the observer.
pub struct KernelProfile {
    shared: Arc<Mutex<Shared>>,
    _obs: LaunchObservation,
}

/// Begin observing every kernel launch in the process.
pub fn start_kernel_profile() -> KernelProfile {
    let shared = Arc::new(Mutex::new(Shared::default()));
    let obs = LaunchObservation::new(Arc::new(KernelTable {
        shared: shared.clone(),
    }));
    KernelProfile { shared, _obs: obs }
}

impl KernelProfile {
    /// Dump `{kernel: {launches, total_ms, mean_ms, methods}}` rows to `path`
    /// as JSON (hand-rolled — brush-process keeps no serde_json dep).
    pub fn write_json(&self, path: &Path) -> anyhow::Result<()> {
        let g = self.shared.lock().expect("kernel profile lock");
        let mut out = String::from("{\n");
        for (i, (kernel, row)) in g.rows.iter().enumerate() {
            let comma = if i + 1 == g.rows.len() { "" } else { "," };
            let methods: Vec<String> = row
                .methods
                .iter()
                .map(|(m, n)| format!("\"{m}\": {n}"))
                .collect();
            out += &format!(
                "  {:?}: {{\"launches\": {}, \"total_ms\": {:.6}, \"mean_ms\": {:.6}, \"methods\": {{{}}}}}{}\n",
                kernel,
                row.launches,
                row.total.as_secs_f64() * 1e3,
                row.total.as_secs_f64() * 1e3 / row.launches as f64,
                methods.join(", "),
                comma
            );
        }
        out += "}\n";
        std::fs::write(path, out)
            .with_context(|| format!("writing kernel profile to {}", path.display()))
    }
}
