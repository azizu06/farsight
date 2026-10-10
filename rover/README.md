# Uno motor firmware

`farsight_uno/farsight_uno.ino` is original firmware for the **ELEGOO Smart Robot
Car V4 shield**. It accepts the serial contract at 115200 baud: `M left right`,
`P degrees`, `R`, newline terminated. Motor values are signed PWM -255..255;
servo is 10..170 degrees, 90 forward. Invalid commands stop the motors.

The V4 pin assignments are facts checked against ELEGOO's
[official hardware definitions](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0-New/blob/main/DeviceDriverSet.h):
left PWM D6/direction D8, right PWM D5/direction D7, bridge standby D3,
pan servo D10, ultrasonic trigger D13/echo D12, button D2. Confirm the actual
shield is V4 before flashing. Direction HIGH is assumed forward; verify with
the wheels off the ground and swap motor leads if needed.

Flash with Arduino IDE (Uno board, official Servo library), or:

```sh
arduino-cli core install arduino:avr
arduino-cli lib install Servo
arduino-cli compile --fqbn arduino:avr:uno rover/farsight_uno
arduino-cli upload --fqbn arduino:avr:uno --port /dev/ttyACM0 rover/farsight_uno
```

**Compile status: compiled and flashed (2026-10-10).** `Sketch uses 6066 bytes
(18%) ... Global variables use 306 bytes (14%)` with `arduino-cli` 1.5.1,
`arduino:avr@1.8.8`, Servo 1.3.0. On Apple silicon the macOS AVR toolchain is
x86_64-only (`avr-g++: bad CPU type`), so use the Linux arm64 build instead,
either in Docker or on the Jetson. The Jetson has no internet on the rig, so
stage everything on the Mac in an arm64 container and copy it over:

```sh
# Mac: arduino-cli_1.5.1_Linux_ARM64.tar.gz from github.com/arduino/arduino-cli/releases, unpacked into work/
docker run --rm --platform linux/arm64 -v $PWD/work:/work \
  -v /etc/ssl/cert.pem:/etc/ssl/certs/ca-certificates.crt:ro \
  -e ARDUINO_DIRECTORIES_DATA=/work/arduino15 -e ARDUINO_DIRECTORIES_USER=/work/Arduino \
  ubuntu:24.04 sh -c 'cd /work && ./arduino-cli core update-index &&
    ./arduino-cli core install arduino:avr && ./arduino-cli lib install Servo'
tar -czf arduino-offline.tgz -C work arduino-cli arduino15 Arduino   # ~143 MB
scp arduino-offline.tgz jetson: && ssh jetson 'mkdir -p arduino-offline && tar -xzf arduino-offline.tgz -C arduino-offline'
# Jetson (nothing else may hold the port; farsight.service runs with FARSIGHT_SERIAL_PORT blank)
cd ~/arduino-offline && export ARDUINO_DIRECTORIES_DATA=$PWD/arduino15 ARDUINO_DIRECTORIES_USER=$PWD/Arduino
./arduino-cli compile --fqbn arduino:avr:uno --output-dir ~/farsight_uno-build ~/farsight/rover/farsight_uno
./arduino-cli upload --fqbn arduino:avr:uno --port /dev/ttyUSB0 --input-dir ~/farsight_uno-build ~/farsight/rover/farsight_uno
```

The ELEGOO Uno uses a CH340 USB-serial chip, so it appears as `/dev/ttyUSB0`, not
`/dev/ttyACM0`; on the Jetson kernel that needs the `ch341` module
([jetson-bringup.md](../docs/jetson-bringup.md#8-wire-the-rover-jetson)). The stock
ELEGOO firmware is silent to `M 0 0` at 115200; `farsight_uno` answers `OK`.

Remove the ESP camera module or put its switch in Upload mode: it shares the
Uno UART. Do not run the serial monitor at the same time as the Jetson.

The Uno starts stopped, stops if no valid `M` arrives for 300 ms, and treats the
D2 button as an immediate motor stop while held. `R -1` means no valid echo;
it is **not** a clear road. Forward translation is removed while the pan head is
off centre or the pan servo settles, and (by default) below 25 cm or without a fresh
echo. For navigation runs where the Jetson's planner steers around obstacles, the
proximity part can be compiled out with
`--build-property compiler.cpp.extra_flags=-DFARSIGHT_DISABLE_PROXIMITY_STOP`; run the
Jetson server with `--proximity-stop off` to match. Reverse and turning always remain
available. Because the stock
range sensor pans with the camera, physically keep that sensor facing forward
or use fixed camera/digital pan to drive while looking sideways.

## Serial protocol test

`rover/serial_test.py` (pyserial) exercises every Uno command: `M 0 0`, `P 90`, `R`,
an invalid line, out-of-range `M`/`P` values and an over-long line (`ERR line`),
then prints PASS/FAIL per command. The default is a **dry run that cannot move
motors**.

```sh
python rover/serial_test.py --port /dev/ttyACM0                  # dry run
python rover/serial_test.py --port /dev/ttyACM0 --allow-motion   # wheels raised
python rover/serial_test.py --selftest                           # simulator, no hardware
python -m pytest rover/tests                                     # unit tests
```

`--allow-motion` sends `M 50 50`, then goes silent and times the stop against the
300 ms watchdog. The firmware has no motor-status reply, so the operator presses
Enter when the wheels stop; `--tolerance-ms` (default 500) absorbs reaction time,
so this only catches a watchdog that is missing or grossly late. Keep the forward
sensor clear or the firmware removes the forward component. `--selftest` and the
tests run against an in-process model of the sketch and are software evidence only.

`--allow-motion` needs the person watching the wheels at the same terminal. When
the observer is elsewhere and reports back, use `rover/wheel_bench.py` instead:
it prints exactly what it commands, renews the watchdog while a side spins,
caps PWM at 120, and sends `M 0 0` after every step. Forward steps read `R` first
and are skipped below 25 cm or with no echo, because the firmware would otherwise
turn a one-side forward command into a half-power pivot.

```sh
python rover/wheel_bench.py --port /dev/ttyUSB0 sides --pwm 80 --seconds 2      # L fwd, R fwd, L rev, R rev
python rover/wheel_bench.py --port /dev/ttyUSB0 ramp --side left --dir rev --start 30 --stop 100 --step 10
python rover/wheel_bench.py --port /dev/ttyUSB0 watchdog --pwm 80               # one M, then 4 s of silence
```

Bench qualification is required before floor driving:

1. Lift the wheels; connect serial. `M 0 0` must return `OK`.
2. Verify positive left/right values move each side forward independently.
3. Clear the forward sensor; send `M 50 50`, then stop sending. Both sides
   must stop within 300 ms (plus the bounded 12 ms sensor call).
4. Send `P 90`, wait 250 ms, and read `R`. Put an object within 25 cm and
   verify forward is blocked; reverse and turning must still work. A
   `FARSIGHT_DISABLE_PROXIMITY_STOP` build must keep driving forward instead.
5. Unplug the USB cable while moving slowly; verify the Uno stops.
6. Hold the board button; both motors must stop while it is held.

Host tests and successful compilation do not establish motor polarity, range
accuracy, braking distance, USB stability, or correct wiring on the real rover.

## Calibrate slow movement

The Jetson server exposes `--max-pwm` (ceiling) and optional `--min-pwm` (floor).
The floor defaults to **0**, preserving the linear mixer. A nonzero wheel
command maps between the calibrated floor and ceiling; zero commands remain
zero. The allowed range is `0 <= min_pwm < max_pwm <= 255`. A low command may
otherwise produce too little torque to start the ELEGOO motors consistently.

With the wheels lifted, begin at floor zero and increase `--min-pwm` in small
steps only until both sides start consistently at small forward and reverse
commands. Repeat with the actual demo battery. Verify release, disconnect,
obstacle, and watchdog stops still produce exact zero; a floor must never turn
a stop into a moving wheel command. Check low arcs and pivots too, since the
floor affects all nonzero wheels and can change turn curvature.

Then qualify on the floor in a clear small area with someone ready to power
off the rover. Recheck the loaded starting threshold and stopping distance;
choose the lowest reliable floor and a ceiling that produces suitably slow
movement. Lifted-wheel spin alone does not establish floor speed or loaded
torque. Do not claim any PWM value is calibrated until these physical checks
pass.
