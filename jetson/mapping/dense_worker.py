"""Best-effort visual accumulation; never wait for it on the camera path."""

import os
import threading
import time

from .phone import measured_points

DENSE_HZ = 2
DENSE_FRAME_POINTS = 4096


class DenseWorker:
    def __init__(self, current):
        self.enabled = os.environ.get("FARSIGHT_DENSE", "1").strip().lower() not in ("0", "false", "off", "no")
        self.current = current
        self.condition = threading.Condition()
        self.pending = None
        self.thread = None
        self.stopped = False
        self.offered = self.processed = self.dropped = 0
        self.processing_ms = 0.
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
            next_at = started + 1 / DENSE_HZ
            try:
                if not self.current(generation, cloud):
                    continue
                xyz, rgb = measured_points(frame, max_points=DENSE_FRAME_POINTS, phase=self.processed)
                if self.current(generation, cloud):
                    # Only the visual cloud lock; never acquire MapState.lock.
                    cloud.integrate(xyz, rgb)
                    self.processed += 1
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                self.processing_ms = round((time.monotonic() - started) * 1000, 1)

    def status(self):
        return dict(enabled=self.enabled, hz=DENSE_HZ, frame_points=DENSE_FRAME_POINTS,
                    queued=int(self.pending is not None), offered=self.offered,
                    processed=self.processed, dropped=self.dropped,
                    processing_ms=self.processing_ms, error=self.error)

    def close(self):
        with self.condition:
            self.stopped = True
            self.pending = None
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=1)
