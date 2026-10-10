# Pitch metrics: rig protocol

Numbers for the README, slides and Devpost (issue #12). Two are automatic
(`tools.pitch_metrics`), two need a camera and a tape measure. Record every
value with its conditions; a laptop-simulated number is never a rig number.

`python -m tools.pitch_metrics` is **read-only toward the rover**. It sends
only deadman-released zero commands, returns no permit, and never moves
motors. Close the Quest page first: control has one owner.

## Measured so far

Newest number for each metric, taken from the team board and the result docs on
2026-10-10. A row labelled **emulation** or **laptop sim** was not measured on
the rover, the phones or the Jetson. No row here is glass-to-glass headset
latency or a floor stop distance: those still need the protocol in sections 2
and 3. Physical motor stop time is not measured.

### Real phones and the Jetson

| Metric | Value | Where measured | Conditions | Source |
|---|---|---|---|---|
| Control round-trip p50 / p95 / max | 119.5 / 167 / 228 ms | Real phones + Jetson | Two-phone chain, Wi-Fi off both phones, Mac–Jetson USB unplugged. Mac `tools.demo` → iproxy → operator iPhone 16 Pro → phone tailnet → rover iPhone → Jetson `172.20.10.3:8443`. Main with #87 and #88 (40 Hz permits, send on each new permit). H.264 also running. Rover phone path was DERP (mia) | [board, 17:12 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237) |
| Expired permits | 0 per 30 s | Real phones + Jetson | Same two-phone chain and #88 load as the row above. Probe mode not stated for this run; assumed the same stop-only, no-rover method as the 16:36 run | [board, 17:12 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237) |
| Permit gap, max | 108 ms | Real phones + Jetson | Same run as the expired-permit row | [board, 17:12 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237) |
| Stream fps, H.264 | about 9 fps | Real phones + Jetson | Same two-phone chain and #88 load. Encoder set to 10 fps / 500 kbps per the 14:49 `/etc/farsight.env` handoff | [board, 17:12 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237); [board, 14:49 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6098730540) |
| Path RTT, Jetson to operator phone | direct IPv6, about 90–126 ms | Real phones + Jetson | Direct path, not DERP. Separate from the control-probe RTT above | [board, 17:12 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237) |
| Control round-trip p50 / p95, older two-phone run | 123 / 182 ms (max 270 ms, 305 probes) | Real phones + Jetson | Same chain, before #88, 20 Hz permits. Superseded by the 119.5 / 167 / 228 ms row | [board, 16:36 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6099765930) |
| Expired permits, older two-phone run | 4 per 30 s; max permit gap 228 ms | Real phones + Jetson | Same pre-#88 chain. Stop-only; server reported `serial_disconnected`. Superseded by 0 per 30 s | [board, 16:36 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6099765930) |
| Stream fps, H.264 / detector / MJPEG, older two-phone run | 9.1 / 6.7 / 6.0 fps | Real phones + Jetson | Same pre-#88 chain. Permit age p95 was 11 ms. Superseded for H.264 by "about 9 fps"; no newer detector or MJPEG figure on this chain | [board, 16:36 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6099765930) |
| Control round-trip, Jetson over its own USB | p50 / p95 / max 46 / 81 / 129 ms | Real Jetson, stop-only | `tools.link_lab` profile `none` against `http://192.168.55.1:8443`. Sub-millisecond ping. Rover phone unplugged, so inference load only, no cellular hop. 20 Hz permits | [link-lab.md](link-lab.md) |
| Pose TensorRT raw inference | median 15.29 ms, p95 18.83 ms | Real Jetson GPU | FP16 engine, `[1,56,8400]`, 20 zero-input runs under concurrent native-build load. Engine time, not camera-to-eye. COCO `[1,84,8400]` on the same smoke: median 11.66 ms, p95 21.44 ms | [jetson-runtime-results.md](jetson-runtime-results.md) |
| Detector fps, Jetson USB | about 7.5–8.9 fps | Real Jetson, static test photo over the phone protocol, Mac viewer | Pose TensorRT while native builds continued. Not a real iPhone camera and not the two-phone chain | [jetson-runtime-results.md](jetson-runtime-results.md) |
| Camera and mapping rate, live phone on Jetson USB | 6.01 Hz (354 frames in 59.1 s) | Real iPhone RGB-D + Jetson | 60-sample read-only run. No new drops or rejections. Motors absent, estop true, zero PWM. Not cellular and not headset rendering | [jetson-runtime-results.md](jetson-runtime-results.md); samples in [qualification/jetson-phone-performance-summary.json](qualification/jetson-phone-performance-summary.json) |
| Map timing, same USB capture run | 117 ms median / 141 ms p95 | Real iPhone RGB-D + Jetson | Same 60-sample run. Source CPU 147% median, FFmpeg 8%, GPU 8% median, RAM about 3.82/7.49 GB | [jetson-runtime-results.md](jetson-runtime-results.md) |
| Detector work, same USB capture run | 104 ms median / 130 ms p95 | Real iPhone RGB-D + Jetson | Includes image decode, preprocessing and the deployed face pass. Not raw TensorRT time | [jetson-runtime-results.md](jetson-runtime-results.md) |
| Desktop idle power, camera waiting | 4.84–4.96 W input; GPU 18–37%; GPU 44.9–45.2 °C | Real Jetson | 11-sample, 1 s `tegrastats`, 25 W mode, Firefox viewer open, no motors, no native model builds. Not inference or loaded-rig power | [jetson-runtime-results.md](jetson-runtime-results.md) |
| Control round-trip over Mac internet, not the phone chain | p50 / p95 101 / 157 ms | Real Jetson, operator Mac relay | 20 s read-only probe. Tailscale direct, 43 ms ping. Permit age p95 34 ms. Not the two-phone route | [board, 14:48 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6098723251) |
| Stream fps on that Mac-internet relay | detector 7.75 fps (pose TRT), H.264 9.6 fps, MJPEG 6 fps | Real Jetson, operator Mac relay | Same 20 s probe. Server camera age 125–142 ms. Older than the two-phone rows | [board, 14:48 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6098723251) |

### Not measured yet

| Metric | Value | Where measured | Conditions | Source |
|---|---|---|---|---|
| Glass-to-glass latency | not measured | — | Protocol is in section 2. Software arrival age is not display age | this file; [qualification-results.md](qualification-results.md) |
| Stop distance after deadman release | not measured | — | Protocol is in section 3. Five floor trials, not run | this file |
| Physical stop time (lease, Uno watchdog) | not measured | — | Design limits are 250 ms server lease and 300 ms Uno watchdog. No scope or video timing on the rig | [rig-rehearsal.md](rig-rehearsal.md); [jetson-runtime-results.md](jetson-runtime-results.md) |
| Assistant latency on the Jetson | not measured beside pose and H.264 | Mac only, below | Voice services were left off on the board. A Mac model comparison is in the emulation table and is not a rig number | [jetson-runtime-results.md](jetson-runtime-results.md); [board, 15:09 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6098920089) |

### Emulation and laptop simulation

| Metric | Value | Where measured | Conditions | Source |
|---|---|---|---|---|
| Control round-trip p50 / p95 | 23.9 / 52.6 ms | Laptop sim | `tools.pitch_metrics`, 20 s, real FFmpeg, HOG fallback, simulated camera and rover. Not a rig number | [README.md](../README.md) |
| Detector fps | 3.0 fps (HOG fallback) | Laptop sim | Same pitch-metrics run | [README.md](../README.md) |
| Stream fps, MJPEG | 14.7 fps | Laptop sim | Same run, frames parsed from `/stream.mjpg` over 20 s | [README.md](../README.md) |
| Stream fps, H.264 | 9.8 fps | Laptop sim | Same run, `/ws/video` segments over 20 s | [README.md](../README.md) |
| Permit age at send p50 / p95 | 31.2 / 51.0 ms | Laptop sim | Same run. Limit is 250 ms | [README.md](../README.md) |
| Control acknowledgement p95, fault fixture | 156 ms (stop-only qualifier p95 205 ms on a separate run) | Laptop sim | macOS ARM64, synthetic RGB, 50 ms each way, shared 2 Mbps, 10 fps / 500 kbps. Not a phone or Jetson | [qualification-results.md](qualification-results.md) |
| Video arrival age p95 / held view peak | 285 ms / 416 ms | Laptop sim | Eight-second RGB trial after the VBV fix, same 100 ms RTT / 2 Mbps profile. Capture-receipt bound, before display | [qualification-results.md](qualification-results.md) |
| Simulated stop after injected fault | FFmpeg killed 169 ms; video paused 333 ms; connections aborted 43 ms | Laptop sim | Includes detection and polling. Not Uno watchdog or physical stop time | [qualification-results.md](qualification-results.md) |
| Detector work p95, CPU pose, after the 2-worker cap | 24.0–35.4 ms | Laptop sim | Apple M5 Pro, 20 s trials, real ONNX pose, real FFmpeg. Quiet and combined load. Not Jetson TensorRT | [qualification-results.md](qualification-results.md) |
| Held-drive video fps, emulated cellular | direct 10.0 fps; DERP-typical 10.0 fps; DERP-bad 7.3 fps (9.5 at 250 kbps); booth 5.7 fps | Emulation on a Mac | `tools.link_lab`, 60 s held drive, synthetic RGB, simulated motors. After the 40 Hz permit change | [link-lab.md](link-lab.md) |
| Emulated probe RTT p50 / p95 / max | direct 91 / 102 / 109 ms; DERP-typical 117 / 212 / 343 ms | Emulation on a Mac | 20 Hz permits. Calibrated against a field DERP Miami probe of 118 / 217 / ~350 ms. Not the 17:12 phone-chain probe | [link-lab.md](link-lab.md) |
| Stutter threshold, network RTT p95 | about 125 ms before, about 155 ms after | Emulation on a Mac | 50 ms base RTT, jitter sweep, no stalls. Lease stayed 250 ms | [link-lab.md](link-lab.md) |
| Bandwidth while driving, optimized map policy | about 635–636 kbps proxy at a 2 Mbps cap | Laptop sim | Two 20 s repeats, 50 ms each way, 0.1% modeled retries. Held deadman, map paused. H.264 payload about 471 kbps. Not a phone or carrier measurement | [qualification-results.md](qualification-results.md) |
| Control-status RTT, Rust server, paced bulk | p50 / p95 / max 204 / 256 / 279 ms (unpaced p95 501 ms, max 1261 ms) | Laptop sim | `tools.fault_proxy`, 2 Mbps, 100 ms, Rust server. Not the Python board runtime | [board, 12:57 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6097741999); [rust/README.md](../rust/README.md) |
| Scan, real depth model, synthetic frames | 596.5 ms, 89,967 points (view latencies 71.08–87.15 ms) | Laptop sim | Mac arm64, ONNX Runtime CPU, two threads, synthetic 160×120 room. No physical turn or settle. A desktop Chrome rehearsal of the same scan was 613 ms | [scan.md](scan.md) |
| Assistant image latency, Mac comparison | Qwen3-VL-2B 548 ms; Qwen2.5-VL-3B 537 ms; SmolVLM-500M 231 ms | Mac only | Image cap 256 applies to Qwen3-VL-2B. Not installed on the Jetson. Not measured beside pose and H.264. Piper keep-warm (machine not stated) was about 50 ms per answer versus 700 ms cold; that is also not a Jetson figure | [board, 15:09 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6098920089); [board, 09:59 UTC](https://github.com/azizu06/farsight/issues/24#issuecomment-6096353293) |

## 1. Automatic metrics (about 1 minute)

On the Jetson (detector fps and stream fps with no network in the way):

```sh
python -m tools.pitch_metrics --url http://127.0.0.1:8443 \
  --seconds 30 --output pitch-jetson.json --markdown pitch-jetson.md
# HTTPS profile: --url https://127.0.0.1:8443 --ca-file /etc/farsight/tls/ca.pem
```

The board serves plain HTTP (TLS omitted, [jetson-runtime-results.md](jetson-runtime-results.md)), so `http://` is the default here.

On the operator laptop, through the relay the Quest uses (control RTT and stream
fps as the demo sees them over the real link):

```sh
python -m tools.demo --upstream http://<jetson>:8443   # separate terminal (HTTPS profile: https://<jetson>:8443 --ca-file ~/farsight-ca.pem)
python -m tools.pitch_metrics --url http://127.0.0.1:8080 --seconds 30 \
  --output pitch-relay.json --markdown pitch-relay.md
```

Start the Jetson with `--h264` or the H.264 phase is skipped. Put a person in
view of the camera during the run, or the detector reports no samples. Repeat
once under cellular and once on USB/LAN; name the link in your notes.

| Reported | Source | Notes |
|---|---|---|
| Detector fps | median of `/health.detector_fps`, polled every 0.5 s | YOLO/TensorRT on the Jetson; the laptop mock uses a HOG fallback |
| Stream fps, MJPEG / H.264 | frames received over `--seconds` each | MJPEG then H.264, run back to back |
| Control round-trip p50 / p95 | stopped `/ws/control` probe to its echoed status, 10 Hz | measured while video is streaming |
| Permit age p50 / p95 | age of newest received permit at each probe | server limit is 250 ms; permits arrive about every 50 ms |

Optional `--hold-permit` also sends zero-velocity held-deadman heartbeats and
reports the server's own `link_ms`. It takes the control lease (still no motion,
but it will recentre the pan servo), so use it on a stopped, chocked rover only.

Server-reported H.264 age (`server_age_ms` in the JSON) is not camera-to-display
latency. Use section 2 for that.

## 2. Glass-to-glass latency (stopwatch, median of 10)

Setup:
1. Run a stopwatch with millisecond display on a phone or laptop (a full-screen
   stopwatch page). Hold it still in front of the rover camera, filling most of
   the frame and well lit so digits are sharp.
2. Wear the Quest, in the live rover view (H.264 or JPEG as demoed). Position a
   second camera, 60 fps or higher (phone slow-motion at 120/240 fps is ideal),
   so one frame holds both the stopwatch itself and the Quest eye-view display
   showing the stopwatch (mirror the Quest to a monitor next to the stopwatch, or film through the lens).
3. Film at least 60 s. Do not move the rover.

Reading samples: pause the recording on a frame where both digits are readable.
Latency = stopwatch reading (real) minus the reading shown inside the headset
view. Take 10 samples at least 3 s apart, discard none, sort, report the
**median** plus min and max. Note the video mode, link, resolution and fps. If
the headset view cannot be filmed, say so and use the desktop viewer instead;
label which one.

Record: `date, link, video mode, 10 samples (ms), median, min, max`.

## 3. Stop distance after deadman release (5 trials)

Marking: lay a tape measure on the floor along the drive line, zero at the
start. Put a mark on the rover (front axle or a tape flag) to read position.

Procedure, per trial:
1. Rover on the start mark, camera and range sensor clear, battery above half.
2. At the demo speed (state the PWM/`max_pwm` and the throttle used, as the
   judge will drive it), drive straight and hold steady speed for about 1 m.
3. Release the deadman. Film from the side (60 fps+) or mark where the front
   mark first stops moving.
4. Stop distance = rover position at rest minus position at release. Release
   is the frame the controller button/key lifts; rest is the first frame with
   no motion.

Report 5 trials, the median and the max (the max is the safety figure). Repeat
on the surface used for the demo. Do not shorten the stop lease to improve it.

## 4. Where the numbers go

Paste the markdown tables from step 1 and your section 2/3 tables into the
README "Measured numbers" table, keeping the "how measured" text, and replace
`pending rig run` with the value and the date. Keep the JSON reports with the
pitch evidence. Simulated rows stay labelled "simulated (laptop)".
