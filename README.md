# Farsight

**Search-and-rescue telepresence: drive into a room from a Meta Quest, find people, then walk through its 3D reconstruction.**

[**Watch the demo →**](https://youtu.be/AmrOGcTBTKU) · [Devpost](https://devpost.com/software/farsight-t4qyli)

Built at **Knight Hacks IX** by Aziz Umarov, Saicharan Ramineni, Dechante (Chang), and Mehdi Chraibi.

## What you'll see

1. **Drive from the headset.** A responder sees the rover's live camera in a Meta Quest. The right joystick drives the ELEGOO chassis; **A** switches to autonomous Explore with Nav2, **B** stops and returns to MANUAL. In AUTO, the sticks move your viewpoint through the map. [Controller implementation](quest/src/rover.ts) · [Controls](quest/README.md#controls).
2. **Find people and keep the evidence.** The NVIDIA Jetson Orin Nano runs YOLO11n-pose person detection and derives face boxes from head keypoints. Encounters retain snapshots and draft SBAR notes. On the laptop, ask about the live view by text or push-to-talk; local Qwen3-VL, Whisper, and Piper provide vision, transcription, and spoken answers, plus English/Spanish survivor phrases. [Perception](jetson/perception.py) · [Faces](jetson/face.py) · [Assistant](jetson/assistant.py) · [Triage](jetson/triage.py) · [Phrases](jetson/phrases.py).
3. **Walk the room after the run.** The LiDAR iPhone records RGB, depth, and ARKit camera poses. A Mac watcher pulls finished captures and trains a Gaussian-splat room with **Brush on Metal**, then publishes a Quest-sized scene. A saved 60-second capture sample produced the room and face evidence in **114 seconds locally** on the M5 Pro ([measurement](#measurements-and-scope)). Browse the rover's route in `walk.html` or enter the reconstruction in `/splat.html` on Quest. Dechante's face walkthrough adds red minimap pins: click a face to revisit its viewpoint and open the real camera frame with the face boxed. [Capture](jetson/mapping/run_capture.py) · [Mac pipeline](tools/postprocess/after_run.py) · [Walk viewer](tools/postprocess/walk.html) · [Quest splat viewer](quest/src/splat-viewer.ts).

Pins mark **observed face locations**, not verified distinct people; moving
people can leave multiple spots. Face pins belong to the browser walkthrough;
the immersive Quest splat viewer is a separate page.
[Face-pin generator](tools/postprocess/faces/face_pins.py).

## Architecture

```text
OPERATOR KIT                                ROVER KIT
Meta Quest                                  LiDAR iPhone
  WebXR camera + controls                     RGB + depth + ARKit pose
       ⇅ USB / ADB reverse                           ⇅ USB
Mac operator relay  ⇄ phone bridges ⇄        NVIDIA Jetson Orin Nano
  HTTP + WebSockets       cellular /          YOLO11n-pose + local voice/VLM
  optional live renderer  Tailscale            Nav2 + capture archive
                                                    ⇅ serial
                                              Arduino Uno → ELEGOO motors

AFTER THE RUN (Mac)
finished capture → SSH pull → posed photos + LiDAR seed
                 → Brush / Metal → prune → scenes/<run>.ply
                 → walk.html (browser) / splat.html (Quest WebXR)
face-pin workflow: posed photos + LiDAR → faces.json + boxed camera frames
```

The Jetson handles perception, navigation, and motor commands beside the sensors.
The Mac handles the operator relay and post-run reconstruction. The primary
cellular route uses two phone bridges; Quest reaches the Mac over USB.
[Relay code](tools/operator_relay.py) · [Phone routing](docs/phone-relay.md) ·
[Navigation](docs/navigation.md) · [Full architecture](docs/architecture.md).

Live LiDAR mapping and post-run Gaussian splatting are different views: the
former retains measured geometry; the latter learns an appearance model from
posed photos and a measured seed. Ordinary RGB-only iPhones supply video;
measured scene depth requires a supported LiDAR iPhone.
[Capture modes](ios/README.md) · [Mapping](docs/mapping.md).

## Per track

| Sponsor track | What Farsight contributes | Concrete implementation |
| --- | --- | --- |
| **Hugging Face — Best Use of Open Source AI** | Local Qwen3-VL-2B-Instruct GGUF via llama.cpp, Whisper `base.en` via whisper.cpp, and Piper voices turn live observations into answers, draft SBAR notes, and spoken phrases. Model artifacts are fetched separately. | [Model sources](models/README.md), [fetcher](tools/fetch_models.py), [assistant](jetson/assistant.py), [triage](jetson/triage.py), [setup](docs/assistant-setup.md) |
| **NVIDIA — Best Use of Jetson Nano** | The Orin Nano is the rover's onboard computer: YOLO11n-pose with a TensorRT backend, camera streaming, navigation integration, and serial motor control. | [TensorRT pose](jetson/pose/trt_backend.py), [server](jetson/mapping/server.py), [control](jetson/control/README.md), [Nav2](ros2/farsight_nav) |
| **Encompass — Data Capturing Hardware** | A rover-mounted LiDAR iPhone captures synchronized images, measured depth, and camera poses; those observations become encounter snapshots and a 3D room you can revisit. | [iPhone app](ios/README.md), [run archive](jetson/mapping/run_capture.py), [mission log](jetson/mission.py), [post-run pipeline](tools/postprocess/after_run.py) |
| **Full Sail — Pathways & Perception** | A search-training prototype combines autonomous pathfinding, person/face detection, a timed rescue drill, and room walkthroughs to practise finding and reviewing encounters. | [Nav2 integration](ros2/farsight_nav), [drill scoring](jetson/drill.py), [face detection](jetson/face.py), [Quest locomotion](quest/src/splat-navigation.ts) |

[Track descriptions](docs/hackers-guide.md#sponsor-challenges-prizes-per-team-member) · [Model provenance and licenses](models/README.md).

## Run it

### Try the software without a rover

Prerequisites: Python 3, Node.js/npm, and FFmpeg for H.264. From the repo root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt \
  -r jetson/mapping/requirements.txt -r jetson/control/requirements.txt
npm --prefix quest ci
.venv/bin/python -m tools.demo --simulate --build --h264
```

Open **http://localhost:8080**. On desktop, hold **Space + WASD** in the live
camera view to drive. This mode is **SIMULATED**; recorded replay has inert
controls. [Simulator/replay judge flow](docs/judge-demo.md).

### Use the assembled rig

Follow the existing setup guides for the hardware, routing, and selected profile:

1. [Jetson bring-up](docs/jetson-bringup.md) and [inference setup](docs/inference-setup.md): install the configured runtime, target-built model engines, and Arduino motor connection. The current rig has no ultrasonic sensor or pan servo; its profile uses iPhone LiDAR and Nav2. Generic server launches require `--nav2` to enable that integration.
2. [iPhone capture](ios/README.md) and [phone relay](docs/phone-relay.md): start the rover's LiDAR capture and the two phone bridges. [Transport setup](docs/transport-setup.md) covers Mac USB forwarding and Quest ADB reverse.
3. [Quest first run](docs/quest-first-run.md) and [current controls](quest/README.md#controls): serve the bundled app through the Mac relay and open it in Quest Browser. The [Mac renderer runbook](docs/macview-rig-acceptance.md) covers the optional live rendered-map/stereo path.

Voice/VLM model files are excluded from Git. Use the
[assistant guide](docs/assistant-setup.md) to install Qwen3-VL-2B-Instruct and
its matching projector, Whisper `base.en`, and Piper voices. Voice controls
are on the laptop page in the current two-mode headset interface.

The default immersive demo mode warns about stale video instead of blocking
motion. `?demo=0` restores strict client guards; server/relay demo settings are
separate. Permits, link-loss stops, B/STOP, and the Uno watchdog remain.
[Current mode semantics](quest/README.md#controls) · [Wire contract](docs/interfaces.md).

### Build a post-run room

Use the [Mac automation guide](tools/postprocess/AFTER_RUN.md) for Brush
installation, the watcher, SSH capture pulls, and the output directory.
For an already-saved run, install Brush and the separate
[face-processing environment](tools/postprocess/AFTER_RUN.md#face-pins-and-offline-walkthrough)
first, and build the Quest app:

```sh
.venv/bin/python -m pip install -r tools/postprocess/requirements-mac.txt
.venv/bin/python tools/postprocess/after_run.py --demo --local /path/to/saved-run \
  --dist "$PWD/quest/dist"
```

The Mac `--demo` preset selects 5,000 steps, a capped LiDAR seed, and CPU face
processing. The runner prints the face-enabled walkthrough URL and records
the separate Quest scene URL in its receipt.
[Dechante's workflow](tools/postprocess/README.md) covers the browser walk,
route minimap, ride-along, and held-out image scoring. The
[face-pin implementation](tools/postprocess/faces/face_pins.py)
produces `faces.json` and `faceframes/` for the face-enabled `walk.html`.

## Measurements and scope

A **60-second / 119-photo saved capture sample → room and face evidence ready
in 113.73 seconds locally** on the Apple M5 Pro. Brush took **112.19 seconds
at 5,000 steps**; face processing took **61.58 seconds on CPU in parallel**.
Adding a 10-second idle allowance and a 0.31-second local rsync stand-in gives
**124.04 seconds**. Real Jetson transfer and browser loading are excluded.
The sample is a prefix of a longer saved run and reuses that run's measured
LiDAR seed, capped to 290,811 points; it does not reproduce the seed from a
separate short drive.
[Mac run receipt and conditions](tools/postprocess/MAC_RECEIPT.md) ·
[Pipeline implementation](tools/postprocess/after_run.py).

Build time varies with the capture, seed size, settings, and concurrent GPU
load. The Gaussian splat is a learned reconstruction, and pruning for Quest
can introduce artifacts. Compare the final viewer with its source photos;
a desktop load or training score does not establish headset quality.

Farsight is a hackathon prototype for search training. Detection boxes, face
spots, and machine-drafted SBAR notes are observations for human review.
[Physical/software evidence](docs/pitch-metrics.md) ·
[Post-run qualification](tools/postprocess/MAC_RECEIPT.md) ·
[Fallback playbook](docs/fallback-playbook.md).

## Credits and project guides

**Aziz Umarov · Saicharan Ramineni · Dechante (Chang) · Mehdi Chraibi**

Dechante's posed-photo/LiDAR-seeded Brush workflow, fast step-budget experiments,
walk viewer, and face-pin workflow underpin the post-run experience. Farsight
builds on Brush, Three.js, WebXR, Spark, ARKit, ROS 2/Nav2, Ultralytics YOLO,
llama.cpp, whisper.cpp, Piper, and models distributed through Hugging Face.
[Pipeline attribution/results](tools/postprocess/README.md) · [Model licenses](models/README.md).

[Wire contract](docs/interfaces.md) · [Dense mapping](docs/dense-map.md) ·
[Scan](docs/scan.md) · [Drive calibration](docs/drive-calibration.md) ·
[Rust services](rust/README.md) · [Printed mounts](hardware/print/README.md) ·
[Devpost update draft](docs/devpost-update.md)
