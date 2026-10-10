import os
import time
from pathlib import Path

import numpy as np
import pytest

from farsight.config import Config
from farsight.detect import Detection, make_detector
from farsight.detect.trt_backend import TensorRTDetector

MODEL = Path(os.environ.get("FARSIGHT_MODEL", Path(__file__).parents[2] / "models" / "yolo11n-pose.onnx"))


def test_make_detector_none_returns_none():
    assert make_detector(Config(detector="none")) is None


def test_make_detector_unknown_backend_raises():
    with pytest.raises(ValueError, match="detector"):
        make_detector(Config(detector="bogus"))


def test_tensorrt_stub_fails_clearly_off_jetson():
    with pytest.raises(NotImplementedError, match="TensorRT"):
        TensorRTDetector("models/yolo11n-pose.engine")


@pytest.mark.skipif(not MODEL.exists(), reason="models/yolo11n-pose.onnx not present")
def test_onnx_backend_runs_on_blank_frame():
    pytest.importorskip("onnxruntime")
    det = make_detector(Config(detector="onnx", model=str(MODEL)))
    out = det.detect(np.zeros((480, 640, 3), np.uint8))
    assert isinstance(out, list)
    assert all(isinstance(d, Detection) for d in out)
