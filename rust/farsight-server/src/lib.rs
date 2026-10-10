//! Rust port of the Farsight Jetson services (jetson/mapping + jetson core).
//! Wire formats and safety policy are byte-compatible with the Python implementation.

pub mod ask_intent;
pub mod assistant;
pub mod autonomy;
pub mod camera;
pub mod control;
pub mod demo;
pub mod depth;
#[cfg(feature = "detector")]
pub mod detector;
pub mod geometry;
pub mod mission;
pub mod perception;
pub mod phone;
pub mod phrases;
pub mod scan;
pub mod server;
pub mod state;
pub mod surfaces;
pub mod tracker;
pub mod triage;
pub mod video;
pub mod voxel;
