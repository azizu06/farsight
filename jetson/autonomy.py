"""Small policies only: no sockets, hardware, clocks, or authority to renew a lease."""

import math
import random


def valid_person(box):
    x, width, identifier = box.get("x"), box.get("w"), box.get("id")
    return (box.get("cls") == "person" and type(identifier) is int and identifier >= 0
            and all(type(value) in (int, float) and math.isfinite(value) for value in (x, width))
            and 0 <= x <= 1 and 0 < width <= 1 and x + width <= 1.000001)


class Autonomy:
    """Reactive fallback used when the navigation stack is unavailable.

    With ``proximity_stop`` (the default) the ultrasonic is a hard gate: unknown
    range stops and below 25 cm latches ``estop``. Without it (explicit opt-out)
    unknown range only removes the obstacle cue: Explore keeps its timed turns
    and Seek estimates arrival from the box width.
    """

    def __init__(self, choose_turn=None, proximity_stop=True):
        self.choose_turn = choose_turn or (lambda: random.choice((-1, 1)))
        self.proximity_stop = proximity_stop
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
            self.phase = {"explore": "forward", "goto": "goto"}.get(mode, "tracking")
            self.phase_at = now
            self.last_seen = now

    def step(self, now, range_cm, snapshot):
        if self.phase in ("arrived", "target_lost") or self.mode == "goto":
            return 0, 0  # go-to-point exists only with the navigation backend
        known = range_cm is not None and math.isfinite(range_cm) and range_cm > 0
        if self.proximity_stop:
            if not known:
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
                if (known and range_cm < 70) or now - self.phase_at >= 4:
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
        error = target["x"] + target["w"] / 2 - .5
        turn = max(-.45, min(.45, error * 1.5))
        if not known:
            # No range: a box half the frame wide is roughly an arm's length away.
            if target["w"] >= .5:
                self.phase = "arrived"
                return 0, 0
            return min(.35, (.5 - target["w"]) * .8), turn
        if range_cm <= 60:
            self.phase = "arrived"
            return 0, 0
        return min(.35, (range_cm - 60) / 200), turn
