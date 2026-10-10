# Farsight — Rust services

Rust port of the Farsight Jetson services, replacing the Python runtime on the
hot path. **Wire protocols are byte-compatible** with the Python
implementations — the Quest web app (`quest/`) and the iPhone app are untouched,
and the Python servers under `jetson/` remain available as a fallback.

## Crates

| Binary | Replaces | What it does |
|---|---|---|
| `farsight-server` | `jetson/mapping/server.py`, `jetson/{camera,video,perception,mission,assistant,tracker}.py`, `jetson/control/{state,serial_link,service}.py`, `jetson/autonomy.py` | RGB-D fusion (voxel map + surface mesh), camera/JPEG + H.264/fMP4 video, person detection tracking, mission log, rover control state machine + Uno serial pump, explore/seek autonomy, `/ask` scene Q&A (Whisper + local vision model + Piper TTS) |
| `farsight-relay` | `tools/operator_relay.py` | Loopback-only relay on the MacBook: static `quest/dist`, HTTP proxy, WebSocket proxy with the same deadman safety enforcement |

## Build

```bash
cd rust
cargo build --release --workspace   # needs pkg-config + libudev-dev on Linux
cargo test --workspace
```

## Run — rover (Jetson)

```bash
# Full sim: synthetic room + simulated rover, no hardware
./target/release/farsight-server --demo --simulate-rover --port 8443 --ssl-certfile cert.pem --ssl-keyfile key.pem

# Real hardware
./target/release/farsight-server --camera /dev/video0 --serial-port /dev/ttyACM0 --port 8443 ...
```

CLI mirrors the Python argparse flags: `--demo --host --port --ssl-certfile
--ssl-keyfile --camera --serial-port --simulate-rover --max-pwm --detector
hog|none --detector-model --h264 --video-fps --video-bitrate --mission-dir
--record --replay --depth-model --scan-pan --scan-fov --scan-intrinsics
--scan-turn-dps --assistant-url --whisper-url --whisper-bin --whisper-model
--piper-bin --piper-model --piper-model-es --speaker-cmd --triage-interval
--tts-dir`.

## Run — operator (MacBook)

```bash
./target/release/farsight-relay --upstream https://<jetson-host> --port 8080
```

Binds loopback only. Non-localhost Host headers get 403, matching the Python
relay.

## Optional ONNX detector

```bash
cargo build --release -p farsight-server --features detector   # needs libonnxruntime at runtime
./target/release/farsight-server --detector-model yolo11n.onnx ...
```

Letterbox/blob/decode/NMS match the cv2.dnn path exactly; `<model>.labels.json`
is honoured. HOG and TensorRT `.engine` stay Python-side (OpenCV / TensorRT have
no Rust runtime); `--detector none` works everywhere.

## Compatibility contract

- `/ws/phone` hello + binary RGB-D frames (`u32le` header len + JSON header +
  JPEG + float32 depth + uint8 confidence)
- `/map/points.bin` `u32le` count + 16-byte records; `/map/mesh.bin`,
  `/map/export.ply`, `surfaces` keyframe encoding identical to Python
- `/ws/control` permits, 250 ms lease, deadman release, seq monotonicity,
  20 Hz adaptive status + `on_status` hook, `on_output` telemetry feed
  (v/w/blocked), forward estop (<25 cm), camera-guard, autonomy
  `explore`/`seek`/`stick_override`, `begin_scan`/`scan_command` bounded
  sweep lease (`w` ±.35, `pan` ±80)
- `/ws/video` fMP4 init/fragment flow (ffmpeg must be installed for `--h264`);
  pipelined `{"type":"next","request_id"}` window of 4, `server_wait_ms`,
  1013 teardown on invalid/excessive requests or consumer lag
- `GET|POST /scan` stopped-position camera sweep (yaws −80..80): `202` with the
  live result object or `409` on refusal (`{"detail": ...}`); `health.scan`
  reports `state|running|simulated|approximate|backend|views|points|scan_ms|markers|error`
- `POST /mission/triage/{key}` SBAR notes (`404` unknown encounter, `503` while a
  note is in flight); `mission/report` gains `triage` map + `encounters[].key|sbar`
- `GET|POST /mission/drill`: full scorer — face dwell (1 s, 0.7 s gap, 1
  miss), estop debounce, dead-reckoned `pose`/`breadcrumb`/`route_home`,
  `summary` on end; POST `duration_s` 10..300, `victims` 1..20
- `--record` writes `replay.jsonl` (frame/control/detections events,
  permit/session_id/received stripped) + `frames/*.jpg` + `replay.json`;
  `--replay` replays a recorded dir read-only (`ReplayControl` status
  shape, commands discarded, `replay`/`recording` health fields)
- `GET|POST /speak` survivor phrases: GET returns `{phrases,speaker,speaker_cmd,voices}`,
  POST `{phrase,lang}` → `{phrase,lang,text,audio_url,played,speaker_warning?}`
  (`404` unknown phrase, `400` bad lang, `503` missing voice/output dir);
  `/tts/phrase-*.wav` served alongside `reply-*.wav`
- Relay: upstream origin validation, same-origin URL rewrite (`/tts/*.wav` et al),
  hop-header stripping, deadman override on control messages, redirect rebinding
