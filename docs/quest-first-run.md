# Quest 2 first run

From unboxing to driving, with what to check when a step fails. Written before
any physical Quest 2 run: every step below is a hardware gate, and nothing here
has been verified on a headset yet. Transport details:
[transport-setup.md](transport-setup.md) and
[demo-connectivity.md](demo-connectivity.md). Controls and safety:
[quest/README.md](../quest/README.md).

The headset never talks to the Jetson directly. Quest Browser opens
`http://localhost:8080`, ADB reverse carries that port over USB to the MacBook
or Windows/WSL operator relay (`tools/demo.py`), and the relay proxies every
route to the rover. USB must connect to the computer running ADB; a coding
agent on a different computer cannot access that USB device automatically.

## 1. Headset setup (once, needs internet)

1. Charge the headset and put a fresh AA battery in each Touch controller.
2. Pair the headset with the Meta Horizon phone app, create or sign in to the
   account, and enable **Developer Mode** for that headset in the app's headset
   settings. This needs membership in a Meta developer team and a verified
   developer account; complete Meta's account verification while online.
3. Update Quest OS and Quest Browser while online.
4. Set up a stationary boundary. A roomscale boundary is unnecessary: the
   operator sits or stands still.

If Developer Mode is missing, check account verification and developer-team
membership against [Meta's device setup instructions](https://developers.meta.com/horizon/documentation/native/android/mobile-device-setup/).
Without Developer Mode and USB debugging, this project's ADB transport cannot
be used.

## 2. USB and ADB

1. Install [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)
   on the USB host (`adb version`). Android Studio and Quest Link are unnecessary
   for this browser app. On Windows/WSL, use the **Windows** `adb.exe`, keeping USB
   owned by Windows; pass its WSL path using `--adb` below.
2. Connect the headset with a **data** USB-C cable. In the headset, accept
   **Allow USB debugging** (tick *Always allow from this computer*).
3. `adb devices` must list the headset as `device`.

| Symptom | Cause and fix |
|---|---|
| No device listed | Try a data cable and direct port, check Developer Mode; Windows may also need [Meta's ADB driver](https://developers.meta.com/horizon/downloads/package/oculus-adb-drivers/) |
| `unauthorized` | The prompt was missed: put the headset on, unplug and replug, accept |
| `offline` | `adb kill-server && adb devices`, then replug |
| Several devices | Pass `--adb-serial SERIAL` to the launcher |

## 3. Start the relay and map the port

Rehearse with the simulator and JPEG first, then use a verified live upstream.
Run one launcher at a time:

```sh
npm --prefix quest ci && npm --prefix quest run build
python -m tools.demo --simulate --quest                  # simulated rover, JPEG
python -m tools.demo --upstream https://JETSON_TAILNET_NAME:8443 --quest
```

For a Windows USB host with the app running inside WSL:

```sh
python -m tools.demo --simulate --quest \
  --adb /mnt/c/path/to/platform-tools/adb.exe
```

The native Windows ADB reverse connection ends at **Windows** `localhost:8080`.
Check Windows can load the WSL-served app there before testing the headset.
`--check` checks prerequisites without probing USB or starting servers; actual
launch checks device authorization and gives each ADB command a 15-second limit.

`--quest` runs `adb reverse tcp:8080 tcp:8080` and prints `adb reverse --list`.
Reapply it after any USB reconnect, headset sleep or reboot (rerun the launcher
or run the `adb reverse` command yourself).

If the launcher exits, read its message: a busy port, missing `adb`, missing
`quest/dist` (add `--build`), or an HTTPS certificate that does not match the
Jetson name (use `--ca-file`, or an explicit `http://` origin inside Tailscale).

The two-phone relay has been tested on Aziz's Mac, through its local
`http://127.0.0.1:18443` forwarding port. That port is not automatically present
on another computer. Before switching a Windows headset session from simulation
to live, establish and verify a reachable rover upstream on Windows: either
qualify its phone USB forwarding or a permitted tailnet route. A working Quest
USB connection alone does not qualify that network path.

## 4. Open the app in Quest Browser

Open `http://localhost:8080`. Expect the camera view with the source banner on
top: **SIMULATED** in rehearsal, **LIVE** only with real rover and camera proof.

| Symptom | Cause and fix |
|---|---|
| Page cannot load | ADB reverse not active: `adb reverse --list`, reapply |
| Banner stays NOT VERIFIED LIVE | Read its detail line; it names the missing proof (rover, camera source, frame age) |
| Button reads **WEBXR NEEDS HTTPS** | The page is not a secure context. Use `localhost`, never the operator computer's LAN IP; other hosts need trusted HTTPS |
| Button reads **VR NOT SUPPORTED** | Not Quest Browser, or WebXR disabled; update Quest Browser |

## 5. Settings before entering VR

Settings close when VR starts and are not reachable from inside VR, so choose
them first (Escape on a keyboard, or the Settings button). The browser saves
each choice for the next load of the same address. The URL can also set
them, which beats a saved choice and is saved in turn, so one deep link sets up
the headset:

```
http://localhost:8080/?mount=fixed&h264=0&voice=0&diag=1
```

This first-run link selects JPEG, fixed camera mounting and diagnostics, without
requesting microphone permission. Once those work, enable H.264 for the live
cellular path. To rehearse H.264 locally, restart the simulator with `--h264`
(requires FFmpeg) and select `h264=1`. Enable voice only when the rover's voice
services are available and microphone permission can be checked separately.

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

1. Start with both controllers on and tracked. Losing a controller that was
   participating stops motion; the stop panel names the cause and the right
   trigger must be released and pressed again.
2. Hold the right trigger: the HUD headline should become ready/driving once
   camera and control are fresh. Release: it stops.
3. Hold the trigger and move the left stick: check the wheel direction for
   forward, reverse and both turns. Calibrate starting PWM on the real car
   ([rover/README.md](../rover/README.md)).
4. Turn your head: servo pan follows (or the digital crop moves). Left X
   recenters.
5. If voice is available and enabled, hold the left trigger to ask; release to send.
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

## 8. Handing the headset to a coding agent on Windows/WSL

The person wearing the headset must complete the account setup, turn on Developer
Mode, plug a data USB cable into this Windows computer, and approve **Allow USB
debugging** in the headset. Keep it awake with both controllers available. Tell
the agent: "Quest is plugged into this PC; Developer Mode is on and USB debugging
is approved." The agent can then list the device, build and start the existing
app, configure ADB reverse, collect logs and reload the browser.

For browser debugging, open desktop Chrome's `chrome://inspect/#devices` on the
USB host and inspect the Quest Browser tab; see
[Meta's browser debugging instructions](https://developers.meta.com/horizon/documentation/web/browser-remote-debugging/).
An agent can attach browser automation once the actual device's debugging socket
is discovered and forwarded. Do not assume its socket name or that a desktop XR
emulator proves the physical headset works.

The human opens the first-run link in **Quest Browser**, presses **Enter VR**, and
performs the short checks in section 6 while the agent watches logs and status.
ADB cannot wear the headset, approve its boundary/permission dialogs, or physically
move its controllers. First use simulation; qualify real camera video next, then
check motor direction, release stopping and disconnect stopping with an operator
present and the car lifted. Record these as hardware results, separately from
desktop/simulated tests.
