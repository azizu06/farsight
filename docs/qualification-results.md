# Software transport qualification

These measurements use localhost simulation on macOS 26.7 ARM64 with Python
3.11. No camera, headset, phone, Jetson, or physical rover was connected to this
test fixture. The source generates a labelled moving RGB pattern at a nominal
15 fps, timestamps it before JPEG encoding, and publishes through the production
CameraService. The alternative `map5` source uses the existing room simulation
and runs RGB-D fusion at roughly 5 fps. The original measurements below coupled
that camera to fusion; the issue #52 section records the independent 15 Hz
synthetic camera now used with the same 5 Hz fusion budget.

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

## Camera/fusion coupling (issue #52, October 10)

On the same Apple M5 Pro / macOS 27.0.1 host, now using Python 3.13.7,
the original map5 path reproduced 0% and 5.4% settled motion in two 20-second
quiet trials. The model, 100 ms RTT / shared 2 Mbps link, actual FFmpeg encoder,
two-fragment window, 20 Hz commands/permits, 250 ms lease and 500 ms view guard
match #41. These remain software-only observations, with no physical device.

The demonstrated cause is sparse camera publication coupled to fusion, followed
by an unconditional 160 ms sleep. Fusion was already off the event loop, and
camera/control readers already avoided its lock. Two instrumented baselines
measured camera snapshot p95 below 0.020 ms, control lock hold p95 0.032–0.040 ms,
fusion lock hold p95 15.2–29.7 ms, and fusion executor queue p95 below 0.5 ms.
Pausing fusion reduced processing p95 to 0.4 ms but still produced 0% motion:
distinct captures observed at encoder submission were 186 ms apart at p95 and
held video reached 532 ms. Thus moving fusion to another thread or shortening
the safety timeouts would not address the minimized failure.

`jetson.mapping.demo.stream_demo` now publishes independently at nominal 15 Hz
and admits at most one fusion job, capped at 5 Hz. Busy jobs drop candidates;
the next admitted job uses a newly captured frame. Shutdown drains the current
fusion job. Camera receipt time includes synthetic ray-casting work, and the
source stays explicitly `SIMULATED`. Only the `--demo` producer changed;
physical RGB/RGB-D ingestion, fusion kernels, control policy and wire schemas
did not change. This is not a live-camera throughput improvement claim.

Each row below is a separate 20-second trial. Before uses the original #41
profiler at `4bfb044`; after includes the new lock/CPU/age instrumentation.
The artifact also retains two failing instrumented baselines, the paused-fusion
probe, and all four intermediate 10 Hz camera trials (one combined failure).

| Build | Load | Repeat | Arrival p95 | Held view peak | Server lag max | Settled moving |
|---|---|---:|---:|---:|---:|---:|
| Before | quiet | 1 | 424.6 ms | 559.5 ms | 22.3 ms | 0.0% |
| Before | quiet | 2 | 414.3 ms | 567.2 ms | 24.9 ms | 5.4% |
| After | quiet | 1 | 329.7 ms | 452.3 ms | 21.4 ms | 100.0% |
| After | combined | 1 | 502.5 ms | 1124.2 ms | 40.6 ms | 7.0% |
| After | quiet | 2 | 321.4 ms | 435.6 ms | 44.2 ms | 100.0% |
| After | combined | 2 | 336.4 ms | 497.4 ms | 37.1 ms | 100.0% |
| After rebase | quiet | 1 | 314.9 ms | 451.2 ms | 22.3 ms | 100.0% |
| After rebase | quiet | 2 | 322.2 ms | 432.1 ms | 26.1 ms | 100.0% |

The first two fixed quiet runs fused 106/106 frames; the rebased runs fused 105/106, so the improvement did not disable
map work. Quiet process CPU p95 increased from 72.9–73.2% to 95.1–95.9% as the
synthetic camera generated more frames. The combined failure recorded a 126.8 ms
client loop pause and a 157.7 ms command gap; the backend lease expired and
correctly latched a stop. Source/encoder work also slowed in that run. The
retained trace does not establish the underlying scheduler/GIL cause. The
passing quiet runs therefore resolve the repeatable camera/fusion coupling,
but **do not qualify arbitrary combined load or the physical link**.

A separate intrusive six-second probe uses thread-local Python profiling with
`time.thread_time()`. Fusion p95 was 19.0 ms in Python, 6.9 ms in native calls,
31.8 ms total thread CPU and 45.8 ms wall time. Python CPU is a GIL-demand proxy;
native calls may hold or release the GIL, and wall-minus-CPU includes scheduling
and other waits. This does not directly measure GIL ownership/wait time or prove
GIL contention caused the residual combined failure. Profiling callback overhead
is excluded from the Python/native buckets, and this intrusive run is separate
from the motion comparison. A cProfile/thread-clock prototype failed a CPU
consistency check and was discarded.

The public camera/control regression was observed failing before the fix
(camera frame ID stayed at 0 while the real fusion lock was held) and passing
afterward. A separate producer regression verifies one outstanding fusion job
and shutdown draining. The CPU probe test verifies simultaneous worker probes
stay within their own thread CPU budgets. Added tests total 97 lines across
three files, with shared existing control helpers and no repeated setup blocks.

Repeated results, all stage p95/max/counts, source labels, workload validation,
burst outcomes and the combined failure's permit trace are in
[issue52-backend-profile.json](qualification/issue52-backend-profile.json).
Run one profiler at a time, with the existing ignored pose model:

```sh
python -m tools.profile_backend --source map5 --detector pose --model models/yolo11n-pose.onnx --duration 20 --repeats 2 --loads quiet combined --report map5.json
# Diagnosis-only, not a sustained-motion qualification:
python -m tools.profile_backend --source map5 --detector pose --model models/yolo11n-pose.onnx --duration 6 --repeats 1 --loads quiet --fusion-python-profile --report fusion-cpu.json
```

To reproduce the original baseline, run the quiet-only command from #41 at
`4bfb044`; apply only the profiler diff there for the additional lock/CPU probes.
`--pause-fusion` is a fixture-only diagnostic switch. No profiler routes or
instrumentation are installed in the production app.

After integrating main through `5b3f087`, all required suites passed:
`pytest jetson/control/tests jetson/mapping/tests jetson/tests tools/tests -q`
(380 tests, 94 subtests, 5 skipped), `npm --prefix quest test` (80 tests) and
`npm --prefix quest run build`. The final packaging-only main change passed its
13 tests and 10 subtests separately; the camera/control runtime did not change.
The two additional 20-second quiet map5 repeats above both sustained 100% motion.

## Client-pause follow-up (issue #52, October 10)

The retained 126.8 ms **client loop pause was measured in the Python fixture
client**, not the operator browser. `tools.profile_backend` calls
`tools.tests.test_demo_qualification.motion_trial`; its `Controller` monitors
an asyncio loop, and `VideoClient` receives MP4 fragments without decoding or
painting them. That trace cannot identify browser JSON parsing, GC, geometry
decoding, texture uploads or H.264 display as the cause.

Two new 20-second combined-load repeats on the same host, at `cd0e4dc`, did not
reproduce the lease stop. Both used map5 fusion, real CPU pose inference and
FFmpeg, scan-kernel/local-summary bursts, 50 ms each-way delay and shared
2 Mbps bandwidth. No runtime code changed between these repeats and the
previous failed combined run. These are additional baselines, not before/after
evidence for a fix.

| Repeat | Settled moving | Client loop lag max | Command gap max | Server loop lag max | Arrival p95 | Held view peak |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 100% | 38.3 ms | 56.0 ms | 40.3 ms | 345.0 ms | 488.8 ms |
| 2 | 100% | below 5 ms monitor threshold | 52.4 ms | 36.1 ms | 359.6 ms | 486.7 ms |

Both workloads were valid, fused frames and ran pose successfully. Neither
recorded a backend lease failure or client invalidation. The raw repeated
results are retained in [issue52-client-baseline.json](qualification/issue52-client-baseline.json).
Reproduce one profiler at a time with the existing ignored pose model:

```sh
python -m tools.profile_backend --source map5 --detector pose --model models/yolo11n-pose.onnx --duration 20 --repeats 2 --loads combined --report baseline.json
```

A separate exploratory run used the production viewer in headless Chromium
156.0.8078.4 with the Chromium/SwiftShader launch flags from
`quest/tests/xr-input.browser.mjs`, against an isolated `Rig` with the same
combined backend workload. Instrumentation installed before navigation measured
WebSocket send/status timestamps, a 10 ms timer and the Long Tasks API. After
enabling actual H.264 playback, it attempted Space+W driving for 10 seconds,
then released input and selected the map for five seconds.

During the camera phase, control-send gaps were p95 71.2 ms / max 101.0 ms,
status-arrival gaps were p95 74.6 ms / max 96.4 ms, and timer lag peaked at
62.8 ms. There were no tasks of at least 50 ms in that phase. Startup separately
recorded one unattributed 119 ms long task. Backend status-publication gaps
peaked at 95.3 ms; ASGI control sends took p95 0.057 ms / max 0.762 ms. These
observations do not isolate the source of shorter scheduling delays.

**The browser run did not qualify motion:** it sent zero active commands,
polled zero moving samples, and decoded-view age exceeded 500 ms (camera-phase
peak 859 ms). The release latch remained in force. Camera view deliberately
pauses map transfers; no binary map snapshot completed during the subsequent
five-second map phase, so this run also does not cover point/mesh decoding or
map uploads. Its procedure, phase summaries, backend timings and successful
burst results are retained in
[issue52-client-browser.json](qualification/issue52-client-browser.json).

No demonstrated browser cause emerged from this short probe, so no production
change follows. The original combined failure remains retained and unexplained;
two passing Python repeats do not eliminate intermittent scheduling stalls or
qualify browser driving. The 250 ms lease, 500 ms view guard, 20 Hz permits and
commands, two-fragment window, release latches and wire schemas are unchanged.
All observations are software-only: no Quest, camera, phone, Jetson inference
or physical motors were tested.
