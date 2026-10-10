# Hardware notes

Facts about the ELEGOO Smart Robot Car V4 we already own (learned on a previous build; no code
carried over).

- Brain board: Arduino Uno with ELEGOO's shield (motor driver, pan servo, HC-SR04 ultrasonic on
  the pan head). The Uno has **one** hardware UART, shared by its USB port and the ESP camera
  module header.
- The ESP32 camera module talks to the Uno at 9600 baud over that shared UART. Ours was
  reflashed last time and no longer serves camera video. **Unplug it, or set its switch to
  Upload, so the Jetson owns the Uno's serial over USB.**
- Stock Uno firmware speaks ELEGOO's JSON protocol (`{"N":...}` commands at 9600 baud). Its
  speed command has no timer on the Uno, so a dropped link keeps the motors running. That is
  why #3 writes our own small sketch with a 300 ms watchdog. Stock protocol is the fallback if
  firmware time runs out (then the Jetson lease is the only stop).
- Motor pack is 2x 18650. Do not power the Jetson from it: motor current dips brown it out.
  Jetson Orin Nano takes 7-20 V at the barrel jack (or a USB-C PD power bank with a 12 V+
  trigger cable).
- Camera: a USB webcam (or CSI camera) mounted on the pan head next to the ultrasonic sensor.
