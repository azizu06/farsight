# Interfaces

Coordinate contract changes with affected owners on the agent message board.
Direct commits to main follow the current team workflow.

```
Quest 2 (WebXR, three.js)
   |  http://localhost:8080 via USB ADB reverse (secure origin on the headset)
   v
MacBook operator relay (tools/operator_relay.py, serves the Quest app, proxies HTTP + WebSocket)
   |  localhost:18443 -> USB-only iproxy
   v
Operator iPhone localhost:8081 -> native phone Tailscale over cellular
   |
   v
Rover iPhone Tailscale IP:8081 -> fixed local USB upstream
   v
Jetson Orin Nano (on the rover)  -- the brain
   camera -> TensorRT YOLO -> detections
   camera -> Depth Anything V2 (stretch) -> point cloud
   control server: 20 Hz pump, 250 ms lease, ultrasonic e-stop (opt-out)
   |  USB serial 115200
   v
Arduino Uno (ELEGOO V4 board) -> motors, ultrasonic (pan servo optional; the phone camera is fixed, look-around is digital)
```

The confirmed phone-owned relay is being implemented and is not yet hardware
qualified; see [phone relay](phone-relay.md). The rover service runs on the
Jetson at port `8443`. The MacBook serves the Quest app and proxies
HTTP/WebSockets on `localhost:8080`; Quest accesses it through USB ADB reverse.
The iPhones own the cellular tunnel; raw rover capture stays on local USB.
The earlier computer-side Tailscale/USB-tether path remains a fallback, whose
operator tether has been intermittent. Neither path depends on venue Wi-Fi.
`tools.demo --simulate` launches
explicit ray-cast camera/map and virtual rover sources. See
[transport setup](transport-setup.md) and [native phone capture](../ios/README.md).

## HTTP

| Path | What |
|---|---|
| `GET /` | Quest WebXR app (built `quest/dist`) |
| `GET /stream.mjpg` | Camera, MJPEG, 640x480, 15 fps by default (camera capture loop, `jetson/camera.py`) |
| `GET /health` | `{"camera": bool, "detector_fps": float, "rover": bool, "estop": bool}`; optional `camera_pan: bool` (see below); optional `face_detector: bool`, `face_error: string\|null` (see below) |
| `GET /camera.jpg` | Latest JPEG; `X-Frame-Id`, `X-Frame-Age-Ms`, `X-Frame-Source`; no-store |
| `GET /control/status` | Read-only control status; no usable permit |
| `GET /mission/report` | Evidence, persistence/source labels, drill results, control `summary` and `timeline` (below) |
| `POST /mission/drill` | `{"duration_s":90,"victims":3}` starts a rescue drill; `GET` polls it. See below |
| `POST /mission/triage/{key}` | Regenerate one person's SBAR now (`key` from the report, URL-encoded); 404 unknown, 503 busy |
| `GET /speak` | `{"phrases": [{"id","en","es"}], "speaker": bool, "speaker_cmd", "voices": ["en","es"]}` |
| `POST /speak` | `{"phrase": "help_coming", "lang": "en"\|"es"}`; see below |

Rescue drill status (`GET`/`POST /mission/drill`, and `drill` in the report) is
scored on the rover. Older fields stay. `found`/`total`/`found_ids`/`found_at_s` count
`cls:"face"` tracks whose stable id stayed in view 1 s; one missed frame is
tolerated, and id flicker does not count. `estops` counts forward drives the
range stop blocked (0.5 s debounce). `pose`, `breadcrumb` `[{t,x,y}]` and
`route_home` `[{x,y}]` (rover first, start last) are dead reckoned from the
applied v/w, in metres, x forward, y left, so they are approximate
(`route_approximate: true`). `summary` is null while running. Once time runs out
or every victim is found, it is
`{time_s, found, total, estops_avoided, found_at_s, breadcrumb}`.

Optional `face_detector: bool` and `face_error: string|null` appear in `/health` and
in `GET`/`POST /mission/drill`. The drill scores only `cls:"face"` boxes, which exist
only when the rover runs with `--face-model`. `face_detector` is true once that model
loaded; false means a drill can never score. `face_error` names why a given model
failed to load (a bad path disables only the face pass; `detector_error` stays
null and the primary detector keeps running) and is null otherwise. Replay servers
omit both. The Quest drill HUD and the settings drill line show "No face model:
victims cannot be found" when `face_detector` is false; absent means no warning.

Legacy camera age is receiver receipt age adjusted by reported producer encoding
time and server queue/decode delay. It excludes pre-callback exposure and transit.
Native verified RGB instead uses the earlier of receiver token issuance and
adjusted receipt, giving a conservative capture-age bound without synchronizing
phone and server clocks. Neither value measures camera-to-display latency.

### Native capture freshness

Both phone WebSocket routes accept optional hello `supports_capture_permits:true`.
The server pipelines an unpaced `capture_permit` every 50 ms:
`{type:"capture_permit",version:1,session_id,map_epoch,token,valid_for_ms:250}`.
Tokens belong to one socket/session/epoch, expire after 250 ms on the server
monotonic clock and are revoked on disconnect. At most eight are retained.

Native RGB converts the actual AVCapture sample timestamp into the Core Media
host clock and captures an exposure strictly after receiving a current token.
Its frame header echoes `capture_permit`, `capture_permit_received_t`,
`capture_clock:"avcapture_sync_to_host"`, `capture_clock_verified:true` and
`capture_bound:true`; `t_capture` is the converted sensor timestamp. The two
phone times share a clock; neither is compared to server time. Verification
requires a live matching token and valid finite, ordered phone timestamps.
The trusted native producer must enforce exposure ordering and sensor freshness;
these fields are not authentication or sensor attestation.

Optional health fields `camera_capture_verified` and `camera_drive_ready`
separate verified physical-driving eligibility from `camera` viewing freshness.
Optional `camera_capture_diagnostics` contains bounded producer observations:
`clock`, `t_capture`, `callback_host_t`, `sensor_timestamp_raw_t`,
`sensor_timestamp_host_t`, `sensor_host_delta_ms`, and `sensor_age_ms` when
provided as finite numbers. It excludes tokens and device identifiers. These
observations support on-device qualification; numerical clock alignment alone
does not prove a documented shared clock or change drive eligibility.
Physical serial control requires drive eligibility. Legacy/unverified RGB and
ARKit RGB-D remain viewable and mappable, but do not qualify physical driving.
ARKit exposure-clock qualification remains a hardware task; measured LiDAR plus
driving is not yet qualified. UVC and explicit simulation/replay behavior stay
as previously defined.

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

## WebSocket `/ws/control` (Quest -> Jetson), JSON, 20 Hz timer plus one per new permit while the deadman is held

```json
{"session_id":"from latest control status","permit":"from latest control status",
 "seq":812,"deadman":true,"v":0.4,"w":-0.2,"pan":15,"mode":"teleop",
 "video_fresh":true,"video_age_ms":120}
```

- `v` forward speed and `w` turn rate, both -1..1. `pan` camera yaw in degrees, -80..80, 0 = forward.
  With `--pan-mount none` (`FARSIGHT_PAN_MOUNT=none`, a rover with no pan servo; the headset
  pans digitally) the server still validates `pan` but then ignores it: it never blocks forward
  drive, the Uno never gets `P`, and `pan_deg` stays 90. Default `servo` is unchanged.
- `mode`: `"teleop"`, `"seek"` (also sends `"target_id"`), `"explore"`, `"scan"`, or `"goto"` (with `--nav2`); the last three are specified below.
- No valid message for 250 ms, or `deadman: false` -> motors zero. Always.
- One socket owns control. A new connection starts stopped. First send an
  ordered release `{"session_id":"...","seq":0,"deadman":false}`; then a new
  physical press can send motion. Repeat release/press after a fault.
- `permit` is an opaque token issued every 25 ms (40 Hz; servers before
  2026-10-10 issued every 50 ms) and expires 250 ms after its **server issue**,
  checked against the Jetson monotonic clock. Arrival never grants a new 250 ms
  lease. Expired/out-of-order/invalid messages stop motion; sequence numbers
  must be increasing JavaScript-safe integers.
- Clients should send the next heartbeat as soon as a new permit arrives while
  driving, in addition to their 20 Hz timer: waiting for the timer spends up to
  50 ms of the permit's 250 ms. A 20 Hz timer-only client remains valid; it just
  has less margin on a jittery link (`docs/link-lab.md`).
- `video_age_ms` must be finite and at most 500 ms; `video_fresh` must be true.
  The server independently requires a fresh camera. Source loss stops all
  motion. Client decoded/drawn freshness also stops when the display stalls.

### Optional drive-profile modes (`velocity`, `calibrate`)

Both are **off by default**; an unconfigured server answers them with
`reason: "unsupported_mode"` and teleop/Explore/Seek are untouched. They use the
same session, ordered `seq`, server permit, held deadman, fresh-video, camera,
lease and forward-range gates as every other mode. `/control/status` and
`/ws/control` status gain `drive_profile`, `calibration_allowed` and
`drive_mode` (`normalized`, `velocity` or `calibrate`).

- `mode: "velocity"` needs `python -m jetson.server --drive-profile FILE`
  (written by `tools/calibrate_drive.py`). Send `v_mps` (forward, |v| <= 5) and
  `w_radps` (**counter-clockwise positive, ROS `cmd_vel` sign**, |w| <= 10) in
  place of `v`/`w`. The profile maps them to left/right PWM (per-side floor, trim,
  measured curves) and saturates keeping curvature; `--max-pwm` still caps. Only
  `v_mps > 0` is held by the ultrasonic range stop. This is the entry for the
  ROS 2 bridge's Nav2 `cmd_vel`.
- `mode: "calibrate"` needs `--allow-calibration`. Send integer `left_pwm` and
  `right_pwm` (|x| <= `max_pwm`), applied raw. Used only by the calibration tool;
  a stationary spin or reverse is not range-stopped, any positive net forward
  command is. `camera_yaw_offset_rad` in the profile is the angle from the camera
  forward axis to the rover's travel direction (+ counter-clockwise), for the
  bridge's `base_link`->`camera_link` yaw. See [drive calibration](drive-calibration.md).

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
- Autonomy centres pan at zero. The PWM ceiling and camera guard still apply.
- The proximity stop is **on by default**: missing/stale front range stops all
  autonomous motion, range below 25 cm also stops autonomous turns, and forward
  translation is blocked; reverse and turn escape remain available. Turning it off
  is an explicit opt-in (server `--proximity-stop off` / `FARSIGHT_PROXIMITY_STOP=0`)
  for runs where navigation steers around obstacles; the rover as built has no
  ultrasonic, so its Uno never blocks on range and `R` always replies `-1`. Then range never stops motion, and the reactive policy
  keeps its 70 cm turn cue and estimates Seek arrival from the box width when there
  is no range.

Status replies add optional `autonomy_state` and `target_id` fields; `mode`
reports the active mode, including automatic Explore-to-Seek handover. States
are `idle`, `forward`, `turning`, `tracking`, `target_missing`, `target_lost`,
`arrived`, `estop`, `stick_override`, and `goto` (see the navigation extension). `estop` and `requires_release` retain
their existing safety meanings. Public status never grants a usable permit.

Jetson replies on the same socket, 40 Hz (20 Hz before 2026-10-10; mission
replay still samples control at 20 Hz):

```json
{"type":"control_status","session_id":"...","permit":"opaque token","permit_ms":250,
 "seq":812,"range_cm":64,"estop":false,"mode":"teleop","link_ms":38,
 "rover":true,"simulated":false,"requires_release":false,"reason":"active",
 "left_pwm":40,"right_pwm":20,"pan_deg":90,"max_pwm":100,"min_pwm":0}
```

`estop` reports a blocked forward translation: unknown/stale range, range below
25 cm and a servo pan outside ±10° (the range conditions only while the
proximity stop is on, the default; the pan condition only with `--pan-mount servo`, the default).
Camera/link failure stops all motion regardless. `link_ms` is permit age at command arrival; the browser's acknowledged
sequence RTT is a different measurement. Optional autonomy fields follow below.

### Optional navigation status and go-to goals (Nav2)

Opt-in: the Python server only navigates with `--nav2` (`FARSIGHT_NAV2=1`) and the
ROS 2 stack from [navigation.md](navigation.md) running on the Jetson. Without the
flag, Explore/Seek are exactly the reactive policy above, `nav` is absent from
status and `/health`, `POST /nav/goal` answers 409 and `POST /nav/cancel` returns an
`idle` nav object with `backend: null` (the Rust server answers the same way).

Servers with the planner add an optional `nav` object to `/control/status` and
every `/ws/control` status. Coordinates are voxel-map world metres: map `x, z`
(ROS odom `-y, -x`, see [navigation.md](navigation.md)) on the floor plane.

```json
"nav": {"state":"driving","mode":"goto","goal":{"x":2.0,"z":-1.5},
        "path":[[0,0],[1.1,-0.6],[2.0,-1.5]],"frontiers":[[3.2,0.4]],
        "backend":"nav2","message":"2.9 m to go"}
```

- `state`: `idle` (no goal, deadman not held, or Nav2 ready but idle), `planning`,
  `driving`, `recovering` (back-up/spin/wait/costmap clearing), `arrived` (latched
  stopped), `failed` (Nav2 aborted, e.g. no path) or `unavailable` (no bridge, a
  silent bridge whose commands are ignored, or missing navigation sensors; the
  rover stops).
- Navigation sensors: Nav2 only drives with a normal-tracking ARKit pose at most
  0.5 s old and LiDAR depth at most 1 s old, both from the current phone session
  and map epoch. A fresh RGB camera alone never qualifies. Losing either while
  driving stops the rover even with a live lease and fresh Nav2 command, with
  control `reason` `nav_pose_lost`, `nav_tracking_limited`, `nav_depth_lost` or
  `nav_sensor_reset` and `requires_release`; it resumes only after the sensors
  recover and the operator presses again. A new phone session or map epoch also
  disarms the goal (Nav2's goal and costmaps are reset).
- `mode`: `explore`, `seek`, `goto` or `null`. `backend`: `nav2` when Nav2 drives the
  current mode, `fallback` when the reactive policy does (Explore/Seek without a
  bridge), `null` when nothing can. `message`: short human-readable detail.
- `path`: at most 200 `[x, z]` points of the current Nav2 plan (Douglas-Peucker
  reduced to 3 cm); empty when not navigating. `frontiers`: at most 50 `[x, z]`
  frontier goal points (widest first) that Explore chooses from. `goal`: the
  go-to point, or in a Nav2 Seek the standoff point 0.8 m short of the person
  (it moves with them); `null` in Explore.
- `POST /nav/goal` with `{"x": 2.0, "z": -1.5}` (metres, millimetre
  precision; optional `session_id` from the `/ws/control` status binds it to that
  controller) returns `202` with the nav object, or `409` `{"nav": <nav object>,
  "message": reason}` when the backend is unavailable, navigation sensors are
  missing, no controller is connected (or `session_id` names another one), a scan
  is running, or the deadman is held but the permit or camera is not valid;
  `422` for non-finite values. Posted while the deadman is not held (released,
  lease expired, release required), the goal is **staged**: `202` with `mode: null`,
  `state: "idle"` and the `goal`. Nothing moves until a new deadman press, after
  an ordered release when `requires_release` is set, passes the usual heartbeat
  gates (permit, drive-ready camera) and carries `mode: "goto"`. `POST /nav/cancel`
  (no body) cancels and returns the nav object. `GET /nav/status` adds a diagnostic
  `detail` object.
- `mode: "goto"` heartbeats are accepted like `explore`/`seek`: same session,
  ordered sequence, server-issued permit, held deadman and fresh-video checks, and
  any nonzero `v`/`w` latches stick override. A `202` only arms the goal: it never
  changes the drive mode, so the rover moves only once the operator's own held
  heartbeats carry `mode: "goto"` (no `target_id`, `v: 0`, `w: 0`); releases still
  carry `mode: "teleop"`, and a zero-stick `teleop` heartbeat in flight during the
  POST simply leaves the goal armed. A release or view change needs a new deadman
  press but keeps the goal armed; the next held `goto` heartbeat resumes it, using
  only Nav2 commands computed after that press. Stick input, STOP, cancel, a new
  goal, selecting Explore/Seek, the controller socket closing (or a new one taking
  over) and a serial disconnect disarm it.
- Viewer (`quest/src/nav-view.js`, `nav-overlay.ts`): an unknown `state` hides the
  overlay, and a path with any non-finite point is not drawn at all. Stick input,
  STOP and cancel send `POST /nav/cancel`. Servers that do not accept
  `mode: "goto"` stop with `unsupported_mode` and return `409` from `/nav/goal`.
- Nav2 velocity commands move the rover only through ControlState: a live held
  lease in an autonomy mode, the PWM ceiling, the camera guard and the pan rule
  apply as for every other mode, and a command older than 250 ms stops the motors.
- `/ws/nav` is the ROS bridge's loopback-only socket; other hosts are closed with
  1008. The operator relay never needs it.

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
The current implementation supports optional loopback llama-server, a running
whisper-server (`--whisper-url`, the default path: the model stays loaded; `--whisper-bin`/`--whisper-model` run
local whisper-cli instead) and Piper. With no VLM, `model` is `local-observation-summary`;
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
  `/ws/control`, which still needs the held deadman. Confirmed Scan arms a scan;
  after closing Settings, release then hold the deadman with centred sticks to start it.
- `timings`: `{"stt": ms|null, "vlm": ms|null, "tts": ms|null}` per stage; `ms` stays the total.
- `source`, `simulated`, `observation` (facts given to the model), `assistant_warning`, `speech_warning`.

Accepted `audio`: WAV, WebM/Opus, Ogg/Opus, or MP4/AAC (whatever the browser's MediaRecorder gives), at most 8 MiB.
Headset (`quest/src/assistant-voice.ts`): a proposal shows "Seek person #3?" with Confirm / Dismiss for
20 seconds; a new question, cancel, or expiry clears it.

## Mission telemetry (additive fields on `GET /mission/report`)

The server samples `control.status()` read-only at most 10 Hz for the whole run (no
`--record` needed); it never issues permits or commands, and states shorter than 100 ms
can be missed. Timestamps are Unix seconds, like `started_at`. All new fields are optional
for consumers; Quest renders them in the mission report dialog (`quest/src/mission-report.js`).

```json
{"summary": {"samples": 412, "sample_hz": 10, "last_sample_ts": 1791640100.2,
   "min_range_cm": 18, "min_range_at": 1791640065.0, "estop_count": 1,
   "stop_reasons": {"forward_blocked": {"count": 2, "first_ts": 1791640010.0, "last_ts": 1791640065.0}},
   "lost": {"rover_link": {"count": 1, "total_s": 3.5, "intervals": [{"start": 1791640080.0, "end": 1791640083.5, "duration_s": 3.5}]},
            "controller": {"count": 0, "total_s": 0, "intervals": []},
            "camera": {"count": 0, "total_s": 0, "intervals": []}},
   "detections_by_class": {"person": 2}, "encounters": 2},
 "timeline": [{"kind": "estop", "ts": 1791640065.0, "range_cm": 18, "reason": "forward_blocked"}]}
```

- `estop_count`: rising edges (0.5 s debounce) of `estop:true` with `range_cm < 25` or
  `reason:"forward_blocked"`; a missing range alone is not an e-stop.
- `stop_reasons`: each change into a non-`active` `reason`, counted with first/last time; at most 64 reasons.
- `lost`: `rover_link` is `rover:false`, `controller` is no control session, `camera` is a stale camera,
  each counted only after it was up once. An open interval has `end: null`. At most 100 intervals per kind.
- `timeline`: newest 300 flagged events, `kind` one of `detection` (`cls`, `key`, `range_cm`), `estop`,
  `stop` (`reason`, `range_cm`, `requires_release`), `<rover_link|controller|camera>_lost` and
  `_restored` (`lost_s`). With `--mission-dir`, non-detection flags also append to `events.jsonl`
  (first 10,000), beside the existing detection events.
- Each `encounters[]` entry gains `first_range_cm`: the forward ultrasonic range within 1 s of
  first sight (not a measured distance to that object), or `null`. `first_seen`/`last_seen` are Unix seconds.

## SBAR triage (additive fields on `GET /mission/report`)

Every new person track gets an SBAR note at once: a deterministic template, then (with
`--assistant-url`) a local VLM rewrite from that track's cropped snapshot, one model call at a
time, at least `--triage-interval` (3 s) apart, off the detector loop. If the model fails, the
template stays with a `warning`. `report.triage` maps encounter key (`"<source>:<track id>"`) to:

```json
{"situation": "...", "background": "...", "assessment": "...", "recommendation": "...",
 "model": "local-vision-model" | "template", "note": "not a medical assessment",
 "track_id": 3, "source": "USB CAMERA", "conf": 0.9, "down": true, "range_cm": 120,
 "snapshot_url": "/mission/snapshots/detection-0-41.jpg", "generated_at": 1791626868.0, "ms": 1166}
```

Each `encounters[]` entry also gains `key` and `sbar` (the same object or `null`). With
`--mission-dir`, notes append to `triage.jsonl` and `report.json` is rewritten on every note.
`/health` gains `triage: {"enabled", "pending", "generated", "last_ms", "min_interval_s"}`.

## `/health.map_fusion` and `/ws/map` status (Jetson -> Quest)

Optional `map_fusion: bool`, default `true`. With `--map-fusion off` (`FARSIGHT_MAP_FUSION=off`) the
Jetson builds no 3D map: `points`, `triangles` and `keyframes` stay `0`, `revision` does not
advance, the map packets are empty and `reason` says fusion is off. Camera, depth view, pose and
Nav2 sensing are unaffected. `source` still names the sensor (`IPHONE LIDAR`). Python server only
so far.

## `/health.camera_pan` (Jetson -> Quest), default camera mount

Optional `camera_pan: bool` is `true` only when the server runs with `--scan-pan` (a physical
pan servo is installed). Python and Rust servers both report it. The Quest viewer uses it only when the operator has chosen no mount
(`?mount=` or a saved choice): `false` selects the fixed mount, which sends `pan: 0`, so head
yaw never trips the server's and Uno's off-center forward block. A missing field (older
servers) keeps the servo default. It never changes any safety rule.

## `POST /speak` (Quest -> Jetson), survivor phrases

Fixed phrase ids only (`help_coming`, `found_you`, `can_you_hear`, `call_out`, `stay_calm`), English or
Spanish, rendered once by Piper into the TTS directory. Response:

```json
{"phrase": "help_coming", "lang": "es", "text": "La ayuda viene en camino, no se mueva.",
 "audio_url": "/tts/phrase-help_coming-es.wav", "played": true}
```

`played` is true when the rover's own speaker started it (`--speaker-cmd`, default `auto`: `aplay -q`
with an ALSA card, else `paplay` with a Pulse sink; `none` disables). When false, the headset plays
`audio_url`; `speaker_warning` explains a failed player. Errors: 404 unknown phrase, 400 language,
503 missing voice (`FARSIGHT_PIPER_MODEL_ES` for Spanish). `/health` gains
`speech: {"speaker", "speaker_cmd", "voices"}`.

## WebSocket `/ws/points` (Jetson -> Quest), binary, 3D scan

Legacy reserved contract; not implemented. Camera-only scans use `/ws/map` and
`/map/points.bin` instead, in the mapper's **negative-Z forward** coordinates.

Little-endian: `uint32 n`, then `n * (float32 x, y, z, uint8 r, g, b, pad)`, metres, rover frame at the scan position
(x right, y up, z forward, origin at the camera with pan 0).

## Camera-only scan: `POST /scan`, `GET /scan`, existing map transport

`POST /scan` takes `{"session_id":"from control socket","permit":"latest control permit"}`
and returns 202 with a scan status. Refuses with 409 if already scanning, moving,
in Explore/Seek, lacking a live held lease, missing fresh camera-only RGB, or
missing the configured depth model (except synthetic `--demo`). Invalid bodies return 422.
The public `/control/status` cannot grant this permit. Motors remain disabled
unless the server explicitly enables a physical serial port or a virtual rover.

Keep sending the ordinary ordered, fresh-video, held-deadman envelope on
`/ws/control`, now with `mode: "scan"`, `v: 0`, `w: 0`. Only operator heartbeats
renew the lease. Release, disconnect, expired permit, stale camera/video, or
teleop/stick override cancels; stale/unsafe range also cancels rover rotation.
One exception covers the pre-POST heartbeat racing `POST /scan` on its separate path:
until the first `mode: "scan"` heartbeat, a `teleop` heartbeat with `v == 0` and
`w == 0` whose permit was issued before the scan began is handled as a scan heartbeat
(lease capped by that permit as always, `pan` ignored). Nonzero `v`/`w`, any other mode,
or a teleop heartbeat with a newer permit still cancels.
Scan mode without an active reservation fails stopped. Completion requires a release.

`GET /scan` and `/health.scan` report `state` (`idle`, `sweeping`, `complete`,
`aborted`), `running`, `simulated`, `approximate`, `backend`, `views` (yaw degrees,
depth milliseconds, sample count), `points`, `scan_ms`, `markers`, and optional `error`.
Fields beyond `state` and `running` are populated when a scan starts.
Views sweep -80, -40, 0, 40, 80 degrees. Physical camera pan requires `--scan-pan`;
the default is bounded timed rover rotation and always reports `approximate: true`.
No SLAM, odometry, or stitching between rover positions is implied.

On success, replace the existing MapState cloud, clear surfaces, increment map
generation/revision, and pause fusion. The ordinary `/ws/map` status adds optional
`scan` metadata for this retained snapshot; its existing binary point snapshot and
`/map/points.bin` use the same 16-byte XYZ/RGB records as phone mapping, with X right,
Y up, **Z negative ahead**, origin at the starting camera. Each new scan replaces
the previous origin; resuming phone/demo fusion clears the scan first.
`scan.markers` contains up to 500 `{id, cls, position:[x,y,z]}` observations from
exact-source/frame-matched sweep detections, projected at estimated box-centre depth.
The existing viewer renders yellow marker points and toggles live camera / scan
with Quest A; trigger + left Y requests a scan. See [scan setup and evidence](scan.md).

### Map route compression (additive)

`GET /map/points.bin`, `/map/mesh.bin` and `/map/export.ply` honour `Accept-Encoding`: `br` is
preferred, then `gzip`; `q=0` refuses a coding. Bodies under 1 KiB and requests without a
supported coding stay identity. Responses always carry `Vary: Accept-Encoding`. The Python server
encodes once per map version (generation, revision, mesh revision) and reuses it across clients.
Decoded bytes are identical to the uncompressed body; no other route is compressed.

### Cellular map subscription (additive)

`/ws/map?points=http` opts into status-only delivery. Each status acknowledges
`points_transport: "http"`; this socket sends no binary point snapshots. Clients
pull authoritative replacements from `/map/points.bin`, with normal HTTP
`Accept-Encoding` negotiation through the operator relay. Plain `/ws/map` retains
its original status + raw binary snapshot behavior. Clients must wait for the
acknowledgement before using HTTP pulls, so an older server can still supply
legacy binary packets. Rust must mirror the query and optional status field.

The canonical viewer allows one map download at a time, with at least two seconds
between starts, alternating point and mesh refreshes when both change. It closes
the map socket and aborts map/preview downloads while the operator holds the
deadman, retaining displayed geometry. Release reconnects and obtains new
authoritative snapshots. Camera view also pauses map transfers as before. H.264 suppresses both the rover JPEG poll
and the settings RGB JPEG poll. Control leases, permits, view age and release
latches are unchanged.

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

## Opt-in bonded upstream control (experimental v1)

Server flag `--bonded-control` adds `/ws/control/bonded`; default `/ws/control`
and its single-owner/replay behavior remain unchanged. This endpoint is for
trusted operator-relay upstreams, not a second Quest controller. At most two
paths attach to one logical owner. Prototype software tests are not permission
to activate physical driving or a claim of cellular continuity qualification.

The first frame, within 3 seconds, is a JSON attachment request:

```json
{"type":"attach","version":1}
```

A second path joins the live owner with its private credential:

```json
{"type":"attach","version":1,"secret":"64 hexadecimal characters"}
```

The private reply has `type:"attached"`, `version:1`, `session_id`, `secret`
(a random 256-bit hex credential) and `path_id`. The relay consumes this reply;
it must never forward it to Quest, put the credential in a URL, log it or save
it in qualification artifacts. Public session IDs are not attachment secrets.
A wrong secret or third path is rejected without revoking the existing owner.
A retired secret cannot create a new owner. New ownership still requires release.

After attachment, the original control command schema applies. The relay
constructs each fresh command once and sends identical copies on live paths;
it does not retry or queue motion for recovery. The receiver uses a bounded
256-entry canonical-payload history. Known identical copies and obsolete copies,
including an old release arriving after a newer press, do not call the control
state again or renew its lease. A conflicting known sequence stops with
`conflicting_duplicate` and requires a fresh release. Expired copied motion is
discarded; the existing output/permit expiry still stops motion at its deadline.
Malformed inputs retain existing fault behavior. Deduplication applies only to
this authenticated opt-in transport, not the legacy control socket.

One logical 40 Hz publisher issues each status and permit once, then copies the
same status to attached paths. Status adds a safe integer `status_seq` that
strictly increases; the relay forwards each ordinal once and rejects older
copies without treating them as fresh. Each path has a single-slot status queue
and a 100 ms send deadline. A congested path is detached independently. Losing
one attachment preserves the owner; losing the last immediately ends it and
revokes the credential. Camera, displayed view, serial, proximity, lease and
release guards remain authoritative throughout. Evidence recording remains
once per logical stream at 20 Hz and contains no attachment credential.

The relay flag `--bonded-upstream` and operator phone pinned dual-listener mode
are opt-in. Mac USB forwarding uses `tools.phone_usb --dual-route` (18443/18444
to phone8081/8082). Redundant view delivery uses complete bounded JPEG responses,
with original age headers and Quest decode/display checks. Health adds
`relay_transport.profile: "bonded-jpeg"`; Quest restricts that profile to JPEG
without overwriting its saved video preference. The initial relay does not
automatically reattach a lost path; the surviving attached path remains active. H.264 streams cannot be spliced across
routes and are not included in this continuity claim. The full two-path software
fault harness and actual phone/Quest/wheels-up rehearsal remain required.

### RGB-D receiver binding propagation

`/phone` now checks frame provenance against that socket's live capture-permit
issuer before publishing camera data. Supported bindings carry both the
conservative server-issued origin and issuer identity in the same immutable
camera snapshot; producer encoding delay can only make its age older. Closing
the issuer immediately revokes camera drive eligibility even if JPEG viewing
remains fresh. Reconnection/reset clears retained binding evidence.

The ARKit RGB-D endpoint currently has **no supported verified capture clock**.
It rejects attempts to claim the RGB-only `avcapture_sync_to_host` contract on
that endpoint. `arkit_unverified` remains view-only. Binding propagation prepares
the receiver for a supported producer contract; it does not qualify the current
ARKit clock or fabricate camera/pose timestamps. The AVFoundation `/phone/rgb`
clock and capture-permit rules are unchanged.

Opt-in `--lidar-drive` (`FARSIGHT_LIDAR_DRIVE=1`, default off) makes
`camera_drive_ready` true for an `IPHONE LIDAR` frame received under 500 ms ago
while that `/phone` session is connected with ARKit tracking `normal`. It does
not mark the frame `camera_capture_verified`, and no wire field changes. The
lease, deadman, release, camera-stale and disconnect stops are unchanged.
