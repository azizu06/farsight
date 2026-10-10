"""TensorRT pose backend for the Jetson. Skeleton only: same interface as OnnxPoseDetector."""

from __future__ import annotations

import numpy as np

from .types import Detection


class TensorRTPoseDetector:
    name = "pose-tensorrt"

    # TODO(#9): build the FP16 engine on the Jetson (trtexec --onnx=models/yolo11n-pose.onnx --fp16)
    # via jetson/setup.sh. Then reuse jetson/tensorrt_detector.py's checked CUDA runtime (it currently
    # rejects pose, so its [1,84,8400] output contract needs a [1,56,8400] variant) and feed the
    # result to decode.decode().
    def __init__(self, engine_path: str, conf_thres: float = 0.35, iou_thres: float = 0.45):
        raise NotImplementedError(
            f"TensorRT pose backend not implemented yet (engine: {engine_path}); "
            "use a .onnx pose model off the Jetson")

    def detect(self, frame: np.ndarray) -> list[Detection]:
        raise NotImplementedError("TensorRT pose backend not implemented yet")
