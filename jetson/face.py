"""Optional UltraFace RFB-320 pass; normalized face boxes for detection packets."""
from pathlib import Path

import cv2
import numpy as np

from .pose.decode import nms


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
