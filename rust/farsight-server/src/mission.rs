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
    drill_start: Option<f64>,
    drill_duration: f64,
    drill_seen: std::collections::HashSet<String>,
    saved_snapshots: u64,
}

type OnPerson = Box<dyn Fn(&str, &Value, &[u8], &Value) + Send + Sync>;

pub struct Mission {
    pub started: f64,
    pub id: String,
    pub directory: Option<PathBuf>,
    inner: Mutex<Inner>,
    on_person: Mutex<Option<OnPerson>>,
}

impl Mission {
    pub fn new(directory: Option<PathBuf>) -> Self {
        let id = chrono::Utc::now().format("%Y%m%dT%H%M%S").to_string();
        let directory = directory.map(|d| d.join(&id));
        if let Some(d) = &directory {
            let _ = std::fs::create_dir_all(d);
        }
        Self {
            started: epoch(),
            id,
            directory,
            inner: Mutex::new(Inner {
                events: VecDeque::new(),
                encounters: indexmap::IndexMap::new(),
                triage: indexmap::IndexMap::new(),
                drill_start: None,
                drill_duration: 90.0,
                drill_seen: std::collections::HashSet::new(),
                saved_snapshots: 0,
            }),
            on_person: Mutex::new(None),
        }
    }

    /// Called as on_person(key, encounter, jpeg, snapshot_url) for each new
    /// person track (e.g. the triage worker).
    pub fn set_on_person(&self, callback: OnPerson) {
        *self.on_person.lock().unwrap() = Some(callback);
    }

    pub fn detect(&self, packet: &Value, frame: &CameraFrame) {
        let mut inner = self.inner.lock().unwrap();
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
            if let Some(ds) = inner.drill_start {
                if now_monotonic() - ds <= inner.drill_duration && boxv["cls"].as_str() == Some("person") {
                    inner.drill_seen.insert(key.clone());
                }
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
            let report = Self::report_locked(&self.id, self.started, self.directory.is_some(), &inner);
            let temporary = dir.join("report.json.tmp");
            if std::fs::write(&temporary, serde_json::to_string(&report).unwrap()).is_ok() {
                let _ = std::fs::rename(&temporary, dir.join("report.json"));
            }
        }
    }

    fn drill_locked(inner: &Inner) -> Value {
        let remaining = inner
            .drill_start
            .map(|ds| (inner.drill_duration - (now_monotonic() - ds)).max(0.0));
        json!({
            "active": remaining.map(|r| r > 0.0).unwrap_or(false),
            "remaining_s": remaining.map(|r| (r * 10.0).round() / 10.0),
            "person_encounters": inner.drill_seen.len(),
            "duration_s": inner.drill_duration,
            "note": "Tracks are encounters, not verified distinct people",
        })
    }

    pub fn start_drill(&self, duration: f64) -> Value {
        let mut inner = self.inner.lock().unwrap();
        inner.drill_start = Some(now_monotonic());
        inner.drill_duration = duration;
        inner.drill_seen.clear();
        Self::drill_locked(&inner)
    }

    pub fn drill(&self) -> Value {
        Self::drill_locked(&self.inner.lock().unwrap())
    }

    fn report_locked(id: &str, started: f64, persisted: bool, inner: &Inner) -> Value {
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
            "drill": Self::drill_locked(inner),
        })
    }

    pub fn report(&self) -> Value {
        let inner = self.inner.lock().unwrap();
        Self::report_locked(&self.id, self.started, self.directory.is_some(), &inner)
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
