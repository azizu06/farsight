"""Rig-day drive calibration: measure the rover from the ARKit pose, write a profile.

python -m tools.calibrate_drive --url https://JETSON:8443 --out drive-profile.json   # hold SPACE to drive
python -m tools.calibrate_drive --url ... --auto-permit                              # explicit unattended hold
python -m tools.calibrate_drive --dry-run                                            # print the pattern, touch nothing
python -m tools.calibrate_drive --simulate --out /tmp/sim-profile.json               # fitting math on a virtual rover

The server must run with `--allow-calibration` (raw-PWM `mode: "calibrate"`; it
still needs a held deadman, a server-issued permit, a fresh camera and the
forward range stop). The rover needs its wheels down and >= 3 m of clear floor.
Patterns: a per-side minimum-moving-PWM ramp, straight runs at several PWM levels
(each followed by a reverse return leg), and in-place spins both ways. Speeds come
from /health `transform` (ARKit world, Y up): along-track slope for m/s, unwrapped
yaw slope for rad/s, drift for left/right trim, chord vs camera heading for the
camera mount yaw. Pose time is `now - age_ms` of a polled /health, so rates are
good to roughly 50 ms of jitter; runs are seconds long. Output is
`jetson.drive_profile.DriveProfile` JSON (+ `<out>.runs.json` raw evidence).
Load it with `python -m jetson.server --drive-profile drive-profile.json`.
"""

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass, asdict
import json
import math
from pathlib import Path
import random
import select
import sys
import threading
import time

from jetson.drive_profile import (DriveProfile, SpinRun, StraightRun, circular_mean, fit_profile,
                                  linear_fit, unwrap, wrap_angle)

TICK = 0.05
MIN_TRAVEL_M = 0.03
MIN_TURN_RAD = 0.06


class CalibrationError(RuntimeError):
    pass


class PatternAborted(CalibrationError):
    pass


class DeadmanReleased(PatternAborted):
    pass


@dataclass(frozen=True)
class Sample:
    t: float
    x: float
    z: float
    yaw: float  # camera heading, CCW positive seen from above


# -- ARKit pose math ----------------------------------------------------------

def heading(x, z):
    """Angle of a horizontal (x, z) vector; increases counter-clockwise from above (Y up)."""
    return math.atan2(-z, x)


def sample_from_transform(t, transform):
    """transform: 16 numbers, column-major, ARKit camera-to-world. Camera looks along -Z."""
    fx, fz = -transform[8], -transform[10]
    if math.hypot(fx, fz) >= 0.2:
        yaw = heading(fx, fz)
    else:  # camera pitched near vertical: use the right axis, forward is 90 degrees CCW of it
        rx, rz = transform[0], transform[2]
        if math.hypot(rx, rz) < 0.2:
            raise ValueError("Camera orientation has no usable horizontal axis")
        yaw = wrap_angle(heading(rx, rz) + math.pi / 2)
    return Sample(t, transform[12], transform[14], yaw)


def window(samples, settle):
    if not samples:
        return []
    start = samples[0].t + settle
    return [s for s in samples if s.t >= start]


def moved(samples, settle=0.3):
    """True if the rover visibly translated or turned over the run."""
    use = window(samples, settle) or samples
    if len(use) < 2:
        return False
    travel = math.hypot(use[-1].x - use[0].x, use[-1].z - use[0].z)
    turn = abs(unwrap([s.yaw for s in use])[-1] - unwrap([s.yaw for s in use])[0])
    return travel >= MIN_TRAVEL_M or turn >= MIN_TURN_RAD


def analyze_straight(samples, pwm, settle=0.6):
    """StraightRun, or None if the run did not translate or has too little data."""
    use = window(samples, settle)
    if len(use) < 5 or use[-1].t - use[0].t < 0.5:
        return None
    dx, dz = use[-1].x - use[0].x, use[-1].z - use[0].z
    distance = math.hypot(dx, dz)
    if distance < MIN_TRAVEL_M:
        return None
    ux, uz = dx / distance, dz / distance
    along = [(s.x - use[0].x) * ux + (s.z - use[0].z) * uz for s in use]
    speed = linear_fit([s.t for s in use], along)[0]
    yaw_rate = linear_fit([s.t for s in use], unwrap([s.yaw for s in use]))[0]
    offset = wrap_angle(heading(ux, uz) - circular_mean([s.yaw for s in use]))
    if speed <= 0:
        return None
    return StraightRun(pwm, speed, yaw_rate, offset)


def analyze_spin(samples, pwm, settle=0.5):
    use = window(samples, settle)
    if len(use) < 5 or use[-1].t - use[0].t < 0.5:
        return None
    yaws = unwrap([s.yaw for s in use])
    if abs(yaws[-1] - yaws[0]) < MIN_TURN_RAD:
        return None
    return SpinRun(pwm, linear_fit([s.t for s in use], yaws)[0])


# -- plan ---------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    max_pwm: int = 100
    ramp_start: int = 15
    ramp_step: int = 5
    hold_s: float = 0.8
    levels: int = 4
    repeats: int = 2
    run_s: float = 2.0
    spin_s: float = 1.6
    rest_s: float = 0.5
    return_leg: bool = True

    def ramp_pwms(self):
        return list(range(self.ramp_start, self.max_pwm + 1, self.ramp_step))

    def run_levels(self, floor):
        low = min(self.max_pwm, -(-int(math.ceil(floor * 1.1)) // 5) * 5)
        if self.levels < 2 or low >= self.max_pwm:
            return [self.max_pwm]
        return sorted({round(low + (self.max_pwm - low) * i / (self.levels - 1)) for i in range(self.levels)})

    def describe(self):
        ramp = self.ramp_pwms()
        legs = 2 if self.return_leg else 1
        total = (2 * len(ramp) * (self.hold_s + self.rest_s)
                 + self.levels * self.repeats * (legs * self.run_s + self.rest_s)
                 + self.levels * self.repeats * (self.spin_s + self.rest_s))
        return [
            f"1. min-PWM ramp per side: one wheel only, {ramp[0]}..{ramp[-1]} step {self.ramp_step}, "
            f"{self.hold_s:g} s each (stops at first level that moves; forward range stop applies)",
            f"2. straight runs: {self.levels} PWM levels between 1.1x the higher floor and {self.max_pwm}, "
            f"x{self.repeats}, {self.run_s:g} s forward" + (" + reverse return leg" if self.return_leg else ""),
            f"3. in-place spins: same levels, alternating CCW/CW, x{self.repeats}, {self.spin_s:g} s",
            f"4. fit: PWM->m/s, PWM->rad/s, half-track, L/R trim, camera yaw offset, floors",
            f"about {total:.0f} s of driving; clear >= 3 m ahead; each run may roll up to ~{self.run_s * 0.8:.1f} m",
        ]


# -- rovers (live and simulated share this interface) --------------------------

class Rover:
    async def run(self, left, right, seconds):  # -> list[Sample]
        raise NotImplementedError

    async def rest(self, seconds):
        raise NotImplementedError


class SimulatedRover(Rover):
    """Virtual skid-steer rover with its own physics (independent of DriveProfile)."""

    def __init__(self, floors=(24, 31), slope=0.0065, offset_pwm=14, gains=(1.0, 0.94), half_track=0.115,
                 camera_yaw=0.35, noise=0.0015, seed=1):
        self.floors, self.slope, self.offset_pwm, self.gains = floors, slope, offset_pwm, gains
        self.half_track, self.camera_yaw = half_track, camera_yaw
        self.rng = random.Random(seed)
        self.t, self.x, self.z, self.theta = 0.0, 0.0, 0.0, 0.0  # theta: body heading, CCW
        self.noise = noise

    def wheel_speed(self, pwm, side):
        if abs(pwm) < self.floors[side]:
            return 0.0
        return math.copysign(self.slope * (abs(pwm) - self.offset_pwm) * self.gains[side], pwm)

    def sample(self):
        n = lambda: self.rng.gauss(0, self.noise)
        return Sample(self.t, self.x + n(), self.z + n(), wrap_angle(self.theta + self.camera_yaw))

    async def run(self, left, right, seconds):
        out = [self.sample()]
        steps = max(1, round(seconds / TICK))
        for _ in range(steps):
            vl, vr = self.wheel_speed(left, 0), self.wheel_speed(right, 1)
            v, w = (vl + vr) / 2, (vr - vl) / (2 * self.half_track)
            self.t += TICK
            self.theta += w * TICK
            # body forward in (x, z): heading() uses atan2(-z, x), so forward = (cos, -sin)
            self.x += v * math.cos(self.theta) * TICK
            self.z -= v * math.sin(self.theta) * TICK
            out.append(self.sample())
        return out

    async def rest(self, seconds):
        self.t += seconds


class KeyboardDeadman:
    """Held while key-repeat events keep arriving (terminal raw mode); released shortly after."""

    FIRST_HOLD = 0.9   # before key repeat starts
    REPEAT_HOLD = 0.35

    def __init__(self, stdin=sys.stdin):
        import termios
        import tty
        self.fd = stdin.fileno()
        self.termios = termios
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.first = self.last = None
        self.stopped = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while not self.stopped:
            if select.select([self.fd], [], [], 0.1)[0]:
                import os
                os.read(self.fd, 64)
                now = time.monotonic()
                if self.last is None or now - self.last > self.FIRST_HOLD:
                    self.first = now
                self.last = now

    def __call__(self):
        if self.last is None:
            return False
        age = time.monotonic() - self.last
        repeating = self.last - self.first > 0.05
        return age < (self.REPEAT_HOLD if repeating else self.FIRST_HOLD)

    def close(self):
        self.stopped = True
        self.termios.tcsetattr(self.fd, self.termios.TCSADRAIN, self.saved)


class LiveRover(Rover):
    """Drives through the real /ws/control path; pose and camera state from /health."""

    FAULTS_OK = {"active"}

    def __init__(self, origin, deadman, ca_file=None, insecure=False, log=print):
        from tools.netutil import tls_context
        self.origin, self.deadman, self.log = origin.rstrip("/"), deadman, log
        self.ssl = tls_context(ca_file, insecure)
        self.client = self.ws = None
        self.status = {}
        self.session_id = None
        self.seq = 0
        self.health = {}
        self.pose = []  # (t, Sample), appended by the poller
        self.tasks = []
        self.last_transform = None

    async def start(self):
        import aiohttp
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        public = await self._get("/control/status")
        if not public.get("rover"):
            raise CalibrationError(f"Rover not connected to the server (reason: {public.get('reason')})")
        if not public.get("calibration_allowed"):
            raise CalibrationError("Server not started with --allow-calibration")
        if public.get("simulated"):
            self.log("WARNING: server reports a SIMULATED rover; the profile will describe no real motors")
        self.max_pwm = int(public["max_pwm"])
        self.tasks.append(asyncio.create_task(self._poll_pose()))
        self.ws = await self.client.ws_connect(self.origin + "/ws/control", ssl=self.ssl, heartbeat=None)
        first = await asyncio.wait_for(self.ws.receive_json(), 2)
        self.status = first
        self.session_id = first.get("session_id")
        if not self.session_id:
            raise CalibrationError("Control server did not supply a session_id")
        self.seq = int(first.get("seq", -1)) + 1
        self.tasks.append(asyncio.create_task(self._read_status()))
        await self._release()
        await self._wait_pose()

    async def close(self):
        if self.ws is not None and not self.ws.closed:
            with suppress(Exception):
                await self._release()
        for task in self.tasks:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self.ws is not None:
            with suppress(Exception):
                await self.ws.close()
        if self.client is not None:
            await self.client.close()

    async def _get(self, path):
        async with self.client.get(self.origin + path, ssl=self.ssl) as response:
            response.raise_for_status()
            return await response.json()

    async def _read_status(self):
        import aiohttp
        async for message in self.ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                self.status = json.loads(message.data)

    async def _poll_pose(self):
        import aiohttp
        while True:
            started = time.monotonic()
            try:
                health = await self._get("/health")
                self.health = health
                age = health.get("age_ms")
                transform = health.get("transform")
                if (health.get("fresh") and health.get("tracking") == "normal" and age is not None
                        and transform and transform != self.last_transform):
                    self.last_transform = transform
                    self.pose.append(sample_from_transform(time.monotonic() - age / 1000, transform))
                    del self.pose[:-4000]
            except (OSError, ValueError, asyncio.TimeoutError, aiohttp.ClientError):
                pass
            await asyncio.sleep(max(0, 0.05 - (time.monotonic() - started)))

    async def _wait_pose(self):
        for _ in range(60):
            if self.pose:
                return
            await asyncio.sleep(0.1)
        raise CalibrationError("No fresh ARKit pose with tracking=normal from /health; start the phone stream")

    async def _send(self, payload):
        self.seq += 1
        await self.ws.send_json({"session_id": self.session_id, "seq": self.seq, **payload})

    async def _release(self):
        await self._send({"deadman": False, "v": 0, "w": 0})

    async def wait_deadman(self):
        while not self.deadman():
            await asyncio.sleep(0.05)

    async def run(self, left, right, seconds):
        await self.wait_deadman()
        for _ in range(100):
            if self.status.get("permit"):
                break
            await asyncio.sleep(0.02)
        started = time.monotonic()
        first_seq = self.seq + 1
        mark = len(self.pose)
        try:
            while time.monotonic() - started < seconds:
                if not self.deadman():
                    raise DeadmanReleased("deadman released")
                tracking = self.health.get("tracking")
                if tracking != "normal" or not self.health.get("fresh"):
                    raise PatternAborted(f"ARKit pose lost (tracking={tracking})")
                status = self.status
                if status.get("seq", -1) >= first_seq and status.get("reason") not in self.FAULTS_OK:
                    raise PatternAborted(f"server stopped the rover: {status.get('reason')}")
                age = self.health.get("age_ms")
                await self._send({"deadman": True, "permit": status.get("permit"), "mode": "calibrate",
                                  "left_pwm": int(left), "right_pwm": int(right), "pan": 0, "v": 0, "w": 0,
                                  "video_fresh": bool(self.health.get("camera_drive_ready", self.health.get("camera"))),
                                  "video_age_ms": age if age is not None else 999})
                await asyncio.sleep(TICK)
        finally:
            with suppress(Exception):
                await self._release()
        return [s for s in self.pose[mark - 1 if mark else 0:] if s.t >= started - 0.1]

    async def rest(self, seconds):
        await asyncio.sleep(seconds)


# -- calibration procedure ------------------------------------------------------

async def attempt(rover, left, right, seconds, wait=None, tries=5, log=print):
    """Run one pattern, resuming from scratch after a deadman release."""
    for n in range(tries):
        try:
            return await rover.run(left, right, seconds)
        except DeadmanReleased:
            log("  deadman released: rover stopped. Hold it to repeat this pattern.")
            if wait is None:
                raise
            await wait()
    raise CalibrationError("Too many deadman releases")


async def find_floor(rover, plan, side, wait=None, log=print):
    """Lowest PWM at which a single wheel visibly moves the rover."""
    for pwm in plan.ramp_pwms():
        left, right = (pwm, 0) if side == "left" else (0, pwm)
        samples = await attempt(rover, left, right, plan.hold_s, wait, log=log)
        await rover.rest(plan.rest_s)
        log(f"  {side} wheel {pwm:>3}: {'moved' if moved(samples) else '-'}")
        if moved(samples):
            return pwm
    raise CalibrationError(f"{side} wheel never moved up to PWM {plan.max_pwm}; check power/wiring or raise --max-pwm")


async def calibrate(rover, plan, wait=None, log=print, meta=None):
    log("1/3 minimum moving PWM")
    floors = {side: await find_floor(rover, plan, side, wait, log) for side in ("left", "right")}
    levels = plan.run_levels(max(floors.values()))
    log(f"  floors {floors}; test levels {levels}")
    raw = {"floors": floors, "levels": levels, "straight": [], "spins": [], "reverse_ratio": []}
    straight, spins = [], []
    log("2/3 straight runs")
    for pwm in levels:
        for rep in range(plan.repeats):
            samples = await attempt(rover, pwm, pwm, plan.run_s, wait, log=log)
            run = analyze_straight(samples, pwm)
            await rover.rest(plan.rest_s)
            if plan.return_leg:
                back = await attempt(rover, -pwm, -pwm, plan.run_s, wait, log=log)
                await rover.rest(plan.rest_s)
                ret = analyze_straight([Sample(s.t, s.x, s.z, s.yaw) for s in back], pwm)
                if run and ret:
                    raw["reverse_ratio"].append(ret.speed_mps / run.speed_mps)
            log(f"  pwm {pwm:>3} #{rep + 1}: " + (f"{run.speed_mps:.3f} m/s, drift {run.yaw_rate_radps:+.3f} rad/s"
                                                  if run else "no usable motion"))
            if run:
                straight.append(run)
            raw["straight"].append(asdict(run) if run else {"pwm": pwm, "failed": True})
    log("3/3 in-place spins")
    for pwm in levels:
        for rep in range(plan.repeats):
            sign = 1 if rep % 2 == 0 else -1  # +1 = counter-clockwise (left wheel back)
            samples = await attempt(rover, -sign * pwm, sign * pwm, plan.spin_s, wait, log=log)
            run = analyze_spin(samples, pwm)
            await rover.rest(plan.rest_s)
            log(f"  pwm {pwm:>3} {'CCW' if sign > 0 else 'CW '}: " + (f"{run.rate_radps:+.3f} rad/s" if run else "no usable rotation"))
            if run:
                spins.append(run)
            raw["spins"].append(asdict(run) if run else {"pwm": pwm, "failed": True})
    ratios = raw["reverse_ratio"]
    info = dict(meta or {})
    info["reverse_over_forward_speed"] = round(sum(ratios) / len(ratios), 3) if ratios else None
    info["plan"] = asdict(plan)
    profile = fit_profile(floors["left"], floors["right"], straight, spins, plan.max_pwm, meta=info)
    return profile, raw


def summary(profile):
    return [
        f"min moving PWM  left {profile.min_pwm_left}  right {profile.min_pwm_right}",
        f"PWM -> m/s      " + ", ".join(f"{p:g}:{v:.3f}" for p, v in zip(profile.linear_pwm, profile.linear_mps)),
        f"PWM -> rad/s    " + ", ".join(f"{p:g}:{v:.3f}" for p, v in zip(profile.angular_pwm, profile.angular_radps)),
        f"half track      {profile.half_track_m:.3f} m (effective, includes skid slip)",
        f"trim (speed)    left {profile.trim_left:.3f}  right {profile.trim_right:.3f}",
        f"camera yaw      {math.degrees(profile.camera_yaw_offset_rad):+.1f} deg (travel direction vs camera forward, +CCW)",
        f"limits          {profile.max_linear_mps():.3f} m/s, {profile.max_angular_radps():.3f} rad/s at PWM {profile.max_pwm}",
    ]


def write_outputs(profile, raw, out):
    out = Path(out)
    profile.save(out)
    out.with_suffix(".runs.json").write_text(json.dumps(raw, indent=2) + "\n")


def parser():
    p = argparse.ArgumentParser(description="Measure the rover's drive response and write a profile")
    p.add_argument("--url", help="Jetson origin, e.g. https://jetson.local:8443")
    p.add_argument("--out", default="drive-profile.json")
    p.add_argument("--ca-file")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification (self-signed Jetson cert)")
    p.add_argument("--auto-permit", action="store_true",
                   help="Hold the permit for the whole sequence without a held key. Only with the floor cleared "
                        "and a person at the power switch")
    p.add_argument("--max-pwm", type=int, help="Test ceiling; default the server's --max-pwm")
    p.add_argument("--levels", type=int, default=4)
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--run-seconds", type=float, default=2.0)
    p.add_argument("--spin-seconds", type=float, default=1.6)
    p.add_argument("--ramp-start", type=int, default=15)
    p.add_argument("--ramp-step", type=int, default=5)
    p.add_argument("--no-return", action="store_true", help="Skip reverse return legs (reposition by hand)")
    p.add_argument("--dry-run", action="store_true", help="Print the pattern; contact nothing")
    p.add_argument("--simulate", action="store_true", help="Fit against a virtual rover (no server, no hardware)")
    return p


def make_plan(args, max_pwm):
    return Plan(max_pwm=max_pwm, ramp_start=args.ramp_start, ramp_step=args.ramp_step, levels=args.levels,
                repeats=args.repeats, run_s=args.run_seconds, spin_s=args.spin_seconds,
                return_leg=not args.no_return)


async def main_async(args, log=print):
    if args.dry_run:
        plan = make_plan(args, args.max_pwm or 100)
        log("DRY RUN - nothing is contacted or driven. Pattern:")
        for line in plan.describe():
            log("  " + line)
        return 0
    if args.simulate:
        plan = make_plan(args, args.max_pwm or 100)
        profile, raw = await calibrate(SimulatedRover(), plan, log=log, meta={"source": "simulated"})
        write_outputs(profile, raw, args.out)
        log("\n".join(summary(profile)))
        log(f"SIMULATED profile written to {args.out}; do not load it on a real rover")
        return 0
    if not args.url:
        raise SystemExit("--url is required (or use --dry-run / --simulate)")
    deadman = None
    if args.auto_permit:
        deadman = lambda: True
        log("--auto-permit: holding the permit for the whole sequence. Keep a hand on the power switch.")
    else:
        if not sys.stdin.isatty():
            raise SystemExit("A held-key deadman needs a terminal; run interactively or pass --auto-permit")
        deadman = KeyboardDeadman()
        log("HOLD the SPACE bar to drive; releasing stops the rover and pauses the sequence.")
    rover = LiveRover(args.url, deadman, args.ca_file, args.insecure, log)
    try:
        await rover.start()
        plan = make_plan(args, min(args.max_pwm or rover.max_pwm, rover.max_pwm))
        for line in plan.describe():
            log("  " + line)
        profile, raw = await calibrate(rover, plan, rover.wait_deadman, log,
                                       meta={"source": "live", "url": args.url, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        write_outputs(profile, raw, args.out)
        log("\n".join(summary(profile)))
        log(f"profile written to {args.out} (raw runs: {Path(args.out).with_suffix('.runs.json')})")
        return 0
    except CalibrationError as exc:
        log(f"FAILED: {exc}")
        return 1
    finally:
        await rover.close()
        if hasattr(deadman, "close"):
            deadman.close()


def main():
    args = parser().parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
