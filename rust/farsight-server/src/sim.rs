//! Closed-loop indoor rover simulator: test floor plans, pop-up obstacles, a
//! walking person, a ray-cast camera and an HC-SR04-style ultrasonic.
//! Ports of jetson/sim/world.py, jetson/sim/worlds.py and the SimFeed half of
//! jetson/sim/stream.py — same world frame (metres, Y up, rover starts facing
//! -Z), same dynamics, same sensor outputs.

use std::collections::{HashSet, VecDeque};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

use crate::camera::CameraFrame;
use crate::geometry::{flatten_f, Calibration, Mat4};
use crate::phone::RgbdFrame;
use crate::state::{now_monotonic, MapState};

pub const CEILING: f64 = 2.6;
pub const CAMERA_HEIGHT: f64 = 0.15;
pub const RADIUS: f64 = 0.12;
pub const SPEED_MPS: f64 = 0.45;
pub const TURN_RADPS: f64 = 3.0;
pub const MAX_DT: f64 = 0.25;
pub const COVERAGE_CELL: f64 = 0.25;
pub const POSE_HZ: f64 = 30.0;
pub const FRAME_HZ: f64 = 10.0;

const WALL: f64 = 0.1;
const FLOOR_TINT: [f64; 3] = [70.0, 74.0, 70.0];
const CEILING_TINT: [f64; 3] = [205.0, 205.0, 200.0];
const PERSON_COLOR: [f64; 3] = [176.0, 74.0, 58.0];

fn r3(v: f64) -> f64 {
    (v * 1000.0).round_ties_even() / 1000.0
}

// ---------------------------------------------------------------------------
// World model (jetson/sim/world.py Block / Person / World)

/// Axis-aligned box on the floor: x0..x1, z0..z1 (metres), height above floor.
#[derive(Clone, Debug)]
pub struct Block {
    pub x0: f64,
    pub z0: f64,
    pub x1: f64,
    pub z1: f64,
    pub height: f64,
    pub color: [f64; 3],
    /// Pop-up obstacles: appear once the rover is within `appear_within` metres
    /// of the box centre, or once `appear_after_s` seconds of motion elapsed.
    pub appear_within: Option<f64>,
    pub appear_after_s: Option<f64>,
    pub visible: bool,
    pub name: String,
}

impl Block {
    pub fn new(x0: f64, z0: f64, x1: f64, z1: f64, height: f64) -> Self {
        Self::full(
            x0,
            z0,
            x1,
            z1,
            height,
            [120.0, 128.0, 132.0],
            None,
            None,
            "",
        )
    }

    pub fn full(
        x0: f64,
        z0: f64,
        x1: f64,
        z1: f64,
        height: f64,
        color: [f64; 3],
        appear_within: Option<f64>,
        appear_after_s: Option<f64>,
        name: &str,
    ) -> Self {
        let (x0, x1) = if x0 <= x1 { (x0, x1) } else { (x1, x0) };
        let (z0, z1) = if z0 <= z1 { (z0, z1) } else { (z1, z0) };
        Self {
            x0,
            z0,
            x1,
            z1,
            height,
            color,
            appear_within,
            appear_after_s,
            visible: appear_within.is_none() && appear_after_s.is_none(),
            name: name.into(),
        }
    }

    pub fn centre(&self) -> (f64, f64) {
        ((self.x0 + self.x1) / 2.0, (self.z0 + self.z1) / 2.0)
    }

    pub fn distance(&self, x: f64, z: f64) -> f64 {
        let dx = (self.x0 - x).max(0.0).max(x - self.x1);
        let dz = (self.z0 - z).max(0.0).max(z - self.z1);
        dx.hypot(dz)
    }
}

/// Upright cylinder walking a closed route of (x, z) waypoints.
#[derive(Clone, Debug)]
pub struct Person {
    pub route: Vec<(f64, f64)>,
    pub speed: f64,
    pub radius: f64,
    pub height: f64,
    pub distance: f64,
}

impl Person {
    pub fn new(route: Vec<(f64, f64)>, speed: f64) -> Self {
        Self {
            route,
            speed,
            radius: 0.22,
            height: 1.7,
            distance: 0.0,
        }
    }

    pub fn position(&self) -> (f64, f64) {
        let points = &self.route;
        if points.len() == 1 {
            return points[0];
        }
        let n = points.len();
        let lengths: Vec<f64> = (0..n)
            .map(|i| {
                let a = points[i];
                let b = points[(i + 1) % n];
                (a.0 - b.0).hypot(a.1 - b.1)
            })
            .collect();
        let total: f64 = lengths.iter().sum();
        let mut remaining = self.distance.rem_euclid(total);
        for (i, length) in lengths.iter().enumerate() {
            if remaining <= *length {
                let a = points[i];
                let b = points[(i + 1) % n];
                let t = if *length != 0.0 {
                    remaining / length
                } else {
                    0.0
                };
                return (a.0 + (b.0 - a.0) * t, a.1 + (b.1 - a.1) * t);
            }
            remaining -= length;
        }
        points[0]
    }
}

#[derive(Clone)]
pub struct World {
    pub name: String,
    pub blocks: Vec<Block>,
    /// x, z, yaw
    pub start: (f64, f64, f64),
    pub people: Vec<Person>,
    pub description: String,
}

// ---------------------------------------------------------------------------
// jetson/sim/worlds.py — the five named test floor plans

fn room(x0: f64, z0: f64, x1: f64, z1: f64) -> Vec<Block> {
    room_h(x0, z0, x1, z1, 2.6)
}

fn room_h(x0: f64, z0: f64, x1: f64, z1: f64, height: f64) -> Vec<Block> {
    vec![
        Block::new(x0 - WALL, z0 - WALL, x1 + WALL, z0, height),
        Block::new(x0 - WALL, z1, x1 + WALL, z1 + WALL, height),
        Block::new(x0 - WALL, z0, x0, z1, height),
        Block::new(x1, z0, x1 + WALL, z1, height),
    ]
}

fn popup() -> World {
    let mut blocks = room(-1.0, -8.0, 1.0, 1.0);
    blocks.push(Block::full(
        -0.35,
        -3.6,
        0.35,
        -3.1,
        0.5,
        [201.0, 146.0, 61.0],
        Some(1.75),
        None,
        "popup box",
    ));
    World {
        name: "popup".into(),
        blocks,
        start: (0.0, 0.0, 0.0),
        people: vec![],
        description: "Corridor with an obstacle that appears in the path. Goal (0, -6.5).".into(),
    }
}

fn doorway() -> World {
    let mut blocks = room(-2.0, -6.5, 2.0, 0.8);
    blocks.push(Block::new(-2.0, -3.1, 0.6, -3.0, CEILING));
    blocks.push(Block::new(1.4, -3.1, 2.0, -3.0, CEILING));
    blocks.push(Block::full(
        -1.6,
        -2.2,
        -0.6,
        -1.6,
        0.7,
        [66.0, 150.0, 145.0],
        None,
        None,
        "table",
    ));
    World {
        name: "doorway".into(),
        blocks,
        start: (0.0, 0.0, 0.0),
        people: vec![],
        description: "Goal (-1.2, -5.5) in the far room, through a 0.8 m door.".into(),
    }
}

fn deadend() -> World {
    let mut blocks = room(-3.0, -7.0, 3.0, 1.0);
    blocks.push(Block::new(-3.0, -4.2, -0.45, -4.0, CEILING));
    blocks.push(Block::new(0.45, -4.2, 2.2, -4.0, CEILING));
    blocks.push(Block::new(-0.45, -4.0, -0.35, -2.0, CEILING));
    blocks.push(Block::new(0.35, -4.0, 0.45, -2.0, CEILING));
    blocks.push(Block::full(
        -0.35,
        -4.2,
        0.35,
        -4.0,
        CEILING,
        [167.0, 92.0, 64.0],
        Some(1.0),
        None,
        "closed door",
    ));
    World {
        name: "deadend".into(),
        blocks,
        start: (0.0, 0.0, 0.0),
        people: vec![],
        description: "Goal (0, -5.5); the straight corridor becomes a dead end.".into(),
    }
}

fn floorplan() -> World {
    let mut blocks = room(-3.5, -5.0, 3.5, 1.0);
    blocks.push(Block::new(0.5, -5.0, 0.6, -3.3, CEILING));
    blocks.push(Block::new(0.5, -2.5, 0.6, -1.0, CEILING));
    blocks.push(Block::new(-3.5, -2.1, -2.2, -2.0, CEILING));
    blocks.push(Block::new(-1.3, -2.1, 0.5, -2.0, CEILING));
    blocks.push(Block::full(
        2.2,
        -4.6,
        3.3,
        -3.9,
        0.8,
        [201.0, 146.0, 61.0],
        None,
        None,
        "bed",
    ));
    blocks.push(Block::full(
        -3.2,
        -4.7,
        -2.0,
        -4.1,
        0.75,
        [66.0, 150.0, 145.0],
        None,
        None,
        "desk",
    ));
    blocks.push(Block::full(
        -3.1,
        0.0,
        -2.0,
        0.8,
        0.45,
        [167.0, 92.0, 64.0],
        None,
        None,
        "sofa",
    ));
    World {
        name: "floorplan".into(),
        blocks,
        start: (0.0, 0.0, 0.0),
        people: vec![],
        description: "Explore: coverage of reachable floor.".into(),
    }
}

fn seek() -> World {
    let mut blocks = room(-3.0, -8.0, 3.0, 1.0);
    blocks.push(Block::full(
        1.2,
        -2.6,
        2.4,
        -2.2,
        0.6,
        [66.0, 150.0, 145.0],
        None,
        None,
        "bench",
    ));
    let person = Person::new(
        vec![(0.0, -5.0), (1.8, -5.5), (1.2, -6.8), (-1.5, -6.2)],
        0.25,
    );
    World {
        name: "seek".into(),
        blocks,
        start: (0.0, 0.0, 0.0),
        people: vec![person],
        description: "Seek a walking person; standoff 0.8 m.".into(),
    }
}

pub fn world_names() -> Vec<&'static str> {
    vec!["popup", "doorway", "deadend", "floorplan", "seek"]
}

/// jetson/sim/worlds.py load(): exact "Unknown sim world" error text.
pub fn load(name: &str) -> Result<World, String> {
    let world = match name {
        "popup" => popup(),
        "doorway" => doorway(),
        "deadend" => deadend(),
        "floorplan" => floorplan(),
        "seek" => seek(),
        _ => {
            return Err(format!(
                "Unknown sim world {name:?}; choose one of {}",
                world_names().join(", ")
            ))
        }
    };
    Ok(world)
}

// ---------------------------------------------------------------------------
// Deterministic per-sim Gaussian noise (numpy default_rng equivalent).

struct SimRng(u64);

impl SimRng {
    fn next_u64(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }

    fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    fn normal(&mut self, sigma: f64) -> f64 {
        // Box-Muller; u1 kept away from 0.
        let u1 = self.uniform().max(f64::MIN_POSITIVE);
        let u2 = self.uniform();
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos() * sigma
    }
}

// ---------------------------------------------------------------------------
// RoverSim (jetson/sim/world.py)

struct SimInner {
    world: World,
    x: f64,
    z: f64,
    yaw: f64,
    v: f64,
    w: f64,
    last: f64,
    moving_s: f64,
    collisions: i64,
    travelled: f64,
    min_clearance: f64,
    trace: Vec<(f64, f64)>,
    appeared: Vec<Value>,
    seen: HashSet<(i64, i64)>,
}

/// Thread-safe simulated rover in a World. Call `apply` with every applied command.
pub struct RoverSim {
    inner: Mutex<SimInner>,
    rng: Mutex<SimRng>,
    pub calibration: Calibration,
    pub max_depth: f64,
    pub speed_mps: f64,
    pub turn_radps: f64,
}

impl RoverSim {
    pub fn new(world: World, speed_mps: Option<f64>, turn_radps: Option<f64>) -> Self {
        let width = 160usize;
        let height = 120usize;
        let fx = 135.0;
        let calibration = Calibration {
            width,
            height,
            fx,
            fy: fx,
            cx: (width - 1) as f64 / 2.0,
            cy: (height - 1) as f64 / 2.0,
        };
        let sim = Self {
            inner: Mutex::new(SimInner {
                world,
                x: 0.0,
                z: 0.0,
                yaw: 0.0,
                v: 0.0,
                w: 0.0,
                last: 0.0,
                moving_s: 0.0,
                collisions: 0,
                travelled: 0.0,
                min_clearance: f64::INFINITY,
                trace: Vec::new(),
                appeared: Vec::new(),
                seen: HashSet::new(),
            }),
            rng: Mutex::new(SimRng(0x9E3779B97F4A7C15)),
            calibration,
            max_depth: 5.0,
            speed_mps: speed_mps.unwrap_or(SPEED_MPS),
            turn_radps: turn_radps.unwrap_or(TURN_RADPS),
        };
        sim.reset();
        sim
    }

    pub fn reset(&self) {
        let mut s = self.inner.lock().unwrap();
        let (x, z, yaw) = s.world.start;
        s.x = x;
        s.z = z;
        s.yaw = yaw;
        s.v = 0.0;
        s.w = 0.0;
        s.last = now_monotonic();
        s.moving_s = 0.0;
        s.collisions = 0;
        s.travelled = 0.0;
        s.min_clearance = f64::INFINITY;
        s.trace = vec![(r3(s.x), r3(s.z))];
        for block in s.world.blocks.iter_mut() {
            block.visible = block.appear_within.is_none() && block.appear_after_s.is_none();
        }
        for person in s.world.people.iter_mut() {
            person.distance = 0.0;
        }
        s.appeared = Vec::new();
        s.seen = HashSet::new();
    }

    // -- dynamics -----------------------------------------------------------

    /// ControlState on_output hook: the command applied from now on.
    pub fn apply(&self, v: f64, w: f64, _blocked: bool) {
        let mut s = self.inner.lock().unwrap();
        self.advance_locked(&mut s, None);
        s.v = v;
        s.w = w;
    }

    pub fn advance(&self) {
        let mut s = self.inner.lock().unwrap();
        self.advance_locked(&mut s, None);
    }

    fn advance_locked(&self, s: &mut SimInner, now: Option<f64>) {
        let now = now.unwrap_or_else(now_monotonic);
        let dt = (now - s.last).max(0.0).min(MAX_DT);
        s.last = now;
        if dt <= 0.0 {
            return;
        }
        for person in s.world.people.iter_mut() {
            person.distance += person.speed * dt;
        }
        let steps = ((dt / 0.02).ceil() as i64).max(1);
        for _ in 0..steps {
            self.integrate(s, dt / steps as f64);
        }
        Self::update_popups(s);
    }

    fn integrate(&self, s: &mut SimInner, dt: f64) {
        let speed = s.v * self.speed_mps;
        // positive w turns right (clockwise)
        let yaw = s.yaw - s.w * self.turn_radps * dt;
        let mid = (s.yaw + yaw) / 2.0;
        let mut x = s.x - mid.sin() * speed * dt;
        let mut z = s.z - mid.cos() * speed * dt;
        if s.v.abs() > 1e-3 || s.w.abs() > 1e-3 {
            s.moving_s += dt;
        }
        let clearance = Self::clearance_inner(s, x, z);
        if clearance < RADIUS {
            let here = Self::clearance_inner(s, s.x, s.z);
            if here >= RADIUS || clearance < here {
                s.collisions += (speed.abs() > 1e-3) as i64;
                x = s.x;
                z = s.z; // wheels slip against the wall; rotation still applies
            }
        }
        s.travelled += (x - s.x).hypot(z - s.z);
        s.x = x;
        s.z = z;
        s.yaw = yaw.sin().atan2(yaw.cos());
        s.min_clearance = s.min_clearance.min(Self::clearance_inner(s, s.x, s.z));
        let last = *s.trace.last().unwrap();
        if (last.0 - s.x).hypot(last.1 - s.z) > 0.05 {
            s.trace.push((r3(s.x), r3(s.z)));
            if s.trace.len() > 4000 {
                let drop = s.trace.len() - 4000;
                s.trace.drain(0..drop);
            }
        }
    }

    fn update_popups(s: &mut SimInner) {
        for i in 0..s.world.blocks.len() {
            if s.world.blocks[i].visible {
                continue;
            }
            let (cx, cz) = s.world.blocks[i].centre();
            let dist = (s.x - cx).hypot(s.z - cz);
            let near = s.world.blocks[i]
                .appear_within
                .map(|w| dist <= w)
                .unwrap_or(false);
            let late = s.world.blocks[i]
                .appear_after_s
                .map(|a| s.moving_s >= a)
                .unwrap_or(false);
            if near || late {
                s.world.blocks[i].visible = true;
                s.appeared.push(json!({
                    "name": s.world.blocks[i].name,
                    "at_moving_s": (s.moving_s * 100.0).round_ties_even() / 100.0,
                    "rover": [r3(s.x), r3(s.z)],
                }));
            }
        }
    }

    /// Distance from (x, z) to the nearest visible block or person surface.
    fn clearance_inner(s: &SimInner, x: f64, z: f64) -> f64 {
        let mut best = f64::INFINITY;
        for block in &s.world.blocks {
            if block.visible {
                best = best.min(block.distance(x, z));
            }
        }
        for person in &s.world.people {
            let (px, pz) = person.position();
            best = best.min((px - x).hypot(pz - z) - person.radius);
        }
        best
    }

    // -- sensors ------------------------------------------------------------

    pub fn camera_pose(&self, yaw_offset: f64) -> Mat4 {
        let s = self.inner.lock().unwrap();
        let yaw = s.yaw + yaw_offset;
        let (c, sn) = (yaw.cos(), yaw.sin());
        [
            [c, 0.0, sn, s.x],
            [0.0, 1.0, 0.0, 0.0],
            [-sn, 0.0, c, s.z],
            [0.0, 0.0, 0.0, 1.0],
        ]
    }

    /// Depth (optical-axis metres, 0 = no return), RGB and the camera-to-world
    /// pose. Full port of RoverSim.render.
    pub fn render(&self, yaw_offset: f64) -> (Vec<f32>, Vec<u8>, Mat4) {
        let k = &self.calibration;
        let (pose, blocks, people, floor) = {
            let s = self.inner.lock().unwrap();
            let yaw = s.yaw + yaw_offset;
            let (c, sn) = (yaw.cos(), yaw.sin());
            let pose: Mat4 = [
                [c, 0.0, sn, s.x],
                [0.0, 1.0, 0.0, 0.0],
                [-sn, 0.0, c, s.z],
                [0.0, 0.0, 0.0, 1.0],
            ];
            let blocks: Vec<(f64, f64, f64, f64, f64, [f64; 3])> = s
                .world
                .blocks
                .iter()
                .filter(|b| b.visible)
                .map(|b| (b.x0, b.z0, b.x1, b.z1, b.height, b.color))
                .collect();
            let people: Vec<(f64, f64, f64, f64)> = s
                .world
                .people
                .iter()
                .map(|p| {
                    let (px, pz) = p.position();
                    (px, pz, p.radius, p.height)
                })
                .collect();
            (pose, blocks, people, -CAMERA_HEIGHT)
        };

        let (w, h) = (k.width, k.height);
        let mut depth = vec![0f32; w * h];
        let mut rgb = vec![0u8; w * h * 3];
        let mut rng = self.rng.lock().unwrap();
        let mut new_cells: Vec<(i64, i64)> = Vec::new();
        let (ox, oy, oz) = (pose[0][3], pose[1][3], pose[2][3]);
        // pose[:3,:3] row-major rotation R; world ray = R * local.
        let r = [
            [pose[0][0], pose[0][1], pose[0][2]],
            [pose[1][0], pose[1][1], pose[1][2]],
            [pose[2][0], pose[2][1], pose[2][2]],
        ];

        for v in 0..h {
            for u in 0..w {
                let lx = (u as f64 - k.cx) / k.fx;
                let ly = -(v as f64 - k.cy) / k.fy;
                let lz = -1.0;
                let rx = r[0][0] * lx + r[0][1] * ly + r[0][2] * lz;
                let ry = r[1][0] * lx + r[1][1] * ly + r[1][2] * lz;
                let rz = r[2][0] * lx + r[2][1] * ly + r[2][2] * lz;
                let safe = |a: f64| if a.abs() < 1e-9 { 1e-9 } else { a };
                let (sx, sy, sz) = (safe(rx), safe(ry), safe(rz));

                let mut nearest = f64::INFINITY;
                let mut color = [0.0f64; 3];
                let mut floor_t = f64::NAN;

                // Floor and ceiling planes.
                for (plane, tint) in [(floor, FLOOR_TINT), (floor + CEILING, CEILING_TINT)] {
                    let t = (plane - oy) / sy;
                    let ok = t > 0.05 && t < nearest;
                    if ok {
                        let hx = ox + rx * t;
                        let hz = oz + rz * t;
                        let checker =
                            ((hx * 2.0).floor() + (hz * 2.0).floor()).rem_euclid(2.0) * 18.0;
                        nearest = t;
                        color = [tint[0] + checker, tint[1] + checker, tint[2] + checker];
                    }
                    if plane == floor {
                        floor_t = if ok { t } else { f64::NAN };
                    }
                }
                // Boxes (slab test).
                if !blocks.is_empty() {
                    let mut best_entry = f64::INFINITY;
                    let mut best_index = usize::MAX;
                    for (i, &(x0, z0, x1, z1, height, _)) in blocks.iter().enumerate() {
                        let lo = [x0, floor, z0];
                        let hi = [x1, floor + height, z1];
                        let first = [(lo[0] - ox) / sx, (lo[1] - oy) / sy, (lo[2] - oz) / sz];
                        let last = [(hi[0] - ox) / sx, (hi[1] - oy) / sy, (hi[2] - oz) / sz];
                        let entry = first[0]
                            .min(last[0])
                            .max(first[1].min(last[1]))
                            .max(first[2].min(last[2]));
                        let exit = first[0]
                            .max(last[0])
                            .min(first[1].max(last[1]))
                            .min(first[2].max(last[2]));
                        let entry = if entry > 0.05 && entry <= exit {
                            entry
                        } else {
                            f64::INFINITY
                        };
                        if entry < best_entry {
                            best_entry = entry;
                            best_index = i;
                        }
                    }
                    let t = best_entry;
                    if t < nearest {
                        let hx = ox + rx * t;
                        let hy = oy + ry * t;
                        let hz = oz + rz * t;
                        let (hx, hy, hz) = if hx.is_finite() && hy.is_finite() && hz.is_finite() {
                            (hx, hy, hz)
                        } else {
                            (0.0, 0.0, 0.0)
                        };
                        let band = if ((hy - floor) * 4.0).rem_euclid(1.0) < 0.08 {
                            30.0
                        } else {
                            0.0
                        } + ((hx * 3.0).floor() + (hz * 3.0).floor()).rem_euclid(2.0)
                            * 8.0;
                        nearest = t;
                        let c = blocks[best_index].5;
                        color = [c[0] + band, c[1] + band, c[2] + band];
                    }
                }
                // People: capped cylinders solved in XZ.
                for &(px, pz, radius, height) in &people {
                    let (oxx, ozz) = (ox - px, oz - pz);
                    let a = rx * rx + rz * rz;
                    let b = 2.0 * (oxx * rx + ozz * rz);
                    let cc = oxx * oxx + ozz * ozz - radius * radius;
                    let disc = b * b - 4.0 * a * cc;
                    let t = if disc >= 0.0 {
                        (-b - disc.max(0.0).sqrt()) / (2.0 * a.max(1e-12))
                    } else {
                        f64::INFINITY
                    };
                    let y = oy + ry * t;
                    if t > 0.05 && t < nearest && y >= floor && y <= floor + height {
                        nearest = t;
                        color = PERSON_COLOR;
                    }
                }

                if floor_t == nearest && nearest <= self.max_depth {
                    let px = ox + rx * nearest;
                    let pz = oz + rz * nearest;
                    new_cells.push((
                        (px / COVERAGE_CELL).floor() as i64,
                        (pz / COVERAGE_CELL).floor() as i64,
                    ));
                }
                let d = if nearest <= self.max_depth {
                    nearest
                } else {
                    0.0
                };
                let d = if d > 0.0 { d + rng.normal(0.004) } else { 0.0 };
                let i = v * w + u;
                depth[i] = d as f32;
                rgb[i * 3] = color[0].clamp(0.0, 255.0) as u8;
                rgb[i * 3 + 1] = color[1].clamp(0.0, 255.0) as u8;
                rgb[i * 3 + 2] = color[2].clamp(0.0, 255.0) as u8;
            }
        }
        drop(rng);
        if !new_cells.is_empty() {
            let mut s = self.inner.lock().unwrap();
            s.seen.extend(new_cells);
        }
        (depth, rgb, pose)
    }

    /// Forward HC-SR04 range; None without an echo. The Uno's 12 ms echo
    /// timeout ends at ~206 cm, so open space reads as no echo, which the
    /// proximity stop (when on) treats as unknown.
    pub fn ultrasonic_cm(&self, max_cm: f64) -> Option<i64> {
        let s = self.inner.lock().unwrap();
        let mut best = f64::INFINITY;
        for spread in [-0.13f64, 0.0, 0.13] {
            let yaw = s.yaw + spread;
            let (dx, dz) = (-yaw.sin(), -yaw.cos());
            let mut distance = 0.05;
            while distance < max_cm / 100.0 {
                if Self::clearance_inner(&s, s.x + dx * distance, s.z + dz * distance) <= 0.0 {
                    best = best.min(distance);
                    break;
                }
                distance += 0.03;
            }
        }
        if best * 100.0 <= max_cm {
            Some((best * 100.0).round_ties_even() as i64)
        } else {
            None
        }
    }

    /// Ground-truth normalized boxes (tracker-style int ids) for people visible
    /// in `depth`. Port of RoverSim.person_boxes.
    pub fn person_boxes(&self, depth: &[f32], pose: &Mat4) -> Vec<Value> {
        let k = &self.calibration;
        let mut boxes = Vec::new();
        let floor = -CAMERA_HEIGHT;
        // Rigid inverse: R^T, -R^T t.
        let r = [
            [pose[0][0], pose[0][1], pose[0][2]],
            [pose[1][0], pose[1][1], pose[1][2]],
            [pose[2][0], pose[2][1], pose[2][2]],
        ];
        let t = [pose[0][3], pose[1][3], pose[2][3]];
        let to_camera = |p: [f64; 3]| -> [f64; 3] {
            let d = [p[0] - t[0], p[1] - t[1], p[2] - t[2]];
            [
                r[0][0] * d[0] + r[1][0] * d[1] + r[2][0] * d[2],
                r[0][1] * d[0] + r[1][1] * d[1] + r[2][1] * d[2],
                r[0][2] * d[0] + r[1][2] * d[1] + r[2][2] * d[2],
            ]
        };
        let people: Vec<(f64, f64, f64, f64)> = {
            let s = self.inner.lock().unwrap();
            s.world
                .people
                .iter()
                .map(|p| {
                    let (px, pz) = p.position();
                    (px, pz, p.radius, p.height)
                })
                .collect()
        };
        for (index, (px, pz, radius, height)) in people.iter().enumerate() {
            let centre = to_camera([*px, floor + height / 2.0, *pz]);
            if centre[2] >= -0.3 {
                continue; // behind or at the camera
            }
            let distance = -centre[2];
            let mut us = Vec::new();
            let mut vs = Vec::new();
            for y in [floor, floor + height] {
                let point = to_camera([*px, y, *pz]);
                for offset in [-radius, *radius] {
                    us.push(k.cx + k.fx * (point[0] + offset) / -point[2]);
                    vs.push(k.cy - k.fy * point[1] / -point[2]);
                }
            }
            let (w1, h1) = (k.width as f64 - 1.0, k.height as f64 - 1.0);
            let x0 = us
                .iter()
                .cloned()
                .fold(f64::INFINITY, f64::min)
                .clamp(0.0, w1);
            let x1 = us
                .iter()
                .cloned()
                .fold(f64::NEG_INFINITY, f64::max)
                .clamp(0.0, w1);
            let y0 = vs
                .iter()
                .cloned()
                .fold(f64::INFINITY, f64::min)
                .clamp(0.0, h1);
            let y1 = vs
                .iter()
                .cloned()
                .fold(f64::NEG_INFINITY, f64::max)
                .clamp(0.0, h1);
            if x1 - x0 < 2.0 || y1 - y0 < 4.0 {
                continue;
            }
            let u = ((x0 + x1) / 2.0) as usize;
            let v = (((y0 + y1) / 2.0).clamp(0.0, h1)) as usize;
            let seen = depth[v * k.width + u] as f64;
            if seen <= 0.0 || seen < distance - radius - 0.15 {
                continue; // occluded by a wall or box
            }
            boxes.push(json!({
                "id": index,
                "cls": "person",
                "class_id": 0,
                "conf": 0.99,
                "simulated": true,
                "x": x0 / k.width as f64,
                "y": y0 / k.height as f64,
                "w": (x1 - x0) / k.width as f64,
                "h": (y1 - y0) / k.height as f64,
            }));
        }
        boxes
    }

    /// Floor cells the chassis can reach from the start among the walls
    /// visible at start (BFS, static blocks only).
    fn reachable_cells(&self) -> HashSet<(i64, i64)> {
        let s = self.inner.lock().unwrap();
        let size = COVERAGE_CELL;
        let statik: Vec<&Block> = s
            .world
            .blocks
            .iter()
            .filter(|b| b.appear_within.is_none() && b.appear_after_s.is_none())
            .collect();
        if statik.is_empty() {
            return HashSet::new();
        }
        let x0 = statik.iter().map(|b| b.x0).fold(f64::INFINITY, f64::min);
        let z0 = statik.iter().map(|b| b.z0).fold(f64::INFINITY, f64::min);
        let x1 = statik
            .iter()
            .map(|b| b.x1)
            .fold(f64::NEG_INFINITY, f64::max);
        let z1 = statik
            .iter()
            .map(|b| b.z1)
            .fold(f64::NEG_INFINITY, f64::max);
        let free = |cell: (i64, i64)| -> bool {
            let x = (cell.0 as f64 + 0.5) * size;
            let z = (cell.1 as f64 + 0.5) * size;
            statik.iter().all(|b| b.distance(x, z) >= RADIUS)
        };
        let start = (
            (s.world.start.0 / size).floor() as i64,
            (s.world.start.1 / size).floor() as i64,
        );
        let mut seen = HashSet::from([start]);
        let mut frontier = VecDeque::from([start]);
        while let Some((cx, cz)) = frontier.pop_back() {
            for (nx, nz) in [(cx + 1, cz), (cx - 1, cz), (cx, cz + 1), (cx, cz - 1)] {
                if !seen.contains(&(nx, nz))
                    && x0 <= nx as f64 * size
                    && nx as f64 * size <= x1
                    && z0 <= nz as f64 * size
                    && nz as f64 * size <= z1
                    && free((nx, nz))
                {
                    seen.insert((nx, nz));
                    frontier.push_back((nx, nz));
                }
            }
        }
        seen
    }

    pub fn coverage(&self) -> f64 {
        let reachable = self.reachable_cells();
        let s = self.inner.lock().unwrap();
        let covered = s.seen.intersection(&reachable).count();
        covered as f64 / reachable.len().max(1) as f64
    }

    /// Ground truth for scenario tests: pose, collisions, clearance, coverage, pop-ups.
    pub fn state(&self) -> Value {
        let (
            world,
            x,
            z,
            yaw,
            v,
            w,
            collisions,
            travelled,
            min_clearance,
            moving_s,
            appeared,
            people,
            obstacles,
            trace,
        ) = {
            let s = self.inner.lock().unwrap();
            let people: Vec<Value> = s
                .world
                .people
                .iter()
                .map(|p| {
                    let (x, z) = p.position();
                    json!([r3(x), r3(z)])
                })
                .collect();
            let obstacles: Vec<Value> = s
                .world
                .blocks
                .iter()
                .filter(|b| !b.name.is_empty())
                .map(|b| {
                    json!({
                        "name": b.name,
                        "visible": b.visible,
                        "box": [b.x0, b.z0, b.x1, b.z1],
                        "height": b.height,
                    })
                })
                .collect();
            let trace: Vec<Value> = s
                .trace
                .iter()
                .rev()
                .take(400)
                .rev()
                .map(|(x, z)| json!([x, z]))
                .collect();
            (
                s.world.name.clone(),
                s.x,
                s.z,
                s.yaw,
                s.v,
                s.w,
                s.collisions,
                s.travelled,
                s.min_clearance,
                s.moving_s,
                s.appeared.clone(),
                people,
                obstacles,
                trace,
            )
        };
        json!({
            "world": world,
            "x": r3(x),
            "z": r3(z),
            "yaw_deg": (yaw.to_degrees() * 10.0).round_ties_even() / 10.0,
            "v": v,
            "w": w,
            "collisions": collisions,
            "travelled_m": r3(travelled),
            "min_clearance_m": if min_clearance.is_infinite() { Value::Null } else { json!(r3(min_clearance)) },
            "moving_s": (moving_s * 100.0).round_ties_even() / 100.0,
            "appeared": appeared,
            "people": people,
            "obstacles": obstacles,
            "trace": trace,
            "speed_mps": self.speed_mps,
            "turn_radps": self.turn_radps,
            "camera_height_m": CAMERA_HEIGHT,
            "coverage": (self.coverage() * 1000.0).round_ties_even() / 1000.0,
        })
    }

    /// cv2 resize-to-320x240 + imencode equivalent (same as demo.rs).
    pub fn jpeg(&self, rgb: &[u8]) -> Vec<u8> {
        let (w, h) = (self.calibration.width, self.calibration.height);
        let img =
            image::RgbImage::from_raw(w as u32, h as u32, rgb.to_vec()).expect("sim RGB buffer");
        let preview =
            image::imageops::resize(&img, 320, 240, image::imageops::FilterType::Triangle);
        let mut jpeg = Vec::new();
        let encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg, 95);
        image::ImageEncoder::write_image(
            encoder,
            &preview,
            320,
            240,
            image::ExtendedColorType::Rgb8,
        )
        .expect("sim JPEG encode");
        jpeg
    }
}

// ---------------------------------------------------------------------------
// SimFeed (jetson/sim/stream.py): the sim's ground-truth detection source and
// the frame/pose producer wired into MapState.

pub struct SimFeed {
    pub sim: RoverSim,
    pub identity: crate::phone::Identity,
    pub started: f64,
    frame_id: AtomicI64,
    boxes: Mutex<(Vec<Value>, Option<f64>)>,
}

impl SimFeed {
    pub fn new(sim: RoverSim) -> Self {
        Self {
            sim,
            identity: ("simulated-room".into(), 1),
            started: now_monotonic(),
            frame_id: AtomicI64::new(0),
            boxes: Mutex::new((Vec::new(), None)),
        }
    }

    pub fn header(&self, now: f64, pose: &Mat4) -> Value {
        let frame_id = self.frame_id.fetch_add(1, Ordering::SeqCst) + 1;
        let wall_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        json!({
            "version": 1,
            "type": "frame",
            "session_id": self.identity.0,
            "map_epoch": self.identity.1,
            "frame_id": frame_id,
            "t_capture": now - self.started,
            "t_wall_ms": wall_ms,
            "transform": flatten_f(pose).to_vec(),
            "tracking": "normal",
        })
    }

    /// Render one RGB-D frame; refreshes ground-truth detections too.
    pub fn frame(&self, now: f64) -> RgbdFrame {
        let (depth, rgb, pose) = self.sim.render(0.0);
        let boxes = self.sim.person_boxes(&depth, &pose);
        *self.boxes.lock().unwrap() = (boxes, Some(now_monotonic()));
        let confidence: Vec<u8> = depth.iter().map(|d| if *d > 0.0 { 2 } else { 0 }).collect();
        let mut header = self.header(now, &pose);
        header
            .as_object_mut()
            .unwrap()
            .insert("_received_monotonic".into(), json!(now));
        let jpeg = self.sim.jpeg(&rgb);
        RgbdFrame {
            header,
            rgb,
            depth,
            confidence,
            calibration: self.sim.calibration,
            transform: pose,
            jpeg,
            depth_width: self.sim.calibration.width,
            depth_height: self.sim.calibration.height,
        }
    }

    /// The perception.snapshot() shape autonomy reads, from ground truth.
    pub fn detections(&self) -> Value {
        let (boxes, boxes_at) = self.boxes.lock().unwrap().clone();
        // Python round() returns an int: age_ms must be a JSON integer.
        let age = boxes_at.map(|at| ((now_monotonic() - at) * 1000.0).round_ties_even() as i64);
        let fresh = age.map(|a| a < 1000).unwrap_or(false);
        json!({
            "boxes": if fresh { boxes } else { Vec::new() },
            "age_ms": age,
            "fresh": fresh,
            "error": Value::Null,
            "backend": "simulated",
        })
    }
}

/// jetson/sim/stream.py stream_world: poses at 30 Hz, RGB-D frames at 10 Hz
/// with one fusion in flight; the frame carries its own pose.
pub fn spawn_world_stream(
    mapping: Arc<MapState>,
    camera: Arc<crate::camera::CameraService>,
    feed: Arc<SimFeed>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let busy = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let mut next_frame = 0.0f64;
        loop {
            let now = now_monotonic();
            feed.sim.advance();
            if !busy.load(Ordering::SeqCst) && now >= next_frame {
                let frame = feed.frame(now);
                let frame_id = frame.header["frame_id"].as_i64().unwrap_or(-1);
                camera.set_phone_rgb(Some(CameraFrame {
                    jpeg: frame.jpeg.clone(),
                    received: now,
                    frame_id,
                    capture_verified: false,
                    capture_binding: None,
                    tracking: None,
                    mirrored: false,
                    socket_received: None,
                    published: None,
                    source: "SIMULATED".into(),
                }));
                let (mapping, busy) = (mapping.clone(), busy.clone());
                busy.store(true, Ordering::SeqCst);
                next_frame = now + 1.0 / FRAME_HZ;
                std::thread::spawn(move || {
                    let _ = mapping.process(&frame);
                    busy.store(false, Ordering::SeqCst);
                });
            } else {
                let pose = feed.sim.camera_pose(0.0);
                let header = feed.header(now, &pose);
                let _ = mapping.observe_pose(&header, &pose);
            }
            let elapsed = now_monotonic() - now;
            let wait = 1.0 / POSE_HZ - elapsed;
            if wait > 0.0 {
                std::thread::sleep(Duration::from_secs_f64(wait));
            }
        }
    })
}
