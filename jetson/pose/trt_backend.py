"""TensorRT 10+ raw YOLO11-pose execution using the checked CUDA runtime."""

from __future__ import annotations

import numpy as np

from jetson.tensorrt_detector import TensorRTDetector, POSE_OUTPUT_SHAPE
from .decode import decode, letterbox, to_tensor
from .types import Detection


class TensorRTPoseDetector:
    name = "pose-tensorrt"

    def __init__(self, engine_path: str, conf_thres: float = 0.35, iou_thres: float = 0.45,
                 *, device=0, warmup=2, cuda=None, trt_module=None):
        if not np.isfinite(conf_thres) or not np.isfinite(iou_thres) or not 0 < conf_thres <= 1 or not 0 <= iou_thres <= 1:
            raise ValueError("Pose confidence must be 0..1 (exclusive zero); NMS IoU must be 0..1")
        self.conf_thres, self.iou_thres = float(conf_thres), float(iou_thres)
        self.engine = TensorRTDetector(engine_path, device=device, warmup=warmup,
            cuda=cuda, trt_module=trt_module, output_shape=POSE_OUTPUT_SHAPE, score_channels=1)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if (not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3
                or min(frame.shape[:2]) < 1 or frame.dtype != np.uint8):
            raise ValueError("Pose input must be a nonempty BGR uint8 HWC camera frame")
        image, _, _ = letterbox(frame, 640)
        prediction = self.engine.predict(to_tensor(image))
        return decode(prediction, frame.shape, 640, self.conf_thres, self.iou_thres)

    def close(self):
        self.engine.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
