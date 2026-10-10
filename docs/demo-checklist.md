# Demo checklist: cold boot to live VR feed

Goal: under 2 minutes from power-on to video in the headset. Network design: [demo-connectivity.md](demo-connectivity.md). Setup once ([jetson-bringup.md](jetson-bringup.md)): `jetson/setup.sh`, `jetson/install_service.sh`, both phones on the same tailnet with cellular enabled, Quest developer mode + USB debugging approved, `npm --prefix quest ci`, and Mac USB tools (`iproxy`, `idevice_id`). The Jetson and Mac do not need Tailscale for the primary route.

## Before the 2 minutes (venue, once)

- [ ] `/etc/farsight.env` has the real serial, detector model and (HTTPS profile only) cert paths (`jetson/farsight.env.example`). `FARSIGHT_CAMERA` stays blank for the rover iPhone.
- [ ] Board profile today is plain HTTP on Jetson port `8443`; the rover phone bridge uses the Jetson's current local USB-network address (last observed `172.20.10.3`, not fixed). TLS is omitted ([jetson-runtime-results.md](jetson-runtime-results.md)); `https://` + `--ca-file` applies only if the service was installed with TLS.
- [ ] Disable Wi-Fi on Quest, MacBook, and Jetson. Both phones use cellular with Wi-Fi off; keep the rover phone's Personal Hotspot available for its cabled USB network to the Jetson.
- [ ] Connect the rover phone to Jetson and operator phone to Mac by USB. Keep Quest connected to Mac by USB. Both Farsight Capture bridge roles are foreground and report connected.
- [ ] Install the signed mesh-capable Farsight Capture app on both phones. Rover iPhone 17 Pro: **Send RGB only** off, **Build 3D on this phone** on, Start capture. Operator phone: **Forward phone 3D over USB** on, Start bridge. See [iOS setup](../ios/README.md#phone-built-3d-mesh).
- [ ] Build the Mac assembler before the rehearsal: `cargo build --release --manifest-path rust/Cargo.toml -p farsight-geometry`. Use a revision containing the `snapshot_complete` status field (`f2a4482` or newer).
- [ ] On the Mac, `python -m tools.phone_usb --geometry --check` passes. Then keep `python -m tools.phone_usb --geometry` running in Terminal 1; it forwards control on `18443` and geometry on `18445` over the attached operator-phone USB cable. The check exits and stops its proxy; restart without `--check` before the next step.
- [ ] With the phone helper running and the Mac-to-Jetson commissioning cable unplugged, `python -m tools.ready --url http://127.0.0.1:18443 --require-tensorrt --require-drive-camera` ends `READY` (HTTPS profile only: add the configured `--ca-file`). This probes the phone-owned path, not a direct Jetson IP.

## Power on, in this order

| # | Do | Check |
|---|---|---|
| 1 | Rover: battery on, Uno USB to Jetson (the rover camera is the mounted iPhone, step 2; a UVC webcam only as fallback) | Uno LED on |
| 2 | Rover phone: USB to Jetson, Personal Hotspot allowed/on for the cabled USB network | Jetson's current USB-interface address is reachable; do not assume it is always `172.20.10.3` |
| 3 | Jetson: power on. Start a stopwatch. `farsight` unit starts itself | Nothing to type |
| 4 | MacBook: operator phone USB, Mac Wi-Fi off; both bridge apps active over cellular/Tailscale | `python -m tools.phone_usb --geometry --check` reaches health through `18443` and geometry status through `18445`; then keep the helper running without `--check`. Record direct/DERP from the phone bridge diagnostics. |
| 5 | MacBook: `python -m tools.ready --url http://127.0.0.1:18443 --require-tensorrt --require-drive-camera` | Ends `READY`: server, camera, drive-camera eligibility, detector, rover and video pass. Missing/false `camera_drive_ready` blocks this check: a viewable LiDAR feed alone does not qualify physical driving. This is a component readiness check; still measure actual motion/stop behavior. |
| 6 | MacBook, Terminal 2: `rust/target/release/farsight-geometry --upstream ws://127.0.0.1:18445/geometry/stream` | Local assembler listens on `127.0.0.1:8484`; keep this process running. |
| 7 | MacBook, Terminal 3: `python -m tools.demo --upstream http://127.0.0.1:18443 --geometry-upstream http://127.0.0.1:8484 --quest` | Prints "Open http://localhost:8080"; `adb reverse --list` shows 8080. This launcher does not start the assembler or phone helper. |
| 8 | MacBook: `python -m tools.qualify_geometry --timeout 5 --output /tmp/farsight-geometry-check.json` | `qualified: true`: complete, connected, matched phone mesh with positive triangles and stable session/epoch/revision. This is HTTP source-path evidence only. |
| 9 | Quest: USB to MacBook, open `http://localhost:8080`, press Enter VR | Camera live; reconstructed phone geometry visible in map view and updates when the rover camera sees new surfaces. Check the actual headset; the HTTP verdict does not prove rendering. Record READY-to-VR time. |
| 10 | Release and press the right-trigger deadman; steer with the right stick | Motors respond continuously while held; releasing stops within 250 ms. Record physical results, including repeated stops or disconnects. |

Enable Jetson `--fusion-bypass` / `FARSIGHT_FUSION_BYPASS=1` only after step 9
passes. Keep fresh phone depth/pose available for navigation and inference;
mesh reconstruction is a separate source. The target owner applies service
changes between rehearsals. Terminal 1, 2 and 3 must remain running; closing
the launcher does not stop the separately started phone helper or assembler.

Step 5 prints seconds since boot when run on the Jetson (`ssh farsight.local`); on the MacBook it shows probe time. Record the Jetson run, three in a row, in the table below.

## If something is not READY

- `camera WAIT`: reopen the Farsight Capture app on the rover iPhone and recheck the Jetson USB-network IP (UVC fallback: replug the webcam); `journalctl -u farsight -n 50` on the Jetson.
- `rover WAIT`: Uno cable, then `ls /dev/ttyACM*`; the unit starts degraded without it. Fix and `sudo systemctl restart farsight`.
- `server WAIT` through the phone relay: check both phone bridge screens, cellular/Tailscale status, rover phone USB networking, then rerun `python -m tools.phone_usb --geometry --check`. Restart without `--check` afterward. Do not switch to venue Wi-Fi.
- Geometry qualification fails: read its JSON error. Check both phone mesh switches, `18445` forwarding, the assembler process and snapshot completeness. A capture-session mismatch needs the current rover capture and Jetson pose to agree; restarting only the Mac relay does not create that agreement.
- Held driving repeatedly stops: preserve the first fault and read `/relay/health.control_closures`. Match its UTC times with Jetson serial errors/kernel USB resets; do not mask the failure by extending leases. See [relay diagnostics](transport-setup.md#relay-behavior).
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
