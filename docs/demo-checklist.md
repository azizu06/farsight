# Demo checklist: cold boot to live VR feed

Goal: under 2 minutes from power-on to video in the headset. Network design: [demo-connectivity.md](demo-connectivity.md). Setup once: `jetson/setup.sh`, `jetson/install_service.sh`, Tailscale on Jetson and MacBook, Quest developer mode + USB debugging approved, `npm --prefix quest ci`.

## Before the 2 minutes (venue, once)

- [ ] `/etc/farsight.env` has the real camera, serial, detector model, cert paths (`jetson/farsight.env.example`).
- [ ] MacBook: `python -m tools.demo --upstream https://<jetson-tailnet-name>:8443 --check` passes.
- [ ] Wi-Fi off on Jetson and MacBook. Both phones have Personal Hotspot allowed and the USB trust prompt accepted.

## Power on, in this order

| # | Do | Check |
|---|---|---|
| 1 | Rover: battery on, Uno USB to Jetson, camera USB to Jetson | Uno LED on |
| 2 | Rover phone: USB to Jetson, Personal Hotspot on | Phone shows "1 connection" |
| 3 | Jetson: power on. Start a stopwatch. `farsight` unit starts itself | Nothing to type |
| 4 | MacBook: second phone USB, hotspot on, Wi-Fi off | `tailscale ping <jetson>` replies (note direct vs relayed) |
| 5 | MacBook: `python -m tools.ready --url https://<jetson>:8443` | Ends `READY`: server, camera, detector, rover, video all OK or SKIP. Stop the stopwatch on this line. |
| 6 | MacBook: `python -m tools.demo --upstream https://<jetson>:8443 --quest` | Prints "Open http://localhost:8080"; `adb reverse --list` shows 8080 |
| 7 | Quest: USB to MacBook, open `http://localhost:8080`, press Enter VR | Video live, HUD shows video age and link; rover estop clear |
| 8 | Release and press the deadman once | Motors respond; releasing stops within 250 ms |

Step 5 prints seconds since boot when run on the Jetson (`ssh farsight.local`); on the MacBook it shows probe time. Record the Jetson run, three in a row, in the table below.

## If something is not READY

- `camera WAIT`: replug the webcam; `journalctl -u farsight -n 50` on the Jetson.
- `rover WAIT`: Uno cable, then `ls /dev/ttyACM*`; the unit starts degraded without it. Fix and `sudo systemctl restart farsight`.
- `server WAIT` over Tailscale: `tailscale status` on both; try the 5 GHz hotspot path in demo-connectivity.md.
- Quest blank: re-run `adb reverse tcp:8080 tcp:8080`; `window.isSecureContext` must be true.

## Fallback if hardware fails

1. Say so plainly, then run the simulator: `python -m tools.demo --simulate --build --quest` (mapped room, virtual stopped rover; no motors, no live camera).
2. Rover dead but Jetson alive: start with `FARSIGHT_SERIAL_PORT` blank for live camera + detections with motion disabled.
3. Everything dead: play the recorded run video. Never present simulation as live.

## Timing log (needs real hardware; issue #11 stays open until filled)

| Run | Power on to READY | READY to VR video | Total |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
