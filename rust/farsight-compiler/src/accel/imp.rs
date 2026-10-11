//! wgpu device plumbing shared by the accel kernels. Owner: W7.
#![cfg(any(target_os = "macos", feature = "gpu"))]

use std::collections::HashMap;
use std::sync::Mutex;
use wgpu::util::DeviceExt;

/// Live device + queue + lazily-built compute pipelines.
pub struct DevInner {
    pub device: wgpu::Device,
    pub queue: wgpu::Queue,
    pipelines: Mutex<HashMap<String, wgpu::ComputePipeline>>,
}

/// Initialise wgpu with a high-performance adapter preference. Returns
/// (name, backend, inner) or `None` when no adapter accepts a device.
pub(crate) fn init() -> Option<(String, String, DevInner)> {
    let inst = wgpu::Instance::new(&wgpu::InstanceDescriptor {
        backends: wgpu::Backends::all(),
        ..Default::default()
    });
    let adapter = pollster::block_on(inst.request_adapter(&wgpu::RequestAdapterOptions {
        power_preference: wgpu::PowerPreference::HighPerformance,
        force_fallback_adapter: false,
        compatible_surface: None,
    }))?;
    let info = adapter.get_info();
    let mut limits = wgpu::Limits::default();
    limits.max_storage_buffer_binding_size = limits.max_storage_buffer_binding_size.min(adapter.limits().max_storage_buffer_binding_size);
    limits.max_buffer_size = adapter.limits().max_buffer_size;
    let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
        label: Some("farsight-accel"),
        required_features: wgpu::Features::empty(),
        required_limits: limits,
        memory_hints: wgpu::MemoryHints::Performance,
        ..Default::default()
    }, None))
    .ok()?;
    Some((
        info.name,
        format!("{:?}", info.backend).to_lowercase(),
        DevInner { device, queue, pipelines: Mutex::new(HashMap::new()) },
    ))
}

impl DevInner {
    /// Build (or fetch from cache) a compute pipeline for `wgsl` source with
    /// entry point `entry`. `key` uniquely identifies the pipeline.
    pub fn pipeline(&self, key: &str, wgsl: &str, entry: &str) -> wgpu::ComputePipeline {
        if let Some(p) = self.pipelines.lock().unwrap().get(key) {
            return p.clone();
        }
        let module = self.device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some(key),
            source: wgpu::ShaderSource::Wgsl(wgsl.into()),
        });
        let pipe = self.device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(key),
            layout: None,
            module: &module,
            entry_point: Some(entry),
            compilation_options: wgpu::PipelineCompilationOptions::default(),
            cache: None,
        });
        self.pipelines.lock().unwrap().insert(key.to_string(), pipe.clone());
        pipe
    }

    /// Storage buffer filled with `data`.
    pub fn buf<T: bytemuck::Pod>(&self, label: &str, data: &[T]) -> wgpu::Buffer {
        self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(label),
            contents: bytemuck::cast_slice(data),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST,
        })
    }

    /// Empty storage buffer of `bytes` size.
    pub fn buf_empty(&self, label: &str, bytes: u64) -> wgpu::Buffer {
        self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some(label),
            size: bytes.max(16),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        })
    }

    /// Uniform buffer filled with `data`.
    pub fn buf_uniform<T: bytemuck::Pod>(&self, label: &str, data: &T) -> wgpu::Buffer {
        self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(label),
            contents: bytemuck::bytes_of(data),
            usage: wgpu::BufferUsages::UNIFORM,
        })
    }

    /// Bind group for `pipe`'s layout 0 over `bufs`.
    pub fn bind(&self, pipe: &wgpu::ComputePipeline, bufs: &[&wgpu::Buffer]) -> wgpu::BindGroup {
        let entries: Vec<wgpu::BindGroupEntry> = bufs
            .iter()
            .enumerate()
            .map(|(i, b)| wgpu::BindGroupEntry { binding: i as u32, resource: b.as_entire_binding() })
            .collect();
        self.device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None,
            layout: &pipe.get_bind_group_layout(0),
            entries: &entries,
        })
    }

    /// Dispatch `n` work items (groups = ceil(n/wg)), submit, and read back
    /// `read` (buffer, byte_count) into a fresh Vec. One readback per call.
    pub fn run_and_read<T: bytemuck::Pod>(
        &self,
        pipe: &wgpu::ComputePipeline,
        bind: &wgpu::BindGroup,
        n: u32,
        wg: u32,
        read: &wgpu::Buffer,
        byte_count: u64,
    ) -> Vec<T> {
        let mut enc = self.device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: None, timestamp_writes: None });
            pass.set_pipeline(pipe);
            pass.set_bind_group(0, bind, &[]);
            pass.dispatch_workgroups(n.div_ceil(wg), 1, 1);
        }
        let stage = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("readback"),
            size: byte_count,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        enc.copy_buffer_to_buffer(read, 0, &stage, 0, byte_count);
        self.queue.submit([enc.finish()]);
        let slice = stage.slice(..);
        let (tx, rx) = futures_channel::oneshot::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| {
            let _ = tx.send(r);
        });
        let _ = self.device.poll(wgpu::Maintain::Wait);
        let _ = pollster::block_on(rx);
        let out = bytemuck::cast_slice::<u8, T>(&slice.get_mapped_range()).to_vec();
        stage.unmap();
        out
    }
}
