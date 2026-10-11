"""Closed-loop navigation scenarios: jetson.server --sim-world + Nav2, driven by a held-deadman operator.

Every number comes from the simulator's ground truth (GET /sim/state) and the bridge status
(GET /nav/status detail.remote). Software evidence only: ray-cast depth, perfect odometry,
no motor deadband. Reports land in /out/scenarios.json.

python3 ros2/tests/scenarios.py [popup doorway deadend ...]   (inside farsight-nav:jazzy)
"""

import json
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from harness import OUT, Operator, Stack, main  # noqa: E402

RESULTS = {}
STILL = 0.02   # |v| and |w| below this (normalized) count as standing still


class CpuSampler:
    """Mean/max CPU (% of one core) per ROS/server process, from /proc, while a scenario runs."""

    NAMES = ("controller_server", "planner_server", "bt_navigator", "behavior_server", "bridge", "jetson.server")
    TICK = os.sysconf("SC_CLK_TCK")

    def __init__(self):
        self.last, self.samples = {}, {}

    def poll(self):
        now = time.monotonic()
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore")
                fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            name = next((n for n in self.NAMES if n in cmd), None)
            if name is None or "ros2 launch" in cmd:
                continue
            busy = (int(fields[11]) + int(fields[12])) / self.TICK
            if pid in self.last:
                then, before = self.last[pid]
                if now > then:
                    self.samples.setdefault(name, []).append(100 * (busy - before) / (now - then))
            self.last[pid] = (now, busy)

    def summary(self):
        return {name: dict(mean=round(statistics.fmean(v), 1), max=round(max(v), 1))
                for name, v in self.samples.items() if v}


class Run:
    """Samples /sim/state and /nav/status at 5 Hz while a scenario drives."""

    def __init__(self, stack, operator):
        self.stack, self.operator = stack, operator
        self.samples = []
        self.cpu = CpuSampler()

    def sample(self):
        self.cpu.poll()
        sim = self.stack.get("/sim/state") or {}
        nav = self.stack.get("/nav/status") or {}
        entry = dict(t=time.monotonic(), sim=sim, nav={k: nav.get(k) for k in ("state", "mode", "backend", "message")},
                     remote=(nav.get("detail") or {}).get("remote") or {}, path=len(nav.get("path") or []),
                     frontiers=len(nav.get("frontiers") or []))
        self.samples.append(entry)
        return entry

    def until(self, done, timeout, period=0.2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entry = self.sample()
            if done(entry):
                return entry
            time.sleep(period)
        return None

    def summary(self, goal=None, since=None):
        samples = [s for s in self.samples if s["sim"]]
        last = samples[-1]["sim"]
        rates = [s["remote"].get("controller_hz") for s in samples if s["remote"].get("controller_hz")]
        start = since if since is not None else samples[0]["t"]
        still, longest, previous = 0.0, 0.0, None
        for s in samples:
            if s["t"] < start:
                continue
            moving = abs(s["sim"].get("v", 0)) > STILL or abs(s["sim"].get("w", 0)) > STILL
            if previous is not None:
                still = 0.0 if moving else still + (s["t"] - previous)
                longest = max(longest, still)
            previous = s["t"]
        out = dict(collisions=last["collisions"], travelled_m=last["travelled_m"],
                   min_clearance_m=last["min_clearance_m"], recoveries=max((s["remote"].get("recoveries") or 0)
                                                                         for s in samples),
                   controller_hz_median=round(statistics.median(rates), 1) if rates else None,
                   controller_hz_min=round(min(rates), 1) if rates else None,
                   longest_standstill_s=round(longest, 2), final=[last["x"], last["z"]],
                   appeared=last.get("appeared"), coverage=last.get("coverage"),
                   seconds=round(samples[-1]["t"] - samples[0]["t"], 1))
        # Forward speed by clearance (captain: fast in open space, slower only in tight spots).
        for band, inside in (("open", lambda c: c > 1.0), ("tight", lambda c: c < 0.5)):
            speeds = [s["sim"]["v"] * s["sim"].get("speed_mps", 0.45) for s in samples
                      if s["t"] >= start and s["sim"].get("v", 0) > STILL
                      and isinstance(s["sim"].get("clearance_m"), (int, float)) and inside(s["sim"]["clearance_m"])]
            out[f"forward_mps_{band}"] = dict(n=len(speeds), median=round(statistics.median(speeds), 3) if speeds else None)
        if goal is not None:
            out["goal"] = list(goal)
            out["final_error_m"] = round(math.dist(goal, (last["x"], last["z"])), 3)
        return out


def timeline(name, run):
    """Per-sample ground truth and nav state, for plots and diagnosis (/out/<name>-timeline.json)."""
    start = run.samples[0]["t"]
    rows = [dict(t=round(s["t"] - start, 2), x=s["sim"].get("x"), z=s["sim"].get("z"), yaw=s["sim"].get("yaw_deg"),
                 v=s["sim"].get("v"), w=s["sim"].get("w"), state=s["nav"].get("state"), path=s["path"],
                 hz=s["remote"].get("controller_hz"), recoveries=s["remote"].get("recoveries"))
            for s in run.samples if s["sim"]]
    last = run.samples[-1]["sim"]
    with open(os.path.join(OUT, f"{name}-timeline.json"), "w") as handle:
        json.dump(dict(rows=rows, trace=last.get("trace"), obstacles=last.get("obstacles"),
                       people=last.get("people")), handle)


def record(name, result):
    RESULTS[name] = result
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "scenarios.json"), "w") as handle:
        json.dump(RESULTS, handle, indent=2)
    return result


def goto(world, goal, timeout, launch_args=()):
    """POST /nav/goal while holding the deadman, then hold `goto` until Nav2 reports arrived."""
    label = world + "".join("_" + a.split(":=")[-1] for a in launch_args)
    with Stack(label, ["--sim-world", world, "--proximity-stop", "off"], launch_args) as stack:
        stack.nav_ready()
        with Operator(stack.url) as operator:
            stack.wait(lambda: operator.status.get("reason") == "active", 10, "operator lease never went active")
            status, body = stack.request("/nav/goal", dict(x=goal[0], z=goal[1]))
            if status != 202:
                raise AssertionError(f"/nav/goal {status}: {body}")
            operator.mode = "goto"
            run = Run(stack, operator)
            started = time.monotonic()
            end = run.until(lambda s: s["nav"]["state"] in ("arrived", "failed")
                            or (s["sim"].get("collisions") or 0) > 0, timeout)
            result = run.summary(goal)
            result.update(state=(end or run.samples[-1])["nav"]["state"], timed_out=end is None,
                          to_goal_s=round(time.monotonic() - started, 1), cpu_percent=run.cpu.summary())
            popped = [s["t"] for s in run.samples if s["sim"].get("appeared")]
            if popped:
                result["after_popup"] = run.summary(goal, since=popped[0])
            timeline(label, run)
            return stack, result


def check_goto(name, result, max_error=0.35):
    if result["timed_out"]:
        raise AssertionError(f"timed out: {json.dumps(result)}")
    if result["state"] != "arrived" or result["final_error_m"] > max_error:
        raise AssertionError(f"did not arrive: {json.dumps(result)}")
    if result["collisions"]:
        raise AssertionError(f"collided: {json.dumps(result)}")


GOTO = dict(  # world: (goal x, z), timeout s
    popup=((0.0, -6.5), 90),
    doorway=((-1.2, -5.5), 120),
    deadend=((0.0, -5.5), 240),
)


def goto_check(world, controller):
    """Drive to the world's goal with one controller; world-specific pass criteria below."""
    def check():
        goal, timeout = GOTO[world]
        name = world if controller == "rpp" else f"{world}_{controller}"
        _, result = goto(world, goal, timeout, [f"controller:={controller}"])
        result["controller"] = controller
        record(name, result)
        check_goto(name, result)
        detail = (f"{controller.upper()} arrived {result['final_error_m']} m from goal in {result['to_goal_s']} s, "
                  f"{result['travelled_m']} m driven, recoveries {result['recoveries']}, "
                  f"min clearance {result['min_clearance_m']} m, controller {result['controller_hz_median']} Hz median")
        if world == "popup":
            # An obstacle appears 1.6 m ahead in a 2 m corridor: steer around it without stopping.
            after = result.get("after_popup")
            if not after:
                raise AssertionError("the obstacle never appeared")
            if after["longest_standstill_s"] > 0.5:
                raise AssertionError(f"stood still {after['longest_standstill_s']} s after the pop-up")
            detail += f"; longest standstill after the pop-up {after['longest_standstill_s']} s"
        if world == "deadend" and not result["appeared"]:
            # The straight corridor closes once the rover is inside; it must back out and take the side gap.
            raise AssertionError("the corridor never closed")
        return detail
    return check


def hold(world, mode, seconds, done, target_id=None, launch_args=()):
    """Hold the deadman in an autonomy mode (no goal POST) and sample until done(entry) or timeout."""
    label = world + "".join("_" + a.split(":=")[-1] for a in launch_args)
    with Stack(label, ["--sim-world", world, "--proximity-stop", "off"], launch_args) as stack:
        stack.nav_ready()
        with Operator(stack.url) as operator:
            stack.wait(lambda: operator.status.get("reason") == "active", 10, "operator lease never went active")
            operator.target_id = target_id
            operator.mode = mode
            run = Run(stack, operator)
            started = time.monotonic()
            end = run.until(lambda s: done(s) or (s["sim"].get("collisions") or 0) > 0, seconds)
            result = run.summary()
            result.update(state=run.samples[-1]["nav"]["state"], seconds=round(time.monotonic() - started, 1),
                          finished=end is not None, cpu_percent=run.cpu.summary(),
                          modes=sorted({s["nav"]["mode"] for s in run.samples if s["nav"]["mode"]}),
                          backend=run.samples[-1]["nav"]["backend"])
            timeline(label, run)
            return run, result


def explore_check(controller):
    def check():
        name = "explore" if controller == "rpp" else f"explore_{controller}"
        run, result = hold("floorplan", "explore", 240, lambda s: (s["sim"].get("coverage") or 0) >= 0.9
                           or s["nav"]["state"] == "arrived", launch_args=[f"controller:={controller}"])
        curve = {}
        for s in run.samples:  # first time each coverage level was reached
            for level in (0.5, 0.7, 0.8, 0.9):
                if level not in curve and (s["sim"].get("coverage") or 0) >= level:
                    curve[level] = round(s["t"] - run.samples[0]["t"], 1)
        result.update(controller=controller, coverage_seconds={str(k): v for k, v in curve.items()},
                      max_frontiers=max(s["frontiers"] for s in run.samples))
        record(name, result)
        if result["collisions"]:
            raise AssertionError(f"collided: {json.dumps(result)}")
        if result["backend"] != "nav2" or (result["coverage"] or 0) < 0.8:
            raise AssertionError(f"explored too little: {json.dumps(result)}")
        return (f"{controller.upper()} saw {round(100 * result['coverage'])}% of reachable floor in {result['seconds']} s "
                f"(levels reached at {result['coverage_seconds']} s), {result['travelled_m']} m driven, "
                f"0 collisions, min clearance {result['min_clearance_m']} m, end state {result['state']}, "
                f"forward m/s median open {result['forward_mps_open']} tight {result['forward_mps_tight']}")
    return check


def seek_check(controller):
    def check():
        name = "seek" if controller == "rpp" else f"seek_{controller}"
        run, result = hold("seek", "seek", 90, lambda s: False, target_id=0, launch_args=[f"controller:={controller}"])
        gaps = []
        for s in run.samples:
            people = s["sim"].get("people") or []
            if people:
                gaps.append((s["t"] - run.samples[0]["t"], math.dist(people[0], (s["sim"]["x"], s["sim"]["z"]))))
        later = [gap for t, gap in gaps if t >= 30]  # after the first approach
        result.update(controller=controller, start_gap_m=round(gaps[0][1], 2), min_gap_m=round(min(g for _, g in gaps), 2),
                      gap_after_30s_median_m=round(statistics.median(later), 2) if later else None,
                      gap_after_30s_max_m=round(max(later), 2) if later else None,
                      states=sorted({s["nav"]["state"] for s in run.samples}))
        record(name, result)
        if result["collisions"]:
            raise AssertionError(f"collided: {json.dumps(result)}")
        if result["gap_after_30s_median_m"] is None or result["gap_after_30s_median_m"] > 1.8:
            raise AssertionError(f"did not keep up with the person: {json.dumps(result)}")
        return (f"{controller.upper()} closed from {result['start_gap_m']} m to a {result['gap_after_30s_median_m']} m "
                f"median gap (max {result['gap_after_30s_max_m']} m) behind a 0.25 m/s walker, standoff 0.8 m, "
                f"0 collisions, {result['travelled_m']} m driven")
    return check


# RPP is the default controller (gating); MPPI runs are recorded for comparison only.
CHECKS = ([(world if controller == "rpp" else f"{world}_{controller}", goto_check(world, controller))
           for world in GOTO for controller in ("rpp", "mppi")]
          + [(f"explore{'' if c == 'rpp' else '_' + c}", explore_check(c)) for c in ("rpp", "mppi")]
          + [(f"seek{'' if c == 'rpp' else '_' + c}", seek_check(c)) for c in ("rpp", "mppi")])
INFORMATIONAL = {name for name, _ in CHECKS if name.endswith("_mppi")}

if __name__ == "__main__":
    main(CHECKS, INFORMATIONAL)
