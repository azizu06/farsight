"""Best-effort visual accumulation; never wait for it on the camera path."""

import os
import math
import threading
import time

from .phone import measured_points

DENSE_HZ = 8
DENSE_FRAME_POINTS = 12_288
DENSE_CPU_BUDGET = .15


def setting(name, default, low, high, integer=False):
    """Bound visual work even when an operator supplies a bad environment value."""
    try:
        value = int(os.environ.get(name, default)) if integer else float(os.environ.get(name, default))
        return value if math.isfinite(value) and low <= value <= high else default
    except (ValueError, TypeError):
        return default


class DenseWorker:
    def __init__(self, current):
        self.enabled = os.environ.get("FARSIGHT_DENSE", "1").strip().lower() not in ("0", "false", "off", "no")
        self.hz = setting("FARSIGHT_DENSE_HZ", DENSE_HZ, 1, 30, integer=True)
        self.frame_points = setting("FARSIGHT_DENSE_FRAME_POINTS", DENSE_FRAME_POINTS, 128, 65_536, integer=True)
        self.cpu_budget = setting("FARSIGHT_DENSE_CPU_BUDGET", DENSE_CPU_BUDGET, .05, .75)
        self.admission_hz = self.hz
        self.sampled_points = 0
        self.current = current
        self.condition = threading.Condition()
        self.pending = None
        self.thread = None
        self.stopped = False
        self.offered = self.processed = self.dropped = 0
        self.processing_ms = 0.
        self.cpu_ms = 0.
        self.error = None

    def offer(self, frame, cloud, generation):
        if not self.enabled:
            return
        with self.condition:
            if self.stopped:
                return
            self.offered += 1
            if self.pending is not None:
                self.dropped += 1
            self.pending = (frame, cloud, generation)
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="dense-visual", daemon=True)
                self.thread.start()
            self.condition.notify()

    def run(self):
        next_at = 0.
        while True:
            with self.condition:
                while not self.stopped:
                    delay = next_at - time.monotonic()
                    if self.pending is not None and delay <= 0:
                        break
                    self.condition.wait(delay if self.pending is not None else None)
                if self.stopped:
                    return
                frame, cloud, generation = self.pending
                self.pending = None
            started = time.monotonic()
            cpu_started = time.thread_time()
            try:
                if not self.current(generation, cloud):
                    continue
                xyz, rgb = measured_points(frame, max_points=self.frame_points, phase=self.processed)
                if self.current(generation, cloud):
                    # Only the visual cloud lock; never acquire MapState.lock.
                    cloud.integrate(xyz, rgb)
                    self.processed += 1
                    self.sampled_points += len(xyz)
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                elapsed = max(0., time.monotonic() - started)
                cpu_elapsed = max(0., time.thread_time() - cpu_started)
                self.processing_ms = round(elapsed * 1000, 1)
                self.cpu_ms = round(cpu_elapsed * 1000, 1)
                # Latest-only backpressure: a costly frame yields CPU time instead
                # of trying to catch up with old poses or monopolising the camera.
                interval = self.interval(cpu_elapsed)
                next_at = started + interval
                self.admission_hz = round(1 / interval, 2)

    def interval(self, cpu_seconds):
        return max(1 / self.hz, cpu_seconds / self.cpu_budget)

    def status(self):
        return dict(enabled=self.enabled, hz=self.hz, frame_points=self.frame_points,
                    cpu_budget=self.cpu_budget, admission_hz=self.admission_hz,
                    sampled_points=self.sampled_points,
                    queued=int(self.pending is not None), offered=self.offered,
                    processed=self.processed, dropped=self.dropped,
                    processing_ms=self.processing_ms, cpu_ms=self.cpu_ms, error=self.error)

    def close(self):
        with self.condition:
            self.stopped = True
            self.pending = None
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=1)
