//! GPU compute layer for farsight-compiler. OWNER: W7.
//!
//! A thin `wgpu` layer that lets the heavy kernels run on the GPU: Metal on
//! Apple Silicon (compiled unconditionally there), Vulkan/llvmpipe elsewhere
//! behind `--features gpu`. [`gpu()`] returns `Option<&'static Gpu>` and is
//! `None` when no adapter exists or the feature is off — every stage keeps a
//! fast CPU path and treats this as an accelerator, never a dependency.
//!
//! Kernels provided:
//! - [`knn`]: tiled brute-force kNN / radius queries for batches of points.
//! - [`tsdf`]: gather-style TSDF observation splat (mirrors `surface::observe`
//!   math) over a brick pool, plus marching-cubes classify/compact.
//! - [`raster`]: per-view depth raster (atomic-min) and texel/vertex
//!   projection + image sampling for texture baking.

pub mod knn;
pub mod raster;
pub mod tsdf;

use std::sync::OnceLock;

/// Whether the wgpu backend was compiled in (always true on macOS targets,
/// elsewhere requires `--features gpu`).
pub const GPU_COMPILED: bool = cfg!(any(target_os = "macos", feature = "gpu"));

#[cfg(any(target_os = "macos", feature = "gpu"))]
pub(crate) mod imp;

#[cfg(any(target_os = "macos", feature = "gpu"))]
pub(crate) use imp::DevInner;

/// A live GPU device + queue, or `None`. Global, lazily initialised.
pub struct Gpu {
    name: String,
    backend: String,
    #[cfg(any(target_os = "macos", feature = "gpu"))]
    pub(crate) inner: DevInner,
    #[cfg(not(any(target_os = "macos", feature = "gpu")))]
    _private: (),
}

impl Gpu {
    /// Adapter name (e.g. "Apple M5 Pro").
    pub fn name(&self) -> &str {
        &self.name
    }
    /// wgpu backend in use (metal / vulkan / ...).
    pub fn backend(&self) -> &str {
        &self.backend
    }
}

static GPU: OnceLock<Option<Gpu>> = OnceLock::new();

/// The process-wide GPU handle, if any adapter was found. Lazy: the first
/// call initialises wgpu; subsequent calls are free.
pub fn gpu() -> Option<&'static Gpu> {
    GPU.get_or_init(init).as_ref()
}

/// One-line description for logs: adapter name or "cpu-only".
pub fn device_line() -> String {
    match gpu() {
        Some(g) => format!("{} ({})", g.name, g.backend),
        None => "cpu-only (no gpu adapter / feature off)".to_string(),
    }
}

#[cfg(any(target_os = "macos", feature = "gpu"))]
fn init() -> Option<Gpu> {
    let (name, backend, inner) = imp::init()?;
    Some(Gpu { name, backend, inner })
}

#[cfg(not(any(target_os = "macos", feature = "gpu")))]
fn init() -> Option<Gpu> {
    None
}
