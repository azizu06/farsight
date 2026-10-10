# Quest 2 first run

From unboxing to driving, with what to check when a step fails. Written before
any physical Quest 2 run: every step below is a hardware gate, and nothing here
has been verified on a headset yet. Transport details:
[transport-setup.md](transport-setup.md) and
[demo-connectivity.md](demo-connectivity.md). Controls and safety:
[quest/README.md](../quest/README.md).

The headset never talks to the Jetson directly. Quest Browser opens
`http://localhost:8080`, ADB reverse carries that port over USB to the MacBook
operator relay (`tools/demo.py`), and the relay proxies every route to the rover.

## 1. Headset setup (once, needs internet)

1. Charge the headset and both controllers; insert controller batteries.
2. Pair the headset with the Meta Horizon phone app, create or sign in to the
   account, and enable **Developer Mode** for that headset in the app's headset
   settings. This needs a Meta developer organization on the account.
3. Update Quest OS and Quest Browser while online.
4. Set up a stationary boundary. A roomscale boundary is unnecessary: the
   operator sits or stands still.

If Developer Mode is missing from the app, the account has no developer
organization yet. Without Developer Mode, `adb` never sees the headset; there is
no other supported transport in this project.

## 2. USB and ADB

1. Install Android Platform Tools on the MacBook (`adb version`).
2. Connect the headset with a **data** USB-C cable. In the headset, accept
   **Allow USB debugging** (tick *Always allow from this computer*).
3. `adb devices` must list the headset as `device`.

| Symptom | Cause and fix |
|---|---|
| No device listed | Charge-only cable or hub port; try another cable or a direct port |
| `unauthorized` | The prompt was missed: put the headset on, unplug and replug, accept |
| `offline` | `adb kill-server && adb devices`, then replug |
| Several devices | Pass `--adb-serial SERIAL` to the launcher |

## 3. Start the relay and map the port

Rehearse with the simulator first, then use the real Jetson:

```sh
npm --prefix quest ci && npm --prefix quest run build
python -m tools.demo --simulate --h264 --quest           # simulated rover, H.264
python -m tools.demo --upstream https://JETSON_TAILNET_NAME:8443 --quest
```

`--quest` runs `adb reverse tcp:8080 tcp:8080` and prints `adb reverse --list`.
Reapply it after any USB reconnect, headset sleep or reboot (rerun the launcher
or run the `adb reverse` command yourself).

If the launcher exits, read its message: a busy port, missing `adb`, missing
`quest/dist` (add `--build`), or an HTTPS certificate that does not match the
Jetson name (use `--ca-file`, or an explicit `http://` origin inside Tailscale).

## 4. Open the app in Quest Browser

Open `http://localhost:8080`. Expect the camera view with the source banner on
top: **SIMULATED** in rehearsal, **LIVE** only with real rover and camera proof.

| Symptom | Cause and fix |
|---|---|
| Page cannot load | ADB reverse not active: `adb reverse --list`, reapply |
| Banner stays NOT VERIFIED LIVE | Read its detail line; it names the missing proof (rover, camera source, frame age) |
| Button reads **WEBXR NEEDS HTTPS** | The page is not a secure context. Use `localhost`, never the MacBook's LAN IP; `http://localhost` is secure, any other host needs trusted HTTPS |
| Button reads **VR NOT SUPPORTED** | Not Quest Browser, or WebXR disabled; update Quest Browser |

## 5. Settings before entering VR

Settings close when VR starts and are not reachable from inside VR, so choose
them first (Escape on a keyboard, or the Settings button). The browser saves
each choice for the next load of the same address. The URL can also set
them, which beats a saved choice and is saved in turn, so one deep link sets up
the headset:

```
http://localhost:8080/?mount=fixed&h264=1&voice=1&diag=1
```

- **H.264 video** (`h264=0|1`): tick for the cellular demo. If the HUD then reads
  *H.264 codec unsupported* or never shows *decoded frame verified*, untick it
  and stay on JPEG; Quest 2 H.264 playback is not yet verified.
- **Camera mounting** (`mount=fixed|servo`): use **fixed** for the phone camera.
  Servo mode sends head yaw as pan, and both the Jetson and the Uno block
  forward driving when pan is more than 10° off center. With nothing chosen,
  the viewer follows the rover: `/health.camera_pan` false (no `--scan-pan`)
  selects fixed. The HUD's second line names the mount and video format, for
  example `JPEG video 80 ms · 120 cm · Fixed mount · digital pan 0°`.
- **Enable in-VR voice** (`voice=1`): press once and accept the microphone
  prompt. Once enabled, later loads ask for the microphone again on their own
  (silently when the permission is remembered). The browser asks only on this
  secure origin; if the prompt never appears, voice stays unavailable and text
  questions in settings still work. Revoke or reset it under Quest Browser site
  settings.
- **Diagnostics overlay** (`diag=1`, or **I** on a desktop keyboard): optional.
  Shows fps, frame time p95, JS time p95, video age, decode time, control RTT,
  server link time and the source, on the desktop page and on a panel to the
  lower right of the camera in VR.

## 6. Enter VR and check the controllers

Press **Enter VR**. With the car lifted or otherwise unable to travel:

1. Both controllers must be on and tracked. With either missing, the stop
   panel names the cause and the right trigger must be released and pressed again.
2. Hold the right trigger: the HUD headline should become ready/driving once
   camera and control are fresh. Release: it stops.
3. Hold the trigger and move the left stick: check the wheel direction for
   forward, reverse and both turns. Calibrate starting PWM on the real car
   ([rover/README.md](../rover/README.md)).
4. Turn your head: the screen stays anchored. Servo pan follows only in servo
   mode; fixed mode retains the whole frame. Left X recenters. Check A switches
   camera/map, B stops, and Y opens the actions menu. Point and squeeze right
   grip to select a highlighted action; the menu must block driving.
5. Hold the left trigger to ask by voice; release to send.
6. Take the headset off for a few seconds: motion stops and requires a release
   when you return.

If the stop panel shows a reason, follow its recovery line; the mapping lives in
`quest/src/stop-reason.js`.

## 7. Frame budget

Quest 2 renders at 72 Hz by default (13.9 ms per frame; 11.1 ms at 90 Hz) on a
much slower CPU and GPU than a laptop. With the diagnostics overlay on, the
first line turns amber or red when frame time p95 exceeds 1.1x or 1.5x the 72 Hz
budget. Record the overlay readings, with video path and load, in
[qualification-results.md](qualification-results.md).

The software profile runs the built app under an emulated Quest 2 (IWER) with
the H.264 simulator and a 4x CPU throttle:

```sh
python -m jetson.mapping.server --demo --simulate-rover --detector pose \
  --detector-model models/yolo11n-pose.onnx --h264 --host 127.0.0.1
npm --prefix quest run build && npm --prefix quest run perf
```

`PERF_JPEG=1` profiles the JPEG path, `PERF_DIAG=1` turns the overlay on,
`PERF_GL=swiftshader` matches the browser regression harness, and
`PERF_PROFILE=1` prints a CPU profile. Desktop Chrome numbers are a relative
measure only; they are not Quest numbers.
