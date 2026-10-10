# Farsight — Devpost draft and judging plan

**Devpost: Sun Oct 11, 10:00 AM. Final repository push: 11:00 AM.**
Judging: **12:00–4:00 PM**. Rules and sponsor briefs are captured in the
[Hacker's Guide](hackers-guide.md), from the
[official guide](https://2026.knighthacks.org/dashboard/guide).
This is a draft, not a submitted entry. Resolve every `TODO` before pasting;
[submission checklist](devpost-checklist.md).

## One story for every room

**Search-and-rescue telepresence. A responder in VR explores a building through
an indoor rover, sees its live camera and person detections, and walks through
the 3D room it builds. The iPhone captures measured depth; the Jetson navigates;
the Mac renders the dense room.**

Use this same story in General and every sponsor room. Change the technical
emphasis, not what the product does. This is a hackathon prototype; describe
what the actual demonstration shows rather than claiming field readiness.
The feature/evidence table below is the source for every pitch claim.

## Tagline (under 200 characters)

Explore a building through a rescue rover: live camera and person detection,
LiDAR-guided indoor autonomy, and a dense room you can walk through in VR.

## Inspiration

A responder should be able to inspect an unfamiliar room before entering it.
We wanted more than remote steering on a small screen: a live view of the
rover's surroundings, help finding people, and spatial context that stays
available as the rover moves. VR telepresence and a measured room map bring
those views together. Our demonstration is a controlled indoor rescue scenario.

## What it does

Farsight connects a Quest 2 responder to a camera/LiDAR rover. In MANUAL,
the right trigger and stick drive the vehicle. Tap A for AUTO: the rover explores
with Nav2 while the stick moves the responder through the retained 3D map.
The live camera remains visible with detections; the local map layer also has
separate person markers.
B stops the rover and returns to MANUAL. The current demo configuration uses
hands-free AUTO; strict mode requires the deadman. [Control evidence](https://github.com/azizu06/farsight/pull/136).

The current rig runs Nav2 on the Jetson with iPhone LiDAR and ARKit pose;
it is no longer a simulation-only navigation project. Nav2 plans around the
measured room geometry, including people and furniture represented in its
costmaps. Frontier handoff and smooth arcs address a real floor-run failure
where the rover spent too long turning. Tonight's improved room coverage and
moving-obstacle performance are still **TODO-RIG**. The generic server CLI
still requires `--nav2`; “default” here means the team's deployed rig profile,
not every installation. [Real floor trace and fix](https://github.com/azizu06/farsight/pull/146),
[measured spin-floor integration](https://github.com/azizu06/farsight/pull/147).

Camera delivery has its own fast lane, independent of map fusion. Dense
geometry accumulates observed, pose-aligned points across viewpoints; bounded
deltas move to the Mac, which retains spatial chunks and renders the room.
The current Mac view is **flat, pose-driven video** inside the Quest experience.
Stereo remains an opt-in integration in progress; do not call the present
Mac stream stereoscopic. [Camera](https://github.com/azizu06/farsight/pull/157),
[dense path](https://github.com/azizu06/farsight/pull/142),
[background worker](https://github.com/azizu06/farsight/pull/151),
[Mac renderer](https://github.com/azizu06/farsight/pull/132),
[AUTO embed](https://github.com/azizu06/farsight/pull/144),
[Quest stereo seam and remaining gates](https://github.com/azizu06/farsight/pull/159).

The rescue drill turns the same viewer into a timed search exercise with a
score and mission report. Optional speech/image questions and camera-only
estimated-depth scans are separate capabilities: include them only if they
actually run in the final demo. A detected person or scored face is an observed
track, not a medically verified victim or proof of a distinct identity.
[Drill checks](https://github.com/azizu06/farsight/pull/79),
[mission/triage](https://github.com/azizu06/farsight/pull/81),
[estimated-depth scan](https://github.com/azizu06/farsight/pull/33),
[assistant component measurements](assistant-setup.md).

**TODO-DEMO:** identify the demonstrated tier: assembled rig / stationary bench /
backup recording / labelled simulation. **TODO-CONFIRM:** voice assistant and/or
estimated-depth scan included? Keep or remove those claims accordingly.

## How we built it

[Architecture diagram and walkthrough](architecture.md):

```text
Rover iPhone camera + LiDAR + ARKit pose
  ├─ local USB → Jetson Orin Nano: fast camera, detection, Nav2, motor control
  │                └─ USB serial → Uno → wheels
  └─ rover phone cellular/Tailscale ↔ operator phone ↔ USB → Mac relay
                                                       ├─ dense retention + render/encode
                                                       └─ USB/ADB reverse → Quest 2
```

Raw capture stays on the rover's local USB path. Processed video, geometry,
status and control traverse the two-phone cellular route; Quest stays on the
Mac's same-origin `:8080` relay. A recorded real two-phone transport run exists;
it does not prove the full headset/floor/fault-recovery path.
[Routing and measurements](phone-relay.md).

The Jetson handles perception and navigation close to the sensors. A dedicated
camera worker publishes validated JPEGs before map work, and latest-only
queues discard excess pending frames. H.264 uses short GOPs and bounded
keyframe-safe recovery. The Mac retains fine visual data independently of the
coarse navigation cloud, and sends bounded view/LOD updates rather than
repeated multi-million-point snapshots. The Quest also avoids repeated live-edge
seeks. [Fast lane](https://github.com/azizu06/farsight/pull/157),
[dense/LOD](https://github.com/azizu06/farsight/pull/142),
[CPU isolation](https://github.com/azizu06/farsight/pull/151),
[playback](https://github.com/azizu06/farsight/pull/152),
[vsync/presentation fix](https://github.com/azizu06/farsight/pull/154).

The rover has **no ultrasonic sensor and no pan servo**. Its obstacle geometry
comes from iPhone LiDAR, not an invented range reading. Demo-mode behavior is
intentional: immersive AUTO is hands-free, stale video produces a warning, and
the separately enabled server demo mode relaxes teleop camera gating and
transient release latches. B/STOP, permits, lease/link-loss stops, Nav2 sensor
gates and the Uno watchdog remain. Do not claim strict stale-video motion
blocking for that demo profile. [Hardware change](https://github.com/azizu06/farsight/pull/91),
[client controls/demo](https://github.com/azizu06/farsight/pull/136),
[server demo mode](https://github.com/azizu06/farsight/pull/134).

## What works and what still needs the rig

**[HW]** means measured on the named physical component; it does not imply
complete system acceptance. **[SIM]** includes synthetic input, desktop/browser
and loopback checks. **[SW]** means merged implementation with software evidence.
**TODO-RIG** stays unknown until tonight's assembled-rig measurement.

| Claim | Evidence and honest scope |
| --- | --- |
| Live indoor Nav2 on Jetson | **[HW, historical floor run]** The 180-second run cited in [frontier/arcs work](https://github.com/azizu06/farsight/pull/146) proves a real Nav2/Explore floor path existed, while showing severe spinning. Improved floor performance is pending; the simulator result is separate. |
| MANUAL/AUTO, map walking, B stop | **[SW/SIM]** [Controls](https://github.com/azizu06/farsight/pull/136), automated browser/judge checks; current real Quest behavior needs the tonight checklist. |
| Person detections and map markers | **[HW component / SW map overlay]** [Jetson perception measurements](pitch-metrics.md), [marker implementation](https://github.com/azizu06/farsight/commit/f1b883c), [map-walk integration](https://github.com/azizu06/farsight/pull/136). The [3/3 software marker checks](../quest/tests/person-markers.test.mjs) verify depth-backed positions and stale expiry. Markers stay separate from fused room geometry; full Mac-stream/headset legibility is TODO-RIG. |
| Camera independent of slow mapping | **[SIM]** [Fast-lane evidence](https://github.com/azizu06/farsight/pull/157): real ffmpeg/TCP loopback on Mac, 25 fragments, video age median 137 / p95 170 / max 175 ms while reconstruction was delayed 800 ms. This is not glass-to-glass latency. |
| Dense room accumulation | **[SIM capacity / SW measured-input path]** [Dense evidence](https://github.com/azizu06/farsight/pull/142): 2,000,000 unique synthetic Mac cells; 5,000,000-cell retention cap is configuration, not a real room count. [Worker change](https://github.com/azizu06/farsight/pull/151) caps each RGB-D job at 4096 real samples, at most twice per second. Unsegmented RGB-D is short-lived to limit moving-body ghosts; unseen space stays empty. |
| Upright phone view | **[SW/SIM]** [180° mount normalization](https://github.com/azizu06/farsight/pull/156), synthetic upright/flipped/upright endpoint checks. Real mount-switch/detection alignment remains TODO-RIG. |
| Serial recovery | **[HW diagnosis / SW fix]** [Serial work](https://github.com/azizu06/farsight/pull/150) reproduced false Uno timeouts during idle CPU starvation; unanswered-command accounting fixes that cause while retaining the motor watchdog. Post-deploy floor stability remains TODO-RIG. |
| Smooth Quest camera playback | **[SW/SIM]** [No re-seek storms](https://github.com/azizu06/farsight/pull/152) and [vsync correction](https://github.com/azizu06/farsight/pull/154); actual headset decoded age/FPS remain TODO-RIG. |
| Mac renders the room | **[SW/desktop]** [Renderer](https://github.com/azizu06/farsight/pull/132), [capture correctness](https://github.com/azizu06/farsight/pull/155), [acceptance protocol](macview-rig-acceptance.md). Flat baseline; [stereo opt-in](https://github.com/azizu06/farsight/pull/159) is not an accepted stereo demo. |
| Jetson AI execution | **[HW component]** [Recorded measurement](pitch-metrics.md): pose TensorRT raw inference median 15.29 / p95 18.83 ms over 20 zero-input runs on Jetson under build load. Component inference time, not application FPS or headset latency. |

Tonight's numbers must record runtime/build SHA, source, duration and conditions:

| Required assembled-rig result | Fill only from tonight's evidence |
| --- | --- |
| Physical camera glass-to-glass p50/p95; Quest decoded age | **TODO-RIG:** stopwatch/high-speed video through the full phone/Jetson/cellular/Mac/Quest chain; distinguish server age, decode time and presentation. |
| Explore rotate % / unique floor coverage / duration | **TODO-RIG:** before/after trace on the same room and payload for frontier/arcs/spin changes; retain path and video. Do not reuse the pre-fix run or simulator percentage as the new result. |
| Dense unique points: Jetson / Mac retained / headset displayed | **TODO-RIG:** same session/epoch and elapsed acquisition time; distinguish measured points, native mesh vertices, historical observations and LOD draw counts. |
| Tailnet bytes/s and Quest FPS/frame-time p95 | **TODO-RIG:** dense on, actual headset, selected bitrate/resolution and refresh rate; desktop decode cadence is not XR FPS. |
| Trigger/B/link-loss stop and loaded-floor behavior | **TODO-RIG:** observed physical stop time/distance and recovery, not just zero PWM in logs. |

Use [pitch measurement protocol](pitch-metrics.md),
[Mac/Quest rig acceptance](macview-rig-acceptance.md) and
[fallback playbook](fallback-playbook.md). No missing number becomes zero or an
estimated success. Replace it with a sourced result or omit the numerical claim.

## Challenges and accomplishments

The hard problem was keeping a live camera responsive while reconstruction,
inference, networking and motor control all ran together. We separated camera
publication from geometry, isolated fine accumulation, and stopped replaying
old video backlog. The measurable result so far is the labelled Mac loopback
above; tonight determines the physical improvement.
[Camera fix](https://github.com/azizu06/farsight/pull/157),
[dense isolation](https://github.com/azizu06/farsight/pull/151).

Indoor exploration also exposed the difference between a simulator and a
loaded skid-steer rover. The historical floor trace was mostly rotation;
frontier handoff, arcs and a spin-specific measured PWM floor address the
observed causes. We will show the new floor trace or explicitly keep the result
pending. [Floor diagnosis](https://github.com/azizu06/farsight/pull/146),
[drive-profile correction](https://github.com/azizu06/farsight/pull/147).

We are proud of joining a real sensor/robot path to a VR operator experience,
and of preserving the distinction between measured room geometry, estimated
scan depth, simulation and display latency throughout the evidence above.

## Team credit

Credit human teammates and identify agent lanes as tooling, not extra entrants.
**TODO-TEAM:** confirm all human names/spellings and registered Devpost emails;
every teammate must be included on Devpost.

| Contributor/lane | Contribution and source |
| --- | --- |
| Aziz and operator/navigation/dense workers | Quest MANUAL/AUTO and walk integration, Jetson navigation/control, camera fast lane and fine accumulation: [controls](https://github.com/azizu06/farsight/pull/136), [navigation](https://github.com/azizu06/farsight/pull/146), [dense](https://github.com/azizu06/farsight/pull/142), [camera](https://github.com/azizu06/farsight/pull/157). |
| Sai, with Codex and Devin engineering lanes | Native/Rust geometry and system integration, Mac renderer/Quest stream integration and cinematic rendering. [Renderer checkpoint](https://github.com/azizu06/farsight/pull/132), [current lane handoff](https://github.com/azizu06/farsight/issues/24#issuecomment-6103237550), [integration ownership](macview-rig-acceptance.md). |
| Dechante | Mac rendering/capture/encode work, including same-task capture correctness: [capture fix](https://github.com/azizu06/farsight/pull/155), [renderer ownership](macview-rig-acceptance.md). |
| Mehdi and audit/optimization lane | Optional local face enrollment/named recognition and integration correctness/performance audits: [recognition](https://github.com/azizu06/farsight/pull/145), [current orientation/association audit](https://github.com/azizu06/farsight/issues/24#issuecomment-6103243190). This optional feature is not a requirement for the rescue story. |

## Sponsor tracks

These are intended opt-ins, not evidence of submission or an appointment.
Sponsor requirements and General categories: [Hacker's Guide](hackers-guide.md#sponsor-challenges-prizes-per-team-member).

**NVIDIA — Best Use of Jetson Nano.** The Jetson Orin Nano is central to the
robot: it consumes phone camera/LiDAR, runs person perception, hosts Nav2's
control integration, and sends motor commands. Show the actual board and live
runtime, then cite the recorded TensorRT component measurement rather than
calling it camera latency. [Jetson measurements](pitch-metrics.md),
[Nav2 floor evidence](https://github.com/azizu06/farsight/pull/146),
[camera integration](https://github.com/azizu06/farsight/pull/157).

**Encompass — Data Capturing Hardware.** Farsight turns real camera, LiDAR and
pose observations into room geometry, located person observations and a mission
record. Show source/session provenance and the same measured room across the
rover, Mac retention and headset. Report tonight's actual counts; do not present
the synthetic two-million-point capacity test as room capture.
[Measured dense path](https://github.com/azizu06/farsight/pull/142),
[mission record](https://github.com/azizu06/farsight/pull/81).

**Full Sail — Pathways & Perception.** Frame Farsight as a serious rescue-training
experience: a responder practices searching, interpreting person detections,
switching between autonomous exploration and manual control, and inspecting a
room spatially in VR. Show the rescue drill and its scoring limitations; the
intelligent navigation/perception must be part of the demonstrated exercise.
[Drill](https://github.com/azizu06/farsight/pull/79),
[VR controls](https://github.com/azizu06/farsight/pull/136),
[navigation](https://github.com/azizu06/farsight/pull/146).

**Hugging Face — Best Use of Open Source AI: TODO-CONFIRM.** Opt in only if the
voice assistant and/or estimated-depth scan is in the final demo and its actual
Hub model/provenance is shown. The optional stack includes Qwen3-VL, Whisper,
Piper and Depth Anything V2; component availability is not proof of the final
live voice/scan flow. Confirm which models ran, record the interaction, and
remove this opt-in if neither capability is demonstrated.
[Assistant evidence](assistant-setup.md), [scan](https://github.com/azizu06/farsight/pull/33),
[model provenance/licenses](../models/README.md).

General categories we fit: **Best Embedded Hack, Best AI Hack, Most “Useful”
Hack, Best Overall**. These use the same story and evidence, not a different
product pitch. General/track handling follows the [event guide](hackers-guide.md).

Use the rubric to organize answers: **Originality** (responder's live and
spatial view), **Technical Understanding** (sensor/compute/transport boundaries
and limits), **Functionality** (show the actual end-to-end flow), **Design**
(readable controls and source cues), **Wow Factor** (move from live rover view
into its measured room). [Official rubric](https://knight-hacks.notion.site/Judging-Rubric-38ae290ccffe80618aaac0649d791591).

## Judging-room plan

**Sun Oct 11, 12:00–4:00 PM.** Claim the project on the Hacker Dashboard and
copy the actual appointments here. Each slot has two minutes setup, six minutes
judging **including Q&A**, and two minutes teardown. General attendance and
every opted-in sponsor room are required. Missing an appointment **without
notice** disqualifies us from that challenge **and all General prizes**;
reschedule with an organizer beforehand. Do not silently skip a room because
the single rover is elsewhere. [Rules](hackers-guide.md#judging).

| Room | Actual time/location | Presenter | Backup | Kit / emphasis |
| --- | --- | --- | --- | --- |
| General | TODO-SLOT | TODO-TEAMMATE | TODO-TEAMMATE | Full rig if ready; complete rescue story |
| NVIDIA | TODO-SLOT | TODO-TEAMMATE | TODO-TEAMMATE | Board/runtime, AI + navigation evidence |
| Encompass | TODO-SLOT | TODO-TEAMMATE | TODO-TEAMMATE | Capture provenance, map and mission record |
| Full Sail | TODO-SLOT | TODO-TEAMMATE | TODO-TEAMMATE | Rescue drill, VR training and pathfinding |
| Hugging Face, only if confirmed | TODO-CONFIRM / TODO-SLOT | TODO-TEAMMATE | TODO-TEAMMATE | Actual open-model interaction and model cards |

Assign a rover mover/operator, headset helper, presenter and timekeeper; one
person may cover multiple roles. **TODO-TEAMMATE:** own the appointment schedule
and negotiate conflicts. Every room gets the same pitch and an honestly labelled
backup if the rig cannot move between adjacent slots.

### Six-minute demo, including questions

| Time | Presenter action / proof |
| --- | --- |
| 0:00–0:30 | TODO-TEAMMATE: one-line rescue scenario; name LIVE, bench, recording or SIMULATED tier. |
| 0:30–1:20 | MANUAL: judge holds right trigger and nudges right stick; release to stop. Keep movement inside the prepared room. |
| 1:20–2:10 | Press A: AUTO Explore; show Nav2 choosing a route around measured furniture. State demo mode's hands-free behavior; keep B accessible. |
| 2:10–3:10 | Walk through the measured 3D map with the right stick; point to the live camera window and source cues. Explain that the Mac renders the flat baseline. |
| 3:10–4:00 | A teammate enters the camera view: person box/marker appears. Show a located observation or drill result if that feature is enabled; do not claim an unknown face is identified. |
| 4:00–4:30 | B stops and returns to MANUAL. Show the architecture and one measured result with HW/SIM scope. |
| 4:30–6:00 | Questions; use the same evidence table for latency, autonomy, density and safety claims. |

For Full Sail, exchange some map time for the drill. For NVIDIA/Encompass,
show the board/data chain during the same sequence. Do not add a live voice or
scan step until **TODO-CONFIRM** is resolved and rehearsed.

### If the rig fails

Press B/STOP and release the trigger. Say “this is our recorded run,” then play
the locally saved backup showing the same four beats: manual drive → A/AUTO →
map walk → person found. Keep source/date visible and use the physical rig as
an exhibit. A labelled simulator/replay can explain controls afterward; it is
not substituted as evidence of autonomous floor performance. Cap recovery at
30 seconds so Q&A remains. [Fallback playbook](fallback-playbook.md).

## Backup demo video: record tonight

Save a local playable file and a second copy; test playback with network off.
**TODO-TEAMMATE:** capture/edit; **TODO-TEAMMATE:** verify provenance and backup.

| Shot | What must be visible |
| --- | --- |
| 1. Rig and room establishing shot | Jetson, mounted iPhone, Uno/wheels, Mac and Quest; full assembled payload and the prepared room. |
| 2. MANUAL and stop | External wheels + headset/cast view: right trigger/stick movement, release stop, then B. Keep the mode/source cue in frame. |
| 3. AUTO route | Press A on camera; continuous floor shot around furniture and a moving teammate, with Nav2 path/status capture. Include failures rather than cutting them into a false uninterrupted success. |
| 4. VR room walk | Actual headset capture/cast of the same measured room; right stick moves the viewer while the rover explores, live camera remains visible. Label flat Mac rendering. |
| 5. Person found | Teammate enters view; matching live box and measured-position marker. Show the drill/report only if enabled. |
| 6. Density proof | Same-session Jetson/Mac/drawn counts, acquisition duration, source/epoch and runtime SHAs. Pair diagnostics with recognizable surfaces, not only a large counter. |
| 7. Latency proof | Film a physical changing stopwatch/light through the camera and headset display; retain raw trials and compute glass-to-glass separately from network/request age. |
| 8. Link and recovery | Record the agreed controlled disconnect/B test, physical stop and explicit recovery; use the rig owner's procedure. |
| 9. Optional HF segment | Only after TODO-CONFIRM: real voice answer or estimated-depth scan, named Hub model, and measured/estimated source label. |
| 10. Closing evidence card | Real measured numbers with scope/date, team credit, architecture and public repository. No placeholder number appears as a result. |

Export a concise submission cut and keep the uncut evidence. **TODO-VIDEO:**
Devpost video URL, local backup path and tested playback owner. **TODO-MEDIA:**
README GIF and headset screenshot. Submit the public repository, add every
registered teammate, confirm sponsor opt-ins, then verify the final Devpost
receipt before 10:00 AM; push final code before 11:00 AM.
