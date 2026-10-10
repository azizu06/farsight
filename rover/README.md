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

Remove the ESP camera module or put its switch in Upload mode: it shares the
Uno UART. Do not run the serial monitor at the same time as the Jetson.

The Uno starts stopped, stops if no valid `M` arrives for 300 ms, and treats the
D2 button as an immediate motor stop while held. `R -1` means no valid echo;
it is **not** a clear road. Its independent 10 Hz range check blocks forward
translation below 25 cm, without an echo, with an off-centre pan head, or while
the pan servo settles. Reverse and turning remain available. Because the stock
range sensor pans with the camera, physically keep that sensor facing forward
or use fixed camera/digital pan to drive while looking sideways.

Bench qualification is required before floor driving:

1. Lift the wheels; connect serial. `M 0 0` must return `OK`.
2. Verify positive left/right values move each side forward independently.
3. Clear the forward sensor; send `M 50 50`, then stop sending. Both sides
   must stop within 300 ms (plus the bounded 12 ms sensor call).
4. Send `P 90`, wait 250 ms, and read `R`. Put an object within 25 cm and
   verify forward is blocked; reverse and turning must still work.
5. Unplug the USB cable while moving slowly; verify the Uno stops.
6. Hold the board button; both motors must stop while it is held.

Host tests and successful compilation do not establish motor polarity, range
accuracy, braking distance, USB stability, or correct wiring on the real rover.
