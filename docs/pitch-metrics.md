# Pitch metrics: rig protocol

Numbers for the README, slides and Devpost (issue #12). Two are automatic
(`tools.pitch_metrics`), two need a camera and a tape measure. Record every
value with its conditions; a laptop-simulated number is never a rig number.

`python -m tools.pitch_metrics` is **read-only toward the rover**. It sends
only deadman-released zero commands, returns no permit, and never moves
motors. Close the Quest page first: control has one owner.

## 1. Automatic metrics (about 1 minute)

On the Jetson (detector fps and stream fps with no network in the way):

```sh
python -m tools.pitch_metrics --url https://127.0.0.1:8443 --ca-file /etc/farsight/tls/ca.pem \
  --seconds 30 --output pitch-jetson.json --markdown pitch-jetson.md
```

On the operator laptop, through the relay the Quest uses (control RTT and stream
fps as the demo sees them over the real link):

```sh
python -m tools.demo --upstream https://<jetson>:8443 --ca-file ~/farsight-ca.pem   # separate terminal
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
