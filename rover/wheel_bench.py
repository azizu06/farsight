#!/usr/bin/env python3
"""Lifted-wheel bench steps for the Farsight Uno, for an observer who reports back.

Every step prints exactly what it commands and keeps renewing the 300 ms motor
watchdog while a side spins, then sends M 0 0. PWM is capped at --cap (default
and maximum 120). Wheels must be off the ground.

  python rover/wheel_bench.py --port /dev/ttyUSB0 sides --pwm 80 --seconds 2
  python rover/wheel_bench.py --port /dev/ttyUSB0 ramp --side left --dir rev --start 30 --stop 100 --step 10
  python rover/wheel_bench.py --port /dev/ttyUSB0 watchdog --pwm 80
"""
from __future__ import annotations

import argparse
import sys
import time

from serial_test import BOOT_DELAY_S, Link

MAX_CAP = 120
RENEW_S = 0.1


def wheels(side: str, direction: str, pwm: int) -> tuple[int, int]:
    value = pwm if direction == "fwd" else -pwm
    return (value if side in ("left", "both") else 0, value if side in ("right", "both") else 0)


def spin(link: Link, side: str, direction: str, pwm: int, seconds: float, cap: int, out=print) -> bool:
    """Run one side (or both) for `seconds`; returns False if the Uno refused a command."""
    pwm = min(pwm, cap)
    left, right = wheels(side, direction, pwm)
    label = f"{side} {'forward' if direction == 'fwd' else 'reverse'} at PWM {pwm} for {seconds:g} s (M {left} {right})"
    out(f"RUN  {label}")
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            reply = link.ask(f"M {left} {right}")
            if reply != "OK":
                out(f"FAIL {label}: M got {reply!r}")
                return False
            time.sleep(RENEW_S)
    finally:
        link.ask("M 0 0")
    out("STOP M 0 0")
    return True


def pause(seconds: float, out=print) -> None:
    out(f"     pause {seconds:g} s")
    time.sleep(seconds)


def cmd_sides(link: Link, a, out=print) -> None:
    steps = [("left", "fwd"), ("right", "fwd"), ("left", "rev"), ("right", "rev")]
    for n, (side, direction) in enumerate(steps, 1):
        out(f"step {n}/{len(steps)}")
        spin(link, side, direction, a.pwm, a.seconds, a.cap, out)
        if n < len(steps):
            pause(a.gap, out)


def cmd_ramp(link: Link, a, out=print) -> None:
    levels = list(range(a.start, min(a.stop, a.cap) + 1, a.step))
    for n, pwm in enumerate(levels, 1):
        out(f"burst {n}/{len(levels)}")
        if not spin(link, a.side, a.dir, pwm, a.seconds, a.cap, out):
            return
        if n < len(levels):
            pause(a.gap, out)


def cmd_watchdog(link: Link, a, out=print) -> None:
    # One reverse command, then silence: the firmware must stop within 300 ms,
    # so the observer should see only a brief twitch, not a run of `silence` seconds.
    pwm = min(a.pwm, a.cap)
    out(f"RUN  both reverse at PWM {pwm}: single M -{pwm} -{pwm}, then no serial for {a.silence:g} s")
    try:
        reply = link.ask(f"M -{pwm} -{pwm}")
        out(f"     reply {reply!r}; silent now")
        time.sleep(a.silence)
    finally:
        link.ask("M 0 0")
    out("STOP M 0 0 (only now sent by host)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cap", type=int, default=MAX_CAP, help=f"PWM ceiling (<= {MAX_CAP})")
    ap.add_argument("--countdown", type=float, default=5.0, help="seconds before the first step")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sides", help="left fwd, right fwd, left rev, right rev")
    s.add_argument("--pwm", type=int, default=80)
    s.add_argument("--seconds", type=float, default=2.0)
    s.add_argument("--gap", type=float, default=4.0)
    r = sub.add_parser("ramp", help="numbered bursts at rising PWM on one side")
    r.add_argument("--side", choices=["left", "right", "both"], required=True)
    r.add_argument("--dir", choices=["fwd", "rev"], required=True)
    r.add_argument("--start", type=int, default=30)
    r.add_argument("--stop", type=int, default=100)
    r.add_argument("--step", type=int, default=10)
    r.add_argument("--seconds", type=float, default=2.0)
    r.add_argument("--gap", type=float, default=3.0)
    w = sub.add_parser("watchdog", help="one M then serial silence; wheels must stop on their own")
    w.add_argument("--pwm", type=int, default=80)
    w.add_argument("--silence", type=float, default=4.0)
    a = ap.parse_args(argv)
    if not 0 < a.cap <= MAX_CAP:
        ap.error(f"--cap must be 1..{MAX_CAP}")
    import serial  # pyserial

    with serial.Serial(a.port, a.baud, timeout=0.05) as port:
        time.sleep(BOOT_DELAY_S)
        link = Link(port)
        link.drain()
        if link.ask("M 0 0") != "OK":
            print("Uno did not answer M 0 0 with OK; is farsight_uno flashed?")
            return 1
        link.ask("P 90")
        print(f"Wheels must be off the ground. Starting in {a.countdown:g} s.")
        time.sleep(a.countdown)
        {"sides": cmd_sides, "ramp": cmd_ramp, "watchdog": cmd_watchdog}[a.cmd](link, a)
        link.ask("M 0 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
