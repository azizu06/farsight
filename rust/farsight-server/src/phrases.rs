//! Preset survivor phrases in English and Spanish, spoken on the rover with
//! Piper. Port of jetson/phrases.py.
//!
//! Phrases are a fixed set (never free text), rendered once per language into
//! the TTS directory and played through the Jetson's default ALSA/Pulse output
//! when a speaker is present. Without a speaker the caller gets the WAV URL so
//! the headset can play it.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::Mutex as AsyncMutex;

const PHRASES: &[(&str, &str, &str)] = &[
    ("help_coming", "Help is coming, stay still.", "La ayuda viene en camino, no se mueva."),
    ("found_you", "We found you. A rescuer is on the way.", "Lo encontramos. Un rescatista va en camino."),
    ("can_you_hear", "Can you hear me? Raise your hand if you can.", "¿Me escucha? Levante la mano si puede."),
    ("call_out", "If you can, call out so we can find you.", "Si puede, grite para que podamos encontrarlo."),
    ("stay_calm", "Stay calm. You are not alone.", "Mantenga la calma. No está solo."),
];
const LANGUAGES: &[(&str, &str, &str)] = &[
    ("en", "English", "FARSIGHT_PIPER_MODEL"),
    ("es", "Spanish", "FARSIGHT_PIPER_MODEL_ES"),
];

#[derive(Debug)]
pub struct SpeechError {
    pub message: String,
    pub status_code: u16,
}

impl SpeechError {
    fn new(message: impl Into<String>, status_code: u16) -> Self {
        Self { message: message.into(), status_code }
    }
}

fn phrase_text(phrase: &str, lang: &str) -> Option<&'static str> {
    PHRASES.iter().find(|(id, _, _)| *id == phrase).map(|(_, en, es)| if lang == "en" { *en } else { *es })
}

/// Python repr() of a short string for error messages (single quotes, escapes).
fn py_repr(s: &str) -> String {
    let mut out = String::from("'");
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            c if c.is_control() => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('\'');
    out
}

/// Default output command when the Jetson has a playback device, else None.
pub fn probe_speaker() -> Option<Vec<String>> {
    if which::which("aplay").is_ok() {
        let cards = std::process::Command::new("aplay")
            .arg("-l")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .output();
        return match cards {
            Ok(out) if out.status.success()
                && String::from_utf8_lossy(&out.stdout).contains("card") =>
            {
                Some(vec!["aplay".into(), "-q".into()])
            }
            _ => None,
        };
    }
    if which::which("paplay").is_ok() && which::which("pactl").is_ok() {
        let sinks = std::process::Command::new("pactl")
            .args(["list", "short", "sinks"])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .output();
        return match sinks {
            Ok(out) if out.status.success()
                && !String::from_utf8_lossy(&out.stdout).trim().is_empty() =>
            {
                Some(vec!["paplay".into()])
            }
            _ => None,
        };
    }
    None
}

/// Minimal WAV check: RIFF/WAVE with a data chunk holding at least one frame.
fn valid_wav(path: &Path) -> bool {
    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(_) => return false,
    };
    if data.len() < 44 || &data[0..4] != b"RIFF" || &data[8..12] != b"WAVE" {
        return false;
    }
    let mut pos = 12usize;
    let mut block_align = 0u32;
    while pos + 8 <= data.len() {
        let kind = &data[pos..pos + 4];
        let size = u32::from_le_bytes(data[pos + 4..pos + 8].try_into().unwrap()) as usize;
        let body = pos + 8;
        if kind == b"fmt " && body + 16 <= data.len() {
            block_align = u16::from_le_bytes(data[body + 12..body + 14].try_into().unwrap()) as u32;
        }
        if kind == b"data" {
            return block_align > 0 && size / block_align as usize > 0;
        }
        pos = body + size + (size % 2);
    }
    false
}

pub struct PhraseSpeaker {
    output_dir: Option<PathBuf>,
    voices: std::collections::HashMap<String, PathBuf>,
    piper_bin: Option<String>,
    speaker_cmd: String,
    probed: Mutex<(Option<Vec<String>>, f64)>,
    player: AsyncMutex<Option<tokio::process::Child>>,
    lock: AsyncMutex<()>,
}

const PROBE_TTL: f64 = 30.0;
const SPEECH_TIMEOUT: u64 = 20;

impl PhraseSpeaker {
    pub fn new(
        output_dir: Option<PathBuf>,
        voices: std::collections::HashMap<String, PathBuf>,
        piper_bin: Option<String>,
        speaker_cmd: Option<String>,
    ) -> Self {
        Self {
            output_dir: output_dir.map(|p| {
                p.canonicalize().unwrap_or_else(|_| {
                    std::env::current_dir().unwrap_or_default().join(&p)
                })
            }),
            voices,
            piper_bin,
            speaker_cmd: speaker_cmd.unwrap_or_else(|| "auto".into()).trim().to_string(),
            probed: Mutex::new((None, f64::NEG_INFINITY)),
            player: AsyncMutex::new(None),
            lock: AsyncMutex::new(()),
        }
    }

    fn output(&self) -> Option<Vec<String>> {
        let lowered = self.speaker_cmd.to_lowercase();
        if matches!(lowered.as_str(), "none" | "off" | "") {
            return None;
        }
        if lowered != "auto" {
            return Some(self.speaker_cmd.split_whitespace().map(String::from).collect());
        }
        let mut probed = self.probed.lock().unwrap();
        if probed.1 == f64::NEG_INFINITY || crate::state::now_monotonic() - probed.1 > PROBE_TTL {
            *probed = (probe_speaker(), crate::state::now_monotonic());
        }
        probed.0.clone()
    }

    fn voice(&self, lang: &str) -> Option<String> {
        let path = self.voices.get(lang)?;
        if path.is_file() && path.metadata().map(|m| m.len() > 0).unwrap_or(false) {
            path.canonicalize().ok()?.to_str().map(String::from)
        } else {
            None
        }
    }

    pub fn status(&self) -> Value {
        let output = self.output();
        json!({
            "speaker": output.is_some(),
            "speaker_cmd": output.as_ref().map(|o| o.join(" ")),
            "voices": LANGUAGES.iter().filter(|(lang, _, _)| self.voice(lang).is_some())
                .map(|(lang, _, _)| *lang).collect::<Vec<_>>(),
        })
    }

    pub fn phrases(&self) -> Value {
        json!(PHRASES.iter().map(|(id, en, es)| json!({"id": id, "en": en, "es": es})).collect::<Vec<_>>())
    }

    pub async fn speak(&self, phrase: &str, lang: &str) -> Result<Value, SpeechError> {
        if phrase_text(phrase, "en").is_none() {
            let short: String = phrase.chars().take(40).collect();
            return Err(SpeechError::new(format!("Unknown phrase {}", py_repr(&short)), 404));
        }
        if !LANGUAGES.iter().any(|(l, _, _)| *l == lang) {
            return Err(SpeechError::new("Language must be 'en' or 'es'", 400));
        }
        let (name, variable) = LANGUAGES
            .iter()
            .find(|(l, _, _)| *l == lang)
            .map(|(_, n, v)| (*n, *v))
            .unwrap();
        let model = self.voice(lang).ok_or_else(|| {
            SpeechError::new(
                format!("No {name} Piper voice installed; set {variable} to a downloaded .onnx voice (jetson/setup.sh fetches them)"),
                503,
            )
        })?;
        let dir = self
            .output_dir
            .clone()
            .ok_or_else(|| SpeechError::new("Phrase speech requires a TTS output directory", 503))?;
        let text = phrase_text(phrase, lang).unwrap();
        let filename = format!("phrase-{phrase}-{lang}.wav");
        let path = dir.join(&filename);
        {
            let _guard = self.lock.lock().await;
            if !valid_wav(&path) {
                self.render(&model, text, &path).await?;
            }
        }
        let mut result = json!({
            "phrase": phrase,
            "lang": lang,
            "text": text,
            "audio_url": format!("/tts/{filename}"),
            "played": false,
        });
        if let Some(output) = self.output() {
            match self.play(output, &path).await {
                Ok(()) => result["played"] = json!(true),
                Err(kind) => {
                    result["speaker_warning"] = json!(format!(
                        "Rover speaker failed ({kind}); play audio_url on the headset"
                    ));
                }
            }
        }
        Ok(result)
    }

    async fn render(&self, model: &str, text: &str, path: &Path) -> Result<(), SpeechError> {
        std::fs::create_dir_all(path.parent().unwrap()).map_err(|_| SpeechError::new("Piper phrase rendering failed (OSError)", 503))?;
        let temporary = path.with_extension("tmp");
        let result = async {
            let Some(bin) = &self.piper_bin else {
                return Err(SpeechError::new("Piper phrase rendering failed (OSError)", 503));
            };
            let mut child = tokio::process::Command::new(bin)
                .args(["--model", model, "--output_file"])
                .arg(&temporary)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|_| SpeechError::new("Piper phrase rendering failed (OSError)", 503))?;
            let work = async {
                use tokio::io::AsyncWriteExt;
                if let Some(mut stdin) = child.stdin.take() {
                    let _ = stdin.write_all(format!("{text}\n").as_bytes()).await;
                }
                child.wait().await
            };
            match tokio::time::timeout(Duration::from_secs(SPEECH_TIMEOUT), work).await {
                Ok(Ok(_)) => {}
                Ok(Err(_)) => return Err(SpeechError::new("Piper phrase rendering failed (OSError)", 503)),
                Err(_) => {
                    let _ = child.kill().await;
                    return Err(SpeechError::new("Piper phrase rendering failed (TimeoutError)", 503));
                }
            }
            if !valid_wav(&temporary) {
                return Err(SpeechError::new("Piper did not produce a phrase WAV", 503));
            }
            std::fs::rename(&temporary, path).map_err(|_| SpeechError::new("Piper phrase rendering failed (OSError)", 503))?;
            Ok(())
        }
        .await;
        let _ = std::fs::remove_file(&temporary);
        result
    }

    /// A new phrase interrupts the previous one; playback runs on without
    /// blocking the request. Returns Err(kind) matching OSError/TimeoutError.
    async fn play(&self, output: Vec<String>, path: &Path) -> Result<(), &'static str> {
        self.close_player().await;
        let mut args = output;
        args.push(path.to_string_lossy().into());
        let child = tokio::process::Command::new(&args[0])
            .args(&args[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "OSError")?;
        *self.player.lock().await = Some(child);
        Ok(())
    }

    pub async fn close(&self) {
        self.close_player().await;
    }

    async fn close_player(&self) {
        let mut guard = self.player.lock().await;
        if let Some(mut child) = guard.take() {
            if matches!(child.try_wait(), Ok(None)) {
                let _ = child.kill().await;
            }
            let _ = child.wait().await;
        }
    }
}
