//! GPU device init with explicit backend selection: Metal on macOS, Vulkan
//! elsewhere, wgpu's auto pick (GL) only when the preferred init fails.
//! Reports the resolved backend + adapter for the bench receipt.

use burn_wgpu::{
    RuntimeOptions, WgpuDevice,
    graphics::{AutoGraphicsApi, GraphicsApi},
};
use brush_process::ProcessDevice;

#[cfg(target_os = "macos")]
type Preferred = burn_wgpu::graphics::Metal;
#[cfg(not(target_os = "macos"))]
type Preferred = burn_wgpu::graphics::Vulkan;

fn opts() -> RuntimeOptions {
    RuntimeOptions {
        tasks_max: 64,
        memory_config: burn_wgpu::MemoryConfiguration::ExclusivePages,
    }
}

/// Initialize the cubecl/wgpu runtime on the preferred backend, falling back
/// to wgpu's auto pick (GL) if the preferred API has no usable adapter.
/// Returns the process device and a "backend adapter-name" receipt string.
pub async fn init_device() -> (ProcessDevice, String) {
    let preferred = if cfg!(target_os = "macos") {
        "metal"
    } else {
        "vulkan"
    };
    // wgpu's own auto selection honours WGPU_BACKEND too — belt & suspenders
    // for any adapter picked outside our generic.
    unsafe { std::env::set_var("WGPU_BACKEND", preferred) };
    let attempt = tokio::spawn(async {
        burn_wgpu::init_setup_async::<Preferred>(&WgpuDevice::default(), opts()).await
    })
    .await;
    let backend = match attempt {
        Ok(_setup) => preferred.to_string(),
        Err(e) => {
            log::warn!("{preferred} init failed ({e}); falling back to auto graphics API");
            burn_wgpu::init_setup_async::<AutoGraphicsApi>(&WgpuDevice::default(), opts()).await;
            format!("auto:{:?}", AutoGraphicsApi::backend()).to_lowercase()
        }
    };
    let adapter = adapter_name().await;
    (brush_process::default_device(), format!("{backend} {adapter}"))
}

/// Best-effort adapter name for the receipt (second, info-only query; the
/// training device is the one cubecl bound).
async fn adapter_name() -> String {
    let desc = wgpu::InstanceDescriptor::new_without_display_handle();
    let instance = wgpu::Instance::new(desc);
    let req = wgpu::RequestAdapterOptions::default();
    match instance.request_adapter(&req).await {
        Ok(a) => a.get_info().name,
        Err(_) => "unknown".to_string(),
    }
}
