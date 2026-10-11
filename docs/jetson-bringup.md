# Jetson bring-up: desk to `/health` green and the Quest page loading

One ordered pass. Each step has a **Check** (what you must see) and **If it fails**. Commands marked
*Mac* run on the laptop in the repo root, *Jetson* over SSH. Passwords (Jetson login, sudo, Wi-Fi) are typed
by a human; no script stores one. `--dry-run` on `tools/bootstrap_jetson.sh`, `tools/push_models.sh`,
`jetson/setup.sh`, `jetson/install_service.sh` and `jetson/make_cert.sh` prints what they would do.

Issues covered: #1 camera stream (steps 4, 7), #9 detector (step 10), #11 one command boot (steps 5, 12),
#19 assistant (step 11), #14 depth engine (step 11b). Start this provisioning pass
only after Ubuntu boots and SSH works. The board now boots (R39.2.1, Ubuntu 24.04) and SSH works as
`ubuntu` ([jetson-recovery-results.md](jetson-recovery-results.md)); the older
[setup handoff](jetson-setup-handoff.md) and [flash handoff](jetson-flash-handoff.md) are history.
`USER` below is the account on the board: `ubuntu`.

**Board profile (what is installed today, [jetson-runtime-results.md](jetson-runtime-results.md)):** plain HTTP on
`http://192.168.55.1:8443` over USB (or the Tailscale IP), TLS omitted (`install_service.sh --no-tls`); the operator
relay supplies the Quest-facing origin. Every command from step 6 on is shown as `http://` first. The `https://farsight.local:8443`
+ `--ca-file` form is the alternative for a service installed with TLS (step 5); do not mix them: a plain-HTTP service
rejects `https://`, and a TLS service rejects `http://`.

The board runs Jetson Linux R39.2.1 (JetPack 7 generation) on the SD card. If it ever needs reinstalling, follow NVIDIA's
[Orin Nano quick start](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/quick_start.html) and the recovery notes in
[jetson-recovery-results.md](jetson-recovery-results.md) (saved UEFI OS-chain state was the boot blocker last time).

Needs: Jetson booted to Ubuntu with an SSH-capable user, USB-C data cable to the Mac, the rover iPhone (or a UVC webcam
on a Jetson USB-A port), a route for downloads (the board has no internet of its own: venue Wi-Fi, a phone hotspot,
macOS Internet Sharing over the USB-C link, or the temporary SOCKS tunnel in
[jetson-runtime-results.md](jetson-runtime-results.md)), the 30 W power bank or the barrel supply.

## 1. Reach the Jetson (Mac)

```sh
ssh-copy-id USER@192.168.55.1          # once; type the Jetson login password yourself
ssh USER@192.168.55.1 'hostname; uname -m; cat /etc/nv_tegra_release | head -1'
ssh USER@192.168.55.1 'curl -sI https://github.com | head -1'      # only if the board has a route out (optional)
```

**Check:** `aarch64` and an `R39` line. `HTTP/2 200` only if you gave the board internet; it has none by default.
**If it fails:**
- *No route to 192.168.55.1*: that address is the dev kit's USB-C device-mode link (data cable required, not
  charge-only). Look for a new `en*` interface in macOS Network settings. Otherwise join Wi-Fi on the Jetson's screen
  (`nmcli device wifi connect "SSID" --ask`) and use `USER@farsight.local` after step 3, or the IP from `ip -br a`.
- *No internet* (expected by default): use the SOCKS tunnel from [jetson-runtime-results.md](jetson-runtime-results.md), or macOS System Settings, General, Sharing, Internet Sharing, from Wi-Fi to the Jetson's USB interface.

## 2. Prepare models (Mac, internet)

```sh
python3 -m pip install ultralytics onnx
python3 -m tools.export_detector --weights yolo11n-pose.pt     # models/yolo11n-pose.onnx (the demo detector)
python3 -m tools.export_detector --weights yolo11n.pt          # models/yolo11n.onnx (object detection, optional)
python3 -m tools.fetch_models --groups piper,face,whisper,vlm --dry-run   # then drop --dry-run (vlm is 2.8 GB)
python3 -m tools.fetch_models --groups piper,face,whisper,vlm,export --check
```

**Check:** every line `present`. Downloads are size/SHA checked and resumable by re-running.
**If it fails:** an offline Mac cannot fetch; ask a teammate for their `models/` folder (see
[models/README.md](../models/README.md)). Missing `vlm`/`whisper` only disables the assistant.

## 3. Bootstrap: repo, models, setup in tmux (Mac)

```sh
tools/bootstrap_jetson.sh --dry-run USER@192.168.55.1          # read it once
tools/bootstrap_jetson.sh --setup-args "--core-only" USER@192.168.55.1
```

It checks SSH, clones or fast-forwards `~/farsight` (branch `main`; `--branch NAME` for another), rsyncs `models/`
(never `*.engine`), then attaches to tmux session `setup` running `jetson/setup.sh`. Type the sudo password once when
prompted. `--core-only` installs the runtime and builds the copied detector engines;
it skips optional model downloads, Piper installation, depth engines, and llama.cpp /
whisper.cpp builds. Confirm camera/control/video first. Once that path works, run
`bash jetson/setup.sh --with-vlm` on the Jetson to add those features. Native CUDA
builds may take 45 to 75 minutes; this has not been timed on our board. Detach with
Ctrl-b d; re-run the same command to re-attach. Bootstrap stops if tracked remote
files have uncommitted changes; commit or stash them before updating.

`setup.sh` does: apt packages and `nvidia-jetpack`, hostname `farsight`, `dialout`/`video` groups, the 15 W power
mode, `jetson/.venv` (with system site packages so TensorRT imports), and TensorRT
FP16 engines for `models/yolo11n*.onnx`. The full pass additionally prepares voices,
face and speech weights, depth engines, and native CUDA assistant builds with
`-DCMAKE_CUDA_ARCHITECTURES=87`.

**Check:** it ends with `Finished with no warnings.` (or a list of warnings you understand), then in a new shell:

```sh
ssh USER@192.168.55.1 'cd farsight && jetson/.venv/bin/python -c "import tensorrt, cv2; print(tensorrt.__version__, cv2.__version__)"; ls models/*.engine; sudo nvpmodel -q'
```

Expect a TensorRT version, `models/yolo11n-pose.engine`, and `NV Power Mode: 15W`.

**If it fails:**
- *TensorRT import* (`ModuleNotFoundError: tensorrt`): `sudo apt install -y python3-libnvinfer python3-libnvinfer-dev`;
  confirm `grep include-system-site-packages jetson/.venv/pyvenv.cfg` says `true` (setup fixes it); `dpkg -l | grep nvinfer`.
  An engine from another TensorRT version refuses to load: `jetson/.venv/bin/python -m jetson.build_engine --force models/yolo11n-pose.onnx`.
- *`no apt candidate for nvidia-jetpack`*: the NVIDIA Jetson apt source is not configured on this image.
  `ls /etc/apt/sources.list.d | grep -i nvidia`; with internet, `sudo apt-get update` and re-run. If it stays absent,
  CUDA/TensorRT must be installed through the supported JetPack compute-stack path:
  that is a blocker for steps 10 and 11, not for steps 4 to 9 (use `FARSIGHT_DETECTOR=hog`).
- *CUDA build failure*: `nvcc --version` must work (`export PATH=/usr/local/cuda/bin:$PATH`). A build killed with
  `c++: fatal error: Killed` or `nvcc error: ... out of memory` is RAM: the script already uses RAM/2 jobs; retry
  with fewer (`cmake --build ~/src/llama.cpp/build -j2 --target llama-server`), close other work, or add swap
  (`sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`).
  Re-running `setup.sh` resumes (cmake reuses the build dir). A `Unsupported gpu architecture` error means the
  compiler is not the JetPack CUDA; fix `PATH`.
- *Low power mode / 15 W*: `grep POWER_MODEL /etc/nvpmodel.conf` lists names and ids; set one with
  `sudo nvpmodel -m ID` or `setup.sh --power-mode NAME`. For brown-outs or sudden reboots under load, check
  `sudo nvpmodel -q`, the actual supply/cable and attached peripherals, and record `tegrastats` power rails.
  A selected 15 W mode alone does not establish that the power bank can sustain the complete rig; measure
  loaded behavior instead of inferring the cause from a reboot. Measured: the Anker Zolo 20K 45 W bank (15 V
  barrel) held 25W mode, VLM + whisper + CPU load and +/-255 wheels-up motor pulses for 3 minutes, VDD_IN peak
  15.3 W, with no under-voltage or reset (`docs/qualification/power-bank-load-2026-10-10.json`).
  `sudo jetson_clocks` only pins clocks within the active mode.
  `install_service.sh` also enables `farsight-warmup.timer`: 90 s after boot it re-runs `jetson_clocks`
  (the early boot pin lost the CPU minimum on 2026-10-11) and asks once by text and once by voice so the first
  question is warm (`journalctl -u farsight-warmup`).
- *Tailnet slow or lossy after replugging the phone*: `ip route | grep ^default` must show Wi-Fi. A new iPhone
  NIC gets an auto profile that can win the default route over cellular (measured 400-585 ms, 25% loss versus
  28 ms on Wi-Fi). `install_service.sh` installs `jetson/90-farsight-phone-tether.conf` (metric 2000 for `enx*`);
  for a profile that already exists: `sudo nmcli con modify '<name>' ipv4.never-default yes ipv6.never-default yes`
  then `sudo nmcli dev reapply <enx...>`.
- *Camera not found at the end checks*: step 4.
- *Uno not found*: unplug the ESP32 camera module, try another cable, `dmesg | tail`.

## 4. First light: camera only, no service yet (Jetson) (#1)

For the rover iPhone, use [ios/README.md](../ios/README.md) to install the signed
capture app and establish USB tethering. Start without `--camera` so the phone
can own the source:

```sh
cd ~/farsight
jetson/.venv/bin/python -m jetson.server --host 0.0.0.0 --port 8443 --detector none --simulate-rover --h264
```

Enter the Jetson's actual phone-USB interface IP in the app, choose RGB only,
and start capture. Confirm `/health` reports `camera=true`, `camera_source=PHONE RGB`,
and `control.simulated=true`; check `/camera.jpg` and the operator view. Phone
trust, Linux tethering, device signing, and sensor/display latency need the rig.
Stop this temporary server before installing the service. For this source,
`FARSIGHT_CAMERA` must stay blank in `/etc/farsight.env`; selecting `/dev/video0`
reserves the camera for UVC and rejects the phone connection.

For a UVC webcam, use the following alternative first-light check:

```sh
ssh USER@192.168.55.1
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext | head -30
cd ~/farsight && jetson/.venv/bin/python -m jetson.server --camera /dev/video0 --detector none --simulate-rover --port 8000
```

In a second SSH session:

```sh
curl -s localhost:8000/health | python3 -m json.tool | head -12
curl -s -o /tmp/f.jpg -w '%{http_code} %{size_download}\n' localhost:8000/camera.jpg
```

**Check:** `/health` shows `"camera": true`, `"camera_source": "USB CAMERA"`, `camera_age_ms` under 200;
`camera.jpg` returns `200` and a few tens of kB (640x480). Stop the server with Ctrl-C.
**If it fails (camera not found):**
- `v4l2-ctl --list-devices` shows nothing: try another USB-A port, check `lsusb`, `dmesg | tail`; a USB-C camera behind
  an adapter needs a data-capable cable. Use the first `/dev/videoN` listed under the camera (the second is metadata).
- `camera_error: Camera /dev/video0 cannot be opened`: `ls -l /dev/video0` must show group `video` and
  `id` must list `video` (log out and in once after setup), or another process holds it: `fuser -v /dev/video0`.
- `Camera capture failed; retrying` forever: the camera does not offer 640x480 MJPG. Look at the
  `--list-formats-ext` output for `MJPG` at 640x480; pick that camera mode or another `/dev/videoN`.

## 5. Install the service, with or without TLS (Jetson)

```sh
cd ~/farsight
bash jetson/install_service.sh --dry-run --camera-source phone --serial-port ''  # review this profile
bash jetson/install_service.sh --camera-source phone --serial-port '' --no-tls   # board profile today: plain HTTP :8443
bash jetson/install_service.sh --camera-source phone --serial-port ''            # alternative: private CA + HTTPS
```

For a UVC webcam, substitute `--camera-source usb`. The initial empty serial
argument keeps physical motors disabled. With an existing config, explicitly
requested camera/serial mismatches are refused before service changes; edit the
file or add `--reset-env` to back it up and apply the chosen profile. Do not
interpret an unchanged existing configuration as a successful profile switch.

Without `--no-tls` it generates a private CA and a server certificate in `/etc/farsight/tls` (names: `farsight.local`, the hostname,
`192.168.55.1`, current LAN IPs, the Tailscale name), renders `/etc/farsight.env` from what exists (pose engine,
else ONNX, else HOG; face model; Piper voices; llama/whisper URLs only if their units can run), installs
`farsight.service` (plus `farsight-llm.service` and `farsight-whisper.service` when built), runs
`python -m jetson.launch --check`, and starts it. An existing `/etc/farsight.env` is kept (`--reset-env` backs it
up and re-renders).
The units order after `network.target`, not `network-online.target`, and the installer disables
`systemd-networkd-wait-online` when networkd manages no links (NetworkManager does). Otherwise that
wait runs to its 2-minute timeout on every power-on and delays the server by about 2 minutes.

**Check:** `systemctl is-active farsight` prints `active`, and:

```sh
journalctl -u farsight -n 20 --no-pager | tail -5
jetson/.venv/bin/python -m jetson.launch --check --env-file /etc/farsight.env     # prints the exact server command, ends OK
```

**If it fails:** `launch --check` names every missing file or device (`FARSIGHT_DETECTOR_MODEL=... does not exist`,
`/dev/ttyACM0 not present`). Fix `/etc/farsight.env` (blank a value to use the server default) and
`sudo systemctl restart farsight`. `status=203/EXEC` means `jetson/.venv` is missing: re-run `setup.sh`. The server
still starts without the Uno or a camera and reports it on `/health`.

## 6. `/health` from the Mac (plain HTTP; HTTPS alternative) (#1)

```sh
python -m tools.ready --url http://192.168.55.1:8443 --skip rover,video --timeout 60
curl http://192.168.55.1:8443/health | python3 -m json.tool | head -20
```

HTTPS alternative (service installed with TLS):

```sh
scp USER@192.168.55.1:/etc/farsight/tls/ca.pem ~/farsight-ca.pem
python -m tools.ready --url https://farsight.local:8443 --ca-file ~/farsight-ca.pem --skip rover,video --timeout 60
curl --cacert ~/farsight-ca.pem https://farsight.local:8443/health | python3 -m json.tool | head -20
```

`--skip rover,video` is for a camera-only pass (no Uno yet, H.264 off); drop it once the rover is wired.

**Check:** `camera OK`, `detector OK pose-tensorrt ... fps` (or `hog`), final `READY`.
**If it fails:** `server WAIT ... unreachable`: `ping 192.168.55.1` (HTTPS profile: `ping farsight.local`, or use
`https://192.168.55.1:8443`, which the certificate covers); `SSLCertVerificationError` (HTTPS profile only) means the name is not in the certificate (re-run
`bash jetson/make_cert.sh --renew EXTRA_NAME` on the Jetson, then `sudo systemctl restart farsight`). `--insecure`
skips verification for a quick look only.

## 7. Measure the camera stream: issue #1 (Mac)

```sh
python -m tools.measure stream --url http://192.168.55.1:8443 --seconds 20
# HTTPS alternative: --url https://farsight.local:8443 --ca-file ~/farsight-ca.pem
```

**Check / record in the PR:** the `fps` and `gap median/p95/max` line and the `RESULT {...}` JSON. Stream rate follows the
camera capture rate (15 fps by default, `jetson/camera.py`); expect 13 to 15 fps at about 10 to 25 kB per frame over the
LAN. Opening `http://192.168.55.1:8443/stream.mjpg` in a browser also works (HTTPS profile: `https://farsight.local:8443/stream.mjpg` once `ca.pem` is trusted in Keychain).

**Hardware JPEG (`nvjpegenc`):** `gst-inspect-1.0 nvjpegenc` (apt `gstreamer1.0-tools`) tells you if the encoder
exists. The server does not use it: the pip OpenCV wheel has no GStreamer, and a UVC webcam already delivers MJPG
(requested first, V4L2 backend), so the CPU only decodes and re-encodes one 640x480 frame at quality 55. Write
"CPU JPEG, camera MJPG" in the PR and add `tegrastats --interval 1000` CPU% from a 20 s stream. Only chase `nvjpegenc`
if that CPU cost starves the detector.
**If fps is low:** `v4l2-ctl -d /dev/video0 --list-formats-ext` must offer MJPG 640x480 at 30; USB 2.0 hubs and
YUYV-only modes cap lower. Check `sudo nvpmodel -q` and `tegrastats` for throttling.

## 8. Wire the rover (Jetson)

Plug in the Uno, then:

```sh
ls /dev/ttyACM* /dev/ttyUSB*       # edit FARSIGHT_SERIAL_PORT in /etc/farsight.env if not ttyACM0
sudo nano /etc/farsight.env && sudo systemctl restart farsight
```

Identify the Uno port manually and keep `FARSIGHT_CAMERA` blank for the phone.

**ELEGOO Uno (CH340, `1a86:7523`) shows in `lsusb` but no `/dev/ttyUSB*`:** the
`6.8.12-1021-tegra` kernel ships `usbserial` but not `ch341`
(`# CONFIG_USB_SERIAL_CH341 is not set`). Build the upstream v6.8.12 driver
against the installed `nvidia-l4t-kernel-headers` (done on the rig 2026-10-10;
`ch341.c` sha256 `b286d49d…235951`, fetched on the Mac and copied because the
Jetson was offline):

```sh
# Mac
curl -fsSL -o ch341.c 'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/usb/serial/ch341.c?h=v6.8.12'
printf 'obj-m += ch341.o\n' > Makefile
ssh jetson mkdir -p ch341-build && scp ch341.c Makefile jetson:ch341-build/
# Jetson
cd ~/ch341-build && make -C /lib/modules/$(uname -r)/build M=$PWD modules
sudo install -D -m 0644 ch341.ko /lib/modules/$(uname -r)/updates/ch341.ko
sudo depmod -a $(uname -r) && sudo modprobe ch341   # pulls in usbserial; udev autoloads it on later boots
ls -l /dev/ttyUSB0                                  # root:dialout
```

The "compiler differs" warning (13.3 vs 13.2) and the unsigned-module taint are
expected. A kernel update to a new `uname -r` needs the build repeated.
Wheels raised first ([rover/README.md](../rover/README.md), [transport-setup.md](transport-setup.md)).
Complete direction, range, deadman, watchdog and disconnect checks before floor
driving; a connected serial port alone does not pass these checks.
**Check (Mac):** `python -m tools.ready --url http://192.168.55.1:8443` (HTTPS alternative: `--url https://farsight.local:8443 --ca-file ~/farsight-ca.pem`)
reaches `READY` with `rover OK Uno connected`. **If it fails:** `rover WAIT`: cable, `groups` shows `dialout`, `dmesg | grep tty`.

## 9. The Quest page (Mac)

```sh
npm --prefix quest ci && npm --prefix quest run build
python -m tools.demo --upstream http://192.168.55.1:8443 --quest
# HTTPS alternative: --upstream https://farsight.local:8443 --ca-file ~/farsight-ca.pem
```

Open `http://localhost:8080` in the Quest Browser (USB-C to the Mac, approve USB debugging; `adb devices`).
**Check:** prints `Open http://localhost:8080`, `adb reverse --list` shows `tcp:8080`, the page shows live video and
the HUD with video age. **If it fails:** [demo-checklist.md](demo-checklist.md) "If something is not READY".

For the constrained cellular path, enable `FARSIGHT_H264=1` in the rover env and
restart the service, then choose compressed video in Quest settings. Confirm
actual MediaSource decoding and displayed age on the Quest before driving.
Defaults remain 640x480, 10 fps, 500 kbps; the 250 ms permit and 500 ms view
guards remain unchanged. The software-only comparisons in
[qualification-results.md](qualification-results.md) include failed loaded runs
and do not qualify the real connection.

## 10. Detector on the GPU: issue #9 (Jetson, then Mac)

```sh
cd ~/farsight
jetson/.venv/bin/python -m jetson.tensorrt_detector models/yolo11n-pose.engine --pose --runs 50     # shapes, median/p95 GPU ms
FARSIGHT_POSE_ENGINE=models/yolo11n-pose.engine jetson/.venv/bin/python -m pytest -q jetson/tests/test_pose_tensorrt.py
```

With a person in view (Mac):

```sh
python -m tools.ready --url http://192.168.55.1:8443 --require-tensorrt --skip rover,video
python -m tools.measure detector --url http://192.168.55.1:8443 --seconds 30
# HTTPS alternative: --url https://farsight.local:8443 --ca-file ~/farsight-ca.pem on both commands
```

Reference: target-built FP16 pose engine, 20 zero-input runs, median 15.29 ms / p95 18.83 ms
([jetson-runtime-results.md](jetson-runtime-results.md); engine execution only, taken under native-build load).
**Pass:** `detector pose-tensorrt: median >= 15 fps`, `ms/frame` well under 66, `errors []`. Repeat while asking
questions (step 11) to see the fps under VLM load; record both.
**If it fails:** `backend pose-onnx` or `hog` means the engine path is wrong or it did not load: read
`detector_error` in `/health`; rebuild with `--force`. `0 samples`: nobody in view, or the camera is down.

## 11. Assistant latency: issue #19 (Jetson, then Mac)

```sh
systemctl is-active farsight-llm farsight-whisper         # both active
curl -s 127.0.0.1:8092/health; curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8091/
```

(Mac) text path, then speech path (Piper makes the question audio on the Jetson):

```sh
python -m tools.measure ask --url http://192.168.55.1:8443 --runs 6
ssh USER@192.168.55.1 'cd farsight && echo "What do you see?" | jetson/.venv/bin/python -m piper -m models/piper/en_US-lessac-medium.onnx -f /tmp/q.wav'
scp USER@192.168.55.1:/tmp/q.wav /tmp/q.wav
python -m tools.measure ask --url http://192.168.55.1:8443 --runs 6 --audio /tmp/q.wav
# HTTPS alternative: --url https://farsight.local:8443 --ca-file ~/farsight-ca.pem on both commands
```

**Record:** `cold` (model load) and `warm median/p95` totals plus the `stt`/`vlm`/`tts` stage medians. Reference: Mac
Metal warm total 0.7 to 0.9 s with Qwen2.5-VL-3B.
Spanish speaker check: `curl -s -H 'content-type: application/json' -d '{"phrase":"help_coming","lang":"es"}' http://192.168.55.1:8443/speak`
(HTTPS profile: add `--cacert ~/farsight-ca.pem` and use `https://farsight.local:8443/speak`) returns `"played": true` with a USB speaker (`aplay -l` lists it), else the headset plays `audio_url`.
**If it fails:** `FARSIGHT_ASSISTANT_URL` blank means the units were not installed: build and models first
(`journalctl -u farsight-llm -n 30`; `ls ~/src/llama.cpp/build/bin/llama-server models/*.gguf`), then
`bash jetson/install_service.sh`. `/health` `assistant` shows what the server actually found.

### 11b. Depth model engine and scan: issue #14

`setup.sh` builds `models/depth_anything*.onnx` into an engine once the ONNX is pushed, and `install_service.sh` puts the engine
(else the ONNX) in `FARSIGHT_DEPTH_MODEL`. Camera geometry and turn rate are `FARSIGHT_SCAN_*` in `/etc/farsight.env`
([scan.md](scan.md)). Check the engine on its own:

```sh
cd ~/farsight && FARSIGHT_DEPTH_MODEL=models/depth_anything_v2_metric_indoor_small_308x406.engine \
  jetson/.venv/bin/python -m unittest jetson.tests.test_scan.DepthModelTests -v
```

**If it fails:** no engine means `models/depth_anything*.onnx` was not pushed (`tools/push_models.sh`) or `trtexec` failed
(`jetson/.venv/bin/python -m jetson.build_engine --force models/depth_anything*.onnx` shows the trtexec tail).
The physical sweep and Quest controls are the rig test in `docs/scan.md`.

## 12. Cold boot to READY: issue #11

```sh
ssh USER@192.168.55.1 sudo reboot            # then, on the Mac, immediately:
python -m tools.ready --url http://192.168.55.1:8443 --timeout 180
# HTTPS alternative: --url https://farsight.local:8443 --ca-file ~/farsight-ca.pem
```

Repeat three times and fill the table in [demo-checklist.md](demo-checklist.md). For the "seconds since boot" line, run
the probe on the Jetson instead: `cd ~/farsight && python3 -m tools.ready --url http://127.0.0.1:8443 --timeout 180`
(HTTPS profile: `--url https://127.0.0.1:8443 --insecure`, which only skips certificate checks for the loopback address). Pass: `READY` within 2 minutes.

## Quick reference

| Need | Command |
|---|---|
| What would the server run? | `jetson/.venv/bin/python -m jetson.launch --check --env-file /etc/farsight.env` |
| Logs | `journalctl -u farsight -f` (also `-u farsight-llm`, `-u farsight-whisper`) |
| Re-render env from scratch | `bash jetson/install_service.sh --reset-env` |
| Renew TLS after an IP/name change (HTTPS profile only) | `bash jetson/make_cert.sh --renew NEWNAME && sudo systemctl restart farsight` |
| Rebuild an engine | `jetson/.venv/bin/python -m jetson.build_engine --force models/yolo11n-pose.onnx` |
| Load, temperature, power | `tegrastats --interval 1000`, `sudo nvpmodel -q` |
