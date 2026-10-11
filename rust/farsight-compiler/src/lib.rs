//! farsight-compiler: turns farsight point clouds (dense.bin / points.bin / PLY,
//! plus optional posed RGB captures) into a photoreal, hole-free 3D environment.
//! Pipeline: io -> clean -> normals -> densify (interpolation) -> surface ->
//! meshops -> texture -> export. See docs/compiler.md.

pub mod core;
pub mod spatial;
pub mod io;
pub mod learned;
pub mod clean;
pub mod normals;
pub mod planes;
pub mod densify;
pub mod surface;
pub mod meshops;
pub mod texture;
pub mod export;
pub mod synth;
pub mod metrics;
pub mod pipeline;
pub mod view;
pub mod accel;

/// Free-form per-stage statistics written into report.json.
pub type Stats = serde_json::Value;
