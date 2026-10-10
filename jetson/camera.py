"""One latest camera frame, shared by video, inference, and mission capture."""

import asyncio
from dataclasses import dataclass
import sys
import threading
import time
import json
import struct
import math

import cv2
import numpy as np


def adjusted_receipt(header, received_at=None):
    delay = header.get("producer_delay_ms", 0)
    if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 60000:
        raise ValueError("Producer encoding delay must be finite milliseconds in 0..60000")
    return (time.monotonic() if received_at is None else received_at) - delay / 1000


def open_capture(device, width, height, fps, platform=sys.platform):
    """Open a UVC camera. On Linux pin the V4L2 backend (the pip OpenCV wheel has no GStreamer) and request the
    camera's own MJPG stream first: uncompressed YUYV caps many webcams below 30 fps and loads USB."""
    index = int(device) if str(device).isdigit() else device
    capture = cv2.VideoCapture(index, cv2.CAP_V4L2) if platform.startswith("linux") else cv2.VideoCapture(index)
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Camera {device} cannot be opened (check: v4l2-ctl --list-devices; another process may hold it)")
    return capture


@dataclass(frozen=True)
class CameraFrame:
    jpeg: bytes
    received: float
    frame_id: int
    source: str
    capture_verified: bool = False
    capture_binding: object = None

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
        self.phone_rgb = None

    def snapshot(self):
        if self.device is None:
            if self.phone_rgb is not None:
                return self.phone_rgb
            jpeg, received, frame_id, source = self.mapping.camera_snapshot()
            return CameraFrame(jpeg, received, frame_id, source) if jpeg is not None else None
        with self.lock:
            return self.frame

    def accept_phone_rgb(self, packet, session_id, previous_frame=-1, received_at=None, permits=None):
        arrived = time.monotonic() if received_at is None else received_at
        if not isinstance(packet, bytes) or not 4 < len(packet) <= 2 * 1024 * 1024:
            raise ValueError("RGB packet length is invalid")
        length = struct.unpack_from("<I", packet)[0]
        if not 0 < length <= 65536 or length + 4 >= len(packet):
            raise ValueError("RGB header length is invalid")
        header = json.loads(packet[4:4 + length])
        if not isinstance(header, dict) or header.get("version") != 1 or header.get("type") != "rgb_frame":
            raise ValueError("Expected RGB frame version 1")
        frame_id, capture = header.get("frame_id"), header.get("t_capture")
        if header.get("session_id") != session_id or type(frame_id) is not int or frame_id <= previous_frame:
            raise ValueError("RGB frame identity or ordering is invalid")
        if type(capture) not in (int, float) or not math.isfinite(capture) or capture < 0:
            raise ValueError("RGB capture timestamp is invalid")
        jpeg = packet[4 + length:]
        if header.get("jpeg_len") != len(jpeg) or not jpeg.startswith(b"\xff\xd8"):
            raise ValueError("RGB JPEG length or format is invalid")
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None or max(image.shape[:2]) > 4096:
            raise ValueError("RGB image cannot be decoded or is excessive")
        origin = permits.verified_origin(header) if permits is not None else None
        received = adjusted_receipt(header, arrived)
        if origin is not None:
            received = min(origin, received)
        self.phone_rgb = CameraFrame(jpeg, received, frame_id, "PHONE RGB", origin is not None, permits)
        return frame_id

    def status(self):
        frame = self.snapshot()
        fresh = bool(frame and frame.age_ms < 500)
        bound = bool(frame and frame.capture_verified and frame.capture_binding
                     and frame.capture_binding.active)
        drive_ready = fresh and (self.device is not None or bound
                                 or frame.source == "SIMULATED" or frame.source.startswith("REPLAY"))
        return dict(camera=fresh, camera_drive_ready=bool(drive_ready),
                    camera_capture_verified=bound,
                    camera_age_ms=frame.age_ms if frame else None,
                    camera_frame=frame.frame_id if frame else None,
                    camera_source=frame.source if frame else "USB CAMERA" if self.device is not None else "WAITING",
                    camera_error=self.error)

    async def start(self):
        if self.device is not None:
            self.task = asyncio.create_task(self._capture_loop())

    def _read(self):
        if self.capture is None or not self.capture.isOpened():
            self.capture = open_capture(self.device, self.width, self.height, self.fps)
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
        return CameraFrame(jpeg.tobytes(), frame.received, frame.frame_id, frame.source,
                           frame.capture_verified, frame.capture_binding) if okay else None
