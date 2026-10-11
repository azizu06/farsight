"""Normalized face boxes for detection packets: YOLO11-pose head keypoints, or an optional UltraFace RFB-320 pass."""
from pathlib import Path

import cv2
import numpy as np

from .pose.decode import nms

HEAD_KEYPOINTS = 5  # COCO-17 starts nose, left eye, right eye, left ear, right ear


def keypoint_faces(boxes, width, height, confidence=.5, min_points=3, scale=1.4):
    """Face boxes from the head keypoints of pose person boxes (normalized kpts, as in packets).

    A head needs min_points of nose/eyes/ears above confidence. Its box is a pixel square,
    scale times the larger extent of those points, centred on them. Run 094259 (715
    upright frames): 100 of 134 agreed faces versus UltraFace's 55, including people
    facing away, at no extra model cost.
    """
    faces = []
    for box in boxes:
        kpts = box.get("kpts")
        if box.get("cls") != "person" or not kpts:
            continue
        head = np.asarray(kpts[:HEAD_KEYPOINTS], float)
        head = head[np.isfinite(head).all(axis=1) & (head[:, 2] > confidence)]
        if len(head) < min_points:
            continue
        x, y = head[:, 0] * width, head[:, 1] * height
        half = scale * max(np.ptp(x), np.ptp(y)) / 2
        cx, cy = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
        x1, y1 = max(0., cx - half), max(0., cy - half)
        x2, y2 = min(float(width), cx + half), min(float(height), cy + half)
        if x2 <= x1 or y2 <= y1:
            continue
        faces.append(dict(cls="face", conf=round(float(head[:, 2].mean()), 3),
                          x=x1 / width, y=y1 / height, w=(x2 - x1) / width, h=(y2 - y1) / height))
    return faces


def decode(scores, boxes, confidence=.7, iou_threshold=.3):
    """Decode UltraFace [1,N,2] scores and normalized [1,N,4] xyxy boxes.

    UltraFace uses direct 320x240 resizing, so its normalized coordinates map
    directly to the source frame; YOLO letterbox reversal would be incorrect.
    """
    scores, boxes = np.asarray(scores), np.asarray(boxes)
    if (scores.ndim != 3 or boxes.ndim != 3 or scores.shape[0] != 1
            or scores.shape[2] != 2 or boxes.shape != (1, scores.shape[1], 4)):
        raise ValueError("Expected UltraFace scores [1,N,2] and boxes [1,N,4]")
    scores, boxes = scores[0, :, 1], boxes[0]
    valid = np.isfinite(scores) & np.isfinite(boxes).all(axis=1) & (scores >= confidence)
    boxes, scores = np.clip(boxes[valid], 0, 1), scores[valid]
    boxes = boxes.copy()
    boxes[:, 2:] -= boxes[:, :2]
    valid = (boxes[:, 2:] > 0).all(axis=1)
    boxes, scores = boxes[valid], scores[valid]
    return [dict(cls="face", conf=round(float(scores[i]), 3),
                 x=float(boxes[i, 0]), y=float(boxes[i, 1]),
                 w=float(boxes[i, 2]), h=float(boxes[i, 3]))
            for i in nms(boxes, scores, iou_threshold)]


class FaceDetector:
    """CPU ONNX rehearsal; the same ONNX can be built with TensorRT on Jetson."""
    def __init__(self, model_path, confidence=.7):
        if not Path(model_path).is_file():
            raise FileNotFoundError(f"Face model not found: {model_path}")
        self.confidence = confidence
        try:
            import onnxruntime as ort
        except ImportError:
            self.session = None
            self.net = cv2.dnn.readNetFromONNX(str(model_path))
        else:
            # UltraFace is a small CPU pass alongside GPU perception, voice,
            # and video. Two threads were faster and steadier than the default
            # six-core pool on the loaded Jetson; avoid reserving every core.
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            self.session = ort.InferenceSession(str(model_path), sess_options=options,
                                                providers=["CPUExecutionProvider"])
            self.input_name = self.session.get_inputs()[0].name

    def detect(self, frame):
        blob = cv2.dnn.blobFromImage(frame, 1 / 128, (320, 240),
                                     mean=(127, 127, 127), swapRB=True)
        if self.session is not None:
            outputs = self.session.run(None, {self.input_name: blob})
        else:
            self.net.setInput(blob)
            outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        # Output names/order differ across ONNX runtimes; use the documented shapes.
        scores = next(output for output in outputs if output.shape[-1] == 2)
        boxes = next(output for output in outputs if output.shape[-1] == 4)
        return decode(scores, boxes, self.confidence)
