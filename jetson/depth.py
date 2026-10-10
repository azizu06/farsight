"""Metric indoor depth and pure camera-to-scan geometry (Three.js: Y up, -Z ahead)."""

import math
from pathlib import Path
import cv2
import numpy as np

DEPTH_INPUT = (1, 3, 308, 406)
DEPTH_OUTPUT = (1, 308, 406)


class MetricDepth:
    """Local HF metric-indoor-small export; no downloads or relative-depth scaling."""

    def __init__(self, model=None, *, session=None):
        self.engine = None
        if session is None and model is not None and Path(model).suffix == ".engine":
            from jetson.tensorrt_detector import TensorRTDetector
            self.engine = TensorRTDetector(model, input_shape=DEPTH_INPUT,
                                           output_shape=DEPTH_OUTPUT, score_channels=0)
            self.backend = "depth-tensorrt"
        else:
            if session is None:
                import onnxruntime as ort
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                session = ort.InferenceSession(str(model), sess_options=options,
                                               providers=["CPUExecutionProvider"])
            inputs = session.get_inputs()
            if len(inputs) != 1 or tuple(inputs[0].shape) != DEPTH_INPUT:
                raise ValueError(f"Depth model must accept {DEPTH_INPUT}")
            self.session, self.input_name = session, inputs[0].name
            self.backend = "depth-onnx-cpu"

    def predict(self, bgr):
        if bgr.dtype != np.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3 or min(bgr.shape[:2]) < 1:
            raise ValueError("Depth input must be nonempty BGR uint8")
        # The prepared fixed-size export uses ImageNet-normalized RGB. Resize
        # directly to its input, then restore depth to the original pixel grid
        # before using the camera's original intrinsics.
        rgb = cv2.cvtColor(cv2.resize(bgr, (406, 308), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
        image = (rgb.astype(np.float32) / 255 - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
        blob = np.ascontiguousarray(image.transpose(2, 0, 1)[None])
        result = (self.engine.predict(blob) if self.engine else
                  self.session.run(None, {self.input_name: blob})[0])
        if result.shape != DEPTH_OUTPUT or not np.isfinite(result).all():
            raise ValueError("Depth model must return finite metric [1,308,406] output")
        return cv2.resize(result[0].astype(np.float32), (bgr.shape[1], bgr.shape[0]), interpolation=cv2.INTER_CUBIC)

    def close(self):
        if self.engine:
            self.engine.close()


def intrinsics_from_fov(width, height, horizontal_fov=70):
    if min(width, height) < 1 or not 1 < horizontal_fov < 179:
        raise ValueError("Invalid image dimensions or horizontal FOV")
    focal = width / (2 * math.tan(math.radians(horizontal_fov) / 2))
    return focal, focal, (width - 1) / 2, (height - 1) / 2


def back_project(depth, rgb, intrinsics, yaw_deg, max_depth=8):
    """Depth is optical-axis metres. Positive commanded yaw turns right."""
    depth = np.asarray(depth)
    if depth.ndim != 2 or rgb.shape != (*depth.shape, 3):
        raise ValueError("Depth and RGB must have matching image dimensions")
    fx, fy, cx, cy = intrinsics
    if not np.isfinite([fx, fy, cx, cy, yaw_deg]).all() or min(fx, fy) <= 0:
        raise ValueError("Intrinsics and yaw must be finite with positive focal lengths")
    v, u = np.mgrid[:depth.shape[0], :depth.shape[1]]
    valid = np.isfinite(depth) & (depth > .15) & (depth <= max_depth)
    d = depth[valid]
    points = np.stack(((u[valid] - cx) * d / fx, -(v[valid] - cy) * d / fy, -d), axis=-1)
    angle = math.radians(yaw_deg)
    c, s = math.cos(angle), math.sin(angle)
    rotation = np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])
    return (points @ rotation.T).astype(np.float32), rgb[valid].astype(np.uint8)
