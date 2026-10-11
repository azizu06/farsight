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

The phone-owned relay is implemented, and an owner has reported a two-phone
cellular transport run with phone Wi-Fi off and the Mac–Jetson commissioning
USB disconnected. This is not full Quest, physical-driving, or fault-recovery
qualification; see [phone relay](phone-relay.md). The rover service runs on the
Jetson at port `8443`. The MacBook serves the Quest app and proxies
HTTP/WebSockets on `localhost:8080`; Quest accesses it through USB ADB reverse.
The iPhones own the cellular tunnel; raw rover capture stays on local USB.
The earlier computer-side Tailscale/USB-tether path remains a fallback, whose
operator tether has been intermittent. Venue Wi-Fi is not in the production
route.
`tools.demo --simulate` launches
explicit ray-cast camera/map and virtual rover sources. See
[transport setup](transport-setup.md) and [native phone capture](../ios/README.md).

## HTTP

| Path | What |
|---|---|
| `GET /` | Quest WebXR app (built `quest/dist`) |
| `GET /stream.mjpg` | Camera, MJPEG, 640x480, 15 fps by default (camera capture loop, `jetson/camera.py`) |
| `GET /health` | `{"camera": bool, "detector_fps": float, "rover": bool, "estop": bool}`; optional `camera_pan: bool` (see below); optional `face_detector: bool`, `face_error: string\|null` (see below) |
| `GET /camera.jpg` | Latest JPEG; `X-Frame-Id`, `X-Frame-Age-Ms`, `X-Frame-Source`, optional `X-Frame-Server-Wait-Ms`; no-store |
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

### Opt-in enrolled face names

Named recognition is separate from the drill's face detector and has no motor
permissions. `FARSIGHT_IDENTITY_YUNET`, `FARSIGHT_IDENTITY_SFACE`, and optional
`FARSIGHT_IDENTITY_GALLERY` configure the Python server's CPU identity worker.
With neither model configured, it stays disabled. See [setup and phone
enrollment](face-identity.md) for pinned models, routes, and hardware checks.

`GET /phone/identity/enroll` serves the mobile photo-picker page;
`GET /phone/identity/status` reports enabled/ready/error state;
`GET /phone/identity/people` lists `{people:[{id,name,samples}],...}`.
`POST /phone/identity/people` accepts
`{name,image:<base64 JPEG/PNG without data-URL prefix>,consent:true,person_id?:id}`
and returns `{id,name,samples}`. `DELETE /phone/identity/people/{id}` removes the
templates and returns `{removed:true}`. Enrollment photos are not persisted.

`WS /ws/identity` emits JSON with
`{type:"identity",source,frame,received,age_ms,fresh,faces,gallery_generation,infer_ms,error}`.
Each face has normalized original-image `x,y,w,h,conf`, plus `name`, `person_id`
and cosine `similarity`; ambiguous/nonmatches are `Unknown` with null ID/score.
At most two faces include a base64 JPEG `thumbnail` of the actual aligned face,
at most112 ×112 and8KiB. Results expire at1000ms from original receiver receipt;
source/frame replacement and gallery mutation suppress obsolete names. Messages
are checked at5Hz, sent when the observation/state changes or for a2s heartbeat;
repeated heartbeats never refresh observation age.

Quest renders up to two independent LAST SEEN cards rather than attaching a
delayed name to a newer video image. Their expiry/source reset runs even when
the camera texture stops updating. These packets contain no persistent person
track, measured world destination, or authorization for named navigation.

Legacy camera age is receiver receipt age adjusted by reported producer encoding
time and server queue/decode delay. It excludes pre-callback exposure and transit.
Native verified RGB instead uses the earlier of receiver token issuance and
adjusted receipt, giving a conservative capture-age bound without synchronizing
phone and server clocks. Neither value measures camera-to-display latency.

JPEG `X-Frame-Server-Wait-Ms` optionally reports completed camera-handler
residence, rounded down. `X-Frame-Age-Ms` is measured after that work and still
refers to the original capture receipt. Quest subtracts a finite, nonnegative
server wait only when it does not exceed the request interval observed at
header arrival; network, body transfer, decode and presentation delays remain
in the displayed-age bound. Missing or malformed wait metadata retains the
full request interval. This optional header changes no capture eligibility,
source expiry or motion cutoff; legacy consumers and Rust servers remain
compatible. Repeating a frame ID never renews its displayed capture age.

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
For phone RGB-D, that picture can be rotated 180 degrees to match the mount.
`mirrored` records that rotation; depth and fused geometry remain sensor-native.
`sensor_key` is the server-created five-element capture key
`[session_id, map_epoch, socket_generation, frame_id, t_capture]`, or null for
sources without paired depth. The socket generation changes even when a phone
reconnects with the same session/epoch. Together with `source`, this key binds
inference to its exact retained depth capture; missing, stale or mismatched keys
cannot select a Nav Seek target. JPEG bytes stay internal. Clients may ignore
these fields when drawing ordinary 2D boxes.

Optional pose fields (YOLO11-pose), present when the detector is a pose model:

```json
{"id": 3, "cls": "person", "conf": 0.91, "x": 0.41, "y": 0.22, "w": 0.18, "h": 0.55,
 "kpts": [[0.50, 0.25, 0.97], "... 17 COCO keypoints, [x, y, conf], same normalization ..."],
 "down": false}
```

- `kpts`: 17 COCO keypoints (nose, eyes, ears, shoulders, elbows, wrists, hips, knees, ankles), each `[x, y, conf]`. Omitted if the detector has no pose.
- `down`: true when the person looks fallen (body axis more than 60 degrees from vertical, or a wide box when keypoints are unreliable). Omitted fields mean false/absent; clients must ignore unknown fields.
- `world` (optional): `[x, y, z]` metres in the map frame (ARKit world, Y up) of a tracked person's torso, from the median confident LiDAR depth in the box centre of the exact RGB-D frame it was detected on. Absent for RGB-only or estimated-depth sources and when too few depth samples are valid. The Quest draws a labeled, expiring `PERSON #id` capsule there (`quest/src/person-markers.js`); it is never fused into the map.
- `id` is stable across frames: the server runs a small IoU + velocity tracker (`jetson/tracker.py`) over every detector mode, keeping an id through brief dropouts (about 5 frames or 2 s) and never reusing one. Clients may rely on it for `target_id`.

## WebSocket `/ws/control` (Quest -> Jetson), JSON, 20 Hz timer plus one per new permit while the deadman is held

```json
{"session_id":"from latest control status","permit":"from latest control status",
 "seq":812,"deadman":true,"v":0.4,"w":-0.2,"pan":15,"mode":"teleop",
 "video_fresh":true,"video_age_ms":120}
```

- One controller at a time: a second socket is closed with 1008 "Another controller owns the rover",
  unless the current session has sent nothing for 1 s. A silent session is a dead socket not yet
  reaped, so the new socket replaces it (stop, then `release_required` as on any new session).
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

### Optional demo guards (`--demo-mode`, `FARSIGHT_DEMO_MODE=1`)

Not the simulated `--demo`. On the real rover these stops still zero the motors
but do not set `requires_release`: `serial_disconnected`, `release_required` (serial
reconnect or a new controller socket), `controller_disconnected`, `camera_stale`,
`video_stale`, `lease_expired`, `expired_permit`, `stale_sequence` and the Nav2
sensor faults (`nav_pose_lost`, `nav_tracking_limited`, `nav_depth_lost`,
`nav_sensor_reset`). The next fresh `deadman: true` heartbeat with a valid permit
resumes. Teleop heartbeats skip the camera-freshness checks (`camera_stale`,
`video_fresh` / `video_age_ms`); autonomy, scan and drive-profile modes keep them.
Unchanged: the 250 ms lease, the first press after server start needs one release,
protocol faults (`invalid_*`, `wrong_session`, `unsupported_mode`, oversized or
binary frames, `uno_rejected_command`) still latch, the Nav2 sensor gate still
holds motion at zero until pose and depth are fresh, and the Uno's 300 ms watchdog.

The operator relay has a separate, explicit `--demo-mode` flag, also accepted
and forwarded to the relay by `tools.demo`. It is **off by default**, has no
environment-variable fallback, and supports only single-route `/ws/control`;
combining it with `--bonded-upstream` is rejected before startup. In this relay
mode, a valid control JSON object retains its actual `video_fresh` and
`video_age_ms` fields: the relay skips only its displayed-video freshness
conversion of held-deadman commands to zero. It never substitutes fresh age or
source evidence. Malformed/non-object JSON and binary control still close with
1008, the 100 ms control send cap and connection teardown remain in effect,
and frame/pose render sockets cannot become control routes. Geometry, rendering
and backend validation are unchanged. The relay flag neither configures the
Jetson server's mode nor changes the viewer's gates. `GET /relay/health` reports
the selected relay `demo_mode` as a boolean; it is not camera qualification or
permission to move.

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
  turns randomly left/right at 0.35 for at most one second and repeats. A
  detected person never switches Explore to Seek; Seek runs only when selected
  with `mode: "seek"` and a `target_id`.
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
reports the active mode. States
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
  current mode, `null` when nothing can (the Python server no longer sends
  `fallback`: with `--nav2`, Explore/Seek hold still while Nav2 is unavailable).
  `message`: short human-readable detail.
- `path`: at most 200 `[x, z]` points of the current Nav2 plan (Douglas-Peucker
  reduced to 3 cm); empty when not navigating. `frontiers`: at most 50 `[x, z]`
  frontier goal points (widest first) that Explore chooses from. `goal`: the
  go-to point, or in a Nav2 Seek the standoff point 0.8 m short of the person
  (it moves with them); `null` in Explore.
- `revision`: integer, bumped whenever `path` or `frontiers` change (additive; the Rust server
  does not send it yet). **Nav lane (opt-in, for cellular):** a `/ws/control` client (direct or
  bonded) that adds `"nav_lane": "revision"` to its command messages (any heartbeat or release;
  latched for that socket, or for the bonded session) then gets `nav.path` and `nav.frontiers` only
  in statuses where `revision` changed, at most every 0.5 s (changes are coalesced to the newest),
  plus a refresh every 2 s and in the first status after opting in or after a bonded path attaches.
  Every other status omits both keys: absent means unchanged, so keep the last arrays (an empty
  array clears them). All other status and lease fields are unchanged and still arrive at 40 Hz.
  Clients that never opt in keep full arrays in every status. `GET /control/status` and
  `GET /nav/status` always carry the full arrays and `revision`.
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

The `/phone` receiver hands RGB-D packets to a latest-only camera validator
(one active JPEG decode plus one replaceable pending packet). It publishes the
validated camera JPEG before offering the packet to the separate map worker;
pose/fusion/dense/detection work cannot hold up this receiver. `/phone/rgb` uses
the same bounded pending policy. New pose tracking loss revokes opt-in LiDAR
drive immediately, without waiting for fusion. ARKit capture remains unverified;
this does not introduce a sensor clock qualification or relax the 500 ms view
guard, 250 ms control permit, disconnect stop, or explicit demo-mode behavior.

Jetson fragments contain one encoded frame, no B-frames, with short IDR
intervals (200 ms at the default 10 fps; at least one frame per GOP). Late joins wait for an IDR; a newer queued IDR supersedes older
queued fragments. Each subscriber retains at most two fragments; overflow of
dependent fragments and stale (500 ms) output close the stream for recovery.
The relay reserves a separate four-connection camera pool, and camera sends
have a 150 ms backpressure deadline. These bounds reset slow consumers rather
than accumulating seconds of video; they are not a glass-to-glass guarantee.

Optional `video_segment` diagnostics (all milliseconds):

| Field | Measurement |
| --- | --- |
| `camera_ingest_ms` | Jetson socket receipt to validated camera publication, including receiver queue/decode delay; null for sources without this instrumentation |
| `jetson_encode_ms` | ffmpeg raw-frame handoff to complete MP4 fragment, including pipe/encoder/mux delay |
| `relay_request_ms` | Relay forwarding `next` to receipt of the matching metadata (round trip plus server wait) |
| `relay_network_bound_ms` | `relay_request_ms` minus only bounded `server_wait_ms`; a round-trip bound, **not** measured one-way tailnet transit |
| `relay_residence_ms` | Metadata receipt to forwarding preparation on the relay |

Existing `age_ms` and `server_wait_ms` retain their freshness meaning; clients
must not add these diagnostics again to the existing conservative request age
bound. `GET /video/status` exposes camera age/ingest/drop metrics, encoder timing
and `mapping_processing_ms` without acquiring the map lock. `/health.video`
also exposes encoder timing. Mac `GET /relay/health` adds `video` last-hop metrics,
completed fragment count, send duration and stale-disconnect count. These are
local monotonic durations; phone-to-Jetson one-way transit cannot be inferred
from unrelated device clocks. Measure physical phone/USB, tailnet and Quest
presentation separately when the rig returns; software loopback is not that
qualification.

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

## `/health.fusion_bypass` and `/ws/map` status (Jetson -> Quest)

Optional `fusion_bypass: bool`, default `false`, in both the Python and Rust servers. With
`--fusion-bypass` (`FARSIGHT_FUSION_BYPASS=1`) the Jetson builds no 3D map. `points`,
`triangles`, `keyframes` and `frames` (fused frames) stay `0`, `revision` does not advance and
the map packets are empty. `reason` is `"Geometry lane external; fusion bypassed"`. The camera,
the depth view, `depth_range`, the pose and Nav2 sensing are unaffected, and `source` still
names the sensor (`IPHONE LIDAR`).

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

### Visual map wire cap (additive)

`/map/points.bin`, `/map/mesh.bin`, `/map/export.ply` and the legacy `/ws/map` binary push are
bounded for display: `FARSIGHT_MAP_POINTS_CAP` (`--map-points-cap`, default 30000) and
`FARSIGHT_MAP_TRIANGLES_CAP` (`--map-triangles-cap`, default 40000); `0` disables. Python and
Rust decimate identically at encode time: points keep records `i*N/cap`; the mesh keeps
triangles `i*T/cap`, then sends only their vertices in first-use order with remapped indices.
Python's phone-fusion mesh instead prefers connected coarse copies of each keyframe (depth grid
stride 4, then 8): the finest level at which every keyframe fits the cap, else the newest whole
keyframes at stride 8. It falls back to the triangle stride when a keyframe has no coarse copy,
and Rust still uses the stride, so capped mesh bytes differ between the two servers. Binary layouts are unchanged; the PLY follows the capped points. Map status (`/health`,
`/ws/map`) gains `capped: true` while either cap is decimating; `points` and `triangles` still
count the full map. Fusion, Nav2 depth/occupancy and scan results keep full resolution.

### Cellular map subscription (additive)

`/ws/map?points=http` opts into status-only delivery. Each status acknowledges
`points_transport: "http"`; this socket sends no binary point snapshots. Clients
pull authoritative replacements from `/map/points.bin`, with normal HTTP
`Accept-Encoding` negotiation through the operator relay. Plain `/ws/map` retains
its original status + raw binary snapshot behavior. Clients must wait for the
acknowledgement before using HTTP pulls, so an older server can still supply
legacy binary packets. Rust must mirror the query and optional status field.

### Additive point feed: `GET /map/points.delta?after=<revision>` (additive)

Returns only the voxel cells integrated since `after`, in the standard
16-byte XYZ/RGB record layout — roughly one fusion frame of points per
request instead of a whole-map replacement. `X-Map-Revision` is the new
client watermark; `X-Map-Generation` and `X-Map-Delta: 1` are also
emitted. `status()` exposes `points_delta: true` as the feature flag;
older servers 404 and clients fall back to full `/map/points.bin` pulls.

The feed is additive-only: each record's identity is its voxel key,
recomputed client-side as `floor(x/voxel_m), floor(y/voxel_m),
floor(z/voxel_m)` (a cell's mean always stays inside its voxel). Clients
merge keyed in place — new cells append, touched cells overwrite — and a
periodic full pull stays authoritative for capacity evictions. `?after=`
is required; missing or non-integer is a 400.

The canonical viewer merges deltas at ~300 ms cadence and keeps full
`/map/points.bin`/`/map/mesh.bin` pulls on the five-second authority
cadence, alternating point and mesh refreshes when both change. Map
transfers now continue while the operator holds the deadman (the map
fills as the rover turns); the Mac render picture and the camera view
still pause map transfers as before. H.264 suppresses both the rover JPEG poll
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


## Fine measured RGB-D visual lane

`GET /map/dense.bin` on Jetson is separate from the legacy capped map and
navigation geometry. The Mac mounts `/dense/manifest`, `/dense/chunk.bin`,
`/dense/delta.bin`, `/dense/status` and `POST /dense/metrics` on relay 8080.
See `docs/dense-map.md` for launch, budgets and evidence limits.
Jetson visual ingestion is latest-only background work (default 8 Hz, 12,288 real
pixels per job), disabled at startup by `FARSIGHT_DENSE=0`. The rate, samples/job
and thread-CPU budget are configurable (`docs/dense-map.md`); costly jobs reduce
admission rather than building a stale-frame backlog. Optional `/health.dense_worker`
reports enabled/budget/queued/processed/dropped/processing_ms/error independently
of the camera/coarse frame timing. Additional `cpu_ms`, `cpu_budget`,
`admission_hz` and cumulative `sampled_points` distinguish CPU use, current
admission ceiling and observations from unique retained cells. Normal
capture/control guards are unchanged.

Delta query: `epoch` (opaque server/capture-generation token), `after` (last
cursor, initially zero), `limit` (1–8192), `snapshot` (boolean), and `anchor`
(snapshot-start revision, echoed while paging). Response body is little-endian
u32 JSON byte length, UTF-8 JSON, then zero or more **16-byte** records:
signed i32 XYZ in millimetres, u8 RGB, u8 operation (1 upsert, 0 removal).
The fine-cell key is floor(XYZ_mm / 10) on every axis, including negatives.
Positions are quantized measured samples, not voxel centers or interpolated
geometry. RGB remains sRGB on wire and is converted to linear in the viewer.

Metadata: `format: "xyz_mm_rgb8_op"`, `epoch`, `source`, `cursor`, `reset`,
`snapshot`, `anchor`, `more`, `points` (current retained unique cells),
`voxel_m: 0.01`, `ttl_s: 30`. Real RGB-D `source` is `[UUID session, safe integer epoch]`.
Only the exact RGB-D simulation identity `["simulated-room", safe integer epoch]`
is admitted without a UUID; it uses a separate `simulated_rgbd` key and the
`SIMULATED RGB-D` label. It is never admitted on the geometry lane. Missing
identity with nonempty data, unknown names and unsafe epochs remain errors.
A mismatched epoch or journal gap returns
`reset:true` once at bootstrap start: clear the consumer, then follow
`snapshot`/`anchor`/`cursor` until `snapshot:false`. The final snapshot cursor
returns to its start anchor so normal deltas replay updates/removals concurrent
with bootstrap. A transport reconnect alone never changes capture identity.
Jetson working-cache budget eviction emits no deletion; expiry, explicit
removal and reset do. Mac's RGB-D history pool keeps observed upserts across
producer-cache expiry tombstones and same-epoch snapshot bootstraps, clearing
on a true producer epoch change or local capacity eviction. Its optional
`retained_history:true` manifest/delta field identifies historical visual
geometry; `ttl_s:86400` preserves compatibility for older consumers. A consumer
that supports the history flag retains the historical cloud across source
disconnects and reports source age separately. Native geometry retains its
explicit anchor-removal semantics. These history rules do not apply to rover
camera/depth freshness or navigation geometry.

The endpoints negotiate Brotli/gzip over bounded records; no complete
multi-million-point body is transferred. Chunk replies use the same framing
with only upserts plus `chunk`, `revision`, `lod`, `sample_voxel_m` and
`points` (selected count). A chunk is an authoritative bounded replacement,
not an addition to its previous version. Mac renderer full-detail deltas use
the delta contract above. The gray ARKit source is selected separately with
`source=geometry`; it never overwrites calibrated RGB-D colors.

## Phone-built mesh lane (v1)

The rover iPhone constructs ARKit mesh anchors. The Mac assembles their latest
replacements and serves Quest map snapshots locally. Jetson retains camera,
fresh measured depth and pose for inference/control/Nav2; `--fusion-bypass`
skips its visual map reconstruction. ARKit mesh updates are reconstruction,
not a replacement for live navigation depth.

The dedicated phone geometry port is **8083**, separate from control/video
8081 and bonded fallback 8082. Native operator-phone USB forwarding uses Mac
**18445 → operator phone 8083 → rover phone tailnet 8083**. Direct tailnet
consumers may pull the rover phone endpoint. The stream path is
`/geometry/stream`; one WebSocket binary message is one complete record.
The standalone Mac assembler binds loopback **8484** by default. These port
reservations describe the implemented software pipeline; they do not prove an
installed phone is serving it yet. Existing demo routes remain available.

The fixed **124-byte** header matches `rust/farsight-geometry` and
`ios/FarsightCapture/GeometryWire.swift`. All numeric values are little-endian:

| Offset | Field |
|---|---|
| 0 | u8 version, 1 |
| 1 | u8 operation: replace 0, remove 1, reset 2, snapshot-end 3 |
| 2 | u16 coding: 0 raw, 1 zlib-framed DEFLATE, 2 standard Brotli; other values are rejected |
| 4 | u64 wire sequence |
| 12 | u64 capture map epoch |
| 20 | 16 raw UUID bytes: capture session |
| 36 | 16 raw UUID bytes: anchor (zero for reset/snapshot-end) |
| 52 | 16 f32 values: column-major anchor-to-ARSession-world transform |
| 116 | u32 vertex count |
| 120 | u32 scalar index count (three per triangle) |

Payload is `vertex_count * 12` bytes of local f32 XYZ, then `index_count * 4`
bytes of u32 indices; v1 has neutral vertex colors. Coordinates are metres,
ARKit X right/Y up/negative Z forward, in the same sensor world as the existing
camera transform. No headset origin is implicitly substituted. Phone encoding
uses public native `COMPRESSION_BROTLI` (fixed level 2, iOS 15+).
Apple `COMPRESSION_ZLIB` is raw DEFLATE and **does not** match the zlib-framed
flag; use an explicitly compatible encoder if selecting that flag.

Records are at most 256 KiB including the header, with at most 65,536 vertices
and 65,536 scalar indices. Replace requires a nonempty triangle mesh, finite
coordinates/transform and indices in range; other operations carry no payload.
Decode output must be bounded to the declared size before allocation/read,
and trailing/missing bytes fail the whole record. Producer oversize rejects
are counted rather than presented as complete geometry.

The producer coalesces dirty state by anchor before assigning wire sequence.
Sequence increases across geometry transport reconnects within the same
capture session/epoch. A reconnect sends reset, a complete bounded snapshot
of retained anchors, then snapshot-end before live deltas. Consumers stage
that snapshot atomically, reject stale sequence values and discard incomplete
staging on disconnect. A new capture session/epoch clears prior coordinates.
Pending and published geometry both have vertex/triangle/anchor budgets;
assembled packets must stay inside Quest decoder limits (600,000 vertices,
160,000 triangles). Geometry traffic has bounded bulk pacing and never shares
or delays the control permit lane. Network recovery grants no motion intent.

### Dense chunked delta feed: `GET /geometry/chunks` (Mac accumulator)

The standalone assembler also serves the retained cloud as an incremental
delta for the Mac dense-reconstruction lane, which accumulates millions of
points beyond the Quest-packet budget. `after=<seq>` pages the journal as
concatenated v1 wire records; `limit=<n>` caps the journaled-ops page at
512. Records use coding 0 (raw) and identity transforms — positions are
already baked into ARSession world coordinates at ingest. Every emitted
record stays inside the 256 KiB packet bound: a Replace whose raw payload
would exceed it is split into fragments sharing the same (seq, anchor);
consumers concatenate all fragments of a (seq, anchor) group in order
before applying the replacement.

Op codes on this feed: replace, remove, reset, and **evict (4)** — an
assembler-outbound marker emitted when the publisher drops an anchor for
its memory budget. Evict is not a geometry removal: dense accumulators
may retain the chunk (this is how the million-point set accumulates past
the publisher's window), while bounded consumers apply it like remove.
Real remove ops are genuine producer removals. Snapshot-end stays a
staging signal and is never replayed.

Consumers page until a response is empty, then track the cursor in the
`X-Geometry-Next-Seq` header; `X-Geometry-Remaining` reports journaled
ops still to read, and `X-Geometry-Session`/`X-Geometry-Epoch` mirror the
current capture session so consumers can detect a session swap even on
empty pages. The journal is bounded at 8192 ops. A bootstrap is sent when
`after=0`, when `after` precedes the oldest journal entry, or when
`after` exceeds the producer's current seq (a cursor left over from a
previous session/epoch, since seqs restart per session): a synthetic
reset record first, then every retained chunk, then the journaled tail —
the complete snapshot always arrives in one response, never truncated by
`limit`. `X-Geometry-Truncated: 1` marks a bootstrap where coverage was
lost (`after` was nonzero).

`GET /geometry/status` additionally reports `retained` with the actual
published counts `{anchors, vertices, triangles, points, bytes}` and
`observed` with cumulative published counts `{anchors, vertices,
triangles}` since session start — retained unique points reported
separately from historical observation count, per the density audit.

### Geometry dispatch on the operator relay (`--geometry-upstream`)

When a relay is given `--geometry-upstream`, the geometry lane owns this
exact surface — everything else still routes to the Jetson:

- `GET /map/status` and `GET /geometry/status` — the merged puller status
  packet (generation, revision, mesh_revision, points, triangles,
  `points_transport: "http"`, `geometry_source`, nested `geometry` block
  with `matched`/`retained`/`connected`/`snapshot_complete`/`partial`/
  `anchors`/`revision`/`receiver_revision`/`last_seq`/`reason`).
- `GET /map/mesh.bin`, `GET /geometry/mesh.bin`, `GET /map/points.bin`,
  `GET /map/export.ply` — served only after `snapshot_complete`; before
  that they are 503 "Matching geometry not available". `points.bin` is a
  u32 LE vertex count plus the same 16-byte vertex records as mesh.bin
  (no index block). `export.ply` is binary_little_endian PLY (15-byte
  vertices, `\x03` + 12-byte faces). Binary routes carry
  `ETag "geometry-{generation}-{revision}-{path}"` (If-None-Match → 304),
  `Cache-Control: no-store`, `X-Geometry-Source`, `X-Geometry-Retained`,
  `X-Map-Generation`, `X-Map-Revision`, `X-Map-Session`, `X-Map-Epoch`,
  and negotiated br/gzip/zstd variants.
- `GET /ws/map` — JSON status every ~500 ms plus a binary points packet
  on each revision change; `?points=http` opts into status-only.
- `POST /map/reset`, `POST /map/pause` → 409 "Phone reconstruction cannot
  be reset or paused here." (restart phone capture to reset its AR
  world).
- `GET /geometry/chunks` — the dense delta feed documented above.

`tools/geometry_relay.py` remains the reference implementation of this
surface; `rust/farsight-geometry` serves it byte-for-byte.

## Local Mac render transport (opt-in)

`tools.demo` and `tools.operator_relay` accept `--render-upstream` with a
credential-free loopback HTTP(S) origin. The renderer's reserved port is
**8090**. Only `/ws/macview` and `/ws/macview-pose` are routed there; without
configuration these endpoints return 503 and never fall through to Jetson.
The relay does not launch the renderer. The same Quest USB reverse on **8080**
can carry both endpoints once the renderer and viewer are installed.

Both lanes forward text/binary messages unchanged. Frame messages are bounded
to 8 MiB with a 500 ms send deadline; pose messages to 16 KiB with a 100 ms
deadline. At most four render connections use a separate pool, with no replay,
automatic reconnection or admission queue. Existing control and geometry
connections use their own pools. Local Host/Origin checks still apply.

`/relay/health.render` reports `configured`, the two `endpoints`, and
`transport_only: true`. It does not establish renderer readiness, hardware
encoding, headset display or stereo. The initial renderer contract uses the
`video_init` / `video_segment` request-and-fragment pattern with explicit
`stereo: false`; renderer message semantics and pose validation belong to the
renderer/viewer owners. Render/pose timestamps do not establish physical
camera freshness and never enter `/ws/control` through this transport.

The Quest AUTO viewer (`?macview=1`, `quest/src/mac-auto.ts`) sends pose
version 1 unchanged: `type`, `version`, `sent_mono_ms`, `sent_wall_ms`, `head`,
and `ray` (currently `null`). `head` is in **AR-map coordinates**, not raw
headset coordinates. It adds these optional fields, which a v1 renderer may
ignore:

| Field | Meaning |
|---|---|
| `pose_id` | Monotonic per Quest page; also sent as `frame_id`. Renderers echo it in `video_segment.pose`. |
| `space` | `"ar_map"`. |
| `session_id` | Renderer view identity, new per AUTO entry: a UUID in secure contexts, otherwise an opaque token. |
| `map_revision` | Point revision of the Quest's local map, or `null`. |
| `viewer` | `{x, y, z, yaw}` of `mapWalk`. |
| `eyes[]` | `{eye, position, quaternion, projection[16]}` per XR eye from the frame's `XRViewerPose`, ordered left then right, in map coordinates. `eye` is `"left"` or `"right"`; `projection` is column-major. |
| `fov_y_deg`, `aspect` | The picture the Quest would like to receive. |
| `stereo` | `false`. |

With `?stereo=1` the viewer opens `/ws/macview?layout=sbs-left-right` and shows
a segment as a stereo pair only when it carries `stereo: true`,
`layout: "sbs-left-right"` and positive `eye_width`/`eye_height`; each half is
placed from the matching sent eye pose and projection. A segment with neither
field is shown flat. A pair is not shown, and the local map returns at once, when
its layout is invalid or its echo lacks this page's `session_id`, names an unknown
`pose_id`, or has no valid left and right eye pose and projection. Any echo naming
another `session_id` is rejected, flat or stereo.

Without a current tracked pose, the packed Mac renderer keeps a **desktop
preview** running from the retained camera with illustrative 64 mm eye spacing.
Its echo uses a separate `desktop-preview-*` session and `desktop_preview: true`;
`pose_to_frame_ms` is null because no tracking input produced that picture.
Current valid headset eyes always replace this fallback. Headset clients reject
the marked desktop frames, including any that accidentally match their session;
the desktop canvas can display them. Geometry source/age/counts are unchanged.
This works even when an unworn headset keeps its visual pose socket connected.
It does not qualify headset stereo, tracking or pose-to-photon latency.

The Mac server's packed mode is explicit: `--stereo sbs-left-right`, default
coded size 1920×1080 (960×1080 per eye); `--width 1280 --height 720` selects the
smaller pair. Without that mode the server remains mono. A packed producer
requires the matching `layout=sbs-left-right` video-socket query; legacy viewers
cannot consume a packed stream as mono. The render page receives the same
`layout` query and `camera=snap` so captured transforms match their pose echo.

For each packed capture, `window.__mac` must declare `stereo: true`,
`layout: "sbs-left-right"`, `eye_width`/`eye_height` matching the coded halves,
and `poseEcho` with the pair's `session_id`, `frame_id`/`pose_id` and both labeled
eye poses/projections. The server validates that pair, carries its identity in
`video_segment.pose`, fixes output dimensions, and publishes only single-frame
fragments. Missing eyes, singular projections, format mismatch or partial pairs
are unavailable; no mono picture is advertised as stereo. Valid metadata and
encoder readiness alone do not prove two rendered eye views: raster parallax,
decoded eye assignment and actual headset acceptance remain separate gates.

The viewer finds a frame's render pose by the echoed `pose_id`, or failing
that by an exact `sent_mono_ms`. It sizes the picture from the echoed
`video_segment.fov_y_deg`, which defaults to 50.

### Optional read-only native RGB-D preview

`FARSIGHT_RAW_CAPTURE=1` enables `GET /phone/frame.bin`; default off. It returns
one accepted phone frame in the existing RGB-D packet layout (u32LE JSON length,
JSON, native JPEG, float32LE metric depth, uint8 confidence), with optional
`map_generation` and `capture_source` metadata. Native intrinsics/pose and JPEG
are from that exact capture, before display rotation. Reset/socket/tracking/
disconnect/pause/>2s age reject with 503; new session/epoch/generation clears the
consumer's cloud. `X-Capture-Key` identifies session:epoch:generation:frame;
`?after=KEY` returns 204 if unchanged. `X-Capture-Age-Ms` measures adjusted receipt
age, not verified sensor latency. Responses use the existing bulk pacer. This
GET takes no drive lease and opens no control socket. The existing Mac relay
`/phone/` prefix forwards it without a relay restart; desktop lab proxy exposes
it as `/capture/frame.bin` on :8086. See `docs/dense-viewer.md` for bounded
worker reconstruction, lifecycle and measurement limits.

The optional raw RGB-D export also negotiates HTTP `br`/`gzip` (identity stays supported); decoded native frame bytes are unchanged. `Vary: Accept-Encoding` accompanies encoded export responses. Relay compression pass-through must explicitly include `/phone/frame.bin`; otherwise its existing identity request preserves compatibility without bandwidth savings. This does not change the shared bulk pacing or capture freshness limits.
