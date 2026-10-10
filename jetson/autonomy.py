"""Small policies only: no sockets, hardware, clocks, or authority to renew a lease."""

import math
import random


def valid_person(box):
    x, width, identifier = box.get("x"), box.get("w"), box.get("id")
    return (box.get("cls") == "person" and type(identifier) is int and identifier >= 0
            and all(type(value) in (int, float) and math.isfinite(value) for value in (x, width))
            and 0 <= x <= 1 and 0 < width <= 1 and x + width <= 1.000001)


class Autonomy:
    def __init__(self, choose_turn=None):
        self.choose_turn = choose_turn or (lambda: random.choice((-1, 1)))
        self.reset()

    def reset(self):
        self.mode = "teleop"
        self.phase = "idle"
        self.target_id = None
        self.last_seen = None
        self.request = ("teleop", None)
        self.phase_at = 0
        self.direction = 1

    def select(self, mode, target_id, now):
        if (mode, target_id) != self.request:
            self.request = mode, target_id
            self.mode, self.target_id = mode, target_id
            self.phase = "forward" if mode == "explore" else "tracking"
            self.phase_at = now
            self.last_seen = now

    def step(self, now, range_cm, snapshot):
        if self.phase in ("arrived", "target_lost"):
            return 0, 0
        if range_cm is None or not math.isfinite(range_cm) or range_cm <= 0:
            return 0, 0
        if range_cm < 25:
            self.phase = "estop"
            return 0, 0
        age = snapshot.get("age_ms")
        boxes = (snapshot.get("boxes", []) if snapshot.get("fresh") and not snapshot.get("error")
                 and isinstance(age, (int, float)) and 0 <= age < 1000 else [])
        boxes = [box for box in boxes if valid_person(box)]
        if self.mode == "explore":
            person = next((box for box in boxes if box.get("cls") == "person"), None)
            if person:
                self.mode, self.target_id = "seek", person["id"]
                self.last_seen = now
            else:
                if self.phase == "turning":
                    if now - self.phase_at < 1:
                        return 0, self.direction * .35
                    self.phase, self.phase_at = "forward", now
                if range_cm < 70 or now - self.phase_at >= 4:
                    self.phase, self.phase_at = "turning", now
                    self.direction = self.choose_turn()
                    return 0, self.direction * .35
                self.phase = "forward"
                return .25, 0
        target = next((box for box in boxes if box.get("id") == self.target_id
                       and box.get("cls") == "person"), None)
        if target is None:
            self.phase = "target_lost" if now - self.last_seen >= 1 else "target_missing"
            return 0, 0
        self.last_seen = now - age / 1000
        self.phase = "tracking"
        if range_cm <= 60:
            self.phase = "arrived"
            return 0, 0
        error = target["x"] + target["w"] / 2 - .5
        return min(.35, (range_cm - 60) / 200), max(-.45, min(.45, error * 1.5))
