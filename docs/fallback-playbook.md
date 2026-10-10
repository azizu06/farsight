# Fallback and recovery playbook (#43)

For the live sequence in [judge-demo.md](judge-demo.md). Rule zero: **say what is happening, label
it on screen, never present simulation or replay as live rover proof.** The safety design stops the
rover on stale video, expired permit, or disconnect on purpose; a stop is not a failure to hide.
Do not lengthen the 250 ms permit or 500 ms video guards to cover a slow link.

## Honesty ladder

Pick the highest tier that works, announce the tier, and stay on it.

| Tier | What it is | Start | On-screen label | May claim |
|---|---|---|---|---|
| **0 Live rover** | Real camera, motors, Jetson | [demo-checklist.md](demo-checklist.md) | none | Only what the rig qualified |
| **1 Live, motors off** | Real camera + detections, no driving | `FARSIGHT_SERIAL_PORT` blank, `sudo systemctl restart farsight` | motion disabled in HUD | Perception, assistant, report. Not driving or Seek |
| **2 Replay of a real run** | Recorded camera/detections/range, controls inert | `python -m jetson.server --replay runs/<UTC timestamp> --h264` then `python -m tools.demo --upstream http://127.0.0.1:8443 --quest` | **REPLAY** in page and headset; `/health` shows `replay: true` | "Recorded earlier on the rover." Not a live run. **No real run is recorded yet (#18 open)** |
| **3 Simulator** | Ray-cast room, virtual rover, no motors/camera | `python -m tools.demo --simulate --build --quest` (add `--h264` if FFmpeg) | **SIMULATED** | Control policy, UI, mapping pipeline. Not perception on real scenes, not hardware |
| **4 Video** | Demo video / screenshots | Devpost video (#15) | Say "recorded" | Whatever the video honestly shows |

Tier 2 today means `tools/fixtures/replay-demo`: 7 frames, ~1.4 s, **simulated** source, empty
boxes. It checks plumbing only. If no real run is recorded before judging, skip to tier 3.
Mac webcam with the virtual rover (`python -m jetson.server --camera 0 --simulate-rover`) is
permitted by the CLI (`jetson/mapping/server.py`, only `--demo` and `--replay` reject cameras), so a
teammate's live image can feed real detection and Seek math with no motors; it is not rehearsed in
this repo, so test it before relying on it and describe it as "laptop camera, virtual rover".

## 30-second recovery scripts

Each: **Detect, Do, Say, Rollback point.** Rig tech executes, presenter speaks, operator releases
the trigger first in every case.

### 1. Link drop (cellular, Tailscale, USB relay)

- **Detect:** HUD video age climbs or stops; rover stops; `tools.ready` shows `server WAIT`.
- **Do (0-10 s):** Operator **release trigger**. Check both phone bridge screens, cellular/Tailscale state, and phone USB cables. On the Mac run `python -m tools.phone_usb --check`; on the Quest side run `adb reverse tcp:8080 tcp:8080`, then reload `http://localhost:8080`.
- **Do (10-25 s):** If the path returns: operator Enter VR, **release then press** the trigger (a restored link never re-arms a held trigger). If not: switch to tier 2 or 3 commands above on the MacBook.
- **Say:** "The cellular link dropped and the rover stopped itself, as designed. I'm switching to a recorded run / simulator and it will be labelled."
- **Do not:** hold the trigger through reconnect expecting movement; swap the 250 ms lease; use venue Wi-Fi unannounced.
- **Evidence kept:** software fault tests stopped simulated motion in 43-333 ms ([qualification-results.md](qualification-results.md)); physical Uno/Jetson stop timing is a pending hardware gate.

### 2. Jetson down (no boot, no SSH, service dead)

- **Detect:** `python -m tools.ready --url http://127.0.0.1:18443` through the running Mac phone-USB helper is not `READY`; `journalctl -u farsight -n 50` on the Jetson if reachable.
- **Do (0-15 s):** One `sudo systemctl restart farsight` if you can reach it. Otherwise do not debug on stage.
- **Do (15-30 s):** Run tier 2 (if a real recording exists) else tier 3 on the MacBook. Both run without the Jetson.
- **Say:** "The Jetson isn't up, so you are seeing the [recorded rover run | simulator], not the live robot. Here is what the Jetson runs when it is up:" then use [architecture.md](architecture.md) and any measured numbers with their stated source.
- **Do not:** claim Jetson/TensorRT numbers measured on the Mac. Only the measured TensorRT pose engine time (median 15.29 ms / p95 18.83 ms, [jetson-runtime-results.md](jetson-runtime-results.md)) comes from the Jetson; label every other latency by its actual source (Mac CPU/Metal, simulated, or the board).

### 3. Camera down (stale or missing frames)

- **Detect:** HUD "camera stale / unavailable"; rover stops (a missing camera never falls back to a simulated view on real motors); `tools.ready` `camera WAIT`.
- **Do (0-15 s):** Release trigger. Replug the USB webcam; `journalctl -u farsight -n 50`. If an iPhone is the camera source, reopen the Farsight Capture app and recheck the Jetson USB-network IP.
- **Do (15-30 s):** Still dead: tier 2 (replay), or tier 3. Tier 1 does not help (no camera).
- **Say:** "Camera feed lost; the rover refuses to drive blind. Switching to recorded footage, labelled."
- **Note:** ordinary iPhone gives RGB only. Never present RGB or camera-only scan as LiDAR.

### 4. Model slow (detector fps low, assistant late, scan long)

- **Detect:** `/health` `detector_fps` and `detector_inference_ms`; HUD boxes sparse; Ask shows a long timing line.
- **Detector slow:** The rover can still drive; boxes are intermittent. Say it: "Detection is running at X fps here." Read X from `/health`; do not quote a target. Pose/GPU claims require `detector_backend` to show TensorRT; HOG/ONNX CPU is a labelled fallback.
- **Assistant slow or model not loaded:** The text assistant answers from observed detections without any language model. Submit text; skip voice and skip the vision question. Fix later with `python -m tools.measure ask`.
- **Scan slow:** Skip it; it is optional. A scan needs the depth model on the Jetson, which is unverified there.
- **Say:** "The vision model is slower than I'd like on this hardware; it still answers from what the detector sees."
- **Do not:** restart services mid-demo for speed; let a VLM reply speak a motion claim (answers never move motors; actions need a confirm and a held trigger).

### 5. Quest or controller problem

- **Detect:** No video in headset but MacBook browser fine; controller loss; system menu opened while holding trigger.
- **Do:** Re-run `adb reverse tcp:8080 tcp:8080`; reload. Drive from the MacBook with Space+WASD only if the rig tech confirms floor is clear; otherwise present from the browser view as tier 1. Switch H.264 off in settings (JPEG is the compatibility fallback).
- **Say:** "Headset tracking dropped; the rover stays stopped until I re-press the trigger."

## Reset after a recovery

1. Operator trigger released; rig tech confirms rover stopped and powered state known.
2. Restart the unit (`sudo systemctl restart farsight`) or the replay/simulator process so mission state matches the tier announced.
3. Confirm `tools.ready` or `/health` flags (`replay`, `simulated`) match what you tell judges.
4. Resume the sequence from the last unfinished row of [judge-demo.md](judge-demo.md).

## Hard stops

- Rover moves when it should not: **battery switch**, then explain. Do not touch the keyboard first.
- Any doubt about a floor-driving gate that did not pass in rehearsal: tier 1 or below.
- Judging appointments are fixed: missing one is a DQ ([hackers-guide.md](hackers-guide.md)). Reschedule before, not after.
