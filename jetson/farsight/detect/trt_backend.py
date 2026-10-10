"""TensorRT backend for the Jetson. Skeleton only: same interface as OnnxDetector."""

from __future__ import annotations

import numpy as np

from .types import Detection


class TensorRTDetector:
    name = "tensorrt"

    # TODO(#9): the FP16 engine is built on the Jetson by jetson/setup.sh
    # (trtexec --onnx=models/yolo11n-pose.onnx --fp16). Once it exists: deserialize it here,
    # reuse decode.letterbox/to_tensor for input and decode.decode for the 1x56x8400 output.
    def __init__(self, engine_path: str, conf_thres: float = 0.35, iou_thres: float = 0.45):
        raise NotImplementedError(
            f"TensorRT backend not implemented yet (engine: {engine_path}); "
            "use FARSIGHT_DETECTOR=onnx off the Jetson")

    def detect(self, frame: np.ndarray) -> list[Detection]:
        raise NotImplementedError("TensorRT backend not implemented yet")
