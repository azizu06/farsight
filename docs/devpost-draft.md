# Devpost draft (#15)

Working text for the Knight Hacks IX submission, due **Sun Oct 11, 10:00 AM** (rules:
[hackers-guide.md](hackers-guide.md)). Paste sections into Devpost once the TODOs are resolved.
Claim status lives in [devpost-checklist.md](devpost-checklist.md); this page is the prose.

Every number carries a tag:

- **[HW]** measured on real hardware, with the board or device named.
- **[SIM]** measured in software on a Mac with simulated or recorded inputs.
- **[TODO-RIG]** only a number from the assembled rig will do. Replace it, or delete the sentence.

Before submitting, search this file for `TODO` and resolve every hit. Do not upgrade a [SIM]
number to a hardware claim, and if the demo ends up running a fallback tier
([fallback-playbook.md](fallback-playbook.md)), say which tier in "What it does".

---

## Tagline (200 characters max)

Put on a Quest 2 and become the rover: live head-coupled video, on-robot AI that finds people,
and safety that lives on the Jetson, all over a cellular link.

## Inspiration

Before a firefighter, inspector or rescuer walks into a smoke-filled room or a damaged building,
someone has to guess what is inside. Small scout drones help, but most of them show a flat video
feed on a phone, and the useful part (who is in there, where, and whether they are moving) is left
to the person squinting at the screen. We wanted the opposite: an operator who feels like they are
standing where the robot stands, with the robot itself doing the looking for people and refusing
to do anything unsafe. The scout drone in Rainbow Six Siege was the mental model, and training for
real search scenarios was the reason to build it.

## What it does

Farsight is a ground rover you inhabit from a Quest 2.

- **See.** The rover's phone camera streams to the headset as H.264 video. Turn your head and the
  view pans with you (digital look-around over the phone's wide frame; the camera is fixed on the chassis).
- **Drive.** One hand: hold the right trigger and push the right stick. Let go and the rover stops. The trigger is a
  deadman: the Jetson issues a short permit that the headset must keep echoing, so a dropped link,
  a stale picture or a lost controller all end in a stop that needs a fresh press to clear.
- **Find people.** A YOLO11 pose model on the Jetson boxes every person, tracks them with stable
  ids, and flags someone who appears to be lying down. Boxes are drawn on the video frame they were
  computed from, or faded when they lag behind it.
- **Run a rescue drill.** A 90-second training scenario: find the "victims" (a face held in view
  for one second counts), avoid emergency stops, and get a score plus an approximate route home.
- **Ask the rover.** Push to talk: "what do you see?" Whisper (base.en by default) transcribes it
  on the rover, Qwen3-VL-2B answers from the live picture and the detections, and Piper speaks the
  answer. The assistant can propose Seek or Scan, but cannot move the motors on its own; a proposal
  needs a confirm and a held trigger. Setup and model sizes:
  [assistant-setup.md](assistant-setup.md).
- **Scan the room.** The rover stops, looks in five directions, and Depth Anything V2 turns each
  view into estimated metric depth. You stand inside the resulting point cloud in VR. With a
  LiDAR iPhone as the camera, the map uses measured depth instead, and the viewer labels which one
  you are looking at.
- **Explore and Seek.** Explore wanders until it finds a person; Seek drives toward a selected
  target. Both run on the Jetson under the same permit. By default they use the reactive policy in
  `jetson/autonomy.py`, which never reads LiDAR. With `--proximity-stop` left on (the default) an
  unknown range holds the rover still. With it off, Explore drives forward and turns on a 4 s timer,
  and Seek calls arrival when the person's box is half the frame wide. LiDAR only blocks obstacles
  through Nav2 (`--nav2`), which is opt-in and has only been driven in simulation **[SIM]**
  ([navigation.md](navigation.md)).
- **Point at the floor.** In the map view, aim and set a go-to goal. The headset draws a glowing
  path, a goal beacon and frontier markers. Cancel is a thumbstick click. The deadman still has to
  be held, and the live camera view hides the overlay (`quest/src/nav-overlay.ts`). Go-to needs
  opt-in Nav2 (`--nav2`). Without that flag, `/nav/goal` answers 409 and the HUD shows
  "Go-to rejected". The headset view was only checked against a mocked `nav` status, and Nav2
  itself has only run in Docker **[SIM]** ([navigation.md](navigation.md),
  [PR #83](https://github.com/azizu06/farsight/pull/83)).
- **Leave a record.** Every encounter goes into a mission report with time, snapshot, and an
  SBAR triage note (Situation, Background, Assessment, Recommendation) drafted on the rover. The
  report also keeps a stop-reason histogram, link-loss intervals and a bounded timeline
  (`jetson/mission.py`). If a tracked person's pose changes from up to down, the SBAR note is
  written again ([PR #81](https://github.com/azizu06/farsight/pull/81)). On this rig there is no
  range: the only thing that can set it is the Uno's `R` reply, and that always answers `-1`, so
  the SBAR note says "range unavailable", and the report view says "no range" or
  "Min range: no ultrasonic reading".
  The simulator's constant 120 cm is simulated, not phone depth **[SIM]**. Runs can be recorded and
  replayed with a REPLAY banner and inert controls.

No venue Wi-Fi is needed. The two kits talk over a Tailscale tunnel carried by two phones'
cellular data, and every model runs locally on the rover.

TODO-DEMO: one sentence on which tier was shown live (full rig, bench rig, or labelled replay).

## How we built it

![Farsight architecture](assets/architecture.png)

The diagram and a written walkthrough are in [architecture.md](architecture.md).

**Two kits.** The operator kit is a Quest 2, a MacBook and a phone. The MacBook runs
`tools/operator_relay.py`, which serves the bundled headset app on `127.0.0.1:8080` and proxies
every HTTP and WebSocket route to the rover; `adb reverse` carries that port to the Quest over USB,
so Quest Browser sees a local secure origin and can enter WebXR. The rover kit is a Jetson Orin
Nano, an iPhone mounted fixed on the chassis as the camera, and an ELEGOO Arduino Uno that drives
the motors. The ultrasonic sensor and the pan servo are off the rover as built. The iPhone's LiDAR
is the only measured depth, and the only obstacle source Nav2 can use; the default Explore and
Seek policy never reads it. Look-around is a digital crop of the phone's wide frame: fixed mode
keeps physical pan at zero and moves a 70% crop inside the measured image
(`quest/src/camera-view.js`, `quest/src/viewer-settings.js`,
[PR #76](https://github.com/azizu06/farsight/pull/76)). The rover phone sends
camera/depth to the Jetson over local USB and uses cellular/Tailscale to link
to the operator phone. The Mac reaches that phone over USB, and the Quest stays
USB-connected to the Mac. Venue Wi-Fi is not in the app path. A real two-phone
transport run is recorded; the complete Quest, physical-driving and fault
recovery path still needs the full on-rig check.

**On the Jetson** (`python -m jetson.server`, FastAPI):

- `CameraService` takes frames from our iOS capture app (`/phone/rgb` for RGB, `/phone` for LiDAR
  RGB-D with ARKit pose) or a USB camera. A missing camera stays missing; it never falls back to a
  simulated one.
- `VideoService` encodes fragmented-MP4 H.264 with FFmpeg and tags every sample with its capture
  id, so the headset can match detections to frames.
- `PerceptionService` runs YOLO11n-pose through a TensorRT FP16 engine built on the board, an IoU
  plus velocity tracker, and an optional UltraFace pass for the drill.
- `ControlService` owns safety: a 250 ms permit lease, a 500 ms stale-view guard, and the Explore
  and Seek policies. It talks to the Uno over USB serial (`/dev/ttyUSB0`, CH340), and the Uno
  firmware independently zeros the motors after 300 ms without a command, on an invalid command,
  an over-long line, or the D2 button. The firmware's `R` always replies `-1`, because there is no
  ultrasonic. Forward blocking from a range reading is on by default (a blank
  `FARSIGHT_PROXIMITY_STOP` means on), so this rover must be started with `--proximity-stop off`
  or that reply is treated as a stale range and forward driving never starts.
- `MapState` and `ScanService` fuse LiDAR frames or run the five-view Depth Anything V2 scan. With
  `--nav2`, the same LiDAR depth is what Nav2's costmap sees (`ros2/farsight_nav`).
- `Mission`, the drill scorer and `TriageService` write the report; `SceneAssistant` and
  `PhraseSpeaker` run the voice loop with whisper.cpp, llama.cpp and Piper (English and Spanish).
  The default vision pair is Qwen3-VL-2B-Instruct (Q4_K_M + Q8_0 projector, 1.55 GB) and the default
  speech model is Whisper base.en; SmolVLM-500M and Qwen2.5-VL-3B still load if that pair is missing
  ([assistant-setup.md](assistant-setup.md)). Voice controls stay hidden unless `/health` reports
  `assistant.whisper`. A TensorRT pose failure rebuilds the engine and context, at most once every
  5 s. `--require-tensorrt` fails the demo check if the detector is not that engine
  ([demo-checklist.md](demo-checklist.md); `--allow-cpu-detector` opts out).
- The operator relay also proxies `POST /speak`, the headset's survivor-phrase route. Before that,
  the Quest got HTTP 405 from the relay ([PR #73](https://github.com/azizu06/farsight/pull/73),
  [interfaces.md](interfaces.md)).

**In the headset** (`quest/src`, TypeScript, Three.js, Vite): WebXR rendering of the video texture,
H.264 decode, frame-matched detection boxes and pose skeletons, the deadman and controller-tracking
safety, a LIVE / SIMULATED / REPLAY source banner, the drill HUD, push-to-talk, and a stop-reason
panel that tells the operator why the rover stopped and how to recover. The saved camera-mount
default is still servo (`quest/src/viewer-settings.js`). It switches to fixed only when nothing
was chosen and `/health` reports `camera_pan: false`, which the server does by default because
`--scan-pan` is off. Older servers without that field keep servo. In fixed mode, head yaw crops
the frame and is never sent as a servo angle. A smoothed head-yaw pan (EMA plus a slew limit, clamped to ±80°)
lives in `quest/src/pan-smoothing.js`. The map view draws the Nav2 path when the server sends one
(`quest/src/nav-view.js`).

**Getting it to fit a phone connection.** We built a fault proxy and link-budget harness
(`tools/qualify_link.py`, `tools/budget_link.py`) that runs the real server, real FFmpeg and real
pose inference behind a bandwidth cap and added delay. It showed that raw map points, not video,
were what saturated a 2 Mbps link, so the viewer now pauses map transfers while you drive and pulls
compressed snapshots when you stop.

**Opt-in navigation, still off unless asked** ([navigation.md](navigation.md)).
`python -m jetson.server --nav2` opens a loopback bridge to ROS 2 Jazzy + Nav2
(`ros2/farsight_nav`). Nav2 can drive go-to, Explore and Seek, but only while the operator holds the
deadman, the camera is fresh, and the current phone session still has a normal-tracking ARKit pose
(at most 0.5 s old) and LiDAR depth (at most 1 s old). Lose either and it stops; there is no dead
reckoning. Without the flag, `/nav/goal` answers 409 and Explore and Seek stay on the reactive
policy. Two more flags sit beside it, both off by default: `slam:=true` runs SLAM Toolbox so a
closed loop can correct ARKit drift (`ros2/farsight_slam`), and `costmap_source:=nvblox` swaps the
depth point cloud for an optional GPU map (`ros2/farsight_nvblox`, [nvblox.md](nvblox.md)). The
point-cloud costmap is the default. `tools/calibrate_drive.py` is the rig-day tool that would turn
Nav2's metres-per-second into wheel PWM; no floor run exists yet
([drive-calibration.md](drive-calibration.md)).

**How we checked it.** `npm --prefix quest run test:judge` drives the whole judge flow in a real
browser against the simulator and the replay fixture (15/15 after the nav-view step,
[PR #83](https://github.com/azizu06/farsight/pull/83)). Python tests cover the control state, tracker,
pose decode, drill, scan and assistant. Nav2 and SLAM were exercised in Docker on a Mac, labelled
simulation throughout ([navigation.md](navigation.md)). `python -m tools.rig_day` runs the rig-day
checks in one command (preflight, ready, measure, link qualify, pitch metrics, then a rehearsal
record) and writes `summary.md` as PASS, FAIL or SKIPPED. It sends no drive commands and holds no
permit; if another control socket owns the rover, or ownership cannot be checked, it refuses the
control-probing steps ([rig-rehearsal.md](rig-rehearsal.md),
[PR #77](https://github.com/azizu06/farsight/pull/77)). The drill scores only face boxes, so without
`--face-model` the HUD says victims cannot be found, and a bad face-model path disables only the
face pass ([PR #79](https://github.com/azizu06/farsight/pull/79)). Rust (`rust/`) is a parallel port
of the server with the same wire contract; the Python server is what we demo.

## Challenges we ran into

- **Safety that survives a bad network.** A deadman over cellular is only as good as its worst
  pause. Early runs expired the lease during short server or client stalls. We kept the 250 ms
  permit and fixed the causes instead: status-triggered command sends, a bounded CPU thread pool for
  pose inference, and decoupled camera capture from map fusion. Each change is measured in
  [qualification-results.md](qualification-results.md), including the runs that still failed.
- **Boxes on the wrong frame.** With the detector slower than the camera, exact frame matching drew
  boxes on about one frame in five. The viewer now uses the newest same-source result within eight
  frames and fades it by its lag ([PR #66](https://github.com/azizu06/farsight/pull/66)).
- **Bandwidth.** The 3D map alone needed more than a 2 Mbps link
  ([PR #67](https://github.com/azizu06/farsight/pull/67)). The fix was scheduling, not lower video
  quality.
- **Bringing up the Jetson.** The board needed USB recovery before it would boot
  ([jetson-recovery-results.md](jetson-recovery-results.md)), and NVIDIA's FFmpeg build ships
  without libx264, so we installed a private software encoder next to it
  ([jetson-runtime-results.md](jetson-runtime-results.md)).
- **Honest depth.** A depth model makes a convincing point cloud whether or not it is right. We
  label estimated and measured geometry separately all the way to the headset.
- **The obstacle sensor we do not have.** The plan assumed an ultrasonic on a pan head. On the
  rover as built there is neither, so a firmware range check would have stopped the rover forever
  on "no echo". The Uno no longer blocks forward motion, `R` always replies `-1`, and the server
  must be started with `--proximity-stop off` or it treats that reply as a stale range and blocks
  driving anyway ([PR #91](https://github.com/azizu06/farsight/pull/91)). The viewer also had to
  stop sending head yaw as a servo angle, or any turn past 10° looked like an off-centre pan and
  blocked forward motion ([PR #76](https://github.com/azizu06/farsight/pull/76)).
- **The phone bridge that would not start.** Tapping Start bridge failed on both phones with
  `POSIX(22)` because the bind port was set twice. Binding only through `requiredLocalEndpoint`
  fixed it; the listener on the rover phone then answered HTTP 200 from the Jetson over Tailscale
  ([PR #87](https://github.com/azizu06/farsight/pull/87)). Mac loopback tests passed before and
  after, so they did not catch it. That check is one HTTP 200 from the listener. No video or
  control has gone over the phones yet.
- **A cellular deadman without stretching the lease.** Emulating a Tailscale DERP leg on a Mac
  showed the 250 ms permit expiring in ordinary relay jitter. The headset now echoes a permit the
  moment it arrives, and the Jetson issues permits at 40 Hz instead of 20 Hz. The lease stays
  250 ms ([link-lab.md](link-lab.md)).

TODO-RIG: add the hardest real-rig problem (motor stiction, power under load) once it has happened.
Motor direction and the starting PWM are not tested yet; the lifted-wheel steps in
`rover/wheel_bench.py` are written and waiting. TODO-RIG: phone routing. The bridge starts and
answers one HTTP 200; video and control over the two phones are not done.

## Accomplishments that we're proud of

- **TensorRT pose on the actual Jetson Orin Nano [HW]:** the YOLO11n-pose engine ran at a median
  15.29 ms and p95 18.83 ms per raw inference (20 zero-input runs, while other builds were running on
  the board), and found 4 people in a real public photo
  ([board post](https://github.com/azizu06/farsight/issues/24#issuecomment-6097633513)). This is
  model inference time only, not camera-to-headset latency.
- **The rover's software stack runs on the board [HW]:** H.264 encode, the phone-protocol frame path,
  Piper speech and Whisper transcription, CUDA initialisation, and the depth model under ONNX Runtime
  ([jetson-runtime-results.md](jetson-runtime-results.md)). These are component checks with a static
  photo and generated speech, not the full rig.
- **Stops that do not depend on the headset [SIM]:** killing the encoder stopped the simulated rover
  in 169 ms, pausing the video consumer in 333 ms, and cutting every connection in 43 ms
  ([qualification-results.md](qualification-results.md)). TODO-RIG: real stop time on the floor,
  from releasing the trigger to wheels stopped.
- **Driving fits a 2 Mbps link [SIM]:** with the deadman held, moving-frame delivery went from 0% to
  100% and video receipt p95 from about 1.1 s to about 310 ms, at 100 ms simulated round trip
  ([PR #67](https://github.com/azizu06/farsight/pull/67)). Not a real cellular measurement.
  TODO-RIG: the same numbers over the two phones and Tailscale with venue Wi-Fi off.
- **Pose inference on a CPU budget [SIM]:** bounding the ONNX thread pool cut server CPU by roughly
  46-74% in the non-stalled trials on a Mac
  ([PR #49](https://github.com/azizu06/farsight/pull/49), [qualification-results.md](qualification-results.md)).
- **A judge flow that tests itself [SIM]:** 15/15 in a real browser, from page load through drive,
  Explore, Seek, drill, Ask, report, scan, the nav view and replay
  ([PR #65](https://github.com/azizu06/farsight/pull/65),
  [PR #83](https://github.com/azizu06/farsight/pull/83)). It was 14 before the nav-view step
  ([PR #81](https://github.com/azizu06/farsight/pull/81),
  [PR #85](https://github.com/azizu06/farsight/pull/85)).
- **A local assistant [SIM]:** on an M5 Pro, Qwen3-VL-2B at 256 image tokens answered in a warm
  median of 548 ms (peak RSS 2.7 GB) over 5 scenes x 2 questions, llama.cpp build 11146
  ([assistant-setup.md](assistant-setup.md), [PR #85](https://github.com/azizu06/farsight/pull/85)).
  The older Qwen2.5-VL-3B figure was about 0.7-0.9 s per warm answer, and about 1.0 s per SBAR note
  ([PR #31](https://github.com/azizu06/farsight/pull/31),
  [PR #35](https://github.com/azizu06/farsight/pull/35)). TODO-RIG: the same on the Jetson GPU, with
  pose TensorRT and H.264 also running. That combination is not measured.
- **A five-view 3D scan [SIM]:** 596.5 ms on a Mac CPU for 89,967 points, from synthetic input
  ([scan.md](scan.md)). Estimated depth; accuracy on a real room is not measured.
- **Path planning in a closed-loop sim [SIM]:** Nav2 with the RPP controller, on a Mac in Docker,
  drove five simulated worlds with 0 collisions. In the pop-up world the rover reached the goal in
  36.9 s (longest standstill 0.41 s); Explore saw 90% of a three-room floor in 24.9 s, about 4x faster than MPPI
  on the same run ([navigation.md](navigation.md),
  [PR #92](https://github.com/azizu06/farsight/pull/92)). Not a floor measurement.
- **Drift correction in a simulated room [SIM]:** SLAM Toolbox on two laps of a 16.7 m loop in an
  8 x 6 m room cut ARKit-like drift from 0.34 m final error to 0.06 m, and heavy drift (3% scale)
  from 1.36 m to 0.03 m ([navigation.md](navigation.md),
  [PR #86](https://github.com/azizu06/farsight/pull/86)). Not real iPhone LiDAR.
- **The Uno talks to the Jetson [HW]:** the CH340 serial driver was missing from the board's
  kernel, so it was built out of tree. The sketch compiled to 5100 bytes (15%) and 292 bytes of RAM,
  flashed over `/dev/ttyUSB0`, and `serial_test.py` reported 14 ok, 0 failed, 1 skipped (the skipped
  step needs the wheels to move) ([PR #91](https://github.com/azizu06/farsight/pull/91)).
  TODO-RIG: motor direction and the PWM level where the wheels first move.
- **The phone bridge starts on device [HW]:** Start bridge worked on an iPhone 16 Pro (operator) and
  an iPhone 17 Pro (rover). The rover listener on the tailnet returned HTTP 200 to the Jetson
  ([PR #87](https://github.com/azizu06/farsight/pull/87)). That is a listener check,
  not a video or control run.
- **Cellular control, emulated [SIM]:** with permits at 40 Hz and a send on every new permit, a
  DERP-typical 60 s held drive went from 43 to 31 interruptions per minute and from 67.8% to 78.5%
  of the time moving. The stutter threshold (5 or more forced releases per minute) moved from about
  125 ms to about 155 ms network round-trip p95. A second seed went 41 to 35 interruptions and 70.1%
  to 75.3% moving. A direct path never stopped. Field DERP (about 190 ms network p95) is still above
  the threshold ([link-lab.md](link-lab.md),
  [PR #88](https://github.com/azizu06/farsight/pull/88)). Not a measurement through the two phones.
  TODO-RIG: the same numbers over the two phones and Tailscale with venue Wi-Fi off.
- TODO-RIG: end-to-end detection fps on the Jetson while streaming video and accepting control.
- TODO-RIG: camera-to-headset latency, with how it was measured.
- TODO-RIG: nvblox on the Orin Nano (startup, RAM, GPU load, slice rate). The package, install
  script and smoke test exist and passed on a Mac against fakes, plus 22 Docker tests including a
  synthetic depth probe at 14.8 Hz ([nvblox.md](nvblox.md),
  [PR #84](https://github.com/azizu06/farsight/pull/84)). Nothing there has run on an NVIDIA GPU.
- TODO-RIG: a real drive-calibration profile. The fitter is tested only against a simulated rover
  ([drive-calibration.md](drive-calibration.md),
  [PR #82](https://github.com/azizu06/farsight/pull/82)).

## What we learned

- Put safety on the robot. The headset and the network are the parts most likely to fail, so the
  rover has to stop by itself when they do. Independent timers (permit, stale view, Uno watchdog)
  were easier to reason about than one clever one. The ultrasonic was going to be a fourth stop. On
  this rover there is no range at all, so a missing range reading must not be treated as "something
  is in the way" (start with `--proximity-stop off`). LiDAR only avoids obstacles when Nav2 is on,
  and that path is simulation so far.
- Measure before tuning. The bandwidth problem turned out to be the map, not the video, and a lease
  failure turned out to be a stalled thread pool, not the timeout value.
- Label everything. SIMULATED, REPLAY, estimated and measured badges cost little and stopped us
  from fooling ourselves during rehearsals.
- TODO-TEAM: one personal line per teammate, in their own words.

## What's next for Farsight

- Close the hardware gates: floor driving with real stop timing, motor direction and the starting
  PWM (`rover/wheel_bench.py`), both phone tethers over cellular with venue Wi-Fi off, and power
  under load.
- Run Nav2 on the rig, not only in Docker. It stays opt-in until a floor run exists, and it needs a
  real drive profile first ([drive-calibration.md](drive-calibration.md)).
- Decide nvblox from the on-board smoke test. Until `jetson/nvblox-smoke.sh` passes on the Orin
  Nano, the costmap stays the depth point cloud ([nvblox.md](nvblox.md)).
- Turn SLAM on for a run that loops back. On a short demo with good ARKit tracking it adds little;
  the sim says it earns its place on revisits ([navigation.md](navigation.md)).
- Run the whole voice loop on the Jetson GPU (Qwen3-VL-2B, Whisper base.en, Piper) and give the
  assistant memory of where people were.
- A second rover, and handing control between operators.

## Built with

Python, FastAPI, NVIDIA Jetson Orin Nano, TensorRT, CUDA, ONNX Runtime, OpenCV, FFmpeg (libx264),
Ultralytics YOLO11 (pose), UltraFace, Depth Anything V2, Qwen3-VL-2B, Qwen2.5-VL and SmolVLM (GGUF),
llama.cpp, whisper.cpp, Piper, Hugging Face Hub, TypeScript, WebXR, Three.js, Vite, Playwright,
Meta Quest 2, Swift, ARKit, ROS 2 Jazzy, Nav2, SLAM Toolbox, Arduino (C++), Tailscale, adb, Rust.
nvblox is optional and not yet run on the board.

Repository (must be **public** at submission): https://github.com/azizu06/farsight

Licences to mention: YOLO artifacts are Ultralytics AGPL-3.0; UltraFace and other models per
[models/README.md](../models/README.md).

---

## Screenshot shot list

Capture at least the first five. Name files `devpost-NN-short-name.png` and keep originals.
Simulated or replayed shots must keep their SIMULATED or REPLAY banner in frame; never crop it out.

| # | What | Where to capture it | Label |
|---|---|---|---|
| 1 | Architecture diagram | [assets/architecture.png](assets/architecture.png), already rendered | n/a |
| 2 | Headset view: live video with person boxes and a pose skeleton | Quest 2 casting or the headset's screenshot, on the rig. Fallback: desktop viewer at `http://localhost:8080` from `.venv/bin/python -m tools.demo --simulate --h264` | Live, or SIMULATED banner visible |
| 3 | Deadman stop: stop-reason panel after releasing the trigger | Same viewer, release mid-drive | Same |
| 4 | Rescue drill HUD: timer, found count, score, route home | Drill started from the headset (left grip) or the desktop Drill control | Same |
| 5 | Mission report with snapshots and an SBAR note. On the rig the note says range unavailable; a 120 cm range is the simulator only | `GET /mission/report` page from the relay, after a drill | Same |
| 6 | 3D scan point cloud, ideally inside VR | Press Scan with `--depth-model` set ([scan.md](scan.md)) | "estimated depth" must be visible |
| 7 | Assistant: question and spoken answer on screen | Push-to-talk in the headset, or the desktop Ask box | Same |
| 8 | The rover itself: Jetson, mounted phone, Uno, motors | Photo of the assembled rig | Real hardware |
| 9 | The operator kit: Quest, MacBook, phone on the table | Photo at the venue | Real hardware |
| 10 | Jetson terminal proving on-board inference | SSH session showing the TensorRT engine run and `/health` | Real hardware |

## Sponsor tracks to opt into

Opt into each on Devpost, then book that sponsor's judging room. General tracks are automatic.

- **NVIDIA: Best Use of Jetson Nano.** The Jetson is the robot's brain, not a side computer: pose
  detection on TensorRT, video encoding, the safety lease, autonomy, mapping and the local voice
  assistant all run on the board, with the phone camera and its LiDAR feeding it. Nav2, SLAM and
  nvblox are opt-in and, so far, simulation or Mac-only; do not pitch them as running on the board.
- **Full Sail: Pathways & Perception.** The rescue drill is a timed training scenario where face and
  person detection decide the score and a route home guides the trainee back, built for the
  responders who need to practise a search before doing one for real.
- **Encompass: Data Capturing Hardware.** The rover turns a phone camera and, on a LiDAR iPhone,
  measured depth into a mission report of timestamped, located encounters with snapshots and triage
  notes, labelled by where each piece of data came from. There is no ultrasonic on the rig, and phone
  depth is not copied into the report: range is unavailable, and the SBAR note says so.
- **Hugging Face: Best Use of Open Source AI** (opt in because the #14 scan shipped in software,
  [PR #33](https://github.com/azizu06/farsight/pull/33)). Depth Anything V2, Qwen3-VL-2B (with
  Qwen2.5-VL and SmolVLM as fallbacks), Whisper, Piper voices and UltraFace are pulled from the Hub
  and run fully offline on the rover. TODO: confirm the scan works in the final demo, or drop this
  opt-in if it does not.

## Judging-room plan

Judging runs **Sun Oct 11, 12:00-4:00 PM**. Each appointment is 2 min setup, 6 min judging and Q&A,
then 2 min teardown, with a hard stop. We need the General room plus four sponsor rooms (five
appointments). Missing one without notice disqualifies us from that challenge **and every General
track**, so every slot needs a named owner and a backup.

There is one rover. The full rig goes to the General and NVIDIA rooms; other rooms use the
desktop viewer with a labelled replay or simulator, plus photos and the video, unless the times
allow the rig to move between rooms.

### Appointments

| Room | Time | Presenter | Backup | Kit | Lead with |
|---|---|---|---|---|---|
| General | TODO | TODO | TODO | Full rig | Headset on the judge, drive, stop, find a person |
| NVIDIA | TODO | TODO | TODO | Full rig + Jetson SSH | What runs on the board, TensorRT numbers |
| Full Sail | TODO | TODO | TODO | Rig or replay laptop | The 90 s drill, who it trains |
| Encompass | TODO | TODO | TODO | Replay laptop + report | Mission report and data provenance |
| Hugging Face | TODO | TODO | TODO | Replay laptop | Open models used, all offline |

### 6-minute run of show (General)

1. 0:00-0:30, TODO-NAME: the problem and the one-line pitch.
2. 0:30-3:30, TODO-NAME: judge in the headset, following [judge-demo.md](judge-demo.md).
3. 3:30-4:30, TODO-NAME: architecture diagram, safety on the robot, cellular link.
4. 4:30-6:00, everyone: questions.

### Before each appointment

- [ ] Rover and Quest batteries charged; spare battery packed. TODO-NAME
- [ ] `tools/ready.py` reports READY on the Jetson; Tailscale up; venue Wi-Fi off. TODO-NAME
- [ ] Replay laptop opens with the REPLAY banner visible. TODO-NAME
- [ ] Fallback tier decided in advance ([fallback-playbook.md](fallback-playbook.md)). TODO-NAME
- [ ] Phone ready to show the Devpost page and the video. TODO-NAME

### Likely questions

- *Is the AI running on the rover?* Yes. Point to the board post, `/health` and the Jetson terminal.
- *What happens if the link drops?* The permit expires within 250 ms, then the Uno watchdog at
  300 ms; show the stop-reason panel. There is no separate ultrasonic stop on this rover.
- *Is the 3D map real?* LiDAR maps are measured; the camera-only scan is estimated, and the headset
  says so. Nav2 paths and the SLAM correction are simulation so far.
- *How is this different from post-mission VR?* You are in the room live, and detection and safety
  run on the robot while you drive ([plan.md](plan.md)).
