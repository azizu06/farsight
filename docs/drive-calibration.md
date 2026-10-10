# Drive calibration

The rover has no wheel encoders, so Nav2's `cmd_vel` (m/s, rad/s) means nothing
until we measure what each PWM level actually does. `tools/calibrate_drive.py`
drives short patterns through the normal control path, reads the motion from the
ARKit pose, and writes a profile that `jetson.server` and the ROS 2 bridge use.

Status: the fitting math, profile round-trip, control modes and the full wire path
are tested against a **simulated** rover (`tools/tests/test_calibrate_drive.py`,
`jetson/control/tests/test_drive_profile.py`). No real-rig run has been made;
every number below is a placeholder until you run it on the floor.

## Wheels-up bench results (real rig, 2026-10-10)

ELEGOO V4 chassis propped on a box, wheels off the ground, rover battery ON, Uno
on the Jetson at `/dev/ttyUSB0` running the sensorless `farsight_uno`. Commands
were sent with `rover/wheel_bench.py`; the captain watched from behind the car.

| Check | Command (UTC) | Observed |
|---|---|---|
| Left forward | `M 80 0`, 3 s (18:00:42) | left wheels forward |
| Right forward | `M 0 80`, 3 s (18:01:25) | right wheels forward, front and back together |
| Left reverse | `M -80 0`, 3 s (18:02:10) | left wheels backward |
| Right reverse | `M 0 -80`, 3 s (18:02:18) | right wheels backward |
| Starting PWM | both sides forward, 2 s pulses at 30, 40, 50, 60, 70, 80 with 4 s stops (18:02:56-18:03:28) | captain chose **min PWM 70** (pulse 5) to leave margin for the Jetson, phone and power bank |
| Full speed | `M 255 255` 2 s (18:05:34), 4 s stop, `M -255 -255` 2 s (18:05:40) | 40/40 commands answered `OK`, no Uno reset, no USB disconnect in `dmesg` |

Motor polarity needs no lead swap or firmware change. Chosen limits:
`--min-pwm 70 --max-pwm 255` (captain's call for a fast demo). This is
lifted-wheel evidence only. Loaded floor starting PWM, speed, stopping distance
and the per-side profile below still need the floor run. The same min applies
to both sides because the pulses drove both sides together.

## What is measured

| Quantity | How |
|---|---|
| Minimum moving PWM, **per side** | Drive one wheel alone, 15..max in steps of 5 (0.8 s each); first level that visibly moves or turns the rover |
| PWM -> m/s | Straight runs at 4 levels (1.1 x higher floor .. max) x2; slope of along-track position after a 0.6 s settle |
| PWM -> rad/s and half-track | In-place spins at the same levels, CCW and CW; slope of unwrapped yaw. `half_track = wheel speed(pwm) / spin rate`, so skid slip is included |
| Left/right trim | Yaw drift in the straight runs: speed gains `trim_left`, `trim_right` (mean 1) so equal commanded speed goes straight |
| Camera mount yaw | Angle from the camera forward axis to the chord of travel in each straight run (+ counter-clockwise from above) |
| Reverse/forward ratio | Reverse return legs; stored in `meta`, not used by the mapper |

Sign convention is ROS: +v forward, +w counter-clockwise (left turn). The legacy
normalized path (`w` positive turns right) is unchanged.

## Run it (rig day)

Wheels down, >= 3 m of clear floor, a person at the power switch, phone streaming
with ARKit `tracking: normal`.

```sh
python -m jetson.server --serial-port /dev/ttyACM0 --max-pwm 120 --allow-calibration   # Jetson
python -m tools.calibrate_drive --url https://JETSON:8443 --insecure --out drive-profile.json   # Mac or Jetson
python -m tools.calibrate_drive --dry-run        # print the pattern without contacting anything
python -m tools.calibrate_drive --simulate       # fit against a virtual rover, no hardware
```

- **Deadman**: by default the tool drives only while you **hold the space bar**
  (terminal key-repeat; ~0.9 s grace for the first repeat, ~0.35 s after). Release
  sends an explicit deadman release; the sequence pauses and repeats that pattern
  when you hold again. `--auto-permit` instead holds the permit for the whole
  sequence; use it only with the floor cleared and a hand on the switch.
- `--allow-calibration` is required on the server and is off by default. Calibrate
  mode keeps the permit, held deadman, fresh camera and lease. With the proximity
  stop on, it also keeps the forward range stop; the rig as built has no ultrasonic
  and runs `--proximity-stop off`.
- Under `farsight.service`, set `FARSIGHT_ALLOW_CALIBRATION=1` in `/etc/farsight.env`
  for the run, then blank it and set `FARSIGHT_DRIVE_PROFILE=/path/drive-profile.json`.
  Both take effect on a service restart.
- **Camera prerequisite.** With `--serial-port`, every motion heartbeat needs
  `camera_drive_ready`: a USB camera on the Jetson, or the phone's capture-bound RGB
  stream. The tool also needs the ARKit pose, which only the phone's LiDAR stream
  (`/phone`) provides. Those LiDAR frames are view-only by default; set
  `FARSIGHT_LIDAR_DRIVE=1` (`--lidar-drive`) so fresh LiDAR frames with normal
  ARKit tracking count as `camera_drive_ready`, then calibrate on the LiDAR stream.
- The test ceiling is the server's `--max-pwm` (or lower `--max-pwm` on the tool).
  The measured curves only cover up to that ceiling.
- Pose time is `now - age_ms` of a polled `/health` (about 20 Hz, 50 ms jitter);
  runs are seconds long so rates are insensitive to it. The tool aborts a pattern
  if tracking is not `normal`, the server stops the rover for any reason, or the
  deadman drops.
- Outputs: `drive-profile.json` and `drive-profile.runs.json` (raw per-run
  numbers; keep it as evidence for the pitch/Devpost with the date and battery
  state). Re-run after changing battery, tyres, floor, or payload.

## Use the profile

```sh
python -m jetson.server --serial-port /dev/ttyACM0 --max-pwm 120 --drive-profile drive-profile.json
```

Without `--drive-profile` nothing changes: no profile is read, `velocity` is
refused with `unsupported_mode`, teleop/Explore/Seek PWM mixing is identical.
With it, a held-deadman heartbeat with `mode: "velocity"`, `v_mps`, `w_radps`
(see [interfaces](interfaces.md#optional-drive-profile-modes-velocity-calibrate))
is mapped as `wheel speed = v -/+ half_track * w` (left/right), divided by the
trim gain, inverted through the measured curve, raised to the side's floor, and
capped at `--max-pwm`. Over-limit requests are scaled down together, keeping
curvature. Speeds under 10% of the slowest tested speed stop that wheel. When
the two wheels turn in opposite directions (in-place turn, very tight arc), each
side gets at least the lowest PWM of the spin table (`angular.pwm[0]`). Skid-steer
scrub stalls turns at the straight-line floors: on the 2026-10-10 floor run,
0.82 rad/s scans mapped to L-75/R+60 and stalled under payload.

The ROS 2 bridge should send Nav2 `cmd_vel` as `velocity` heartbeats (same
session/permit/deadman as any controller) and read the same JSON for
`camera_yaw_offset_rad` (the `base_link`->`camera_link` yaw), `limits.max_linear_mps`
and `limits.max_angular_radps` (Nav2 `max_vel_x` / `max_vel_theta`),
and `half_track_m`. `jetson/drive_profile.py` (`DriveProfile.load`, `wheel_pwm`) is
stdlib-only and can be imported directly.

## Profile file

`version: 1`; `max_pwm`; `min_pwm {left,right}`; `linear {pwm[], mps[]}`;
`angular {pwm[], radps[]}` (magnitudes, monotone); `half_track_m`;
`trim {left,right}`; `camera_yaw_offset_rad`; `limits`; `meta`. Invalid files
(non-monotone curves, floors above the first curve point, out-of-range trim)
are rejected at server start, not mid-drive. A profile from `--simulate`
carries `meta.source: "simulated"` and the server logs a warning if it is loaded.

## Known limits

- One speed curve is used for both wheels plus a gain; stiction-limited low
  speeds and asymmetric reverse are approximated, not modeled.
- Open-loop: battery sag and floor changes move the numbers. Nav2 closes the loop
  with ARKit odometry; this only makes its commands land close.
- Single-wheel ramp pivots the rover about the other wheel; give it room.
