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
hog|none --detector-model --h264 --mission-dir`.

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
  forward estop (<25 cm), camera-guard, autonomy `explore`/`seek`/`stick_override`
- `/ws/video` fMP4 init/fragment flow (ffmpeg must be installed for `--h264`)
- Relay: upstream origin validation, same-origin URL rewrite (`/tts/*.wav` et al),
  hop-header stripping, deadman override on control messages, redirect rebinding
