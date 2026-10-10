# Plan: Farsight

## One line
A rover you inhabit: put on a Quest 2 and see, steer, and search through the rover's eyes.
An NVIDIA Jetson Orin Nano on the robot does all perception and safety.

## Who it helps
First responders and inspectors who need eyes in a room before a person walks in (smoke,
collapse, hazardous plant areas), and trainees who need to practice that search safely.

## Demo (6 minutes with judges)
1. Judge puts on the Quest. Live rover camera on a big panel.
2. Turn your head: the rover's camera turns with you.
3. Hold the trigger, push the stick: the rover drives. Let go: it stops instantly.
4. Walk a teammate into view: a box locks onto them, labelled, with distance.
5. Drive at a wall: the rover refuses to hit it (e-stop on the Jetson, not the headset).
6. **Rescue drill**: 90 s timer, find the "victims" (faces); score and route back shown.
7. Take the headset off: the mission report lists every detection with time, snapshot, range.
8. "What do you see?" The Jetson answers out loud, offline (#19).
9. Press Scan: the rover sweeps its head and you stand inside a 3D snapshot of the room (#14).
10. Explore: the rover wanders on its own until it finds a person, then Seek drives to them (stretch).

## Why the Jetson is central (NVIDIA)
| On the Jetson | Why it can't be the headset or an Arduino |
|---|---|
| TensorRT YOLO + face detection, real time | GPU inference on the robot, no laptop |
| Hardware JPEG/H.264 encode | Streams video with little CPU |
| 20 Hz control pump, 250 ms lease, ultrasonic e-stop | Safety holds even if Wi-Fi drops |
| Mission logger | Data captured where the sensors are |
| Depth Anything V2 3D scan (#14) | Metric 3D from one camera, stitched by servo angle |
| Local voice + vision assistant (#19) | Answers with no internet: Whisper + small VLM on the Jetson GPU |

## Challenge map
| Challenge | What earns it | Issues |
|---|---|---|
| NVIDIA Jetson | Perception, encode, safety, logging all on Jetson; fps/latency numbers | #1 #7 #9 #12 |
| Full Sail Pathways & Perception | Rescue drill: training scenario with face detection, score, route back | #16 |
| Encompass Data Capturing Hardware | Rover captures camera + range + detections into a mission report | #17 |
| Hugging Face | Models pulled from the HF Hub (detector, Depth Anything V2, Whisper, VLM) | #9 #14 #19 |
| General: Embedded, Most Useful, Overall | End-to-end reliable demo, clear story | all P0 |

Rubric (General): Originality, Technical Understanding (diagrams help), Functionality (90% working,
end to end), Design, Wow. See `docs/hackers-guide.md`.

## Prior art: RIIS (LA Hacks 2026)
[RIIS](https://github.com/srinisriram/RIIS) is close: a search-and-rescue rover (Pi + PiCar-X), YOLO
pose for survivors, and a VR walkthrough. Their VR is **after the mission**: archived frames go to
MASt3R + InstantSplat on a cloud GPU, and the resulting Gaussian splat is loaded into a Unity build on
the Quest. Their dashboard replays pre-computed detections, and the autonomy is a random-walk with one
ultrasonic sensor.

**Our difference, say it in the pitch:** RIIS shows you the building afterwards; Farsight puts you in
it *live*. Head-coupled camera, live teleop, and detection plus safety running on the robot's own
GPU, in real time.

Best practices we adopt from RIIS (their code stays theirs; ours is written here):
- Live source with recorded fallback, clearly labelled (#18)
- Hardware abstraction with mocks that switch on automatically when hardware is missing (#2, Jetson code)
- One protocol doc as the contract between rover and UI (`docs/interfaces.md`)
- Small pub/sub event bus inside the Jetson server (detections, range, mode changes)
- Config file with environment overrides instead of hard-coded IPs and thresholds
- YOLO pose for people, with a "person down" flag (#9)
- Frame archive every Nth frame (#17)
- Explore by random walk until a person is found, then stop (#13)
- SBAR triage summary (Situation, Background, Assessment, Recommendation) per person found, written by
  the local model into the mission report (#17, #19)
- Survivor phrases in more than one language, spoken on the rover (#19 stretch, Piper has Spanish voices)

Not adopted: ElevenLabs and cloud LLMs (not Knight Hacks sponsors, need internet; our pitch is offline),
Fetch.ai agents, Unity splat build.

## Scope rules
- P0 first, end to end, before anything else. Teleop must be boringly reliable.
- No 3D mapping or SLAM: without phone pose tracking it will not be ready in time.
- Feature freeze Sun 4 AM.

## Owners
Load is weighted on purpose: hardest and most work first.

| Owner | Tier | Issues |
|---|---|---|
| @azizu06 | 1 | #1 Jetson bring-up, #9 TensorRT pose/detection, #19 local AI assistant, #14 3D scan, #13 Explore/Seek, #11 launcher |
| @GodlyDonuts | 2 | #3 Uno firmware, #7 control + safety, #6 power + mount, #5 network + HTTPS, #17 mission log |
| @mchdich | 3 | #4 WebXR shell, #8 controls, #10 HUD, #16 rescue drill |
| @chang-pro | 3 | #2 mock server, #18 recorded-run replay, #12 measurements, #15 video + Devpost + diagram |
