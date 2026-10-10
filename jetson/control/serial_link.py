"""Dedicated serial pump: a blocked HTTP/inference worker cannot renew motion."""

import logging
import threading
import time

LOG = logging.getLogger(__name__)


class SerialPump:
    def __init__(self, state, port=None, simulate=False, factory=None, simulated_range=None):
        if port and simulate:
            raise ValueError("Choose a real serial port OR simulated rover")
        self.state, self.port, self.simulate, self.factory = state, port, simulate, factory
        # Simulated ultrasonic in cm (None = no echo); a fixed clear 120 cm without a world.
        self.simulated_range = simulated_range
        self.quit = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is not None:
            return
        if not self.port and not self.simulate:
            self.state.stop("rover_disabled")
            return
        self.thread = threading.Thread(target=self.run, name="rover-serial", daemon=True)
        self.thread.start()

    def close(self):
        self.state.stop("shutdown")
        self.quit.set()
        if self.thread:
            self.thread.join(timeout=3)

    def run(self):
        if self.simulate:
            LOG.warning("SIMULATED ROVER: no motor hardware is connected")
            self.state.set_connected(True)
            while not self.quit.is_set():
                try:
                    cm = self.simulated_range() if self.simulated_range else 120
                except Exception:
                    cm = None
                self.state.update_range(-1 if cm is None else cm)
                self.state.output()
                self.quit.wait(0.05)
            self.state.set_connected(False, "shutdown")
            return
        while not self.quit.is_set():
            device = None
            try:
                factory = self.factory
                if factory is None:
                    import serial
                    factory = serial.Serial
                # Non-blocking writes: pyserial's write_timeout fires on GIL stalls even
                # after bytes queue; Uno watchdog and reply timeout prove liveness.
                device = factory(self.port, 115200, timeout=0, write_timeout=0)

                def send(data):
                    if getattr(device, "out_waiting", 0) > 256:
                        raise OSError("Uno serial output stalled")
                    count = device.write(data)
                    if count is not None and count != len(data):
                        raise OSError("Uno serial write incomplete")
                # Uno's USB open can reset its bootloader; no motion survives
                # opening or reconnect. Wait without blocking the web server.
                if self.quit.wait(2):
                    break
                device.reset_input_buffer()
                device.reset_output_buffer()
                servo = self.state.pan_mount == "servo"
                send(b"M 0 0\nP 90\nR\n" if servo else b"M 0 0\nR\n")
                pending, started = bytearray(), time.monotonic()
                last_response, unanswered = started, 0
                next_tick, next_range, last_pan, settle_at = started, started, 90, started
                ready = False
                while not self.quit.is_set():
                    now = time.monotonic()
                    chunk = device.read(min(getattr(device, "in_waiting", 0), 256))
                    pending.extend(chunk)
                    if len(pending) > 1024:
                        raise OSError("Uno sent an overlong serial reply")
                    while b"\n" in pending:
                        raw, _, pending = pending.partition(b"\n")
                        reply = bytes(raw).decode("ascii", errors="replace").strip()
                        if reply == "OK" or reply.startswith("R "):
                            last_response, unanswered = now, 0
                        if not ready and reply == "OK":
                            self.state.set_connected(True)
                            ready = True
                        if ready and reply.startswith("R ") and now >= settle_at:
                            try:
                                self.state.update_range(float(reply[2:]))
                            except ValueError:
                                self.state.update_range(-1)
                        if reply.startswith("ERR"):
                            self.state.stop("uno_rejected_command")
                    # Silence only counts while commands go out: a GIL-starved pump sends
                    # nothing, and blaming the Uno costs a 2 s port reopen and bootloader reset.
                    if now - last_response > (0.5 if ready else 2) and (unanswered >= 4 or not ready):
                        raise OSError("Uno response timeout")
                    if now >= next_tick:
                        left, right, pan = self.state.output() if ready else (0, 0, 90)
                        # Commands are never buffered for replay; recompute from
                        # monotonic lease immediately before every serial write.
                        data = f"M {left} {right}\n"
                        if servo and pan != last_pan:
                            data += f"P {pan}\n"
                            last_pan, settle_at = pan, now + 0.2
                        if now >= next_range:
                            data += "R\n"
                            next_range = now + 0.1
                        send(data.encode("ascii"))
                        unanswered += 1
                        next_tick = now + 0.05
                    self.quit.wait(0.005)
            except Exception as exc:
                LOG.warning("Rover serial disconnected: %s", exc)
                self.state.set_connected(False, "serial_disconnected")
            finally:
                self.state.set_connected(False, "shutdown" if self.quit.is_set() else "serial_disconnected")
                if device:
                    try:
                        device.write(b"M 0 0\n")
                    except Exception:
                        pass
                    device.close()
            self.quit.wait(1)
