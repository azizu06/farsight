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
8. Point at a person, press A: rover drives to them on its own (Seek, stretch).

## Why the Jetson is central (NVIDIA)
| On the Jetson | Why it can't be the headset or an Arduino |
|---|---|
| TensorRT YOLO + face detection, real time | GPU inference on the robot, no laptop |
| Hardware JPEG/H.264 encode | Streams video with little CPU |
| 20 Hz control pump, 250 ms lease, ultrasonic e-stop | Safety holds even if Wi-Fi drops |
| Mission logger | Data captured where the sensors are |
| Depth Anything V2 (stretch) | Live 3D from one camera |

## Challenge map
| Challenge | What earns it | Issues |
|---|---|---|
| NVIDIA Jetson | Perception, encode, safety, logging all on Jetson; fps/latency numbers | #1 #7 #9 #12 |
| Full Sail Pathways & Perception | Rescue drill: training scenario with face detection, score, route back | #16 |
| Encompass Data Capturing Hardware | Rover captures camera + range + detections into a mission report | #17 |
| Hugging Face | Models pulled from the HF Hub (detector, Depth Anything V2) | #9 #14 |
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

Ideas we took (ideas only, no code): recorded-run fallback (#18), pose model for skeletons and a
"person down" flag (#9), Explore random-walk before Seek (#13), frame archive (#17), mock hardware
so the code runs without the rover (#2).

## Scope rules
- P0 first, end to end, before anything else. Teleop must be boringly reliable.
- No 3D mapping or SLAM: without phone pose tracking it will not be ready in time.
- Feature freeze Sun 4 AM.

## Owners
Load is weighted on purpose: hardest and most work first.

| Owner | Tier | Issues |
|---|---|---|
| @azizu06 | 1 | #1 Jetson bring-up, #9 TensorRT detection, #7 control + safety, #11 launcher, #13 Seek, #14 depth |
| @GodlyDonuts | 2 | #3 Uno firmware, #6 power + mount, #5 network + HTTPS, #17 mission log |
| @mchdich | 3 | #4 WebXR shell, #8 controls, #10 HUD, #16 rescue drill |
| @chang-pro | 3 | #2 mock server, #18 recorded-run replay, #12 measurements, #15 video + Devpost + diagram |
