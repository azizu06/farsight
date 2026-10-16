#![recursion_limit = "256"]
//! farsight-splat: the fast splat pipeline CLI (T1's foundation crate).
//! See `pipeline::run` / `pipeline::bench` and `hooks::Pipeline` for the lane
//! plug-in points (T3 schedule, T4 densify, T5 seed, T6 views/appearance).

pub mod capture;
pub mod device;
pub mod cull;
pub mod eval;
pub mod export;
pub mod hooks;
pub mod pipeline;
pub mod score;
pub mod seed;
pub mod synth;
pub mod train;

pub use capture::{Capture, Frame, Transforms};
pub use hooks::Pipeline;
pub use pipeline::{RunOptions, RunReport, bench, run};
