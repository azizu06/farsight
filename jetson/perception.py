"""Local person detection: bundled CPU fallback, optional YOLO ONNX/TensorRT, or YOLO11-pose (adds kpts/down)."""

import asyncio
from collections import deque
from contextlib import suppress
import math
import time
import json
import logging
from pathlib import Path

import cv2
import numpy as np

from .tensorrt_detector import TensorRTError
from .tracker import Tracker

log = logging.getLogger(__name__)


def yolo_boxes(prediction, original_shape, side=640, confidence=.35, labels=None):
    """Decode standard YOLOv8/11 detection exports (xywh plus class scores)."""
    rows = np.asarray(prediction)
    if rows.ndim == 3:
        rows = rows[0]
    if rows.ndim != 2:
        raise ValueError("Expected YOLO detection tensor [1, classes+4, candidates]")
    if rows.shape[0] < rows.shape[1]:
        rows = rows.T
    if rows.shape[1] < 5:
        raise ValueError("Unsupported YOLO output; export a detection model without NMS")
    height, width = original_shape[:2]
    scale = min(side / width, side / height)
    pad_x, pad_y = (side - round(width * scale)) // 2, (side - round(height * scale)) // 2
    boxes, scores, classes = [], [], []
    # Select candidates in NumPy before entering Python: iterating all 8400
    # anchors held the GIL long enough to interfere with control deadlines.
    classes_raw = np.argmax(rows[:, 4:], axis=1)
    scores_raw = rows[np.arange(len(rows)), classes_raw + 4]
    selected = np.flatnonzero(np.isfinite(rows).all(axis=1) & (scores_raw >= confidence))
    for index in selected:
        row = rows[index]
        cls = int(classes_raw[index])
        score = float(scores_raw[index])
        cx, cy, w, h = row[:4]
        x1 = float(np.clip((cx - w / 2 - pad_x) / scale, 0, width))
        y1 = float(np.clip((cy - h / 2 - pad_y) / scale, 0, height))
        x2 = float(np.clip((cx + w / 2 - pad_x) / scale, 0, width))
        y2 = float(np.clip((cy + h / 2 - pad_y) / scale, 0, height))
        if x2 <= x1 or y2 <= y1:
            continue
        boxes.append([x1, y1, x2 - x1, y2 - y1])
        scores.append(score)
        classes.append(cls)
    # Suppress overlaps within each class, never suppress different classes.
    result = []
    for cls in set(classes):
        indices = [i for i, value in enumerate(classes) if value == cls]
        keep = cv2.dnn.NMSBoxes([boxes[i] for i in indices], [scores[i] for i in indices], confidence, .45)
        for local in np.asarray(keep).ravel():
            i = indices[int(local)]
            x, y, w, h = boxes[i]
            name = labels[cls] if labels and cls < len(labels) else "person" if cls == 0 else f"class_{cls}"
            result.append(dict(cls=name, class_id=cls,
                               conf=round(scores[i], 3), x=x / width, y=y / height,
                               w=w / width, h=h / height))
    return result


class PerceptionService:
    REBUILD_INTERVAL = 5.0  # seconds between GPU engine re-creations after a CUDA fault

    def __init__(self, camera, model=None, mode="hog", fps=3, on_detection=None, face_model=None, annotate=None,
                 face_source="pose"):
        self.camera, self.mode, self.model, self.fps = camera, mode, model, fps
        self.on_detection = on_detection
        # annotate(boxes, camera_frame) -> boxes, e.g. depth-backed 3D person positions.
        self.annotate = annotate
        self.task = None
        self.net = None
        self.engine = None
        self.hog = None
        self.pose = None
        # "pose": face boxes from YOLO11-pose head keypoints; "ultraface": the face_model ONNX pass.
        self.face_source = face_source
        self.face_model = face_model
        self.face = None
        self.face_error = None
        # The server points this at the rescue drill, so face boxes only appear while a drill runs.
        self.face_wanted = lambda: True
        self.error = None
        self._rebuild = None  # callable that builds a fresh TensorRT detector, set by _setup
        self._rebuilt_at = None
        self.rebuilds = 0
        self.packet = dict(ts=0, frame=-1, fps=0, boxes=[], detector="disabled", source="WAITING")
        self.tracker = Tracker()
        self.completions = deque()  # monotonic inference completions in the last FPS_WINDOW seconds
        self.labels = None

    def _setup(self):
        if self.face_source == "pose" and self.mode != "pose":
            self.face_error = "Keypoint faces need the YOLO11-pose detector (--detector pose)"
        if self.face_source == "ultraface" and self.face_model:
            # A bad face model disables only the face pass; primary detection still starts.
            try:
                from .face import FaceDetector
                self.face = FaceDetector(self.face_model)
            except Exception as exc:
                self.face_error = f"Face model unavailable: {exc}"
        if self.mode == "pose":
            if not self.model:
                raise ValueError("pose mode needs --detector-model (YOLO11-pose .onnx, or .engine on the Jetson)")
            from .pose import make_pose_detector
            self.pose = make_pose_detector(self.model)
            self._rebuild = lambda: make_pose_detector(self.model)
            # CPU pose is ~25 ms/frame; the 3 fps HOG default would waste it. TensorRT matches the COCO engine rate.
            self.fps = max(self.fps, 15 if self.pose.name == "pose-tensorrt" else 10)
            return
        if self.model:
            label_path = Path(self.model).with_suffix(".labels.json")
            if label_path.exists():
                labels = json.loads(label_path.read_text())
                if not isinstance(labels, list) or len(labels) > 256 or not all(isinstance(x, str) and len(x) <= 100 for x in labels):
                    raise ValueError("Detector labels must be a bounded string list")
                self.labels = labels
            if Path(self.model).suffix.lower() in (".engine", ".plan"):
                from .tensorrt_detector import TensorRTDetector
                self.engine = TensorRTDetector(self.model)
                self._rebuild = lambda: TensorRTDetector(self.model)
                self.mode = "tensorrt"
                self.fps = 15
            else:
                self.net = cv2.dnn.readNetFromONNX(self.model)
                self.mode = "onnx"
        elif self.mode == "hog":
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def _detect(self, jpeg):
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Invalid detector camera image")
        boxes = self._detect_primary(image)
        if self.face_wanted():
            if self.face is not None:
                boxes.extend(self.face.detect(image))
            elif self.face_source == "pose" and self.pose is not None:
                from .face import keypoint_faces
                boxes.extend(keypoint_faces(boxes, image.shape[1], image.shape[0]))
        return boxes

    def _recover_gpu(self, exc):
        """After a CUDA/TensorRT fault the execution context is unusable; close it and build a fresh one.

        Runs at most every REBUILD_INTERVAL. A failed rebuild leaves the closed detector in place, so
        the error stays visible in /health and the next interval tries again.
        """
        holder = self.engine if self.engine is not None else self.pose
        if self._rebuild is None or holder is None or not isinstance(exc, TensorRTError):
            return False
        now = time.monotonic()
        if self._rebuilt_at is not None and now - self._rebuilt_at < self.REBUILD_INTERVAL:
            return False
        self._rebuilt_at = now
        with suppress(Exception):
            holder.close()
        try:
            fresh = self._rebuild()
        except Exception as failure:
            self.error = f"TensorRT engine rebuild failed: {failure}"
            log.warning("%s", self.error)
            return False
        if self.engine is not None:
            self.engine = fresh
        else:
            self.pose = fresh
        self.rebuilds += 1
        log.warning("TensorRT engine re-created after: %s", exc)
        return True

    def _detect_primary(self, image):
        if self.pose is not None:
            from .pose.boxes import detection_to_box
            h, w = image.shape[:2]
            return [detection_to_box(d, w, h) for d in self.pose.detect(image)]
        if self.net is not None or self.engine is not None:
            h, w = image.shape[:2]
            scale = min(640 / w, 640 / h)
            resized = cv2.resize(image, (round(w * scale), round(h * scale)))
            canvas = np.full((640, 640, 3), 114, np.uint8)
            y, x = (640 - resized.shape[0]) // 2, (640 - resized.shape[1]) // 2
            canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
            blob = cv2.dnn.blobFromImage(canvas, 1 / 255, (640, 640), swapRB=True)
            if self.engine is not None:
                prediction = self.engine.predict(blob)
            else:
                self.net.setInput(blob)
                prediction = self.net.forward()
            return yolo_boxes(prediction, image.shape, labels=self.labels)
        if self.hog is not None:
            h, w = image.shape[:2]
            resized = cv2.resize(image, (320, max(128, round(h * 320 / w))))
            rectangles, weights = self.hog.detectMultiScale(resized, winStride=(8, 8), padding=(8, 8), scale=1.1)
            return [dict(cls="person", class_id=0, conf=round(1 / (1 + math.exp(-float(score))), 3),
                         x=float(x / resized.shape[1]), y=float(y / resized.shape[0]),
                         w=float(width / resized.shape[1]), h=float(height / resized.shape[0]))
                    for (x, y, width, height), score in zip(rectangles, weights)]
        return []

    def track(self, boxes, now):
        return self.tracker.update(boxes, now)

    async def start(self):
        if self.mode == "none" and not self.model and not self.face_model:
            return
        try:
            await asyncio.to_thread(self._setup)
            self.task = asyncio.create_task(self._run())
        except TensorRTError as exc:
            self.error = f"Detector unavailable: {exc}"
            self.task = asyncio.create_task(self._retry_startup())
        except Exception as exc:
            self.error = f"Detector unavailable: {exc}"

    async def _retry_startup(self):
        # Initial CUDA allocation can fail while other models are loading.
        # Preserve the fault in health and retry at the same bounded cadence
        # as runtime GPU recovery, without blocking camera/control startup.
        while True:
            await asyncio.sleep(self.REBUILD_INTERVAL)
            setup = asyncio.create_task(asyncio.to_thread(self._setup))
            try:
                await asyncio.shield(setup)
            except asyncio.CancelledError:
                # to_thread cannot stop an allocating worker. Drain it before
                # close() releases any detector it finishes constructing.
                with suppress(Exception):
                    await setup
                raise
            except TensorRTError as exc:
                self.error = f"Detector unavailable: {exc}"
                continue
            except Exception as exc:
                self.error = f"Detector unavailable: {exc}"
                return
            self.error = None
            await self._run()
            return

    FPS_WINDOW = 2.0

    def windowed_rate(self, completed):
        """Inferences per second over the last FPS_WINDOW s. The rate between only the last
        two inferences swung 2-30 with phone frame-arrival jitter at a steady ~6 fps."""
        self.completions.append(completed)
        while completed - self.completions[0] > self.FPS_WINDOW:
            self.completions.popleft()
        span = completed - self.completions[0]
        return (len(self.completions) - 1) / span if span > 0 else 0

    async def _run(self):
        previous = None
        while True:
            started = time.monotonic()
            frame = self.camera.snapshot()
            identity = (frame.source, frame.frame_id, frame.received) if frame else None
            if frame and frame.age_ms < 500 and identity != previous:
                previous = identity
                try:
                    inference = asyncio.create_task(asyncio.to_thread(self._detect, frame.jpeg))
                    try:
                        boxes = await asyncio.shield(inference)
                    except asyncio.CancelledError:
                        # Do not release the detector while its worker still
                        # holds it, or publish an inference cancelled on close.
                        with suppress(Exception):
                            await inference
                        raise
                    duration = time.monotonic() - started
                    completed = time.monotonic()
                    rate = self.windowed_rate(completed)
                    boxes = self.track(boxes, time.monotonic())
                    if self.annotate:
                        try:
                            boxes = self.annotate(boxes, frame)
                        except Exception:
                            pass  # optional 3D positions must never cost the detection itself
                    # Inference results retain the input timestamp, not completion freshness.
                    self.packet = dict(ts=time.time(), frame=frame.frame_id, fps=round(rate, 1), inference_ms=round(duration * 1000, 1),
                           boxes=boxes, detector=self.mode,
                           source=frame.source, received=frame.received, age_ms=frame.age_ms,
                           sensor_key=getattr(frame, "sensor_key", None), mirrored=getattr(frame, "mirrored", False))
                    self.error = None
                    if self.on_detection:
                        await asyncio.to_thread(self.on_detection, self.packet, frame)
                except Exception as exc:
                    self.error = str(exc)
                    recovery = asyncio.create_task(asyncio.to_thread(self._recover_gpu, exc))
                    try:
                        await asyncio.shield(recovery)
                    except asyncio.CancelledError:
                        # A rebuild may still install a fresh detector. Finish
                        # it before close() chooses the resource to release.
                        with suppress(Exception):
                            await recovery
                        raise
            await asyncio.sleep(max(.01, 1 / self.fps - (time.monotonic() - started)))

    def face_status(self):
        """Whether the cls==face pass the rescue drill scores on is loaded."""
        loaded = self.pose is not None if self.face_source == "pose" else self.face is not None
        return dict(face_detector=loaded, face_error=self.face_error)

    def health(self):
        """Detector fields for GET /health: mode, concrete backend, error, and live fps/latency."""
        packet = self.snapshot()
        return dict(detector=self.mode, detector_backend=self.pose.name if self.pose is not None else self.mode,
                    detector_error=self.error, detector_fps=packet["fps"] if packet["fresh"] else 0,
                    detector_inference_ms=packet.get("inference_ms") if packet["fresh"] else None,
                    **self.face_status())

    def snapshot(self):
        packet = dict(self.packet)
        received = packet.pop("received", None)
        packet["age_ms"] = round((time.monotonic() - received) * 1000) if received else None
        packet["fresh"] = bool(received and packet["age_ms"] < 1000)
        packet["error"] = self.error
        if not packet["fresh"]:
            packet["boxes"] = []
        return packet

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.engine:
            await asyncio.to_thread(self.engine.close)
        if self.pose and hasattr(self.pose, "close"):
            await asyncio.to_thread(self.pose.close)
