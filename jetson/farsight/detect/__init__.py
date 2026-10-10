"""Person + pose detection behind a small Detector interface."""

from __future__ import annotations

from ..config import Config
from .types import Detection, Detector

__all__ = ["Detection", "Detector", "make_detector"]


def make_detector(cfg: Config) -> Detector | None:
    """Backend picked by FARSIGHT_DETECTOR: none | onnx | tensorrt."""
    if cfg.detector == "none":
        return None
    if cfg.detector == "onnx":
        from .onnx_backend import OnnxDetector
        return OnnxDetector(cfg.model, cfg.conf_thres, cfg.iou_thres)
    if cfg.detector in ("tensorrt", "trt"):
        from .trt_backend import TensorRTDetector
        return TensorRTDetector(cfg.model, cfg.conf_thres, cfg.iou_thres)
    raise ValueError(f"unknown detector backend {cfg.detector!r} (expected none, onnx, tensorrt)")
