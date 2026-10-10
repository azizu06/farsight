"""Bounded, payload-free diagnostics for the relay's single-route control WS."""

from collections import deque
from datetime import datetime, timezone
import re
import time


class ControlClosures:
    def __init__(self):
        self.attempts = self.opened = self.active = self.ended = 0
        self.connect_failures = self.prepare_failures = 0
        self.history = deque(maxlen=8)

    def begin(self):
        self.attempts += 1
        return ClosureAttempt(self)

    def status(self, enabled=True):
        return dict(scope="single-route-control", enabled=enabled,
                    excluded="bonded-control" if not enabled else None,
                    attempts=self.attempts, opened=self.opened, active=self.active,
                    ended=self.ended, connect_failures=self.connect_failures,
                    prepare_failures=self.prepare_failures,
                    history=[dict(record) for record in self.history])


class ClosureAttempt:
    def __init__(self, owner):
        self.owner = owner
        self.started = time.monotonic()
        self.is_open = self.finished = False

    def opened(self):
        if not self.is_open and not self.finished:
            self.is_open = True
            self.owner.opened += 1
            self.owner.active += 1

    def end(self, side, reason, local_code=None, upstream_code=None, exception=None, operation="receive"):
        # First completion wins, including simultaneously completed pipe tasks.
        if self.finished:
            return
        self.finished = True
        if self.is_open:
            self.owner.active -= 1
            self.owner.ended += 1
        if reason == "connect_failure":
            self.owner.connect_failures += 1
        elif reason == "prepare_failure":
            self.owner.prepare_failures += 1

        def code(value):
            return int(value) if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65535 else None

        name = type(exception).__name__ if exception is not None else None
        if name is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) is None:
            name = "Exception"
        self.owner.history.append(dict(
            timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            duration_ms=round(max(0, time.monotonic() - self.started) * 1000, 3),
            first_ended_side=side if side in ("quest", "upstream") else "upstream",
            operation=operation if operation in ("receive", "send", "connect", "prepare") else "receive",
            reason=reason if reason in ("normal", "error", "send_timeout", "invalid_control", "prepare_failure", "connect_failure") else "error",
            local_close_code=code(local_code), upstream_close_code=code(upstream_code),
            exception_class=name))
