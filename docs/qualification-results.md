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
