# Sustained physical capture evidence

Run the collector against the running rover or the Mac's operator relay:

```sh
python -m tools.qualify_capture --url http://127.0.0.1:8080 \
  --seconds 60 --interval 0.5 --timeout 2 --out /tmp/capture-evidence.json
```

For HTTPS, the system trust store is used by default; `--ca-file trusted-ca.pem`
adds a private CA. The URL must be an HTTP(S) origin without credentials, a path,
query or fragment. The collector sends only `GET /health`. It never opens control,
phone capture or video sockets and never changes a target service. HTTP redirects
are rejected. Configure the connection and capture application separately before
running it.

Exit zero means every collected sample met this capture evidence profile and
frame identities advanced. Exit one means the evidence failed; the complete
sample history and reasons remain in the JSON. A later recovery cannot erase a
network error, gate drop, stale frame, reset or interrupted run. `Ctrl-C` preserves
the partial report and fails the run.

A fresh view and physical capture eligibility are separate fields. A view needs
a healthy backend camera, a valid frame identity, and a conservative camera age
below the existing 500 ms limit. The receipt bound adds the full health-request
elapsed time to the backend age. It is deliberately conservative and is **not**
a camera-to-eye or glass-to-glass measurement.

Physical eligibility additionally requires explicit boolean backend
`camera_drive_ready: true` and `camera_capture_verified: true`, explicit
non-simulated/non-replay status, `PHONE RGB`, and the supported
`avcapture_sync_to_host` clock. Sensor age and sensor/host delta must be finite,
nonnegative and at most the existing native 150 ms sensor bound. The backend's
verified gate supplies the capture-permit binding; numerical plausibility alone
does not establish that binding. An `arkit_unverified` clock never passes,
regardless of its reported delta or a fresh LiDAR view. This profile does not
qualify UVC cameras lacking the phone capture-permit/clock evidence either.

Advances, repeated frame identities, and source/epoch/generation resets are
counted. Resets fail the sustained run. Repeated frames never renew their age:
the collector advances the previous receipt-age bound by its own monotonic
elapsed time. Brief repeated polls can remain fresh, but a run without advancing
frames fails. Choose an interval suitable for the source's frame cadence.

The report uses an explicit numeric/categorical whitelist. It omits session IDs,
permits, device/account names, URLs, positions, transforms, arbitrary error text,
raw health and image payloads. Unknown source/clock strings become categories.
Only sanitized frame numbers, optional epochs/generations, age/processing
metrics, clock/sensor diagnostics and boolean gates are retained.

Duration is bounded to 0.1–300 seconds, interval to 0.02–10 seconds, request timeout
to 0.1–5 seconds and retained sample count to 2,000. Requests have socket limits,
a header/body deadline watchdog, and a 256 KiB response cap; response sockets and
watchdogs are cleaned up. System name resolution remains subject to the host's
resolver. Sampling waits after each attempt instead of queuing catch-up requests;
request latency and actual elapsed time are recorded.

A passing report is observational capture evidence at that time. It does not
authorize motion or qualify Quest decoding/display, phone VPN/cellular routing,
operator input, the rover's motors or physical stop timing. Keep those hardware
checks separate; this tool cannot make an unqualified capture source safe by
observing it.

Tests use only local HTTP fixtures and the evidence evaluator:

```sh
python -m unittest tools.tests.test_qualify_capture -v
```
