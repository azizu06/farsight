//! Per-phase wall-clock profile of the Brush render pipeline (fwd + bwd).
//!
//! Run: `BRUSH_PROFILE=1 cargo run --release -p brush-render --example pipeline_prof`
//! Env knobs: SPLATS_N (default 1_450_000), ITERS (default 3), WIDTH, HEIGHT.
//!
//! Each phase ends with a device sync, so numbers include sync bubbles — use
//! them relative (before/after), like-for-like, not as absolute GPU times.

use brush_render::{
    bwd::render_splats,
    camera::Camera,
    gaussian_splats::{SplatRenderMode, Splats},
    profile,
};
use burn::{backend::wgpu::WgpuDevice, tensor::Device};
use glam::{Quat, Vec3, uvec2};

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> f32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f32 / (1u64 << 53) as f32
    }
    fn range(&mut self, lo: f32, hi: f32) -> f32 {
        lo + self.next() * (hi - lo)
    }
}

fn gen_splats(count: usize, device: &Device) -> Splats {
    let mut rng = Rng(0x9E3779B97F4A7C15);
    // Room-ish shell in front of the camera: z in [-8,-1.5], wide xy spread,
    // scales giving footprints from a few px to ~half a tile column — meant to
    // mimic a trained room scan, not a uniform cloud.
    let means: Vec<f32> = (0..count)
        .flat_map(|_| {
            [
                rng.range(-4.0, 4.0),
                rng.range(-3.0, 3.0),
                rng.range(-4.0, 4.0),
            ]
        })
        .collect();
    let log_scales: Vec<f32> = (0..count)
        .flat_map(|_| {
            let s = rng.range(-4.8, -1.4);
            [s, s * rng.range(0.7, 1.0), s * rng.range(0.7, 1.0)]
        })
        .collect();
    let rotations: Vec<f32> = (0..count)
        .flat_map(|_| {
            let u1 = rng.next();
            let u2 = rng.next();
            let u3 = rng.next();
            let s1 = (1.0 - u1).sqrt();
            let s2 = u1.sqrt();
            let t1 = 2.0 * std::f32::consts::PI * u2;
            let t2 = 2.0 * std::f32::consts::PI * u3;
            [
                s1 * t1.sin(),
                s1 * t1.cos(),
                s2 * t2.sin(),
                s2 * t2.cos(),
            ]
        })
        .collect();
    let sh: Vec<f32> = (0..count)
        .flat_map(|_| [rng.range(0.1, 0.9), rng.range(0.1, 0.9), rng.range(0.1, 0.9)])
        .collect();
    let opac: Vec<f32> = (0..count).map(|_| rng.range(-3.0, 3.5)).collect();

    Splats::from_raw(
        means,
        rotations,
        log_scales,
        sh,
        opac,
        SplatRenderMode::Default,
        device,
    )
    .with_sh_degree(0)
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    profile::set_enabled(true);
    let device = Device::from(WgpuDevice::default()).autodiff();
    let n = env_usize("SPLATS_N", 1_450_000);
    let iters = env_usize("ITERS", 3);
    let w = env_usize("WIDTH", 640) as u32;
    let h = env_usize("HEIGHT", 480) as u32;

    println!("generating {n} splats @ {w}x{h} ...");
    let splats = gen_splats(n, &device);
    let camera = Camera::new(
        Vec3::new(0.0, 0.0, -5.0),
        Quat::IDENTITY,
        1.0,
        0.75,
        glam::vec2(0.5, 0.5),
        brush_render::kernels::camera_model::CameraModel::Pinhole,
    );

    // warmup + JIT compile (forward + backward)
    for _ in 0..2 {
        let diff_out = render_splats(splats.clone(), &camera, uvec2(w, h), Vec3::ZERO).await;
        let _ = diff_out.img.mean().backward();
        device.sync().unwrap();
    }
    profile::take(); // discard warmup marks

    let mut phase_acc: Vec<(String, f64)> = Vec::new();
    let mut wall_total = 0.0f64;

    for it in 0..iters {
        let t0 = std::time::Instant::now();
        let diff_out = render_splats(splats.clone(), &camera, uvec2(w, h), Vec3::ZERO).await;
        let _ = diff_out.img.mean().backward();
        device.sync().unwrap();
        wall_total += t0.elapsed().as_secs_f64() * 1e3;

        let (times, counts) = profile::take();
        for (name, ms) in times.iter() {
            match phase_acc.iter_mut().find(|(n, _)| n == name) {
                Some(e) => e.1 += ms,
                None => phase_acc.push((name.clone(), *ms)),
            }
        }
        if it == 0 {
            for (k, v) in &counts {
                println!("  count {k:20} {v}");
            }
        }
    }

    println!("\n=== avg over {iters} iters (ms, sync-bracketed) ===");
    let mut sum = 0.0;
    for (name, ms) in &phase_acc {
        let v = ms / iters as f64;
        sum += v;
        println!("{name:20} {v:9.2}");
    }
    println!("{:20} {:9.2}", "phase_sum", sum);
    println!("{:20} {:9.2}", "wall_fwd+bwd", wall_total / iters as f64);
}
