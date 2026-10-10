"""onnxruntime backend (laptops, CI). The Jetson uses the TensorRT backend."""

from __future__ import annotations

import numpy as np

from .decode import decode, letterbox, to_tensor
from .types import Detection

SIDE = 640


class OnnxDetector:
    name = "onnx"

    def __init__(self, model_path: str, conf_thres: float = 0.35, iou_thres: float = 0.45):
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise RuntimeError("onnxruntime not installed: pip install 'farsight-jetson[onnx]'") from e
        self.conf_thres, self.iou_thres = conf_thres, iou_thres
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def detect(self, frame: np.ndarray) -> list[Detection]:
        img, _, _ = letterbox(frame, SIDE)
        raw = self._session.run(None, {self._input: to_tensor(img)})[0]
        return decode(raw, frame.shape, SIDE, self.conf_thres, self.iou_thres)
