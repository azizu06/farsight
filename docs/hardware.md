# Hardware

The ELEGOO Smart Robot Car V4 from our previous build, adapted. No code carried over.

## What stays
- Chassis, 4 motors, 2x 18650 motor pack, Arduino Uno + ELEGOO shield (motor driver,
  pan servo, HC-SR04 ultrasonic on the pan head).

## What we have
- Jetson Orin Nano Super dev kit, **JetPack 7.x** (see Software stack below)
- Camera: **OBSBOT Meet SE** (USB-C UVC webcam, small and light: good candidate for the pan head)
- Jetson power: **Anker Zolo power bank, 30 W** USB-C PD (20 V x 1.5 A max)
- No depth camera: depth comes from Depth Anything V2 on the normal camera

## What changes
| Change | Why | Check |
|---|---|---|
| **Remove the ESP32 camera module** (or set its switch to Upload) | It shares the Uno's only UART with USB; the Jetson needs that port. It was reflashed last time and no longer streams video anyway | Uno answers on `/dev/ttyACM0` or `/dev/ttyUSB0` from the Jetson |
| **Add Jetson Orin Nano dev kit** on the top deck | The brain | Standoffs or velcro; fan not blocked; centre of gravity low and central |
| **Separate Jetson power** | Motor current dips brown out the Jetson | Dev kit ships with a 19 V barrel adapter. On the car: Anker Zolo 30 W + a USB-C PD to 5.5x2.5 mm barrel **trigger cable set to 20 V** (buy if missing). Confirm the carrier's input range first. 30 W is not enough for MAXN SUPER (25 W plus peaks): **run the 15 W power mode** and check `tegrastats` under load |
| **Add a camera** | Replaces the phone | USB UVC webcam is easiest. CSI (IMX219) is lighter but the ribbon must flex with the pan |
| **Camera on the pan head** | Head-tracked look-around | The stock SG90-class servo is weak. If it can't hold the camera steady: fixed wide-angle camera and **digital pan** (crop the frame by head yaw). Same VR experience, no servo risk |
| **Jetson USB-A -> Uno USB-B cable** | Serial link | Short cable, strain relief |
| **Wi-Fi antennas on the Jetson** | Video to the Quest | Dev kit Wi-Fi card antennas attached; check `nmcli dev wifi` |

## Software stack (JetPack 7 on Orin Nano)
JetPack 7.2 (L4T r39.2, CUDA 13.2, TensorRT 10) works on the Orin Nano, but most **prebuilt** Jetson
software still targets JetPack 6 / CUDA 12.6: jetson-containers images fail, Ollama's installer falls
back to CPU, PyTorch wheels are partial. Do not downgrade (QSPI firmware + an Ubuntu x86 host for
SDK Manager; too risky tonight). Instead:

| Need | Use |
|---|---|
| YOLO / Depth Anything | Export ONNX on a laptop, build engines on the Jetson with `trtexec`, run with TensorRT Python |
| Vision-language model | `llama.cpp` built from source with `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87`, `llama-server` with `--mmproj` (Moondream2 or Qwen2.5-VL GGUF from Hugging Face) |
| Speech to text | `whisper.cpp` built the same way |
| Text to speech | Piper (CPU) |
| PyTorch | Only if needed; test the SBSA cu126 wheel early |

First command on the Jetson: `cat /etc/nv_tegra_release; nvcc --version; dpkg -l | grep -i tensorrt`.

## Firmware
Stock Uno firmware speaks ELEGOO's JSON protocol (`{"N":...}`, 9600 baud). Its speed command has
no Uno-side timer, so a dropped link keeps the motors running. #3 writes our own small sketch with
a 300 ms watchdog. The stock protocol is the fallback if time runs out (then the Jetson lease in #7
is the only stop).

## Tonight's checks (before writing code)
1. Jetson boots, `cat /etc/nv_tegra_release` shows JetPack 6.x. Not flashed? Flashing an SD image
   takes about an hour: start now.
2. Camera shows up: `v4l2-ctl --list-devices`.
3. Uno shows up over USB with the ESP module removed.
4. Servo can hold the camera (else plan digital pan).
5. Power bank runs the Jetson under load (`tegrastats`) for 20 minutes.
6. Quest 2 charged; Quest browser opens a page from a laptop on the team router.

## Shopping list (UCF Amazon pickup is a 5-minute walk; Sat 11 AM-5 PM)
USB-C PD to 5.5x2.5 mm barrel 20 V trigger cable (if missing),
USB-A to USB-B cable, travel router, velcro/standoffs, microSD 64 GB+ (if reflashing).
