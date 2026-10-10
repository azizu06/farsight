"""One latest camera frame, shared by video, inference, and mission capture."""

import asyncio
from dataclasses import dataclass
import threading
import time

import cv2
import numpy as np


@dataclass(frozen=True)
class CameraFrame:
    jpeg: bytes
    received: float
    frame_id: int
    source: str

    @property
    def age_ms(self):
        return max(0, round((time.monotonic() - self.received) * 1000))


class CameraService:
    def __init__(self, mapping, device=None, width=640, height=480, fps=15, quality=55):
        self.mapping = mapping
        self.device = device
        self.width, self.height, self.fps, self.quality = width, height, fps, quality
        self.frame = None
        self.error = None
        self.capture = None
        self.lock = threading.Lock()
        self.task = None

    def snapshot(self):
        if self.device is None:
            jpeg, received, frame_id, source = self.mapping.camera_snapshot()
            return CameraFrame(jpeg, received, frame_id, source) if jpeg is not None else None
        with self.lock:
            return self.frame

    def status(self):
        frame = self.snapshot()
        return dict(camera=bool(frame and frame.age_ms < 500),
                    camera_age_ms=frame.age_ms if frame else None,
                    camera_frame=frame.frame_id if frame else None,
                    camera_source=frame.source if frame else "USB CAMERA" if self.device is not None else "WAITING",
                    camera_error=self.error)

    async def start(self):
        if self.device is not None:
            self.task = asyncio.create_task(self._capture_loop())

    def _read(self):
        if self.capture is None or not self.capture.isOpened():
            device = int(self.device) if str(self.device).isdigit() else self.device
            self.capture = cv2.VideoCapture(device)
            self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.capture.set(cv2.CAP_PROP_FPS, self.fps)
            self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not self.capture.isOpened():
                raise RuntimeError(f"Camera {self.device} cannot be opened")
        okay, image = self.capture.read()
        if not okay:
            self.capture.release()
            self.capture = None
            raise RuntimeError("Camera capture failed; retrying")
        height, width = image.shape[:2]
        scale = min(self.width / width, self.height / height, 1)
        if scale < 1:
            image = cv2.resize(image, (round(width * scale), round(height * scale)))
        okay, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not okay:
            raise RuntimeError("Camera JPEG encode failed")
        return jpeg.tobytes()

    async def _capture_loop(self):
        frame_id = 0
        while True:
            started = time.monotonic()
            try:
                jpeg = await asyncio.to_thread(self._read)
                with self.lock:
                    self.frame = CameraFrame(jpeg, time.monotonic(), frame_id, "USB CAMERA")
                frame_id += 1
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                await asyncio.sleep(1)
            await asyncio.sleep(max(0, 1 / self.fps - (time.monotonic() - started)))

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.capture:
            self.capture.release()

    def preview(self, max_width=640, quality=50):
        frame = self.snapshot()
        if not frame:
            return None
        image = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return None
        if image.shape[1] > max_width:
            image = cv2.resize(image, (max_width, round(image.shape[0] * max_width / image.shape[1])))
        okay, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return CameraFrame(jpeg.tobytes(), frame.received, frame.frame_id, frame.source) if okay else None
