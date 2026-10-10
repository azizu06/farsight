# Rover control service

The server defaults to **no motor backend**. A real backend requires an explicit
serial port; simulation requires an explicit flag and reports `simulated: true`.
There is no stock firmware fallback: flash the fail-stopped sketch in `rover/`.

```python
from jetson.control import ControlService

control = ControlService(serial_port="/dev/ttyACM0", max_pwm=100,
                         min_pwm=0,
                         camera_ready=lambda: camera.status()["camera"])
app.include_router(control.router)
# In the app lifespan: control.start(); ...; control.close()
# Add control.status() to app health reporting.
```

Install `jetson/control/requirements.txt` along with the server requirements.
Opening USB may reboot the Uno; the service waits two seconds then confirms an
`OK` before exposing the rover as connected. Every serial reconnect invalidates
all previous permits and requires a new deadman release. A silent/disconnected
Uno removes connection readiness. No command is stored for reconnection.

One controller owns `/ws/control`; a second is closed with code 1008. The server
sends a `control_status` object every 100 ms including an opaque `session_id`,
`permit`, `permit_ms: 250`, `seq`, `range_cm`, `estop`, `rover`, `simulated`,
`requires_release`, `reason`, motor PWM, and `max_pwm`. Public status is also at
`GET /control/status`, without its session ID or a usable permit.

After connecting, send a release with the current session ID and increasing
integer sequence number:

```json
{"session_id":"from status","seq":1,"deadman":false}
```

Then send at 20 Hz while the operator holds the deadman and sees a fresh frame:

```json
{"session_id":"from status","permit":"from latest status","seq":2,
 "deadman":true,"v":0.3,"w":0,"pan":0,"mode":"teleop",
 "video_fresh":true,"video_age_ms":50}
```

`v,w` are -1..1; `pan` is -80..80 relative to forward. Positive `w` turns right.
Only teleop is supported; seek proposals never move motors. A command's lease
expires at most 250 ms after **server permit issue**, regardless of when the
command arrives. Delayed packets cannot restore stale movement. Invalid,
expired, out-of-order, wrong-session or stale-video commands stop motion and
require a deadman release. A release stops even if its sequence is stale, but
only an ordered release allows another press. Release again after any
`requires_release: true` fault. Sequence values must fit a JS safe integer.

The serial pump runs on a separate thread at 20 Hz and recomputes output from
monotonic time before each write. HTTP/inference work cannot extend its lease.
The server supplies a camera freshness callback (capture age at most 500 ms);
its failure stops **all** motion even if a client claims it sees fresh video.
Camera recovery still requires a new deadman release before motion resumes.
Uncertain or stale (>350 ms) range data and a pan head outside ±10 degrees
block forward translation; turns and reverse remain usable. Motion defaults
to maximum PWM 100 for a small demo area. This does not replace hardware tests
or a person ready to power off the rover.

An optional `min_pwm` motor floor defaults to zero. After mixing and normalizing
the left/right wheel commands, each nonzero wheel maps to
`sign * (min_pwm + (max_pwm - min_pwm) * abs(value))`; an exact zero stays zero.
This can overcome motor stiction, but a nonzero floor changes the response near
zero and also affects turns and reverse. Bounds must satisfy
`0 <= min_pwm < max_pwm <= 255`. Calibrate the real rover as described in
`rover/README.md`; no minimum is assumed to work on your motor/battery setup.

Run deterministic policy and WebSocket integration tests with:

```sh
python3 -m unittest discover -s jetson/control/tests -v
```
