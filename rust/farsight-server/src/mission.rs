//! Bounded live mission evidence, persisted when a directory is set.
//! Port of jetson/mission.py.

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::Mutex;

use serde_json::{json, Value};

use crate::camera::CameraFrame;
use crate::state::now_monotonic;

fn epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

struct Inner {
    events: VecDeque<Value>,
    encounters: indexmap::IndexMap<String, Value>,
    triage: indexmap::IndexMap<String, Value>,
    drill_seen: std::collections::HashSet<String>,
    saved_snapshots: u64,
    record_frames: u64,
}

type OnPerson = Box<dyn Fn(&str, &Value, &[u8], &Value) + Send + Sync>;

pub struct Mission {
    pub started: f64,
    pub id: String,
    pub directory: Option<PathBuf>,
    pub recording: bool,
    pub record_started: f64,
    inner: Mutex<Inner>,
    on_person: Mutex<Option<OnPerson>>,
    pub drill_score: crate::drill::Drill,
}

impl Mission {
    pub fn new(directory: Option<PathBuf>, record: bool) -> Result<Self, String> {
        let id = chrono::Utc::now().format("%Y%m%dT%H%M%S").to_string();
        let directory = directory.map(|d| d.join(&id));
        if let Some(d) = &directory {
            let _ = std::fs::create_dir_all(d);
        }
        if record && directory.is_none() {
            return Err("Recording requires --mission-dir".into());
        }
        if record {
            let _ = std::fs::create_dir_all(directory.as_ref().unwrap().join("frames"));
        }
        let mission = Self {
            started: epoch(),
            id,
            directory,
            recording: record,
            record_started: now_monotonic(),
            inner: Mutex::new(Inner {
                events: VecDeque::new(),
                encounters: indexmap::IndexMap::new(),
                triage: indexmap::IndexMap::new(),
                drill_seen: std::collections::HashSet::new(),
                saved_snapshots: 0,
                record_frames: 0,
            }),
            on_person: Mutex::new(None),
            drill_score: crate::drill::Drill::new(),
        };
        if record {
            mission.finish_recording(Some(0.0));
        }
        Ok(mission)
    }

    /// Append replay evidence to the mission directory, without control tokens.
    pub fn record(&self, kind: &str, packet: &Value, frame: Option<&CameraFrame>) {
        if !self.recording {
            return;
        }
        let mut inner = self.inner.lock().unwrap();
        let mut event = json!({
            "kind": kind,
            "t": now_monotonic() - self.record_started,
            "ts": epoch(),
        });
        if kind == "frame" {
            if let (Some(f), Some(dir)) = (frame, &self.directory) {
                let name = format!("frames/{:08}.jpg", inner.record_frames);
                let _ = std::fs::write(dir.join(&name), &f.jpeg);
                inner.record_frames += 1;
                event["file"] = json!(name);
                event["frame"] = json!(f.frame_id);
                event["source"] = json!(f.source);
            } else {
                return;
            }
        } else {
            let mut stripped = packet.as_object().cloned().unwrap_or_default();
            for key in ["permit", "session_id", "received"] {
                stripped.remove(key);
            }
            event["packet"] = Value::Object(stripped);
        }
        if let Some(dir) = &self.directory {
            if let Ok(mut log) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("replay.jsonl")) {
                use std::io::Write;
                let _ = writeln!(log, "{}", serde_json::to_string(&event).unwrap());
            }
        }
    }

    pub fn finish_recording(&self, elapsed: Option<f64>) {
        if self.recording {
            if let Some(dir) = &self.directory {
                let duration = elapsed.unwrap_or_else(|| now_monotonic() - self.record_started);
                let _ = std::fs::write(
                    dir.join("replay.json"),
                    serde_json::to_string(&json!({"version": 1, "duration_s": duration})).unwrap(),
                );
            }
        }
    }

    /// Called as on_person(key, encounter, jpeg, snapshot_url) for each new
    /// person track (e.g. the triage worker).
    pub fn set_on_person(&self, callback: OnPerson) {
        *self.on_person.lock().unwrap() = Some(callback);
    }

    pub fn detect(&self, packet: &Value, frame: &CameraFrame) {
        self.record("detections", packet, None);
        let mut inner = self.inner.lock().unwrap();
        if let Some(boxes) = packet["boxes"].as_array() {
            self.drill_score.observe(boxes);
        }
        let drilling = self.drill_score.running();
        let mut new_persons: Vec<(String, Value, Value)> = Vec::new();
        let Some(boxes) = packet["boxes"].as_array() else { return };
        for boxv in boxes {
            let key = format!("{}:{}", frame.source, boxv["id"]);
            let now = epoch();
            let mut encounter = boxv.clone();
            let first_seen = inner
                .encounters
                .get(&key)
                .and_then(|p| p["first_seen"].as_f64())
                .unwrap_or(now);
            encounter["source"] = json!(frame.source);
            encounter["last_seen"] = json!(now);
            encounter["first_seen"] = json!(first_seen);
            let is_new = !inner.encounters.contains_key(&key);
            inner.encounters.insert(key.clone(), encounter);
            if drilling && boxv["cls"].as_str() == Some("person") {
                inner.drill_seen.insert(key.clone());
            }
            if !is_new {
                continue;
            }
            let image_name = format!("detection-{}-{}.jpg", inner.saved_snapshots, frame.frame_id);
            let save = self.directory.is_some() && inner.saved_snapshots < 1000;
            let event = json!({
                "kind": "detection",
                "ts": now,
                "frame": frame.frame_id,
                "source": frame.source,
                "box": boxv,
                "snapshot": if save { Value::from(image_name.clone()) } else { Value::Null },
                "snapshot_url": if save { Value::from(format!("/mission/snapshots/{image_name}")) } else { Value::Null },
            });
            inner.events.push_back(event.clone());
            while inner.events.len() > 1000 {
                inner.events.pop_front();
            }
            if save {
                if let Some(dir) = &self.directory {
                    let _ = std::fs::write(dir.join(&image_name), &frame.jpeg);
                    inner.saved_snapshots += 1;
                    if let Ok(mut log) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("events.jsonl")) {
                        use std::io::Write;
                        let _ = writeln!(log, "{}", serde_json::to_string(&event).unwrap());
                    }
                }
            }
            if boxv["cls"].as_str() == Some("person") {
                if let Some(stored) = inner.encounters.get(&key).cloned() {
                    new_persons.push((key, stored, event["snapshot_url"].clone()));
                }
            }
        }
        while inner.encounters.len() > 500 {
            inner.encounters.shift_remove_index(0);
        }
        drop(inner);
        // Callbacks re-enter the mission (set_sbar); they must run unlocked.
        if !new_persons.is_empty() {
            let cb = self.on_person.lock().unwrap();
            if let Some(cb) = cb.as_ref() {
                for (key, encounter, snapshot_url) in new_persons {
                    cb(&key, &encounter, &frame.jpeg, &snapshot_url);
                }
            }
        }
    }

    pub fn encounter(&self, key: &str) -> Option<Value> {
        self.inner.lock().unwrap().encounters.get(key).cloned()
    }

    /// Store a person's SBAR and rewrite the persisted report so it always
    /// carries one.
    pub fn set_sbar(&self, key: &str, sbar: Value) {
        let mut inner = self.inner.lock().unwrap();
        inner.triage.insert(key.to_string(), sbar.clone());
        while inner.triage.len() > 500 {
            inner.triage.shift_remove_index(0);
        }
        if let Some(dir) = &self.directory {
            let mut record = sbar.as_object().cloned().unwrap_or_default();
            let mut entry = serde_json::Map::new();
            entry.insert("key".into(), json!(key));
            entry.append(&mut record);
            if let Ok(mut log) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("triage.jsonl")) {
                use std::io::Write;
                let _ = writeln!(log, "{}", serde_json::to_string(&Value::Object(entry)).unwrap());
            }
            let report = self.report_locked(&self.id.clone(), self.started, self.directory.is_some(), &inner);
            let temporary = dir.join("report.json.tmp");
            if std::fs::write(&temporary, serde_json::to_string(&report).unwrap()).is_ok() {
                let _ = std::fs::rename(&temporary, dir.join("report.json"));
            }
        }
    }

    fn drill_locked(&self, inner: &Inner) -> Value {
        let mut status = self.drill_score.status();
        status["person_encounters"] = json!(inner.drill_seen.len());
        status["note"] = json!("Victims are faces held in view for 1 s; the route home is dead reckoning and approximate");
        status
    }

    pub fn start_drill(&self, duration: f64, victims: i64) -> Value {
        let mut inner = self.inner.lock().unwrap();
        inner.drill_seen.clear();
        drop(inner);
        self.drill_score.start(duration, victims);
        self.drill()
    }

    /// ControlState.on_output hook: the applied command feeds the route home
    /// and e-stop count.
    pub fn drill_motion(&self, v: f64, w: f64, blocked: bool) {
        self.drill_score.motion(v, w, blocked);
    }

    pub fn drill(&self) -> Value {
        let inner = self.inner.lock().unwrap();
        self.drill_locked(&inner)
    }

    fn report_locked(&self, id: &str, started: f64, persisted: bool, inner: &Inner) -> Value {
        json!({
            "mission_id": id,
            "started_at": started,
            "duration_s": (epoch() - started).round() as i64,
            "persisted": persisted,
            "events": inner.events.iter().cloned().collect::<Vec<_>>(),
            "encounters": inner.encounters.iter().map(|(key, entry)| {
                let mut e = entry.as_object().cloned().unwrap_or_default();
                e.insert("key".into(), json!(key));
                e.insert("sbar".into(), inner.triage.get(key).cloned().unwrap_or(Value::Null));
                Value::Object(e)
            }).collect::<Vec<_>>(),
            "triage": serde_json::Map::from_iter(
                inner.triage.iter().map(|(k, v)| (k.clone(), v.clone()))
            ),
            "saved_snapshots": inner.saved_snapshots,
            "snapshot_limit": 1000,
            "drill": self.drill_locked(inner),
        })
    }

    pub fn report(&self) -> Value {
        let inner = self.inner.lock().unwrap();
        self.report_locked(&self.id, self.started, self.directory.is_some(), &inner)
    }

    /// Serve a persisted detection snapshot; enforces the same name
    /// constraints as the Python route.
    pub fn snapshot_file(&self, name: &str) -> Option<Vec<u8>> {
        let dir = self.directory.as_ref()?;
        if name.contains('/') || name.contains('\\') || !name.starts_with("detection-") || !name.ends_with(".jpg") {
            return None;
        }
        let path = dir.join(name);
        if path.is_file() {
            std::fs::read(path).ok()
        } else {
            None
        }
    }
}
