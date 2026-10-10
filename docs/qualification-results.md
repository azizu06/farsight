# Software transport qualification

These measurements use localhost simulation on macOS 26.7 ARM64 with Python
3.11. No camera, headset, phone, Jetson, or physical rover was connected to this
test fixture. The source generates a labelled moving RGB pattern at a nominal
15 fps, timestamps it before JPEG encoding, and publishes through the production
CameraService. The alternative `map5` source uses the existing room simulation
and runs RGB-D fusion at roughly 5 fps.

The fixture uses the real FFmpeg H.264 encoder, application WebSockets, operator
relay, control policy, and simulated serial pump. The fault proxy applies 50 ms
in each direction and a shared 2 Mbps cap. Video remains 640x480, 10 fps,
500 kbps, with exactly two requested fragments in flight. All view guards remain
500 ms; the Jetson control permit remains 250 ms. Tests never open physical motor
hardware.

## Reproduce

Run with other performance tests stopped:

```sh
python -m tools.tests.test_demo_qualification --report /tmp/farsight-qualification.json
python -m unittest tools.tests.test_demo_qualification -v
```

To opt into the sustained-motion timing assertion on a quiet host:

```sh
FARSIGHT_TIMING_GATES=1 python -m unittest tools.tests.test_demo_qualification.QualificationTests.test_rgb_capture_and_fixed_permits_sustain_simulated_motion -v
```

The runner reserves fresh localhost ports and starts its own server, fault proxy,
and relay. It leaves existing demo servers alone. The JSON report is saved after
each completed scenario so failed qualification is retained. The test-only
encoder-kill routes exist only in this fixture and require simulated motors and
a localhost caller.

Timing integration tests disable asyncio's per-task debug traceback recording
to match the production event loop. They retain the same control and view guards
and require active motion immediately before injecting a failure.

## Findings that changed the implementation

1. The fault proxy's accepted TCP sockets had `TCP_NODELAY` disabled on this Mac.
   Their socket protocol was zero, so asyncio skipped its automatic setup. The
   proxy now explicitly sets `TCP_NODELAY=1` on both sockets. Darwin reports the
   enabled internal flag as `4`; both observed sockets are enabled after the fix.
   Earlier results included this unintended Nagle behavior.
2. One-second encoder VBV buffering allowed 26–35 kB keyframe bursts. Those
   fragments correlated directly with 198–208 ms arrival gaps at 2 Mbps. Although
   average video used about 500 kbps, the gap aged the previous frame past the
   500 ms guard. Increasing the request window from two to three did not fix this.
3. Reducing VBV to 100 kbits at 500 kbps capped measured fragments at roughly
   12.5 kB. In a subsequent five-second RGB trial, arrival age had p95 284 ms and
   maximum 301 ms; the held view age peaked at 411 ms with no stale-view samples.
   Fragment cadence p95 was 137 ms. These are capture-receipt bounds at software
   arrival, before display decoding.
4. The same trial exposed a control lease race. At 10 Hz permit publication and
   20 Hz commands, an old valid permit can leave only 50 ms before expiry. A
   subsequent valid command arrived 2.9 ms after the previous lease expired,
   correctly requiring a release. Video was still fresh. This is an ordinary
   timing margin problem; increasing the lease would conceal it. Continuous
   motion was not qualified by that run.

The server now publishes permits on a fixed 20 Hz schedule, subtracting send and
recording overhead from its sleep. Commands and the serial pump remain 20 Hz.
The 250 ms permit and 500 ms view guards are unchanged.

## Final software rehearsal

After both fixes, the eight-second RGB trial maintained motion for every sampled
instant after its initial one-second settling window: 190 of 192 samples moved
overall, with no stale view samples, invalidations, or connection errors.

| Scenario | Arrival age p95 | Held view peak | Result |
|---|---:|---:|---|
| Synthetic RGB, nominal 15 fps | 285 ms | 416 ms | Continuous simulated motion after initial network transit |
| Room/map simulation, roughly 5 fps | 488 ms | 711 ms | View guard stopped motion; unsuitable for continuous driving under this profile |

RGB command cadence p95 was 51 ms, control acknowledgement p95 156 ms, and
fragment arrival cadence p95 138 ms. Each fragment contained one 100 ms sample;
maximum encoded payload was 12,497 bytes. These are software observations on the
Mac, with other preview/simulation processes present, not Jetson measurements.
The stop-only qualifier's conservative default 200 ms acknowledgement threshold
still failed its separate run (p95 205 ms); that failure is retained in the JSON
report rather than replaced with the motion trial's stronger cadence evidence.

A later timing test under concurrent preview/background activity failed: held
view age peaked at 610 ms, command cadence p95 rose to 58 ms, and the view guard
correctly latched a stop. That failed run remains part of the result. Sustained
timing is an explicit benchmark, not an unconditional CI assertion on an
uncontrolled host. The functional test claim is three passing fault/recovery
tests; the passing eight-second run does not generally qualify the link.

All fault trials verified that motion was active immediately before injection:

| Injected failure | Observed simulated stop | Recovery check |
|---|---:|---|
| Actual FFmpeg process killed | 169 ms | Encoder PID changed, fresh MP4 initialization arrived, original held deadman stayed stopped, release/new press restored motion |
| Video consumer paused | 333 ms | Capture-age guard disabled motion while the control channel remained connected |
| Every fault-proxy connection aborted | 43 ms | Controller disconnected and simulated PWM returned to zero |

These stopwatch values include fault detection and polling. They do not replace
independent physical Jetson lease and Uno watchdog timing. The slow map source
remains an explicit failed continuous-drive case; a real 15 fps camera source
must be validated separately.

## Residual lease-gap investigation

The final quiet eight-second run after the original handoff still had a late
lease stop: 98.18% of settled motion samples moved, with zero stale-view samples,
arrival age p95 318 ms, and held view peak 443 ms. A subsequent valid command
arrived after the old permit's deadline, so the server correctly required a new
release. This was not evidence that the link had become generally qualified.

The fixture client now supports three experimental policies without changing
production code: periodic 20 Hz, periodic 40 Hz, and periodic 20 Hz plus an
immediate serialized command when a new status/permit arrives. Every policy
keeps the 250 ms expiry, 500 ms view guard, two-fragment video window, and release
latch. The extra send uses the current input and current permit, with a bounded
send deadline; it does not queue old commands or rearm a held deadman.

Reproduce the repeated comparison:

```sh
python -m tools.tests.test_demo_qualification --compare-cadence --duration 20 --repeats 2 --policies periodic20 periodic20_plus_status --report /tmp/farsight-cadence.json
```

Four twenty-second repeats on the same 100 ms RTT / shared 2 Mbps profile:

| Fixture policy | Repeat | Settled moving samples | First cause / observed scheduling |
|---|---:|---:|---|
| Periodic 20 Hz | 1 | 45.6% | Lease expired; server loop lag up to 49 ms and client loop lag up to 62 ms |
| 20 Hz plus status-triggered send | 1 | 100% | No invalidation; server lag maximum 5 ms, client monitor recorded no lag above 5 ms |
| Periodic 20 Hz | 2 | 48.2% | Lease expired during a 360 ms server pause and 236 ms client pause |
| 20 Hz plus status-triggered send | 2 | 95.3% | View guard stopped after a 113 ms client pause; no first backend lease failure |

The first periodic failure's last accepted command carried a 170.6 ms old permit,
leaving 79.4 ms before expiry. The next command reached the server 162 ms later.
Near that failure, permit publication gaps reached 98 ms, received-status gaps
reached 166 ms, and client command gaps reached 94 ms. The trace retained the
actual issued/expiry times, accepted and rejected commands, and client send
context around the first failure rather than relying only on final fractions.

ASGI control WebSocket send duration p95 was about 0.05–0.07 ms. No mission
recording callback was configured in this fixture, so these failures cannot be
attributed to recording overhead. Measured event-loop scheduling pauses explain
the publication and command gaps. An earlier 40 Hz trial also stopped during a
139 ms command gap / 158 ms status gap, so increased periodic traffic alone did
not prevent a process stall.

The policies experienced different uncontrolled host pauses. Their fractions
therefore do not prove that the status-triggered policy is superior, and no
production policy change follows from this comparison. A controlled paired
delay/stall experiment or the actual hardware rehearsal is needed to isolate any
benefit. Four fixture-policy unit tests verify sequence serialization, newest
permit use, cadence validation, and unchanged stale-view/release behavior.

The report's fault trials require motion to be active immediately before fault
injection. A trial that has already stopped cannot claim a measured fault-stop
latency. Recovery checks require a changed real FFmpeg PID, newly received MP4
initialization atoms, stopped motion while the original deadman remains held,
and an explicit release and new press before movement returns.

## Remaining hardware gates

Software arrival age is not camera-to-headset display age. These measurements
exclude real sensor exposure/ISP latency and do not verify Quest decoding, WebXR,
USB tethering, cellular radio conditions, Jetson CPU/GPU performance, or physical
Uno watchdog timing. Run the hardware checklist in [transport-setup.md](transport-setup.md)
and the 15-minute battery/failure rehearsal before calling the demo qualified.

## Backend load profile (issue #41, October 10)

New measurements on Apple M5 Pro, macOS 27.0.1 ARM64, Python 3.13.5, OpenCV 4.14.0, ONNX Runtime 1.31.0 and FFmpeg 9.0.2. These are software-only observations on a shared host; unrelated host pauses were not controlled. Read-only `tools.preflight --role operator` identified this host as non-Jetson. No camera, Quest, phone or physical motors were opened by these trials.

The RGB source repeats the same moving 640x480 bus-photo scene at 15 fps, labelled `SIMULATED RGB 15 FPS`, with capture timestamps before JPEG encoding. Pose uses the official YOLO11n-pose weights exported with `tools.export_detector` (Ultralytics 8.4.175, opset 12, input `[1,3,640,640]`, output `[1,56,8400]`). Model SHA-256: `b4d3abde2f7233a9f4f869e8ff1048263ef6ed28957bad1382504ade24bd6299`. Scene SHA-256: `c02019c4979c191eb739ddd944445ef408dad5679acab6fd520ef9d434bfbc63`.

Every trial uses the real production FFmpeg encoder, 10 fps / 500 kbps / 200 ms VBV, two requested fragments, fixed 20 Hz permits and commands, the 250 ms lease and 500 ms view guard, plus 50 ms each-way delay and shared 2 Mbps bandwidth. Combined load adds one bounded five-view scan-kernel burst and one `/ask` request every two seconds. Scan bursts run production synthetic-frame/back-projection/voxel integration kernels; they do not exercise scan control admission or a depth model. Assistant requests use the real local observation summary, with no VLM/STT/TTS model. Thus this does not qualify full assistant-model contention.

First, the requested cadence comparison ran both policies for 20 seconds twice. All four trials sustained 100% settled simulated motion; arrival p95 was 281–294 ms, with no recorded server lag above the fixture's 5 ms threshold. Production cadence was left at 20 Hz.

With pose enabled, the second quiet baseline reproduced a stop: 229.6 ms server lag, 84.8 ms client lag, capture-receipt p95 641 ms, and 43.0% settled moving samples. The last accepted command used a 248.5 ms old permit, leaving about 1.5 ms before expiry. The lease and view guards correctly stopped motion. The trace also measured inference queue wait up to 110.9 ms and detector work up to 466.3 ms.

The only runtime change bounds the CPU pose session to two intra-op workers. [ONNX Runtime documents](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) that its default pool uses physical cores and enables worker spinning. The paired measurements below establish lower process CPU use and fewer threads, with inference still within the 100 ms target period in the final trials. They do not establish that this change eliminates unrelated process stalls. OpenCV, FFmpeg, camera/control/relay code, capture identity, queues and safety settings were unchanged.

Each row is a separate 20-second trial. CPU is process p95 sampled every 100 ms (100% = one core); threads are process p95, including native pools and executor workers. Inference is ONNX `run` wall time. H.264 latency includes raw input submission through complete fragment publication, including mux/pipe delay; it is not pure codec execution time.

| Build | Load | Repeat | CPU p95 | Threads p95 | ONNX p95 | Server lag max | Arrival age p95 | Settled moving |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Before | quiet | 1 | 244.0% | 30 | 20.1 ms | 22.9 ms | 313.2 ms | 100.0% |
| Before | combined | 1 | 245.5% | 31 | 26.5 ms | 35.2 ms | 325.9 ms | 100.0% |
| Before | quiet | 2 | 270.1% | 30 | 253.7 ms | 229.6 ms | 641.0 ms | 43.0% |
| Before | combined | 2 | 240.4% | 31 | 24.1 ms | 33.3 ms | 314.8 ms | 100.0% |
| After | quiet | 1 | 85.7% | 25 | 22.6 ms | 21.9 ms | 311.2 ms | 100.0% |
| After | combined | 1 | 132.3% | 25 | 26.8 ms | 31.5 ms | 339.3 ms | 100.0% |
| After | quiet | 2 | 63.0% | 26 | 19.2 ms | 22.1 ms | 308.6 ms | 100.0% |
| After | combined | 2 | 129.1% | 26 | 23.2 ms | 31.0 ms | 317.0 ms | 100.0% |

The intermediate experiment explicitly overriding two ONNX workers also sustained motion in all four quiet/combined repeats. Quiet CPU p95 was 84.5–88.4%; combined CPU p95 was 132.0–133.7%. Final production trials omit the override. The non-stalled before/after rows show a CPU reduction of roughly 46–74%, while typical scheduling lag changes little. Retain the original failed baseline rather than attributing its entire improvement to the pool limit.

| Final source / detector | Load | Repeat | Detector work p95 | JPEG encode p95 | H.264 input-to-fragment p95 | Server lag max | Settled moving |
|---|---|---:|---:|---:|---:|---:|---:|
| RGB / pose | quiet | 1 | 28.4 ms | 0.8 ms | 118.8 ms | 21.9 ms | 100.0% |
| RGB / pose | combined | 1 | 35.4 ms | 0.8 ms | 126.8 ms | 31.5 ms | 100.0% |
| RGB / pose | quiet | 2 | 24.0 ms | 0.7 ms | 118.4 ms | 22.1 ms | 100.0% |
| RGB / pose | combined | 2 | 29.7 ms | 0.8 ms | 119.9 ms | 31.0 ms | 100.0% |
| RGB / HOG | quiet | 1 | 17.3 ms | 1.2 ms | 126.9 ms | 30.7 ms | 100.0% |
| RGB / HOG | combined | 1 | 5.5 ms | 1.3 ms | 127.3 ms | 34.0 ms | 100.0% |
| RGB / HOG | quiet | 2 | 4.5 ms | 1.2 ms | 126.7 ms | 38.3 ms | 100.0% |
| RGB / HOG | combined | 2 | 20.7 ms | 1.3 ms | 127.6 ms | 32.4 ms | 100.0% |
| Room map / pose | quiet | 1 | 28.7 ms | inside map processing | 118.6 ms | 29.8 ms | 0.0% |
| Room map / pose | quiet | 2 | 25.2 ms | inside map processing | 119.9 ms | 21.5 ms | 1.6% |

Detailed stage queue/work times, CPU/thread samples, permit/command timing, first failure and all repeated summaries are retained in [the measurement artifact](qualification/issue41-backend-profile.json). Raw per-trial profiler reports also retain the qualification traces. HOG is a comparison workload, not a pose replacement. The slower room/map source remains a separate freshness-limited case and must not be advertised as continuous-driving qualified.

### Reproduce the load profile

Install the optional profiling dependency (`python -m pip install psutil`), plus ONNX Runtime for the CPU pose measurements. Prepare the model with the existing exporter; model files remain ignored artifacts. The bus scene is from [Ultralytics](https://raw.githubusercontent.com/ultralytics/ultralytics/main/ultralytics/assets/bus.jpg). Run one profiler at a time:

```sh
python -m tools.export_detector --weights yolo11n-pose.pt
curl -L --fail https://raw.githubusercontent.com/ultralytics/ultralytics/main/ultralytics/assets/bus.jpg -o models/profile-bus.jpg
# Reproduce the old unrestricted CPU pool on current code:
python -m tools.profile_backend --detector pose --model models/yolo11n-pose.onnx --scene models/profile-bus.jpg --ort-threads 0 --duration 20 --repeats 2 --report before.json
# Measure the production two-worker CPU pool:
python -m tools.profile_backend --detector pose --model models/yolo11n-pose.onnx --scene models/profile-bus.jpg --duration 20 --repeats 2 --report after.json
python -m tools.profile_backend --detector hog --scene models/profile-bus.jpg --duration 20 --repeats 2 --report hog.json
# Production create_app(demo=True, simulate_rover=True), equivalent source flags:
python -m tools.profile_backend --source map5 --detector pose --model models/yolo11n-pose.onnx --duration 20 --repeats 2 --loads quiet --report map.json
```

The tool starts isolated localhost ports, uses simulated motors only, bounds stage samples to 4096 and encoder sample bookkeeping to 600, rejects overlapping scan-kernel bursts, and closes its own processes. Profiling routes are fixture-only. Executor timing separates queue wait from worker execution; repeated frame identities retain their original capture timestamps. An invalid detector or missing combined burst is a failed workload, not a passing qualification.

Full VLM/audio models, real camera capture, Jetson CUDA/TensorRT, Quest decode/display, physical scan behavior and motor stop timing remain unverified. Keep #12 and the physical rig gates open.

Validation after rebasing onto current main: `pytest jetson/control/tests jetson/mapping/tests jetson/tests tools/tests -q` passed 359 tests and 84 subtests (5 skipped). The actual FFmpeg recovery/freeze/outage and freshness/release regressions passed. Quest tests passed 49/49 and its production build passed. A separate real-ONNX comparison on four shifted bus scenes found four people in each and matching boxes/keypoints (rtol 1e-5, atol 1e-3), confidence (absolute error <1e-5) and down flags with unrestricted versus two-worker sessions. The CPU-budget regression was observed failing before the runtime change and passing afterward.
