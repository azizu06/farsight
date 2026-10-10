"""Runs the detector on its own thread against camera.latest_frame().

The camera thread and the MJPEG stream never wait on inference: this thread just takes
whatever frame is newest, skips frames it cannot keep up with, and publishes the latest
message for the websocket to fan out.
"""

from __future__ import annotations

import logging
import threading
import time

from ..camera import Camera
from .types import Detection, Detector

log = logging.getLogger("farsight.detect")
FPS_WINDOW_S = 1.0


def build_message(detections: list[Detection], frame_shape: tuple[int, ...], frame_seq: int,
                  fps: float, ts: float | None = None) -> dict:
    """docs/interfaces.md message: boxes normalized 0..1, top-left origin, plus kpts/down."""
    h, w = frame_shape[:2]
    boxes = []
    for i, d in enumerate(detections):
        x, y, bw, bh = d.box
        box = {"id": i, "cls": "person", "conf": round(d.conf, 3),
               "x": round(x / w, 4), "y": round(y / h, 4), "w": round(bw / w, 4), "h": round(bh / h, 4),
               "down": bool(d.down)}
        if d.kpts is not None:
            box["kpts"] = [[round(float(kx) / w, 4), round(float(ky) / h, 4), round(float(kc), 3)]
                           for kx, ky, kc in d.kpts]
        boxes.append(box)
    return {"ts": time.time() if ts is None else ts, "frame": frame_seq, "fps": round(fps, 1), "boxes": boxes}


class DetectorWorker:
    def __init__(self, camera: Camera, detector: Detector):
        self.camera, self.detector = camera, detector
        self._lock = threading.Lock()
        self._message: dict | None = None
        self._count = 0
        self._fps = 0.0
        self._latency_ms = 0.0
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return self.detector.name

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    def latest(self) -> tuple[int, dict | None]:
        """(publish counter, message). The counter changes with every new message."""
        with self._lock:
            return self._count, self._message

    def start(self) -> "DetectorWorker":
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="detector", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        last_seq = -1
        window_start, done = time.monotonic(), 0
        while self._running:
            seq, frame = self.camera.latest_frame()
            if frame is None or seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            t0 = time.monotonic()
            try:
                detections = self.detector.detect(frame)
            except Exception:
                log.exception("detector failed on frame %d", seq)
                time.sleep(0.1)
                continue
            self._latency_ms = (time.monotonic() - t0) * 1000
            done += 1
            elapsed = time.monotonic() - window_start
            if elapsed >= FPS_WINDOW_S:
                self._fps, window_start, done = done / elapsed, time.monotonic(), 0
            elif self._fps == 0.0:
                self._fps = done / max(elapsed, 1e-3)
            message = build_message(detections, frame.shape, seq, self._fps)
            with self._lock:
                self._message, self._count = message, self._count + 1
