//! Port of jetson/tracker.py — dependency-free multi-object tracker giving
//! detections stable integer ids. Boxes are normalized {cls,x,y,w,h}.

use serde_json::{json, Value};

pub const IOU_MATCH: f64 = 0.2;
pub const CENTROID_LIMIT: f64 = 1.0;

pub fn iou(a: &Value, b: &Value) -> f64 {
    let f = |v: &Value| v.as_f64().unwrap_or(0.0);
    let x1 = f(&a["x"]).max(f(&b["x"]));
    let y1 = f(&a["y"]).max(f(&b["y"]));
    let x2 = (f(&a["x"]) + f(&a["w"])).min(f(&b["x"]) + f(&b["w"]));
    let y2 = (f(&a["y"]) + f(&a["h"])).min(f(&b["y"]) + f(&b["h"]));
    let intersection = (x2 - x1).max(0.0) * (y2 - y1).max(0.0);
    intersection / (f(&a["w"]) * f(&a["h"]) + f(&b["w"]) * f(&b["h"]) - intersection).max(1e-9)
}

fn center(box_: &Value) -> (f64, f64) {
    let f = |v: &Value| v.as_f64().unwrap_or(0.0);
    (f(&box_["x"]) + f(&box_["w"]) / 2.0, f(&box_["y"]) + f(&box_["h"]) / 2.0)
}

struct Track {
    id: i64,
    box_: Value,
    seen: f64,
    misses: i64,
    velocity: (f64, f64),
}

impl Track {
    fn predicted(&self) -> Value {
        let steps = (self.misses + 1) as f64;
        let mut b = self.box_.clone();
        b["x"] = json!(self.box_["x"].as_f64().unwrap_or(0.0) + self.velocity.0 * steps);
        b["y"] = json!(self.box_["y"].as_f64().unwrap_or(0.0) + self.velocity.1 * steps);
        b
    }

    fn update(&mut self, box_: Value, now: f64) {
        let steps = (self.misses + 1) as f64;
        let (ox, oy) = center(&self.box_);
        let (nx, ny) = center(&box_);
        let measured = ((nx - ox) / steps, (ny - oy) / steps);
        self.velocity = (0.5 * self.velocity.0 + 0.5 * measured.0, 0.5 * self.velocity.1 + 0.5 * measured.1);
        self.box_ = box_;
        self.seen = now;
        self.misses = 0;
    }
}

pub struct Tracker {
    max_age: i64,
    max_age_s: f64,
    tracks: Vec<Track>,
    next_id: i64,
}

impl Tracker {
    pub fn new() -> Self {
        Self { max_age: 5, max_age_s: 2.0, tracks: Vec::new(), next_id: 1 }
    }

    /// Assign `id` to each detection in place and return the boxes.
    /// Call once per detection frame.
    pub fn update(&mut self, boxes: &mut [Value], now: f64) -> Vec<Value> {
        self.tracks.retain(|t| now - t.seen < self.max_age_s);
        let mut pairs: Vec<(f64, usize, usize)> = Vec::new();
        for (d, b) in boxes.iter().enumerate() {
            for (t, track) in self.tracks.iter().enumerate() {
                if track.box_["cls"] != b["cls"] {
                    continue;
                }
                let guess = track.predicted();
                let overlap = iou(b, &guess);
                if overlap >= IOU_MATCH {
                    pairs.push((1.0 + overlap, d, t));
                    continue;
                }
                let (gx, gy) = center(&guess);
                let (bx, by) = center(b);
                let limit = CENTROID_LIMIT
                    * track.box_["w"].as_f64().unwrap_or(0.0).max(track.box_["h"].as_f64().unwrap_or(0.0));
                let distance = ((gx - bx).powi(2) + (gy - by).powi(2)).sqrt();
                if distance < limit {
                    pairs.push((1.0 - distance / limit, d, t));
                }
            }
        }
        pairs.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        let mut used: Vec<bool> = vec![false; self.tracks.len()];
        let mut matched_map: Vec<Option<usize>> = vec![None; boxes.len()];
        for (_, d, t) in pairs {
            if matched_map[d].is_none() && !used[t] {
                matched_map[d] = Some(t);
                used[t] = true;
            }
        }
        for (t, track) in self.tracks.iter_mut().enumerate() {
            if !used[t] {
                track.misses += 1;
            }
        }
        for (d, b) in boxes.iter_mut().enumerate() {
            let id = if let Some(t) = matched_map[d] {
                let track = &mut self.tracks[t];
                track.update(b.clone(), now);
                track.id
            } else {
                let id = self.next_id;
                self.next_id += 1;
                self.tracks.push(Track { id, box_: b.clone(), seen: now, misses: 0, velocity: (0.0, 0.0) });
                id
            };
            b["id"] = json!(id);
        }
        self.tracks.retain(|t| t.misses <= self.max_age);
        boxes.to_vec()
    }
}
