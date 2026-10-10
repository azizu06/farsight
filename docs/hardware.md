# Hardware

The ELEGOO Smart Robot Car V4 from our previous build, adapted. No code carried over.

## What stays
- Chassis, 4 motors, 2x 18650 motor pack, Arduino Uno + ELEGOO shield (motor driver,
  pan servo, HC-SR04 ultrasonic on the pan head).

## What changes
| Change | Why | Check |
|---|---|---|
| **Remove the ESP32 camera module** (or set its switch to Upload) | It shares the Uno's only UART with USB; the Jetson needs that port. It was reflashed last time and no longer streams video anyway | Uno answers on `/dev/ttyACM0` or `/dev/ttyUSB0` from the Jetson |
| **Add Jetson Orin Nano dev kit** on the top deck | The brain | Standoffs or velcro; fan not blocked; centre of gravity low and central |
| **Separate Jetson power** | Motor current dips brown out the Jetson | Dev kit ships with a 19 V barrel adapter. On the car: USB-C PD power bank (>= 45 W) + PD-to-5.5x2.5 mm barrel trigger cable set to 19-20 V. Confirm the carrier's input range before plugging in. Run `sudo nvpmodel` in 15 W mode to save battery |
| **Add a camera** | Replaces the phone | USB UVC webcam is easiest. CSI (IMX219) is lighter but the ribbon must flex with the pan |
| **Camera on the pan head** | Head-tracked look-around | The stock SG90-class servo is weak. If it can't hold the camera steady: fixed wide-angle camera and **digital pan** (crop the frame by head yaw). Same VR experience, no servo risk |
| **Jetson USB-A -> Uno USB-B cable** | Serial link | Short cable, strain relief |
| **Wi-Fi antennas on the Jetson** | Video to the Quest | Dev kit Wi-Fi card antennas attached; check `nmcli dev wifi` |

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
USB-C PD power bank >= 45 W, PD trigger cable to 5.5x2.5 mm barrel, USB webcam (if none),
USB-A to USB-B cable, travel router, velcro/standoffs, microSD 64 GB+ (if reflashing).
