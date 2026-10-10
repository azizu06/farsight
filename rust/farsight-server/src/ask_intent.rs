//! Turn /ask questions into confirm-first proposals and keep model answers
//! from claiming motion. Port of jetson/ask_intent.py.
//!
//! The model never decides motion: a keyword match proposes an action, the
//! Quest asks the operator to confirm it, and only then does the normal
//! deadman/permit control path run it.

use serde_json::{json, Value};

fn seek_re() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(r"(?i)\b(go|drive|move|head|get|roll)\s+(to|toward|towards|closer)\b|\b(seek|approach|find)\b").unwrap()
    })
}

fn scan_re() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"(?i)\bscan\b|\blook around\b|\bsweep\b").unwrap())
}

fn claim_re() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        let subject = r"\b(i|i'm|i am|i'll|i will|we|we're|we'll|let me|let's)\s+(\w+\s+){0,2}?";
        let motion = r"(go|going|head|heading|move|moving|drive|driving|approach|approaching|turn|turning|roll|rolling|navigate|navigating|steer|steering)\b";
        regex::Regex::new(&format!(
            r"(?i){subject}{motion}|^(okay,?\s*|sure,?\s*)?(moving|heading|driving|going|approaching|turning|rolling|navigating)\b"
        ))
        .unwrap()
    })
}

fn is_int(v: &Value) -> bool {
    v.is_i64() || v.is_u64()
}

fn finite_num(v: &Value) -> bool {
    v.as_f64().map(|x| x.is_finite()).unwrap_or(false) && !v.is_boolean()
}

pub fn located(boxes: &[Value]) -> Vec<&Value> {
    boxes
        .iter()
        .filter(|b| {
            b.is_object()
                && ["x", "y", "w", "h"].iter().all(|k| finite_num(&b[k]))
        })
        .collect()
}

/// Spoken command to a proposal the operator must confirm; None for plain questions.
pub fn propose_action(question: &str, boxes: &[Value]) -> Option<Value> {
    if scan_re().is_match(question) {
        return Some(json!({"type": "scan"}));
    }
    if seek_re().is_match(question) {
        let people: Vec<&Value> = located(boxes)
            .into_iter()
            .filter(|b| b["cls"] == "person" && is_int(&b["id"]))
            .collect();
        if people.is_empty() {
            return None;
        }
        // A person flagged down is the rescue priority; otherwise the largest
        // box is the nearest.
        let target = people
            .iter()
            .max_by(|a, b| {
                let ka = (a["down"] == Value::Bool(true), a["w"].as_f64().unwrap_or(0.0) * a["h"].as_f64().unwrap_or(0.0));
                let kb = (b["down"] == Value::Bool(true), b["w"].as_f64().unwrap_or(0.0) * b["h"].as_f64().unwrap_or(0.0));
                ka.partial_cmp(&kb).unwrap_or(std::cmp::Ordering::Equal)
            })
            .unwrap();
        return Some(json!({"type": "seek", "target_id": target["id"]}));
    }
    None
}

/// Compact grounding text for the model, e.g. 'person #3 left close (down)'.
pub fn summarize_boxes(boxes: &[Value]) -> String {
    static LABEL_RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let label_re = LABEL_RE.get_or_init(|| regex::Regex::new(r"[^a-z0-9 _-]").unwrap());
    let mut words = Vec::new();
    for boxv in located(boxes).into_iter().take(12) {
        let center = boxv["x"].as_f64().unwrap_or(0.0) + boxv["w"].as_f64().unwrap_or(0.0) / 2.0;
        let side = if center < 0.35 { "left" } else if center > 0.65 { "right" } else { "center" };
        let h = boxv["h"].as_f64().unwrap_or(0.0);
        let distance = if h >= 0.5 { "close" } else if h >= 0.25 { "mid-range" } else { "far" };
        let raw = boxv["cls"].as_str().unwrap_or("object").to_lowercase();
        let label: String = label_re.replace_all(&raw, "").chars().take(40).collect();
        let identifier = if is_int(&boxv["id"]) {
            format!(" #{}", boxv["id"].as_i64().unwrap_or(0))
        } else {
            String::new()
        };
        let down = if boxv["down"] == Value::Bool(true) { " (down)" } else { "" };
        words.push(format!("{label}{identifier} {side} {distance}{down}"));
    }
    if words.is_empty() {
        "nothing detected".into()
    } else {
        words.join("; ")
    }
}

/// Drop sentences claiming the rover moves, keep the first few, then name any
/// pending proposal. `max_sentences` of None keeps every clean sentence.
pub fn guard_answer(answer: &str, action: Option<&Value>, max_sentences: Option<usize>) -> String {
    let normalized = answer.replace('\u{2019}', "'");
    let normalized = normalized.trim();
    // Split after sentence-final punctuation followed by whitespace
    // (Python re.split(r"(?<=[.!?])\s+")).
    let mut sentences: Vec<&str> = Vec::new();
    let bytes = normalized.as_bytes();
    let mut start = 0;
    let mut i = 0;
    while i < bytes.len() {
        if matches!(bytes[i], b'.' | b'!' | b'?') {
            let mut j = i + 1;
            while j < bytes.len() && bytes[j].is_ascii_whitespace() {
                j += 1;
            }
            if j > i + 1 {
                sentences.push(&normalized[start..j]);
                start = j;
                i = j;
                continue;
            }
        }
        i += 1;
    }
    sentences.push(&normalized[start..]);
    let kept: Vec<&str> = sentences
        .iter()
        .copied()
        .filter(|s| !s.is_empty() && !claim_re().is_match(s))
        .take(max_sentences.unwrap_or(usize::MAX))
        .collect();
    let mut kept = kept.join(" ");
    if kept.is_empty() {
        kept = "I can't move the rover myself.".to_string();
    }
    match action.and_then(|a| a["type"].as_str()) {
        Some("seek") => {
            kept.push_str(&format!(
                " Confirm on the headset to seek person #{}.",
                action.unwrap()["target_id"].as_i64().unwrap_or(0)
            ));
        }
        Some("scan") => {
            kept.push_str(" Confirm on the headset to scan the room.");
        }
        _ => {}
    }
    kept
}
