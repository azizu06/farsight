# Architecture, in one page (#43, #15)

Farsight puts an operator inside a rover: Quest 2 for sight and steering, a Jetson Orin Nano on
the rover for perception, safety and logging. There are two physical kits. The **operator kit**
(Quest 2, MacBook relay, operator phone) sits with the judges; the **rover kit** (Jetson, mounted
phone camera, Uno, motors and pan servo) drives freely. The only link between them is a Tailscale
tunnel over the two phones' cellular data. Wire details are in [interfaces.md](interfaces.md);
this page is the explanation for judges and the Devpost diagram.

Rendered for slides and Devpost: [architecture.png](assets/architecture.png) (2x),
[architecture.svg](assets/architecture.svg). Source: [assets/architecture.mmd](assets/architecture.mmd);
re-render both after editing it with
`npx -y @mermaid-js/mermaid-cli@11.4.2 -i docs/assets/architecture.mmd -o docs/assets/architecture.png -b white -s 2`
(and again with `-o docs/assets/architecture.svg`).

```mermaid
---
config:
  flowchart:
    wrappingWidth: 420
    nodeSpacing: 28
    rankSpacing: 55
---
flowchart TB
  subgraph RV["ROVER KIT · drives freely"]
    direction LR
    RPh["<b>Rover phone</b>, mounted · Farsight Capture app<br/>RGB, or measured LiDAR RGB-D + pose<br/>Personal Hotspot is the rover uplink"]
    subgraph JS["<b>Jetson Orin Nano</b> · python -m jetson.server"]
      direction LR
      Cam["<b>CameraService</b><br/>/phone/rgb, /phone, or USB camera"]
      Perc["<b>PerceptionService</b><br/>YOLO11n-pose, TensorRT FP16<br/>IoU tracker, optional UltraFace faces"]
      Vid["<b>VideoService</b><br/>H.264 fMP4 via FFmpeg, JPEG"]
      Map["<b>MapState + ScanService</b><br/>LiDAR fusion, or Depth Anything V2<br/>five-view scan (estimated depth)"]
      Mis["<b>Mission</b><br/>drill score, SBAR triage,<br/>report + snapshots, record / replay"]
      Asst["<b>SceneAssistant + PhraseSpeaker</b><br/>Whisper, local VLM, Piper EN / ES"]
      Ctl["<b>ControlService</b><br/>250 ms permit lease · 500 ms view guard<br/>ultrasonic stop · Explore / Seek"]
      API["<b>FastAPI app</b> · HTTP + WebSocket routes"]
      Cam --> Perc
      Cam --> Vid
      Cam --> Map
      Perc --> Mis
      Perc -->|Seek target| Ctl
      Vid --> API
      Perc --> API
      Map --> API
      Mis --> API
      Asst <--> API
      Ctl <--> API
    end
    Uno["<b>Arduino Uno</b> · rover/farsight_uno<br/>300 ms motor watchdog<br/>drive motors, pan servo, ultrasonic"]
    RPh -->|"camera frames,<br/>local USB network"| Cam
    Ctl <-->|"USB serial 115200<br/>PWM, pan · range"| Uno
  end

  TS{{"<b>Tailscale tunnel</b><br/>rover-phone cellular ⇄<br/>operator-phone cellular<br/>no venue Wi-Fi"}}

  subgraph OP["OPERATOR KIT · judging table"]
    direction LR
    OPh["<b>Operator phone</b><br/>USB tether, cellular data"]
    Mac["<b>MacBook</b> · tools/operator_relay.py<br/>127.0.0.1:8080 serves quest/dist,<br/>proxies every HTTP + WebSocket route"]
    Quest["<b>Quest 2</b> · Quest Browser, WebXR (quest/src)<br/>main.ts · rover.ts · h264.ts<br/>detection-match.js · control-safety.js<br/>pose-hud.js · assistant-voice.ts"]
    OPh ---|USB| Mac
    Mac <-->|"USB + adb reverse :8080"| Quest
  end

  API ==>|"<b>DOWN</b><br/>video: /ws/video<br/>detections: /ws/detections<br/>map: /ws/map, Brotli HTTP<br/>permits, status: /ws/control<br/>report: /mission/report<br/>answers + Piper audio"| TS
  TS ==>|"<b>UP</b><br/>deadman, v, w, pan, mode<br/>+ permit echo, 20 Hz<br/>voice: /ask audio<br/>Scan, drill, Seek requests"| API
  TS <==>|"every stream<br/>through the relay"| Mac

  classDef op fill:#e8f1ff,stroke:#2f6fd6,color:#0b2547
  classDef rv fill:#eaf7ee,stroke:#2e8b57,color:#0d2e1a
  classDef safe fill:#fff1e6,stroke:#d9661f,color:#3d1a05
  classDef net fill:#f4ecff,stroke:#7a4cc2,color:#26104a
  classDef hub fill:#ffffff,stroke:#2e8b57,color:#0d2e1a,stroke-width:2px
  class Quest,Mac,OPh op
  class RPh,Cam,Vid,Perc,Map,Mis,Asst rv
  class Ctl,Uno safe
  class TS net
  class API hub
```

## How a frame becomes an action

1. **Camera to Jetson.** One frame source at a time (USB camera, `/phone/rgb`, replay, or the
   explicit simulator). A missing real camera never falls back to a simulated one.
2. **Perception on the rover.** The detector runs on the Jetson (TensorRT engine on target; ONNX
   or HOG as labelled CPU fallbacks). A small tracker keeps ids through brief dropouts and never
   reuses one; pose adds 17 keypoints and a `down` flag.
3. **Video + boxes stay matched.** Each detection packet carries the capture id of its frame (JPEG
   id, or H.264 per-sample capture ids). The headset draws the exact match, or the newest packet from
   the same source within 8 frames, faded by its lag; anything older is dropped, never guessed
   ([PR #66](https://github.com/azizu06/farsight/pull/66)).
4. **Quest to rover.** The headset sends control at 20 Hz while the right trigger is held, echoing a
   server-issued permit. The permit expires 250 ms after issue; the Uno separately zeros the motors
   after 300 ms of silence. Stale video, disconnect, or a lost controller stops the rover and
   requires a release and new press.
5. **Autonomy is a policy on the same lease.** Explore and Seek are computed on the Jetson from
   detections and ultrasonic range. They never renew a permit themselves; stick input overrides them.
6. **Evidence.** Detections, snapshots, range, drill results and SBAR notes go into the mission
   report where the sensors are. `--record` saves a run that `--replay` plays back with REPLAY badges
   and inert controls.
7. **Local assistant.** `/ask` answers from live detections, with an optional local VLM, Whisper and
   Piper. It cannot move motors; a proposed Seek or Scan needs a confirm *and* a held trigger.

## What is deliberately not clever

- Safety lives on the robot (lease, ultrasonic stop, Uno watchdog), not in the headset or the network.
- The network is two cellular tethers and a Tailscale tunnel. Models run offline; the *link* needs
  cellular internet. No path means no untethered live video; we then show labelled recordings.
- Geometry has three labelled kinds: **measured** (LiDAR iPhone, ARKit pose), **estimated**
  (monocular Depth Anything V2, five-view stationary scan, optical-axis metres) and **none** (ordinary
  iPhone or webcam: RGB only). The viewer and docs never present estimated depth as measured.
- Rust (`rust/`) is a parallel port with the same wire contract. It lacks replay and may lag the
  newest Python routes; the Python server is the demo runtime until parity is checked.

## Pointers

| Topic | Doc |
|---|---|
| Wire contract | [interfaces.md](interfaces.md) |
| Cellular + Tailscale plan | [demo-connectivity.md](demo-connectivity.md), [transport-setup.md](transport-setup.md) |
| Jetson bring-up | [jetson-bringup.md](jetson-bringup.md), [inference-setup.md](inference-setup.md) |
| Mapping (LiDAR) / scan (camera-only) | [mapping.md](mapping.md), [scan.md](scan.md) |
| Assistant + triage + phrases | [assistant-setup.md](assistant-setup.md) |
| Software measurements | [qualification-results.md](qualification-results.md), [ui-validation.md](ui-validation.md) |
