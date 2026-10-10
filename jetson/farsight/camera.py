"""Camera capture on a background thread. Keeps only the latest JPEG.

Real camera: V4L2 through OpenCV, requesting MJPG from the webcam (OBSBOT Meet SE).
Mock camera: synthetic moving frames, used when no camera opens, so everything runs on a laptop.
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np

from .config import Config


class Camera:
    """Latest-frame camera. Subclasses implement _grab() -> BGR frame or None."""

    is_mock = False

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._fps = 0.0
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> "Camera":
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._release()

    def latest(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._seq, self._jpeg

    def latest_frame(self) -> tuple[int, np.ndarray | None]:
        with self._lock:
            return self._seq, self._frame

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def ok(self) -> bool:
        return self._jpeg is not None

    def _loop(self) -> None:
        params = [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality]
        window_start, frames = time.monotonic(), 0
        while self._running:
            frame = self._grab()
            if frame is None:
                time.sleep(0.01)
                continue
            ok, buf = cv2.imencode(".jpg", frame, params)
            if not ok:
                continue
            with self._lock:
                self._frame, self._jpeg = frame, buf.tobytes()
                self._seq += 1
            frames += 1
            elapsed = time.monotonic() - window_start
            if elapsed >= 1.0:
                self._fps, window_start, frames = frames / elapsed, time.monotonic(), 0

    def _grab(self) -> np.ndarray | None:
        raise NotImplementedError

    def _release(self) -> None:
        pass


class V4L2Camera(Camera):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        source = int(cfg.camera) if cfg.camera.isdigit() else cfg.camera
        self._cap = cv2.VideoCapture(source, cv2.CAP_V4L2 if hasattr(cv2, "CAP_V4L2") else cv2.CAP_ANY)
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self._cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @property
    def opened(self) -> bool:
        return self._cap.isOpened() and self._cap.read()[0]

    def _grab(self) -> np.ndarray | None:
        ok, frame = self._cap.read()
        return frame if ok else None

    def _release(self) -> None:
        self._cap.release()


class MockCamera(Camera):
    is_mock = True

    def _grab(self) -> np.ndarray | None:
        time.sleep(1 / self.cfg.fps)
        h, w = self.cfg.height, self.cfg.width
        frame = np.full((h, w, 3), 40, np.uint8)
        t = time.monotonic()
        x = int((np.sin(t) * 0.4 + 0.5) * w)
        cv2.circle(frame, (x, h // 2), 40, (80, 200, 120), -1)
        cv2.putText(frame, f"MOCK CAMERA {time.strftime('%H:%M:%S')}", (16, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        return frame


def open_camera(cfg: Config) -> Camera:
    """Real camera if it opens, otherwise the mock. /health reports which one is live."""
    if cfg.camera != "mock":
        cam = V4L2Camera(cfg)
        if cam.opened:
            return cam.start()
        cam._release()
    return MockCamera(cfg).start()
