"""Local person detection: bundled CPU fallback, optional YOLO ONNX/TensorRT, or YOLO11-pose (adds kpts/down)."""

import asyncio
from contextlib import suppress
import math
import time
import json
from pathlib import Path

import cv2
import numpy as np


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
    for row in rows:
        if not np.isfinite(row).all():
            continue
        cls = int(np.argmax(row[4:]))
        score = float(row[4 + cls])
        if score < confidence:
            continue
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


def iou(a, b):
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    return intersection / max(a["w"] * a["h"] + b["w"] * b["h"] - intersection, 1e-9)


class PerceptionService:
    def __init__(self, camera, model=None, mode="hog", fps=3, on_detection=None):
        self.camera, self.mode, self.model, self.fps = camera, mode, model, fps
        self.on_detection = on_detection
        self.task = None
        self.net = None
        self.engine = None
        self.hog = None
        self.pose = None
        self.error = None
        self.packet = dict(ts=0, frame=-1, fps=0, boxes=[], detector="disabled", source="WAITING")
        self.tracks = {}
        self.next_id = 1
        self.last_inference = None
        self.labels = None

    def _setup(self):
        if self.mode == "pose":
            if not self.model:
                raise ValueError("pose mode needs --detector-model (YOLO11-pose .onnx, or .engine on the Jetson)")
            from .pose import make_pose_detector
            self.pose = make_pose_detector(self.model)
            self.fps = max(self.fps, 10)  # CPU pose is ~25 ms/frame; the 3 fps HOG default would waste it
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
        self.tracks = {key: value for key, value in self.tracks.items() if now - value[1] < 2}
        available = set(self.tracks)
        for box in boxes:
            candidates = [(iou(box, self.tracks[key][0]), key) for key in available
                          if self.tracks[key][0]["cls"] == box["cls"]]
            overlap, identifier = max(candidates, default=(0, None))
            if overlap < .25:
                identifier = self.next_id
                self.next_id += 1
            else:
                available.remove(identifier)
            box["id"] = identifier
            self.tracks[identifier] = (box.copy(), now)
        return boxes

    async def start(self):
        if self.mode == "none" and not self.model:
            return
        try:
            await asyncio.to_thread(self._setup)
            self.task = asyncio.create_task(self._run())
        except Exception as exc:
            self.error = f"Detector unavailable: {exc}"

    async def _run(self):
        previous = None
        while True:
            started = time.monotonic()
            frame = self.camera.snapshot()
            identity = (frame.source, frame.frame_id, frame.received) if frame else None
            if frame and frame.age_ms < 500 and identity != previous:
                previous = identity
                try:
                    boxes = await asyncio.to_thread(self._detect, frame.jpeg)
                    duration = time.monotonic() - started
                    completed = time.monotonic()
                    rate = 1 / (completed - self.last_inference) if self.last_inference else 0
                    self.last_inference = completed
                    # Inference results retain the input timestamp, not completion freshness.
                    self.packet = dict(ts=time.time(), frame=frame.frame_id, fps=round(rate, 1), inference_ms=round(duration * 1000, 1),
                           boxes=self.track(boxes, time.monotonic()), detector=self.mode,
                           source=frame.source, received=frame.received, age_ms=frame.age_ms)
                    self.error = None
                    if self.on_detection:
                        await asyncio.to_thread(self.on_detection, self.packet, frame)
                except Exception as exc:
                    self.error = str(exc)
            await asyncio.sleep(max(.01, 1 / self.fps - (time.monotonic() - started)))

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
