# Farsight — Devpost update

[Demo video](https://youtu.be/AmrOGcTBTKU) · [Devpost](https://devpost.com/software/farsight-t4qyli)

> Editor note (not for pasting): paste the six sections below. Face pins
> belong to the browser `walk.html`; the immersive `/splat.html` page is
> separate. The source map and sponsor fit are reference material for the
> editor. No Devpost changes have been made by this task.

## What it does

Farsight lets a responder explore a room from a Meta Quest instead of entering
it first. An ELEGOO rover carries a LiDAR iPhone and an NVIDIA Jetson Orin Nano.
The headset shows the live camera: move the right joystick to drive, press A
for autonomous Explore, and press B to stop and return to MANUAL. In AUTO,
the joysticks move your viewpoint through the map while Nav2 navigates the rover.

YOLO11n-pose finds people and derives face boxes from head keypoints. The
mission report keeps encounter snapshots and draft SBAR notes. From the laptop,
a responder can type or use push-to-talk to ask about the live view. Local
Qwen3-VL-2B-Instruct answers, Whisper base.en transcribes, and Piper speaks
replies or preset survivor phrases in English and Spanish. The models run
locally; remote driving still needs the cellular link.

After the run, the Mac pulls the posed photos and measured LiDAR seed, then
trains a Gaussian-splat reconstruction with Brush on Metal. Walk the room in a
browser, replay the rover's route, or open the splat in the Quest. Dechante's
face-enabled browser walkthrough puts red dots on the minimap; clicking a face
jumps to its recorded viewpoint and opens the real camera frame with the face
boxed. These are observed face locations, so a moving person may leave more
than one spot.

## How we built it

Swift and ARKit on the iPhone supply synchronized RGB, measured depth, camera
intrinsics, and poses. The Jetson runs the Python/FastAPI rover server,
YOLO11n-pose through TensorRT, Nav2 integration, and serial commands to the
Arduino Uno. Two phone bridges carry the cellular/Tailscale connection. The
Mac operator relay serves the TypeScript/Three.js/WebXR app to the Quest over
USB using ADB reverse.

Our local assistant uses Hugging Face model artifacts: Qwen3-VL-2B-Instruct
GGUF with its vision projector through llama.cpp, Whisper base.en through
whisper.cpp, and Piper voices. The same vision-language service drafts SBAR
notes from encounter crops and metadata.

The post-run pipeline selects a continuous drive, preserves its posed photos,
seeds Brush with measured LiDAR points, trains on the Mac GPU, and prunes the
output for Quest. A watcher waits for captures to stop changing before a
SSH pull. Face processing runs on the CPU alongside GPU training.
The browser walk combines the reconstructed room, recorded route, and face-pin
evidence. The immersive Quest splat page provides
stick walking and snap turns.

## Challenges

We had to make camera streaming, perception, navigation, and motor control
share a small computer and an uneven cellular link. Mapping and reconstruction
also belong on different schedules: a detailed post-run room should not delay
the live camera or control path.

A convincing reconstruction needs coherent camera poses and useful views.
Parked stretches repeat images; malformed JPEGs can stop Brush; an oversized
LiDAR seed can make a short training budget much slower. Reducing splat count
for a headset can also introduce visible streaks even when the original
training score looks good.

We kept measured LiDAR, learned reconstruction, simulation, and replay distinct.
A face location is evidence of a sighting, not proof of a unique person, and a
model-generated note still needs a responder's judgment.

## Accomplishments

- Connected headset driving, live onboard perception, autonomous navigation,
  and mission evidence in one search-and-rescue prototype.
- Built a local vision/voice assistant with spoken answers and bilingual
  survivor phrases on the rover's Jetson.
- Turned real rover captures into walkable Gaussian-splat rooms with a Mac
  pipeline based on Dechante's LiDAR-seeded Brush method.
- Built a room and its face evidence in **114 seconds locally** on an M5 Pro
  from a saved 60-second / 119-photo capture sample. Brush took 112 seconds at
  5,000 steps, with 62 seconds of CPU face processing in parallel. A 10-second
  idle allowance and local-copy stand-in bring that to **124 seconds**, excluding
  real Jetson transfer and browser loading. The sample reuses a longer run's
  LiDAR seed; it is not a live short-drive end-to-end timing.
- Added a browser walkthrough that connects face pins to the original boxed
  camera frames, making the room useful for reviewing what the rover saw.

## What we learned

Give each device the work that fits it: the phone captures depth and pose,
the Jetson handles perception and navigation beside the motors, and the Mac
trains the post-run room. Capture quality and seed size matter as much as
training steps. Measure the final pruned viewer result as well as the training
score, and compare settings on the same held-out photos.

A useful search record connects a location to the actual observation. A red
pin becomes much more informative when it opens the camera frame that created
it. Keeping those links also makes it easier to spot duplicate sightings and
model mistakes.

## What's next

Improve room quality within a predictable end-to-end build budget, including
transfer, face processing, and publication. Bring the browser's face-evidence
interaction into the immersive Quest viewer. Evaluate localization, detection,
and operator performance across repeated physical search-training runs, and
make multi-room review easier.

---

## Source map for the editor (not for pasting)

| Claim | Source |
| --- | --- |
| MANUAL/AUTO, A/B, current headset interface | [rover.ts](../quest/src/rover.ts), [Quest controls](../quest/README.md#controls) |
| iPhone measured capture, pose archive | [iOS guide](../ios/README.md), [run_capture.py](../jetson/mapping/run_capture.py) |
| YOLO pose, keypoint faces, TensorRT | [perception.py](../jetson/perception.py), [face.py](../jetson/face.py), [TensorRT backend](../jetson/pose/trt_backend.py) |
| Local models, speech, SBAR | [assistant.py](../jetson/assistant.py), [triage.py](../jetson/triage.py), [phrases.py](../jetson/phrases.py), [model sources](../models/README.md) |
| Cellular/USB relay and navigation | [operator_relay.py](../tools/operator_relay.py), [phone route](phone-relay.md), [Nav2](../ros2/farsight_nav) |
| Automatic capture-to-splat pipeline and timings | [after_run.py](../tools/postprocess/after_run.py), [automation](../tools/postprocess/AFTER_RUN.md), [M5 Pro receipt](../tools/postprocess/MAC_RECEIPT.md) |
| Browser walk and Quest splat | [walk.html](../tools/postprocess/walk.html), [splat-viewer.ts](../quest/src/splat-viewer.ts) |
| Face pins and boxed-frame interaction | [face_pins.py](../tools/postprocess/faces/face_pins.py), [walk.html](../tools/postprocess/walk.html) |

## Sponsor fit (optional paste)

- **Hugging Face — Best Use of Open Source AI:** local vision, speech recognition,
  and synthesis use Hugging Face-distributed models for a responder's workflow.
- **NVIDIA — Best Use of Jetson Nano:** the Orin Nano runs onboard perception,
  local AI, navigation integration, camera streaming, and motor control.
- **Encompass — Data Capturing Hardware:** a rover-mounted LiDAR iPhone turns
  synchronized real-world observations into encounter records and a 3D room.
- **Full Sail — Pathways & Perception:** pathfinding, face detection, timed rescue
  drill scoring, and room walkthroughs support a search-training scenario.

Team: **Aziz Umarov, Saicharan Ramineni, Dechante (Chang), Mehdi Chraibi**.
