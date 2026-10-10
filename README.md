# Farsight

**Search-and-rescue telepresence: explore a building through a rover, then walk through the room it maps in VR.**

A responder wears a Quest 2, drives manually or switches to indoor autonomous
exploration, sees the live camera with person detections, and inspects the
measured 3D room. The iPhone supplies camera, LiDAR and pose; the Jetson Orin
Nano runs perception, navigation and motor control; the Mac retains the dense
map and renders the remote room. Current rig operation uses Nav2 on the Jetson;
this is a hackathon prototype with remaining full-rig acceptance checks, not a
validated rescue deployment. See the [current evidence and pitch](docs/devpost-draft.md#what-works-and-what-still-needs-the-rig).

> **TODO-MEDIA:** add a real-rig demo GIF: MANUAL → A/AUTO → map walk → person found.
> Add a headset screenshot with source and mode visible; label replay/simulation.

[![Operator and rover architecture](docs/assets/architecture.svg)](docs/architecture.md)

[Architecture](docs/architecture.md) · [Devpost draft and 6-minute script](docs/devpost-draft.md) ·
[Backup playbook](docs/fallback-playbook.md) · [Rig acceptance](docs/macview-rig-acceptance.md)

## Current demo

- **MANUAL → AUTO:** right trigger and stick drive; A starts Explore and turns
  the map into a walk-through, with the live camera still visible. B stops and
  returns to MANUAL. [Controls and software evidence](https://github.com/azizu06/farsight/pull/136).
- **Indoor navigation:** the current rig profile enables Nav2 using iPhone
  LiDAR/ARKit. Frontier handoff, gentle arcs and the measured spin PWM floor
  address a real floor-run spinning problem. The improved floor-run result is
  **TODO-RIG**, not the simulator's coverage number.
  [Frontiers/arcs](https://github.com/azizu06/farsight/pull/146),
  [spin floor](https://github.com/azizu06/farsight/pull/147).
- **Live camera and people:** camera publication runs independently of mapping;
  playback avoids repeated seeks. Person detections also have separate,
  short-lived LiDAR-positioned markers in the local map; moving people are not permanent
  room geometry. Upright normalization handles a 180° phone mount.
  [Camera fast lane](https://github.com/azizu06/farsight/pull/157),
  [playback](https://github.com/azizu06/farsight/pull/152),
  [vsync correction](https://github.com/azizu06/farsight/pull/154),
  [upright mount](https://github.com/azizu06/farsight/pull/156),
  [marker implementation](https://github.com/azizu06/farsight/commit/f1b883c),
  [3/3 software marker checks](quest/tests/person-markers.test.mjs).
- **Measured dense room:** a separate fine visual lane accumulates observed,
  pose-aligned RGB-D; bounded revision pages feed Mac retention and spatial LOD
  chunks. No duplicated or interpolated points are presented as new detail.
  The Mac retention cap is five million cells; two million were tested with
  synthetic input, **not** measured in a room.
  [Dense path](https://github.com/azizu06/farsight/pull/142),
  [background accumulation](https://github.com/azizu06/farsight/pull/151).
- **Mac rendering:** the Mac renders/encodes the room and the Quest decodes it.
  The current path is flat and pose-driven. Stereo is an opt-in integration in
  progress; the Quest-side compositor is merged, but full stereo headset
  acceptance is pending.
  [Renderer checkpoint](https://github.com/azizu06/farsight/pull/132),
  [capture correction](https://github.com/azizu06/farsight/pull/155),
  [AUTO integration](https://github.com/azizu06/farsight/pull/144),
  [stereo opt-in](https://github.com/azizu06/farsight/pull/159).

## Quick start: assembled rig

Use the deployment owner's existing services and listeners; preserve the active
TLS, geometry and drive-profile settings. The [Jetson bring-up](docs/jetson-bringup.md),
[phone relay](docs/phone-relay.md) and [Mac/Quest acceptance runbook](docs/macview-rig-acceptance.md)
contain the full setup. The rover has **no ultrasonic sensor or pan servo**.
Its current rig profile enables Nav2 and uses LiDAR for obstacle geometry;
generic server launches still require `--nav2`. Do not substitute the older
reactive/no-range policy for that profile. [Firmware change](https://github.com/azizu06/farsight/pull/91),
[live floor evidence](https://github.com/azizu06/farsight/pull/146).

1. On the Jetson, check the installed profile without starting another server:

   ```sh
   .venv/bin/python -m jetson.launch --check --env-file /etc/farsight.env
   ```

   The no-ultrasonic/no-servo rig needs its corresponding proximity/pan settings,
   Nav2 bridge, measured drive profile and LiDAR capture. The selected rig demo
   profile also enables demo mode. See [navigation](docs/navigation.md) and
   [profile settings](jetson/farsight.env.example); the example file's blank
   values are generic defaults, not a copy of the deployed rig.

2. Start camera/LiDAR capture and the configured bridge in Farsight Capture on
   the rover iPhone. Keep the operator iPhone bridge foregrounded. Connect both
   computers to their phones by USB, and Quest to the Mac by USB. The primary
   route is phone-to-phone cellular/Tailscale; raw RGB-D stays local to the
   Jetson. [Routing and recorded transport evidence](docs/phone-relay.md).

3. On the Mac, install dependencies and bundle while internet is available:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r tools/requirements.txt
   npm --prefix quest ci
   npm --prefix quest run build
   .venv/bin/python -m tools.phone_usb --check
   ```

   In separate terminals, for a fresh launch with no existing listeners:

   ```sh
   .venv/bin/python -m tools.phone_usb
   .venv/bin/python -m tools.demo --upstream http://127.0.0.1:18443 --quest \
     --dense-map --demo-mode --render-upstream http://127.0.0.1:8091
   .venv/bin/python -m tools.mac_render --upstream http://127.0.0.1:8080 \
     --port 8091 --width 1280 --height 720 --fps 30 --hardware videotoolbox
   ```

   Install Python Playwright/Chromium and FFmpeg with VideoToolbox **before**
   starting the renderer; these are additional renderer dependencies. Follow the [renderer runbook](docs/macview-rig-acceptance.md#2-launch-only-through-the-existing-deployment-owner).
   Preserve `--geometry-upstream` when the existing native geometry assembler is
   in use. Open `http://localhost:8080/?macview=1` in the existing Quest Browser
   tab through ADB reverse. Select H.264 once ready; a server flag alone does
   not change the viewer. Keep stereo off for the flat baseline.

## Quest controls

| Action | Current behavior |
| --- | --- |
| Right trigger + right stick | MANUAL physical driving; release trigger to stop |
| Tap right A | AUTO Explore; right stick now moves only the virtual viewer, with sideways snap turns |
| Tap A again | Cancel autonomy/navigation, return to MANUAL; release trigger before driving again |
| Right B | Hard stop, cancel navigation, return to MANUAL |
| Hold A | Actions menu; point and select with trigger or grip |
| Right grip | MANUAL: select a person for Seek; AUTO: set a floor go-to goal |
| Right stick click | Cancel go-to |
| Left X / Y | Recenter / show controller diagram |

[Control evidence](https://github.com/azizu06/farsight/pull/136) and
[full controls](quest/README.md#controls). In the default immersive **demo mode**,
AUTO sends hands-free Explore heartbeats until A/B; the client warns about stale
video rather than blocking motion, and tolerates brief controller interruptions.
`?demo=0` restores strict client guards. Server/relay demo flags are separate;
the demo server bypasses teleop camera-freshness gating and transient-stop
release latches. Permits, link-loss stops, B/STOP, Nav2 sensor checks and the
Uno watchdog remain. Do not describe demo mode as having strict stale-video
protection. [Server demo semantics](https://github.com/azizu06/farsight/pull/134).

## Rehearse without the rover

```sh
.venv/bin/python -m pip install -r jetson/mapping/requirements.txt -r jetson/control/requirements.txt
.venv/bin/python -m tools.demo --simulate --h264
```

Open `http://localhost:8080`. Desktop: Live rover, hold Space and use WASD;
release to stop. This is **SIMULATED**, with real software/FFmpeg and simulated
camera/rover input. A recorded session is **REPLAY** with inert controls.
[Judge software check](docs/judge-demo.md):

```sh
FARSIGHT_PYTHON="$PWD/.venv/bin/python" npm --prefix quest run test:judge
```

The camera fast-lane loopback measured fragment age p95 **170 ms** while mapping
was deliberately delayed **800 ms**; this is a Mac software measurement, not
physical camera-to-eye latency. [Evidence](https://github.com/azizu06/farsight/pull/157).
**TODO-RIG:** tonight's glass-to-glass p50/p95, Explore rotate percentage/coverage,
Jetson/Mac/headset unique point counts, tailnet bytes/s and headset FPS. Record
source, runtime revision and conditions using the [submission evidence table](docs/devpost-draft.md#what-works-and-what-still-needs-the-rig).

<details>
<summary>Earlier laptop software baseline (not current rig performance)</summary>

Measured 2026-10-10 with `tools.pitch_metrics`, 20 seconds per phase against
`jetson.server --demo --simulate-rover --h264` on a laptop, real FFmpeg and HOG,
simulated camera and motors. Retained as a historical measurement, not a claim
about the assembled rover. [Measurement index](docs/pitch-metrics.md).

| Metric | Historical SIM result |
| --- | --- |
| HOG detector / MJPEG / H.264 delivery | 3.0 / 14.7 / 9.8 fps |
| Stopped control RTT p50 / p95 | 23.9 / 52.6 ms |
| Permit age at send p50 / p95 | 31.2 / 51.0 ms |

</details>

## Team and project guides

Aziz's lanes cover operator controls, Jetson/navigation and dense/camera
integration. Sai and the Codex/Devin lanes contribute phone/native and Rust
geometry, Mac rendering and integration; Dechante contributes the Mac render,
capture and encode work. Mehdi contributes opt-in local face enrollment/named
recognition and correctness/performance audits. See the
[credit/evidence table](docs/devpost-draft.md#team-credit) for scope and sources;
agent labels name engineering lanes, not extra human team members.

[Wire contract](docs/interfaces.md) · [Mapping](docs/mapping.md) ·
[Inference](docs/inference-setup.md) · [Assistant](docs/assistant-setup.md) ·
[Models and licenses](models/README.md) · [Hardware checks](docs/hardware-preflight.md)
