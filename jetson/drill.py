"""Rescue drill scoring: timed face search, e-stop count and a dead-reckoned route home.

Server-authoritative so the headset only renders. A face is "found" once one
stable tracker id has stayed in view for DWELL_S. The route is integrated from
the v/w the control loop actually applied, so it is approximate (no odometry).
"""

import math
import threading
import time


class Drill:
    DWELL_S = 1.0
    # A face missing for longer than this, or from more than MAX_MISSES detector
    # frames, restarts its dwell: one dropped frame is tolerated, id flicker is not.
    GAP_S = 0.7
    MAX_MISSES = 1
    # Repeated blocks within this window are one obstacle (sensor chatter).
    ESTOP_DEBOUNCE_S = 0.5
    # Nominal full-command speeds of the rover; uncalibrated, hence "approximate".
    MAX_SPEED_MPS = 0.35
    MAX_TURN_RADPS = 1.6
    # Control outputs arrive at ~20 Hz; a longer silence is a stall, not motion.
    MAX_DT = 0.25
    CRUMB_M = 0.1
    MAX_CRUMBS = 600

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.started = None
        self.duration = 90
        self.victims = 3
        self._reset()

    def _reset(self):
        self.ended = None
        self.visible = {}  # id -> [first_seen, last_seen, missed_frames]
        self.found = {}  # id -> elapsed seconds when found
        self.estops = 0
        self.blocked = False
        self.blocked_at = -math.inf
        self.x = self.y = self.heading = 0.0
        self.command = (0.0, 0.0)
        self.command_at = None
        self.crumbs = [dict(t=0.0, x=0.0, y=0.0)]

    def start(self, duration=90, victims=3):
        with self.lock:
            self.started, self.duration, self.victims = self.clock(), duration, victims
            self._reset()
            return self.status()

    def _running(self, now):
        if self.started is None or self.ended is not None:
            return False
        if now - self.started >= self.duration:
            self.ended = self.started + self.duration
            return False
        return True

    def running(self):
        with self.lock:
            return self._running(self.clock())

    def observe(self, boxes):
        with self.lock:
            now = self.clock()
            if not self._running(now):
                return
            present = set()
            for box in boxes:
                track = box.get("id")
                if box.get("cls") != "face" or type(track) is not int:
                    continue
                present.add(track)
                seen = self.visible.get(track)
                if seen is None or now - seen[1] > self.GAP_S:
                    seen = self.visible[track] = [now, now, 0]
                seen[1] = now
                if track not in self.found and now - seen[0] >= self.DWELL_S:
                    self.found[track] = round(now - self.started, 1)
                    if len(self.found) >= self.victims:
                        self.ended = now
            for track, seen in list(self.visible.items()):
                if track not in present:
                    seen[2] += 1
                if seen[2] > self.MAX_MISSES or now - seen[1] > self.GAP_S:
                    del self.visible[track]

    def motion(self, v, w, blocked):
        """Record the command applied from now on; v,w in [-1,1], positive w turns right."""
        with self.lock:
            now = self.clock()
            if not self._running(now):
                self.command_at = None
                return
            if self.command_at is not None:
                self._integrate(min(now - self.command_at, self.MAX_DT), now)
            self.command, self.command_at = (float(v), float(w)), now
            if blocked:
                if not self.blocked and now - self.blocked_at > self.ESTOP_DEBOUNCE_S:
                    self.estops += 1
                self.blocked_at = now
            self.blocked = bool(blocked)

    def _integrate(self, dt, now):
        if dt <= 0:
            return
        v, w = self.command
        turn = -w * self.MAX_TURN_RADPS * dt
        mid = self.heading + turn / 2
        self.x += v * self.MAX_SPEED_MPS * dt * math.cos(mid)
        self.y += v * self.MAX_SPEED_MPS * dt * math.sin(mid)
        self.heading = math.atan2(math.sin(self.heading + turn), math.cos(self.heading + turn))
        last = self.crumbs[-1]
        if math.hypot(self.x - last["x"], self.y - last["y"]) >= self.CRUMB_M:
            self.crumbs.append(self._crumb(now))
            if len(self.crumbs) > self.MAX_CRUMBS:
                self.crumbs = self.crumbs[:1] + self.crumbs[2::2]

    def _crumb(self, now):
        return dict(t=round(now - self.started, 2), x=round(self.x, 3), y=round(self.y, 3))

    def status(self):
        with self.lock:
            now = self.clock()
            running = self._running(now)
            if self.started is None:
                remaining = elapsed = None
            else:
                end = self.ended if self.ended is not None else now
                elapsed = round(min(end - self.started, self.duration), 1)
                remaining = round(max(0, self.duration - elapsed), 1) if running else 0
            crumbs = list(self.crumbs)
            if self.started is not None:
                current = self._crumb(self.ended if self.ended is not None else now)
                if math.hypot(current["x"] - crumbs[-1]["x"], current["y"] - crumbs[-1]["y"]) >= .01:
                    crumbs.append(current)
            found = [track for track, _ in sorted(self.found.items(), key=lambda item: item[1])]
            status = dict(active=running, remaining_s=remaining, elapsed_s=elapsed, duration_s=self.duration,
                          found=len(found), total=self.victims, found_ids=found,
                          found_at_s=[self.found[track] for track in found], estops=self.estops,
                          pose=dict(x=round(self.x, 3), y=round(self.y, 3), heading_rad=round(self.heading, 3)),
                          breadcrumb=crumbs, route_home=[dict(x=c["x"], y=c["y"]) for c in reversed(crumbs)],
                          route_approximate=True,
                          dwell_s=self.DWELL_S, summary=None)
            if self.started is not None and not running:
                status["summary"] = dict(time_s=elapsed, found=len(found), total=self.victims,
                                         estops_avoided=self.estops, found_at_s=status["found_at_s"],
                                         breadcrumb=crumbs, route_approximate=True)
            return status
