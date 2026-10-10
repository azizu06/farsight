#!/usr/bin/env python3
"""Exercise every command of the Farsight Uno serial protocol.

Protocol (rover/farsight_uno/farsight_uno.ino, 115200 baud, newline terminated):
  M <l> <r>  -> OK     signed PWM -255..255, renews the 300 ms motor watchdog
  P <deg>    -> OK     pan servo 10..170
  R          -> R -1   (no range sensor is mounted; kept for protocol compatibility)
  anything else, or out-of-range values -> ERR command (and motors stop)
  line longer than 47 bytes             -> ERR line (rest of the line is dropped)

Default run is a dry run: no step can spin a wheel. Motion steps need
--allow-motion with the wheels lifted. The watchdog stop is not visible on the
serial link (the firmware has no motor status reply), so it is observed by the
operator pressing Enter when the wheels stop; --selftest uses a simulator that
knows the motor state.

  python rover/serial_test.py --port /dev/ttyACM0            # dry run
  python rover/serial_test.py --port /dev/ttyACM0 --allow-motion
  python rover/serial_test.py --selftest                      # no hardware
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

BAUD = 115200
WATCHDOG_MS = 300
MAX_LINE = 47  # firmware buffer is 48 bytes including the terminator
BOOT_DELAY_S = 2.0  # opening the port pulses DTR and resets the Uno


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


class Link:
    """Line-oriented wrapper over a pyserial-like port (write/read/timeout)."""

    def __init__(self, port, timeout_s: float = 0.5):
        self.port = port
        self.timeout_s = timeout_s

    def send(self, data: bytes) -> None:
        self.port.write(data)

    def readline(self) -> str:
        """One reply line without terminator, or '' on timeout."""
        deadline = time.monotonic() + self.timeout_s
        buf = bytearray()
        while time.monotonic() < deadline:
            b = self.port.read(1)
            if not b:
                continue
            if b == b"\n":
                return buf.decode("ascii", "replace").strip("\r")
            buf += b
        return ""

    def drain(self) -> None:
        self.port.reset_input_buffer()

    def ask(self, line: str) -> str:
        self.send(line.encode("ascii") + b"\n")
        return self.readline()


RANGE_RE = re.compile(r"^R -?\d+$")

# (name, line, matcher description, predicate)
CASES = [
    ("M 0 0", "M 0 0", "OK", lambda r: r == "OK"),
    ("P 90", "P 90", "OK", lambda r: r == "OK"),
    ("R", "R", "R <cm>", lambda r: bool(RANGE_RE.match(r))),
    ("invalid command", "X 1 2", "ERR command", lambda r: r == "ERR command"),
    ("M missing arg", "M 5", "ERR command", lambda r: r == "ERR command"),
    ("M trailing junk", "M 1 2 3", "ERR command", lambda r: r == "ERR command"),
    ("M over 255", "M 256 0", "ERR command", lambda r: r == "ERR command"),
    ("M under -255", "M 0 -256", "ERR command", lambda r: r == "ERR command"),
    ("P below 10", "P 9", "ERR command", lambda r: r == "ERR command"),
    ("P above 170", "P 171", "ERR command", lambda r: r == "ERR command"),
]

Observer = Callable[[], Optional[float]]  # returns monotonic time the wheels stopped


def run_cases(link: Link) -> list[Result]:
    results = []
    for name, line, want, ok in CASES:
        got = link.ask(line)
        results.append(Result(name, "PASS" if ok(got) else "FAIL",
                              f"sent {line!r} want {want!r} got {got!r}"))
    # Over-long line: ERR line once, remainder dropped, link recovers.
    long_line = "M " + "1" * (MAX_LINE + 20)
    got = link.ask(long_line)
    results.append(Result("over-long line", "PASS" if got == "ERR line" else "FAIL",
                          f"sent {len(long_line)} bytes want 'ERR line' got {got!r}"))
    extra = link.readline()
    after = link.ask("R")
    good = extra == "" and bool(RANGE_RE.match(after))
    results.append(Result("recover after over-long line", "PASS" if good else "FAIL",
                          f"stray reply {extra!r}, R -> {after!r}"))
    # Invalid input must leave motors stopped: finish by re-arming a zero command.
    got = link.ask("M 0 0")
    results.append(Result("M 0 0 (final)", "PASS" if got == "OK" else "FAIL", f"got {got!r}"))
    return results


def run_motion(link: Link, observer: Observer, watchdog_ms: int, tolerance_ms: int) -> list[Result]:
    got = link.ask("M 50 50")
    if got != "OK":
        link.ask("M 0 0")
        return [Result("M 50 50 + watchdog", "FAIL", f"want 'OK' got {got!r}")]
    t0 = time.monotonic()  # serial silence starts now
    stopped = observer()
    link.ask("M 0 0")
    if stopped is None:
        return [Result("watchdog stop", "FAIL", "wheels never reported stopped")]
    ms = (stopped - t0) * 1000
    limit = watchdog_ms + tolerance_ms
    status = "PASS" if watchdog_ms * 0.5 <= ms <= limit else "FAIL"
    return [Result("watchdog stop", status,
                   f"stopped {ms:.0f} ms after last M (watchdog {watchdog_ms} ms, "
                   f"limit {limit} ms)")]


def operator_observer() -> Optional[float]:
    """Operator watches the raised wheels and presses Enter when they stop."""
    print(">>> Wheels should spin. Press Enter the instant BOTH stop (Ctrl-D if they never do).")
    try:
        input()
    except EOFError:
        return None
    return time.monotonic()


def run(port, allow_motion: bool, observer: Observer, tolerance_ms: int,
        watchdog_ms: int = WATCHDOG_MS, out=print, reply_timeout_s: float = 0.5) -> int:
    link = Link(port, reply_timeout_s)
    link.drain()
    results = run_cases(link)
    if allow_motion:
        results += run_motion(link, observer, watchdog_ms, tolerance_ms)
    else:
        results.append(Result("watchdog stop", "SKIP", "motion disabled; pass --allow-motion"))
    for r in results:
        out(f"{r.status:4} {r.name}: {r.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    out(f"{len(results) - len(failed)} ok, {len(failed)} failed, "
        f"{sum(r.status == 'SKIP' for r in results)} skipped")
    return 1 if failed else 0


class FakeUno:
    """In-process model of farsight_uno.ino's serial and watchdog behavior.

    Implements the pyserial subset used here. Time-dependent state (watchdog,
    partial-line timeout) is evaluated lazily against time.monotonic().
    """

    def __init__(self):
        self.pan = 90
        self._left = self._right = 0
        self._lease = False
        self._last_motor = 0.0
        self._line = bytearray()
        self._partial_at = 0.0
        self._discard = False
        self._out = bytearray()
        self._lock = threading.Lock()

    @property
    def motors(self) -> tuple[int, int]:
        with self._lock:
            self._tick()
            return self._left, self._right

    def _tick(self) -> None:
        now = time.monotonic()
        if self._lease and now - self._last_motor >= WATCHDOG_MS / 1000:
            self._stop()
        if self._line and now - self._partial_at > 0.05:
            self._stop()
            self._line.clear()
            self._discard = True

    def _stop(self) -> None:
        self._left = self._right = 0
        self._lease = False

    def _reply(self, text: str) -> None:
        self._out += text.encode() + b"\n"

    @staticmethod
    def _ints(args: str, count: int) -> Optional[list[int]]:
        # strtol-style: fields are space separated, trailing spaces/CR allowed.
        parts = args.replace("\r", " ").split()
        if len(parts) != count or not args.startswith(" "):
            return None
        try:
            return [int(p) for p in parts]
        except ValueError:
            return None

    def _command(self, text: str) -> None:
        if text.startswith("M"):
            v = self._ints(text[1:], 2)
            if v and all(-255 <= x <= 255 for x in v):
                self._last_motor = time.monotonic()
                self._lease = True
                self._left, self._right = v
                return self._reply("OK")
        elif text.startswith("P"):
            v = self._ints(text[1:], 1)
            if v and 10 <= v[0] <= 170:
                self.pan = v[0]
                return self._reply("OK")
        elif text.rstrip(" \r") == "R":
            return self._reply("R -1")
        self._stop()
        self._reply("ERR command")

    # -- pyserial subset ------------------------------------------------
    def write(self, data: bytes) -> int:
        with self._lock:
            self._tick()
            for byte in data:
                c = chr(byte)
                if c == "\n":
                    if not self._discard and self._line:
                        self._command(self._line.decode("ascii", "replace"))
                    self._line.clear()
                    self._discard = False
                elif not self._discard:
                    if len(self._line) >= MAX_LINE or byte == 0:
                        self._stop()
                        self._line.clear()
                        self._discard = True
                        self._reply("ERR line")
                    else:
                        self._line.append(byte)
                        self._partial_at = time.monotonic()
        return len(data)

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            chunk, self._out = bytes(self._out[:n]), self._out[n:]
        if not chunk:
            time.sleep(0.001)
        return chunk

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._out.clear()


def sim_observer(uno: FakeUno) -> Observer:
    def wait() -> Optional[float]:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if uno.motors == (0, 0):
                return time.monotonic()
            time.sleep(0.001)
        return None
    return wait


def selftest() -> int:
    ok = True
    print("selftest: dry run")
    ok &= run(FakeUno(), False, lambda: None, 100) == 0
    print("selftest: motion + watchdog (simulated Uno)")
    uno = FakeUno()
    ok &= run(uno, True, sim_observer(uno), 100) == 0
    print("selftest: detects a broken Uno (watchdog never fires)")
    class Stuck(FakeUno):
        def _tick(self):  # no watchdog
            pass
    stuck = Stuck()
    ok &= run(stuck, True, sim_observer(stuck), 100, out=lambda *_: None) == 1
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial device, e.g. /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--allow-motion", action="store_true",
                    help="send M 50 50 and time the watchdog stop (wheels must be raised)")
    ap.add_argument("--tolerance-ms", type=int, default=500,
                    help="slack over the 300 ms watchdog; covers operator reaction time (default 500)")
    ap.add_argument("--selftest", action="store_true", help="run against the built-in simulator")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.port:
        ap.error("--port is required unless --selftest")
    import serial  # pyserial; imported late so --selftest and unit tests need nothing

    with serial.Serial(args.port, args.baud, timeout=0.05) as port:
        time.sleep(BOOT_DELAY_S)
        if args.allow_motion:
            print("Motion enabled: wheels must be off the ground.")
        return run(port, args.allow_motion, operator_observer, args.tolerance_ms)


if __name__ == "__main__":
    sys.exit(main())
