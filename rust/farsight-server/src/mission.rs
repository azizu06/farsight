//! Bounded live mission evidence, persisted when a directory is set.
//! Port of jetson/mission.py.

use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::Mutex;

use serde_json::{json, Map, Value};

use crate::camera::CameraFrame;
use crate::state::now_monotonic;

fn epoch() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

/// Emit whole floats as integers — Python keeps int range_cm as int in JSON.
fn num2(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 9e15 {
        json!(v as i64)
    } else {
        json!(round2(v))
    }
}

/// ControlState blocks forward motion below this ultrasonic range.
const RANGE_STOP_CM: f64 = 25.0;
const SAMPLE_HZ: i64 = 10;
const TIMELINE: usize = 300;
const INTERVALS: usize = 100;
const REASONS: usize = 64;
const ESTOP_DEBOUNCE_S: f64 = 0.5;
const PERSON_REFRESH_S: f64 = 3.0;
const LINK_KINDS: [&str; 3] = ["rover_link", "controller", "camera"];

/// ControlTelemetry: mission summary folded from sampled control status;
/// never changes control. All collections are bounded.
struct Telemetry {
    samples: u64,
    last_sample: Option<f64>,
    min_range_cm: Option<f64>,
    min_range_at: Option<f64>,
    range_cm: Option<f64>,
    range_at: Option<f64>,
    estops: u64,
    estop_active: bool,
    estop_at: f64,
    reason: Option<String>,
    reasons: indexmap::IndexMap<String, Value>,
    open: HashMap<&'static str, f64>,
    lost: HashMap<&'static str, VecDeque<Value>>,
    lost_counts: HashMap<&'static str, u64>,
    seen: HashMap<&'static str, bool>,
    timeline: VecDeque<Value>,
}

impl Telemetry {
    fn new() -> Self {
        Self {
            samples: 0,
            last_sample: None,
            min_range_cm: None,
            min_range_at: None,
            range_cm: None,
            range_at: None,
            estops: 0,
            estop_active: false,
            estop_at: f64::NEG_INFINITY,
            reason: None,
            reasons: indexmap::IndexMap::new(),
            open: HashMap::new(),
            lost: LINK_KINDS.iter().map(|k| (*k, VecDeque::new())).collect(),
            lost_counts: LINK_KINDS.iter().map(|k| (*k, 0u64)).collect(),
            seen: LINK_KINDS.iter().map(|k| (*k, false)).collect(),
            timeline: VecDeque::new(),
        }
    }

    fn range_near(&self, now: f64, max_age: f64) -> Option<f64> {
        match (self.range_cm, self.range_at) {
            (Some(cm), Some(at)) if now - at <= max_age => Some(cm),
            _ => None,
        }
    }
}

struct Inner {
    events: VecDeque<Value>,
    encounters: indexmap::IndexMap<String, Value>,
    triage: indexmap::IndexMap<String, Value>,
    drill_seen: std::collections::HashSet<String>,
    saved_snapshots: u64,
    record_frames: u64,
    telemetry: Telemetry,
    flagged_saved: u64,
    classes: indexmap::IndexMap<String, u64>,
    person_notified: indexmap::IndexMap<String, (Option<bool>, f64)>,
}

type OnPerson = Box<dyn Fn(&str, &Value, &[u8], &Value) + Send + Sync>;
type FaceStatus = Box<dyn Fn() -> Value + Send + Sync>;

/// Evidence writes go through one dedicated thread so disk stalls on the
/// Jetson's storage can never sit on the 40 Hz control status task or the
/// camera path. Appends keep a file open; bursts batch, quiet periods flush.
enum WriterJob {
    Append(PathBuf, Vec<u8>),
    Write(PathBuf, Vec<u8>),
    WriteRename(PathBuf, PathBuf, Vec<u8>),
}

fn spawn_writer() -> std::sync::mpsc::Sender<WriterJob> {
    let (tx, rx) = std::sync::mpsc::channel::<WriterJob>();
    std::thread::spawn(move || {
        use std::collections::HashMap;
        use std::io::Write;
        let mut files: HashMap<PathBuf, std::io::BufWriter<std::fs::File>> = HashMap::new();
        let flush_all = |files: &mut HashMap<PathBuf, std::io::BufWriter<std::fs::File>>| {
            for f in files.values_mut() {
                let _ = f.flush();
            }
        };
        while let Ok(job) = rx.recv() {
            match job {
                WriterJob::Append(path, line) => {
                    if !files.contains_key(&path) {
                        if let Ok(f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
                            files.insert(path.clone(), std::io::BufWriter::new(f));
                        }
                    }
                    if let Some(f) = files.get_mut(&path) {
                        let _ = f.write_all(&line);
                    }
                }
                WriterJob::Write(path, bytes) => {
                    let _ = std::fs::write(&path, &bytes);
                }
                WriterJob::WriteRename(tmp, path, bytes) => {
                    if std::fs::write(&tmp, &bytes).is_ok() {
                        let _ = std::fs::rename(&tmp, &path);
                    }
                }
            }
            if rx.try_recv().is_err() {
                flush_all(&mut files);
            }
        }
        flush_all(&mut files);
    });
    tx
}

pub struct Mission {
    pub started: f64,
    pub id: String,
    pub directory: Option<PathBuf>,
    pub recording: bool,
    pub record_started: f64,
    inner: Mutex<Inner>,
    on_person: Mutex<Option<OnPerson>>,
    face_status: Mutex<FaceStatus>,
    pub drill_score: crate::drill::Drill,
    writer: Option<std::sync::mpsc::Sender<WriterJob>>,
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
        let writer_tx = directory.as_ref().map(|_| spawn_writer());
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
                telemetry: Telemetry::new(),
                flagged_saved: 0,
                classes: indexmap::IndexMap::new(),
                person_notified: indexmap::IndexMap::new(),
            }),
            on_person: Mutex::new(None),
            face_status: Mutex::new(Box::new(Value::default)),
            drill_score: crate::drill::Drill::new(),
            writer: writer_tx,
        };
        if record {
            mission.finish_recording(Some(0.0));
        }
        Ok(mission)
    }

    /// Queue one JSONL line on the evidence writer thread.
    fn append_jsonl(&self, name: &str, event: &Value) {
        if let (Some(dir), Some(tx)) = (&self.directory, &self.writer) {
            let mut line = serde_json::to_string(event).unwrap().into_bytes();
            line.push(b'\n');
            let _ = tx.send(WriterJob::Append(dir.join(name), line));
        }
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
                if let Some(tx) = &self.writer {
                    let _ = tx.send(WriterJob::Write(dir.join(&name), f.jpeg.clone()));
                }
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
        if self.directory.is_some() {
            self.append_jsonl("replay.jsonl", &event);
        }
    }

    pub fn finish_recording(&self, elapsed: Option<f64>) {
        if self.recording {
            if let Some(dir) = &self.directory {
                let duration = elapsed.unwrap_or_else(|| now_monotonic() - self.record_started);
                let bytes = serde_json::to_string(&json!({"version": 1, "duration_s": duration}))
                    .unwrap()
                    .into_bytes();
                if let Some(tx) = &self.writer {
                    let _ = tx.send(WriterJob::Write(dir.join("replay.json"), bytes));
                }
            }
        }
    }

    /// Called as on_person(key, encounter, jpeg, snapshot_url) for each new
    /// person track (e.g. the triage worker).
    pub fn set_on_person(&self, callback: OnPerson) {
        *self.on_person.lock().unwrap() = Some(callback);
    }

    /// {face_detector, face_error} fields merged into /mission/drill; the
    /// server points this at the perception service (mission.face_status).
    pub fn set_face_status(&self, callback: FaceStatus) {
        *self.face_status.lock().unwrap() = callback;
    }

    /// Mission telemetry hook: fold one control_status packet (plus optional
    /// camera freshness) into the report summary. Read-only; at most 10 Hz.
    pub fn observe_control(&self, status: &Value, camera: Option<bool>) {
        if !status.is_object() {
            return;
        }
        let now = epoch();
        let mut guard = self.inner.lock().unwrap();
        let inner = &mut *guard;
        let t = &mut inner.telemetry;
        t.samples += 1;
        t.last_sample = Some(now);
        let cm = status["range_cm"].as_f64().filter(|v| v.is_finite());
        if let Some(cm) = cm.filter(|c| *c > 0.0) {
            t.range_cm = Some(cm);
            t.range_at = Some(now);
            if t.min_range_cm.map_or(true, |m| cm < m) {
                t.min_range_cm = Some(cm);
                t.min_range_at = Some(now);
            }
        }
        let reason = status["reason"].as_str().map(|r| r.chars().take(64).collect::<String>());
        let blocked = status["estop"].as_bool() == Some(true)
            && (reason.as_deref() == Some("forward_blocked")
                || cm.map_or(false, |c| c < RANGE_STOP_CM));
        if blocked && !t.estop_active && now - t.estop_at > ESTOP_DEBOUNCE_S {
            t.estops += 1;
            let mut fields = Map::new();
            fields.insert("range_cm".into(), cm.map_or(Value::Null, num2));
            fields.insert("reason".into(), json!(reason));
            self.flag(inner, "estop", now, fields);
        }
        if blocked {
            inner.telemetry.estop_at = now;
        }
        inner.telemetry.estop_active = blocked;
        if reason != inner.telemetry.reason {
            inner.telemetry.reason = reason.clone();
            if let Some(r) = reason.filter(|r| r != "active") {
                let t = &mut inner.telemetry;
                if !t.reasons.contains_key(&r) && t.reasons.len() < REASONS {
                    t.reasons.insert(r.clone(), json!({"count": 0, "first_ts": now, "last_ts": now}));
                }
                if let Some(entry) = t.reasons.get_mut(&r) {
                    entry["count"] = json!(entry["count"].as_i64().unwrap_or(0) + 1);
                    entry["last_ts"] = json!(now);
                }
                let mut fields = Map::new();
                fields.insert("reason".into(), json!(r));
                fields.insert("range_cm".into(), cm.map_or(Value::Null, num2));
                fields.insert(
                    "requires_release".into(),
                    json!(status["requires_release"].as_bool() == Some(true)),
                );
                self.flag(inner, "stop", now, fields);
            }
        }
        let up: [(&'static str, Option<bool>); 3] = [
            ("rover_link", status["rover"].as_bool()),
            ("controller", if status.get("session_id").is_some() {
                Some(status["session_id"].as_str().is_some())
            } else {
                None
            }),
            ("camera", camera),
        ];
        for (kind, alive) in up {
            let Some(alive) = alive else { continue };
            let t = &mut inner.telemetry;
            if alive {
                *t.seen.get_mut(kind).unwrap() = true;
                if let Some(started) = t.open.remove(kind) {
                    let lost_s = round2(now - started);
                    let queue = t.lost.get_mut(kind).unwrap();
                    queue.push_back(json!({"start": started, "end": now, "duration_s": lost_s}));
                    while queue.len() > INTERVALS {
                        queue.pop_front();
                    }
                    let mut fields = Map::new();
                    fields.insert("lost_s".into(), json!(lost_s));
                    self.flag(inner, &format!("{kind}_restored"), now, fields);
                }
            } else if *t.seen.get(kind).unwrap_or(&false) && !t.open.contains_key(kind) {
                // Only a loss after the link was up counts; startup is not a loss.
                t.open.insert(kind, now);
                *t.lost_counts.get_mut(kind).unwrap() += 1;
                self.flag(inner, &format!("{kind}_lost"), now, Map::new());
            }
        }
    }

    /// Timeline flag + events.jsonl persistence (persist_flag): detection
    /// events already persist with their snapshot; flagged entries cap at 10k.
    fn flag(&self, inner: &mut Inner, kind: &str, ts: f64, fields: Map<String, Value>) {
        let mut event = Map::new();
        event.insert("kind".into(), json!(kind));
        event.insert("ts".into(), json!(ts));
        for (k, v) in fields {
            event.insert(k, v);
        }
        let event = Value::Object(event);
        inner.telemetry.timeline.push_back(event.clone());
        while inner.telemetry.timeline.len() > TIMELINE {
            inner.telemetry.timeline.pop_front();
        }
        if self.directory.is_none() || kind == "detection" || inner.flagged_saved >= 10_000 {
            return;
        }
        inner.flagged_saved += 1;
        self.append_jsonl("events.jsonl", &event);
    }

    fn summary_locked(&self, inner: &Inner, now: f64) -> Value {
        let t = &inner.telemetry;
        let mut lost = Map::new();
        for kind in LINK_KINDS {
            let mut rows: Vec<Value> = t.lost[kind].iter().cloned().collect();
            if let Some(start) = t.open.get(kind) {
                rows.push(json!({"start": start, "end": Value::Null, "duration_s": round2(now - start)}));
            }
            let total: f64 = rows.iter().filter_map(|r| r["duration_s"].as_f64()).sum();
            lost.insert(
                kind.into(),
                json!({"count": t.lost_counts[kind], "total_s": round2(total), "intervals": rows}),
            );
        }
        json!({
            "samples": t.samples,
            "sample_hz": SAMPLE_HZ,
            "last_sample_ts": t.last_sample,
            "min_range_cm": t.min_range_cm.map_or(Value::Null, num2),
            "min_range_at": t.min_range_at,
            "estop_count": t.estops,
            "stop_reasons": Value::Object(
                t.reasons.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
            ),
            "lost": Value::Object(lost),
        })
    }

    pub fn detect(&self, packet: &Value, frame: &CameraFrame) {
        self.record("detections", packet, None);
        let mut inner = self.inner.lock().unwrap();
        if let Some(boxes) = packet["boxes"].as_array() {
            self.drill_score.observe(boxes);
        }
        let drilling = self.drill_score.running();
        let mut new_persons: Vec<(String, Value, Value)> = Vec::new();
        let mut refreshed: Vec<(String, Value)> = Vec::new();
        let has_on_person = self.on_person.lock().unwrap().is_some();
        let Some(boxes) = packet["boxes"].as_array() else { return };
        for boxv in boxes {
            let key = format!("{}:{}", frame.source, boxv["id"]);
            let now = epoch();
            let mut encounter = boxv.clone();
            let previous = inner.encounters.get(&key).cloned();
            let first_seen = previous
                .as_ref()
                .and_then(|p| p["first_seen"].as_f64())
                .unwrap_or(now);
            let first_range_cm = previous
                .as_ref()
                .and_then(|p| p.get("first_range_cm").cloned())
                .unwrap_or_else(|| {
                    inner
                        .telemetry
                        .range_near(now, 1.0)
                        .map_or(Value::Null, num2)
                });
            encounter["source"] = json!(frame.source);
            encounter["last_seen"] = json!(now);
            encounter["first_seen"] = json!(first_seen);
            encounter["first_range_cm"] = first_range_cm.clone();
            let is_new = previous.is_none();
            inner.encounters.insert(key.clone(), encounter.clone());
            if drilling && boxv["cls"].as_str() == Some("person") {
                inner.drill_seen.insert(key.clone());
            }
            if !is_new {
                // Re-issue on_person when a person's down flag differs from
                // the one their SBAR was written for (PERSON_REFRESH window).
                if has_on_person && boxv["cls"].as_str() == Some("person") {
                    if let Some(down) = boxv.get("down").and_then(Value::as_bool) {
                        let (noted, at) = inner
                            .person_notified
                            .get(&key)
                            .cloned()
                            .unwrap_or((None, -PERSON_REFRESH_S));
                        if Some(down) != noted && now - at >= PERSON_REFRESH_S {
                            let stored = inner.encounters[&key].clone();
                            inner.person_notified.insert(
                                key.clone(),
                                (stored.get("down").and_then(Value::as_bool), now),
                            );
                            while inner.person_notified.len() > 500 {
                                inner.person_notified.shift_remove_index(0);
                            }
                            refreshed.push((key, stored));
                        }
                    }
                }
                continue;
            }
            let cls = boxv["cls"]
                .as_str()
                .unwrap_or("object")
                .chars()
                .take(32)
                .collect::<String>();
            if inner.classes.contains_key(&cls) || inner.classes.len() < 100 {
                *inner.classes.entry(cls.clone()).or_insert(0) += 1;
            }
            let mut fields = Map::new();
            fields.insert("cls".into(), json!(cls));
            fields.insert("key".into(), json!(key));
            fields.insert("range_cm".into(), first_range_cm);
            self.flag(&mut inner, "detection", now, fields);
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
                    if let Some(tx) = &self.writer {
                        let _ = tx.send(WriterJob::Write(dir.join(&image_name), frame.jpeg.clone()));
                    }
                    inner.saved_snapshots += 1;
                    self.append_jsonl("events.jsonl", &event);
                }
            }
            if boxv["cls"].as_str() == Some("person") && has_on_person {
                if let Some(stored) = inner.encounters.get(&key).cloned() {
                    inner.person_notified.insert(
                        key.clone(),
                        (stored.get("down").and_then(Value::as_bool), now),
                    );
                    while inner.person_notified.len() > 500 {
                        inner.person_notified.shift_remove_index(0);
                    }
                    new_persons.push((key, stored, event["snapshot_url"].clone()));
                }
            }
        }
        while inner.encounters.len() > 500 {
            inner.encounters.shift_remove_index(0);
        }
        drop(inner);
        // Callbacks re-enter the mission (set_sbar); they must run unlocked.
        if !new_persons.is_empty() || !refreshed.is_empty() {
            let cb = self.on_person.lock().unwrap();
            if let Some(cb) = cb.as_ref() {
                for (key, encounter, snapshot_url) in new_persons {
                    cb(&key, &encounter, &frame.jpeg, &snapshot_url);
                }
                // None snapshot_url keeps the first snapshot.
                for (key, encounter) in refreshed {
                    cb(&key, &encounter, &frame.jpeg, &Value::Null);
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
            self.append_jsonl("triage.jsonl", &Value::Object(entry));
            let report = self.report_locked(&self.id.clone(), self.started, true, &inner);
            if let Some(tx) = &self.writer {
                let _ = tx.send(WriterJob::WriteRename(
                    dir.join("report.json.tmp"),
                    dir.join("report.json"),
                    serde_json::to_string(&report).unwrap().into_bytes(),
                ));
            }
        }
    }

    fn drill_locked(&self, inner: &Inner) -> Value {
        let mut status = self.drill_score.status();
        status["person_encounters"] = json!(inner.drill_seen.len());
        if let Some(fields) = (self.face_status.lock().unwrap())().as_object() {
            for (k, v) in fields {
                status[k] = v.clone();
            }
        }
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
        let mut summary = self
            .summary_locked(inner, epoch())
            .as_object()
            .cloned()
            .unwrap_or_default();
        summary.insert(
            "detections_by_class".into(),
            Value::Object(
                inner
                    .classes
                    .iter()
                    .map(|(k, v)| (k.clone(), json!(v)))
                    .collect(),
            ),
        );
        summary.insert("encounters".into(), json!(inner.encounters.len()));
        let timeline: Vec<Value> = inner.telemetry.timeline.iter().cloned().collect();
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
            "summary": Value::Object(summary),
            "timeline": timeline,
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
