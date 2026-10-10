"""CPU ONNX backend (laptops, CI). Uses onnxruntime when installed, else OpenCV's DNN module."""

from __future__ import annotations

import cv2
import numpy as np

from .decode import decode, letterbox, to_tensor
from .types import Detection

SIDE = 640


class OnnxPoseDetector:
    name = "pose-onnx"

    def __init__(self, model_path: str, conf_thres: float = 0.35, iou_thres: float = 0.45):
        self.conf_thres, self.iou_thres = conf_thres, iou_thres
        try:
            import onnxruntime as ort
        except ImportError:
            self._session, self._net = None, cv2.dnn.readNetFromONNX(model_path)
            self.name = "pose-onnx-cv2dnn"
        else:
            self._net = None
            self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            self._input = self._session.get_inputs()[0].name

    def detect(self, frame: np.ndarray) -> list[Detection]:
        img, _, _ = letterbox(frame, SIDE)
        blob = to_tensor(img)
        if self._session is not None:
            raw = self._session.run(None, {self._input: blob})[0]
        else:
            self._net.setInput(blob)
            raw = self._net.forward()
        return decode(raw, frame.shape, SIDE, self.conf_thres, self.iou_thres)
