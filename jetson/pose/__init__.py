"""YOLO11-pose person detection: pure numpy decode plus swappable inference backends."""

from __future__ import annotations

from pathlib import Path

from .types import Detection, PoseDetector

__all__ = ["Detection", "PoseDetector", "make_pose_detector"]


def make_pose_detector(model: str, conf_thres: float = 0.35, iou_thres: float = 0.45) -> PoseDetector:
    """.engine/.plan -> checked TensorRT pose; anything else -> ONNX on CPU."""
    if Path(model).suffix.lower() in (".engine", ".plan"):
        from .trt_backend import TensorRTPoseDetector
        return TensorRTPoseDetector(model, conf_thres, iou_thres)
    if not Path(model).is_file():
        raise FileNotFoundError(f"Pose model not found: {model}")
    from .onnx_backend import OnnxPoseDetector
    return OnnxPoseDetector(model, conf_thres, iou_thres)
