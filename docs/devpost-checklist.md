# Devpost evidence checklist (#43)

Devpost is due **Sun Oct 11, 10:00 AM**; code freeze 11:00 AM ([hackers-guide.md](hackers-guide.md)).
Every claim below is marked **V** (verified, with scope) or **P** (pending hardware or a real run).
Write **P** items in the submission only as "planned/in progress", or leave them out. No prize
guarantees; the rubric is Originality, Technical Understanding, Functionality, Design, Wow.

Status as of 2026-10-10 (morning): software verified on a Mac and desktop browser. **No Jetson
inference, Quest session, motor, phone or cellular run has been measured.** The Jetson was still not
reachable over SSH ([jetson-setup-handoff.md](jetson-setup-handoff.md)). Update this table from
`docs/qualification-results.md` and #42's report before submitting.

## Submission mechanics

- [ ] All team members on the Devpost team; same registration email.
- [ ] Opt into: NVIDIA Best Use of Jetson Nano, Full Sail Pathways & Perception, Encompass Data Capturing Hardware, Hugging Face Best Use of Open Source AI. (Tracks and Overall are automatic.)
- [ ] **Public** repo link (https://github.com/azizu06/farsight).
- [ ] Demo video attached (#15). It must label any simulated or replayed segment on screen and in the voiceover.
- [ ] Architecture diagram from [architecture.md](architecture.md); screenshots (HUD with boxes, mission report, map).
- [ ] Judging appointments claimed on the Hacker Dashboard: General room **and** each sponsor room.
- [ ] Licence note: YOLO artifacts are Ultralytics AGPL-3.0; UltraFace per [models/README.md](../models/README.md).
- [ ] Final push by 11:00 AM; no repo changes after.

## Evidence by challenge

### NVIDIA: Best Use of Jetson Nano (Jetson central)

| Claim | Status | Evidence / scope |
|---|---|---|
| Perception, safety, logging run on the Jetson, not the headset | **V** (design/tests) | `jetson/` services, [architecture.md](architecture.md). Runs on Mac in tests |
| TensorRT FP16 engine builder with hash cache; pose backend | **V** builds code, **P** on device | `jetson/build_engine.py`, [inference-setup.md](inference-setup.md); never run on the Jetson |
| Real-time detection fps on Jetson | **P** | Only Mac numbers exist (ONNX CPU, roughly 3-10 fps on Mac). Do not quote as Jetson |
| Local assistant on Jetson GPU (Whisper + VLM + Piper) | **P** | Mac Metal warm: ~0.7-0.9 s/answer (Qwen2.5-VL-3B), SBAR ~1.0 s ([PR #31](https://github.com/azizu06/farsight/pull/31), [PR #35](https://github.com/azizu06/farsight/pull/35)). Jetson CUDA unmeasured |
| Safety holds with no Wi-Fi: 250 ms lease, 300 ms Uno watchdog | **V** software, **P** physical | Simulated stops 43-333 ms ([qualification-results.md](qualification-results.md)). Real stop timing pending |
| One-command boot, service, readiness probe | **V** scripts, **P** timing | `tools/ready.py`, `jetson/farsight.service`; 3 timed cold boots in [demo-checklist.md](demo-checklist.md) are empty |
| Cameras + sensors together | **P** | Needs real camera + Uno ultrasonic on the rig |

### Full Sail: Pathways & Perception (training scenario with perception)

| Claim | Status | Evidence / scope |
|---|---|---|
| Rescue drill: 90 s timer, face dwell 1 s, e-stop count, route-home | **V** software | `jetson/drill.py`, simulator end-to-end ([PR #38](https://github.com/azizu06/farsight/pull/38)). Route is dead-reckoned and **approximate** |
| Face detection | **V** loads, CPU | Optional UltraFace Hub model; positive rate on real people unmeasured |
| Who it helps | **V** (story) | First responders, inspectors, trainees; [plan.md](plan.md) |
| Drill on a real rover with real people | **P** | Left grip start is unverified on a Quest |
| Distinct people count | **Do not claim** | Drill and report count *tracks*, not verified distinct people |

### Encompass: Data Capturing Hardware

| Claim | Status | Evidence / scope |
|---|---|---|
| Camera + ultrasonic range + detections into one mission report with snapshots and times | **V** software | `GET /mission/report`, `--mission-dir`; shown on simulated/photo fixtures |
| Source labels (live, SIMULATED, REPLAY) preserved in the data | **V** | Report and HUD badges |
| Record/replay of a run | **V** plumbing, **P** real run | Only the 1.4 s synthetic fixture exists; record a real run (#18) |
| Measured geometry vs estimated | **V** protocol, **P** device | LiDAR RGB-D mapping needs a Pro iPhone on the rig; camera-only scan is *estimated* depth (Mac scan 596.5 ms, 89,967 points, synthetic input, [scan.md](scan.md)) |
| SBAR triage notes per person | **V** software | Template always; model rewrite when the VLM is up. Draft text, not clinical triage |

### Hugging Face: Best Use of Open Source AI

| Claim | Status | Evidence / scope |
|---|---|---|
| Open models actually used | **V** | Depth Anything V2 Metric Indoor Small, Qwen2.5-VL-3B / SmolVLM GGUF, Whisper, Piper voices, UltraFace; fetch via `tools/fetch_models.py`, manifests in [models/README.md](../models/README.md) |
| Runs fully local | **V** on Mac, **P** Jetson | Models offline; cellular link is the only network need |
| Quality honestly bounded | **V** | Depth is estimated; VLM answers have a motion-claim guard and 2-sentence cap |
| Credits pitch | n/a | No claim about prizes |

### General (Overall, Embedded, AI, Useful)

| Claim | Status | Evidence / scope |
|---|---|---|
| End-to-end: camera, boxes, driving, Seek, report | **V** simulator + desktop browser, **P** rig | [ui-validation.md](ui-validation.md): positive JPEG and H.264 overlays on a SIMULATED public photo with real YOLO11n ONNX; Quest 2 unverified |
| Frame-matched overlays | **V** desktop | Boxes drawn only on matching frame ids; ~3 fps ONNX on Mac so intermittent |
| Stable ids, pose, "down" | **V** synthetic | Tracker and decode tests; not yet on real video from the rover |
| Seek / Explore | **V** unit + sim, **P** rover | Arrival is a software bound from the front ultrasonic |
| Software link behaviour | **V** simulated | 8 s RGB trial: arrival p95 285 ms, 190/192 settled samples moving; a later loaded run stopped at stale video and is kept. Software receipt age, **not** camera-to-eye latency |
| Offline fallback | **V** | Replay and simulator with REPLAY / SIMULATED badges ([fallback-playbook.md](fallback-playbook.md)) |
| Originality vs prior art (RIIS) | **V** (story) | Live head-coupled teleop with on-robot detection and safety vs post-mission viewing; [plan.md](plan.md) |
| Rust port | **V** parallel, optional | Same wire contract; not the demo runtime until parity is checked |

## Open claims to resolve before submitting (owners)

| # | Item | Owner | Unblocks |
|---|---|---|---|
| 1 | Jetson boots, SSH, `tools.ready` READY | Sai / human | Every **P** under NVIDIA |
| 2 | Quest 2 session: controls, texture resize, H.264, mic | #40 agent + human | Functionality, headset claims |
| 3 | One real recorded run (`--record`) for tier 2 replay | Rig team | Honest fallback, Encompass |
| 4 | Jetson fps / latency with source and load noted (`python -m tools.measure`) | Aziz's agents / Sai | NVIDIA numbers |
| 5 | Timing log in [demo-checklist.md](demo-checklist.md) (3 cold boots) | Rig team | Reliability claim |
| 6 | Motor direction, start PWM, power under load, disconnect stop on the rig | Sai's Codex | Never close from software tests |
| 7 | Demo video with on-screen SIMULATED / REPLAY labels | #15 owner | Devpost requirement |

## Wording rules for the write-up

- "Tracked encounters", not "people found", unless a human counted.
- "Estimated depth" for scan, "measured LiDAR" only for Pro iPhone data.
- "Software-measured video age" vs "camera-to-display latency": the latter only once measured on the rig.
- Attribute every number to its machine, load and date. Mac numbers are Mac numbers.
- If the final demo runs a fallback tier, say which in the submission.
