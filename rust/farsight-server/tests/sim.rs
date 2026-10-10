//! SimFeed/RoverSim tests — subset port of tests/test_sim.py covering world
//! loading, the simulated feeds (camera/depth/range/detections) and drive.

use farsight_server::sim::{load, world_names, RoverSim, SimFeed};
use serde_json::json;
use std::time::Instant;

#[test]
fn every_world_loads() {
    assert_eq!(
        world_names(),
        vec!["popup", "doorway", "deadend", "floorplan", "seek"]
    );
    for name in world_names() {
        load(name).unwrap_or_else(|e| panic!("{name}: {e}"));
    }
    assert!(load("bogus-world").is_err());
    assert!(load("/etc/passwd").is_err());
}

#[test]
fn pose_and_motion() {
    let sim = RoverSim::new(load("floorplan").unwrap(), None, None);
    let before = sim.state();
    assert_eq!(before["x"].as_f64().unwrap(), 0.0);
    assert_eq!(before["z"].as_f64().unwrap(), 0.0);
    assert!(before["yaw_deg"].is_number());
    // apply(v, w, blocked) then advance integrates motion.
    sim.apply(1.0, 0.0, false);
    for _ in 0..50 {
        sim.advance();
        std::thread::sleep(std::time::Duration::from_millis(4));
    }
    let after = sim.state();
    let moved = (after["x"].as_f64().unwrap() - before["x"].as_f64().unwrap()).abs()
        + (after["z"].as_f64().unwrap() - before["z"].as_f64().unwrap()).abs();
    assert!(moved > 0.01, "rover should have driven forward: {moved}");
}

#[test]
fn ultrasonic_reports_centimetres() {
    let sim = RoverSim::new(load("floorplan").unwrap(), None, None);
    let cm = sim.ultrasonic_cm(206.0);
    assert!(cm.is_some());
    let cm = cm.unwrap();
    assert!(cm > 0 && cm <= 206);
}

#[test]
fn rendered_frame_has_depth_and_jpeg() {
    let feed = SimFeed::new(RoverSim::new(load("floorplan").unwrap(), None, None));
    let frame = feed.frame(Instant::now().elapsed().as_secs_f64() + 1.0);
    assert_eq!(frame.depth_width, 160);
    assert_eq!(frame.depth_height, 120);
    assert_eq!(frame.depth.len(), 160 * 120);
    assert_eq!(frame.rgb.len(), 160 * 120 * 3);
    // Depth is metres, finite and inside the render range.
    assert!(frame.depth.iter().all(|d| *d > 0.0 && *d <= 5.0));
    // JPEG of the rendered RGB starts with the SOI marker.
    let jpeg = feed.sim.jpeg(&frame.rgb);
    assert_eq!(&jpeg[..2], &[0xFF, 0xD8]);
}

#[test]
fn header_and_detections_wire_shapes() {
    let feed = SimFeed::new(RoverSim::new(load("seek").unwrap(), None, None));
    let pose = feed.sim.camera_pose(0.0);
    let header = feed.header(100.0, &pose);
    // stream.py SimFeed.header shape, field-for-field.
    assert_eq!(header["version"], json!(1));
    assert_eq!(header["type"], json!("frame"));
    assert!(header["frame_id"].is_number());
    assert_eq!(header["t_capture"], json!(100.0 - feed.started));
    assert_eq!(header["tracking"], json!("normal"));
    assert!(header["transform"].as_array().unwrap().len() == 16);

    let detections = feed.detections();
    assert!(detections["boxes"].is_array());
    assert!(detections["age_ms"].is_i64() || detections["age_ms"].is_null());
    assert!(detections["fresh"].is_boolean());
}

#[test]
fn ground_truth_state() {
    let sim = RoverSim::new(load("seek").unwrap(), None, None);
    let state = sim.state();
    for key in [
        "world",
        "x",
        "z",
        "yaw_deg",
        "v",
        "w",
        "collisions",
        "travelled_m",
        "min_clearance_m",
        "moving_s",
        "appeared",
        "people",
        "obstacles",
        "trace",
        "speed_mps",
        "turn_radps",
        "camera_height_m",
        "coverage",
    ] {
        assert!(state.get(key).is_some(), "missing {key}");
    }
    assert_eq!(state["collisions"], json!(0));
    assert_eq!(state["world"], json!("seek"));
    assert_eq!(state["camera_height_m"], json!(0.15));
}
