# Demo checklist: cold boot to live VR feed

Goal: under 2 minutes from power-on to video in the headset. Network design: [demo-connectivity.md](demo-connectivity.md). Setup once ([jetson-bringup.md](jetson-bringup.md)): `jetson/setup.sh`, `jetson/install_service.sh`, Tailscale on Jetson and MacBook, Quest developer mode + USB debugging approved, `npm --prefix quest ci`.

## Before the 2 minutes (venue, once)

- [ ] `/etc/farsight.env` has the real serial, detector model and (HTTPS profile only) cert paths (`jetson/farsight.env.example`). `FARSIGHT_CAMERA` stays blank for the rover iPhone.
- [ ] Board profile today is plain HTTP on `192.168.55.1:8443` (USB) or the Tailscale IP, TLS omitted ([jetson-runtime-results.md](jetson-runtime-results.md)); `https://` + `--ca-file` applies only if the service was installed with TLS.
- [ ] MacBook: `python -m tools.demo --upstream http://<jetson-tailnet-ip>:8443 --check` passes (HTTPS profile: `https://<jetson-tailnet-name>:8443`, plus `--ca-file` for a private CA).
- [ ] Wi-Fi off on Jetson and MacBook. Both phones have Personal Hotspot allowed and the USB trust prompt accepted.

## Power on, in this order

| # | Do | Check |
|---|---|---|
| 1 | Rover: battery on, Uno USB to Jetson (the rover camera is the mounted iPhone, step 2; a UVC webcam only as fallback) | Uno LED on |
| 2 | Rover phone: USB to Jetson, Personal Hotspot on | Phone shows "1 connection" |
| 3 | Jetson: power on. Start a stopwatch. `farsight` unit starts itself | Nothing to type |
| 4 | MacBook: second phone USB, hotspot on, Wi-Fi off | `tailscale ping <jetson>` replies (note direct vs relayed) |
| 5 | MacBook: `python -m tools.ready --url http://<jetson>:8443 --require-tensorrt` (HTTPS profile: `https://<jetson>:8443 --ca-file ...`) | Ends `READY`: server, camera, detector, rover, video all OK or SKIP. `--require-tensorrt` fails a CPU (ONNX/HOG) detector, which cannot hold the demo frame rate. Stop the stopwatch on this line. |
| 6 | MacBook: `python -m tools.demo --upstream http://<jetson>:8443 --quest` (HTTPS profile: `https://<jetson>:8443 --ca-file ...`) | Prints "Open http://localhost:8080"; `adb reverse --list` shows 8080 |
| 7 | Quest: USB to MacBook, open `http://localhost:8080`, press Enter VR | Video live, HUD shows video age and link; rover estop clear |
| 8 | Release and press the deadman once | Motors respond; releasing stops within 250 ms |

Step 5 prints seconds since boot when run on the Jetson (`ssh farsight.local`); on the MacBook it shows probe time. Record the Jetson run, three in a row, in the table below.

## If something is not READY

- `camera WAIT`: reopen the Farsight Capture app on the rover iPhone and recheck the Jetson USB-network IP (UVC fallback: replug the webcam); `journalctl -u farsight -n 50` on the Jetson.
- `rover WAIT`: Uno cable, then `ls /dev/ttyACM*`; the unit starts degraded without it. Fix and `sudo systemctl restart farsight`.
- `server WAIT` over Tailscale: `tailscale status` on both; try the 5 GHz hotspot path in demo-connectivity.md.
- Quest blank: re-run `adb reverse tcp:8080 tcp:8080`; `window.isSecureContext` must be true.

## Fallback if hardware fails

1. Say so plainly, then run the simulator: `python -m tools.demo --simulate --build --quest` (mapped room, virtual stopped rover; no motors, no live camera).
2. Rover dead but Jetson alive: start with `FARSIGHT_SERIAL_PORT` blank for live camera + detections with motion disabled.
3. Everything dead: play the recorded run video. Never present simulation as live.

Passive 15-minute rehearsal log and go/no-go report: [rig-rehearsal.md](rig-rehearsal.md).

## Timing log (needs real hardware; issue #11 stays open until filled)

| Run | Power on to READY | READY to VR video | Total |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |

## Recorded-run fallback (Python server, part of #18)

Record one good **real** camera/rover run before Sun 4 AM. On the qualified rig:

```sh
python -m jetson.server --camera 0 --serial-port /dev/ttyACM0 --h264 --record --mission-dir runs
```

For the native phone camera, omit `--camera 0`. Drive the normal Quest demo with
people, detections and changing range visible, then stop the server with Ctrl-C.
Copy the complete `runs/<UTC timestamp>/` directory to each demo laptop. It
contains the existing mission evidence plus `frames/*.jpg`, `replay.jsonl`
(timestamped frames, inference and control telemetry), and `replay.json`
(duration/version). Permits and control session tokens are excluded. Recording
is opt-in and uses disk for every captured JPEG; check free space, keep the run
short, and verify it before relying on it. Graceful shutdown preserves the final pause/duration. A truncated JSONL
record must be removed before recovering a run after an unclean shutdown.

```sh
python -m jetson.server --replay runs/<UTC timestamp> --h264
# For Quest USB/relay, in another terminal:
python -m tools.demo --upstream http://127.0.0.1:8443 --quest
```

The normal JPEG, MJPEG, H.264 and detection endpoints play recorded data in real
time and loop. H.264 is re-encoded from the archived JPEGs and needs FFmpeg.
`/health` must show `replay: true`; both the page and headset HUD must show
**REPLAY**. Control/status reports recorded range, `replay: true`, no usable
permit, zero motor output and a read-only reason. Replay never constructs a
serial pump and rejects serial ports, other camera sources and recording flags.
Controls are ignored. This is retained evidence, not a live rescue or a new drill.
Rust implements `--record`/`--replay` (`rust/farsight-server/src/replay.rs`); Python remains the demo runtime until parity is checked.

Software rehearsal only (does **not** satisfy the real-run acceptance):

```sh
python -m jetson.server --demo --simulate-rover --record --mission-dir runs
# Stop after several seconds, then use --replay on that run.
```

The short checked-in `tools/fixtures/replay-demo` run was captured from `--demo`
and is labelled SIMULATED as well as REPLAY. Use it to check the fallback plumbing,
never as physical rover evidence. The real recording remains outstanding in #18.
