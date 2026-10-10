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

The current rover service runs on the Jetson at port `8443` (HTTP inside
Tailscale or configured HTTPS). The MacBook serves the Quest app and proxies
HTTP/WebSockets on `localhost:8080`; Quest accesses it through USB ADB reverse.
Two USB-tethered phones supply the cellular tunnel. The private LAN is an
alternative, not a venue-network dependency. `tools.demo --simulate` launches
explicit ray-cast camera/map and virtual rover sources. See
[transport setup](transport-setup.md) and [native phone capture](../ios/README.md).

## HTTP

| Path | What |
|---|---|
| `GET /` | Quest WebXR app (built `quest/dist`) |
| `GET /stream.mjpg` | Camera, MJPEG, 640x480, target 30 fps |
| `GET /health` | `{"camera": bool, "detector_fps": float, "rover": bool, "estop": bool}` |
| `GET /camera.jpg` | Latest JPEG; `X-Frame-Id`, `X-Frame-Age-Ms`, `X-Frame-Source`; no-store |
| `GET /control/status` | Read-only control status; no usable permit |
| `GET /mission/report` | Evidence, persistence/source labels, drill results |
| `POST /mission/drill` | `{"duration_s":90}`, person-track encounters, not distinct identities |

Camera age is receiver receipt age adjusted by reported producer encoding time
and server queue/decode delay. It does not measure pre-callback sensor exposure
or the phone-to-Jetson transit. Measure physical camera-to-display latency on
the actual rig; never advertise this metadata as that full measurement.

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
{"session_id":"from latest control status","permit":"from latest control status",
 "seq":812,"deadman":true,"v":0.4,"w":-0.2,"pan":15,"mode":"teleop",
 "video_fresh":true,"video_age_ms":120}
```

- `v` forward speed and `w` turn rate, both -1..1. `pan` camera yaw in degrees, -80..80, 0 = forward.
- `mode`: `"teleop"` or `"seek"` (seek also sends `"target_id"`).
- No valid message for 250 ms, or `deadman: false` -> motors zero. Always.
- One socket owns control. A new connection starts stopped. First send an
  ordered release `{"session_id":"...","seq":0,"deadman":false}`; then a new
  physical press can send motion. Repeat release/press after a fault.
- `permit` is an opaque token issued every 100 ms and expires 250 ms after its
  **server issue**, checked against the Jetson monotonic clock. Arrival never
  grants a new 250 ms lease. Expired/out-of-order/invalid messages stop motion;
  sequence numbers must be increasing JavaScript-safe integers.
- `video_age_ms` must be finite and at most 500 ms; `video_fresh` must be true.
  The server independently requires a fresh camera. Source loss stops all
  motion. Client decoded/drawn freshness also stops when the display stalls.

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
{"type":"control_status","session_id":"...","permit":"opaque token","permit_ms":250,
 "seq":812,"range_cm":64,"estop":false,"mode":"teleop","link_ms":38,
 "rover":true,"simulated":false,"requires_release":false,"reason":"active",
 "left_pwm":40,"right_pwm":20,"pan_deg":90,"max_pwm":100,"min_pwm":0}
```

`estop` also covers unknown/stale range and an ultrasonic pan outside ±10°.
Those conditions block forward translation; camera/link failure stops all
motion. `link_ms` is permit age at command arrival; the browser's acknowledged
sequence RTT is a different measurement. Optional autonomy fields follow below.

## H264 over WebSocket `/ws/video`

Enable the server with `--h264`; enable the viewer's H.264 checkbox separately.
Initial profile is 640x480, 10 fps, 500 kbps software encoding. The Orin Nano has
no NVENC. JPEG remains a compatibility path.

The server sends `video_init` JSON containing `mime` (actual H.264 codec string)
and `source`, followed by MP4 initialization bytes. The client pipelines exactly
two `{"type":"next","request_id":1}` requests. Each response is JSON then
fragment bytes:

```json
{"type":"video_segment","request_id":1,"media_start_s":0.1,"media_end_s":0.2,
 "frame":42,"source":"PHONE RGB","age_ms":90,"server_wait_ms":40,
 "samples":[{"media_start_s":0.1,"media_end_s":0.2,"frame":42}]}
```

Media times use the MP4 presentation timeline. Decoder frame callbacks select
the matching capture sample for overlays and freshness. Add full per-request
elapsed time and subsequent local elapsed time to `age_ms`. Subtract only the
finite, nonnegative, bounded `server_wait_ms` measured after server request
receipt; missing or invalid wait metadata retains the full conservative bound.
Never subtract network transit. Congestion resets the complete stream from
initialization/keyframe; no dependent fragments are silently dropped and reused.
An encoder failure clears readiness, closes consumers, and restarts the encoder.

## `POST /ask` (Quest -> Jetson), local assistant

Request: `multipart/form-data` with `audio` (webm/wav from the Quest mic) or JSON `{"text": "what do you see?"}`.
The current implementation supports optional loopback llama-server, local
whisper-cli and Piper. With no VLM, `model` is `local-observation-summary`;
successful VLM answers use `local-vision-model`. Answers never execute motors;
`action` may contain a confirmation-only proposal, and motion still needs the deadman. Missing audio dependencies return 503 while typed text remains
available. Uploads are bounded to 8 MiB and local conversion to twenty seconds.
See [assistant setup](assistant-setup.md).
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

Optional additive fields (clients ignore unknown fields):

- `action` types: `{"type": "seek", "target_id": <int>}` (a fresh tracked person; one flagged `down` first,
  otherwise the largest box) or `{"type": "scan"}`. Proposals come from keywords in the question
  ("go to", "seek", "approach"; "scan", "look around"), never from the model. The answer then ends with
  "Confirm on the headset to ...". On confirm, the Quest sends `mode: "seek"` + `target_id` on
  `/ws/control`, which still needs the held deadman; scan has no rover handler yet.
- `timings`: `{"stt": ms|null, "vlm": ms|null, "tts": ms|null}` per stage; `ms` stays the total.
- `source`, `simulated`, `observation` (facts given to the model), `assistant_warning`, `speech_warning`.

Accepted `audio`: WAV, WebM/Opus, Ogg/Opus, or MP4/AAC (whatever the browser's MediaRecorder gives), at most 8 MiB.
Headset (`quest/src/assistant-voice.ts`): a proposal shows "Seek person #3?" with Confirm / Dismiss for
20 seconds; a new question, cancel, or expiry clears it.

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
