# Interfaces

The contract that lets four people build in parallel. Change it only in a PR that
every affected owner has seen.

```
Quest 2 (WebXR, three.js)
   |  HTTPS page + WSS over the team LAN (own router/hotspot, NOT UCF Wi-Fi)
   v
Jetson Orin Nano (on the rover)  -- the brain
   camera -> TensorRT YOLO -> detections
   camera -> Depth Anything V2 (stretch) -> point cloud
   control server: 20 Hz pump, 250 ms lease, ultrasonic e-stop
   |  USB serial 115200
   v
Arduino Uno (ELEGOO V4 board) -> motors, pan servo, ultrasonic
```

All servers run on the Jetson, port `8443` (HTTPS). The mock server
(`tools/mock_jetson.py`) serves the same endpoints from a laptop with a video file.

## HTTP

| Path | What |
|---|---|
| `GET /` | Quest WebXR app (built `quest/dist`) |
| `GET /stream.mjpg` | Camera, MJPEG, 640x480, target 30 fps |
| `GET /health` | `{"camera": bool, "detector_fps": float, "rover": bool, "estop": bool}`; optional `detector_backend` (e.g. `"pose-tensorrt"`), `detector_inference_ms` (float or null) |

## WebSocket `/ws/detections` (Jetson -> Quest), JSON, every inference

```json
{"ts": 1760090000.123, "frame": 1042, "fps": 24.8,
 "boxes": [{"id": 3, "cls": "person", "conf": 0.91, "x": 0.41, "y": 0.22, "w": 0.18, "h": 0.55}]}
```

Box coordinates are normalized 0..1, top-left origin, relative to the MJPEG frame.

Optional pose fields (YOLO11-pose), present when the detector is a pose model:

```json
{"id": 3, "cls": "person", "conf": 0.91, "x": 0.41, "y": 0.22, "w": 0.18, "h": 0.55,
 "kpts": [[0.50, 0.25, 0.97], "... 17 COCO keypoints, [x, y, conf], same normalization ..."],
 "down": false}
```

- `kpts`: 17 COCO keypoints (nose, eyes, ears, shoulders, elbows, wrists, hips, knees, ankles), each `[x, y, conf]`. Omitted if the detector has no pose.
- `down`: true when the person looks fallen (body axis more than 60 degrees from vertical, or a wide box when keypoints are unreliable). Omitted fields mean false/absent; clients must ignore unknown fields.
- `id` is stable across frames: the server runs a small IoU + velocity tracker (`jetson/tracker.py`) over every detector mode, keeping an id through brief dropouts (about 5 frames or 2 s) and never reusing one. Clients may rely on it for `target_id`.

## WebSocket `/ws/control` (Quest -> Jetson), JSON, 20 Hz while the deadman is held

```json
{"seq": 812, "deadman": true, "v": 0.4, "w": -0.2, "pan": 15, "mode": "teleop"}
```

- `v` forward speed and `w` turn rate, both -1..1. `pan` camera yaw in degrees, -80..80, 0 = forward.
- `mode`: `"teleop"` or `"seek"` (seek also sends `"target_id"`).
- No valid message for 250 ms, or `deadman: false` -> motors zero. Always.

### Optional Explore / Seek extension

The composed rover server also accepts `mode: "explore"`. Existing teleop and
`mode: "seek"` + integer `target_id` fields retain their meanings. All modes use
the same session, ordered sequence, server-issued permit, held deadman, and
fresh-video checks described in [control setup](../jetson/control/README.md).
Autonomy never renews a permit or lease itself. Example held-deadman heartbeat:

```json
{"session_id":"from status","permit":"from latest status","seq":813,
 "deadman":true,"v":0,"w":0,"pan":0,"mode":"seek","target_id":3,
 "video_fresh":true,"video_age_ms":50}
```

- Select Explore with `mode: "explore"`, `v: 0`, `w: 0`. It drives at 0.25
  normalized speed until front range is below 70 cm or four seconds pass, then
  turns randomly left/right at 0.35 for at most one second and repeats. A fresh
  person detection automatically hands over to Seek, retaining its track ID
  even when subsequent heartbeats still say `explore`.
- Seek turns proportionally to the selected box's horizontal centre. Forward
  speed tapers to zero at 60 cm (maximum 0.35). Arrival latches stopped. The front
  ultrasonic measures the nearest obstacle, not necessarily the selected person.
- A missing target stops movement immediately; one second without a fresh
  observation latches `target_lost`. Stale/error detections cannot steer Seek.
- Any nonzero `v` or `w` returns immediately to teleop. This override stays
  latched against further autonomous heartbeats until a deadman release or an
  explicit `teleop` message. Release/repress or select teleop then reselect
  autonomy to retry an arrived/lost target.
- Autonomy centres pan at zero. Missing/stale front range stops all autonomous
  motion. Range below 25 cm also stops autonomous turns; normal teleop reverse
  and turn escape remain available. The PWM ceiling and camera guard still apply.

Status replies add optional `autonomy_state` and `target_id` fields; `mode`
reports the active mode, including automatic Explore-to-Seek handover. States
are `idle`, `forward`, `turning`, `tracking`, `target_missing`, `target_lost`,
`arrived`, `estop`, and `stick_override`. `estop` and `requires_release` retain
their existing safety meanings. Public status never grants a usable permit.

Jetson replies on the same socket, 10 Hz:

```json
{"seq": 812, "range_cm": 64, "estop": false, "mode": "teleop", "link_ms": 38}
```

`estop` is true when `range_cm < 25`; forward motion is blocked, reverse and turning are allowed.

## `POST /ask` (Quest -> Jetson), local assistant

Request: `multipart/form-data` with `audio` (webm/wav from the Quest mic) or JSON `{"text": "what do you see?"}`.
Response:

```json
{"question": "what do you see?", "answer": "One person sitting near the door, no obstacles ahead.",
 "action": null, "model": "moondream", "ms": 2140, "audio_url": "/tts/1042.wav"}
```

`audio_url` (optional) is the answer spoken by Piper TTS on the Jetson, for the Quest to play; if absent,
the Quest falls back to browser speechSynthesis. Mic and speaker are the Quest's own: the rover
needs no audio hardware for this.

`action` is `null` or a proposal like `{"type": "seek", "target_id": 3}` that the Quest shows as a
confirmation prompt. The Jetson never executes it without that confirmation.

## WebSocket `/ws/points` (Jetson -> Quest), binary, 3D scan

Little-endian: `uint32 n`, then `n * (float32 x, y, z, uint8 r, g, b, pad)`, metres, rover frame at the scan position
(x right, y up, z forward, origin at the camera with pan 0).

## Serial (Jetson <-> Uno), 115200 baud, newline-terminated ASCII

| Jetson sends | Meaning |
|---|---|
| `M <left> <right>` | Motor PWM, -255..255 |
| `P <deg>` | Pan servo, 10..170 (90 = forward) |
| `R` | Request range |

| Uno sends | Meaning |
|---|---|
| `R <cm>` | Ultrasonic range |
| `OK` / `ERR <msg>` | Ack |

Uno watchdog: no `M` for 300 ms -> motors stop on its own.
