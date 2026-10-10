"""Settings from environment variables (FARSIGHT_*), so nothing is hard-coded per machine."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FARSIGHT_{name}", default)


@dataclass(frozen=True)
class Config:
    camera: str = "0"  # V4L2 index or device path; "mock" forces the synthetic camera
    width: int = 640
    height: int = 480
    fps: int = 30
    jpeg_quality: int = 80
    host: str = "0.0.0.0"
    port: int = 8443
    certfile: str = ""  # HTTPS for WebXR; empty = plain HTTP (fine on a laptop)
    keyfile: str = ""
    detector: str = "onnx"  # none | onnx | tensorrt
    model: str = "models/yolo11n-pose.onnx"  # .engine for tensorrt
    conf_thres: float = 0.35
    iou_thres: float = 0.45

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            camera=_env("CAMERA", cls.camera),
            width=int(_env("WIDTH", str(cls.width))),
            height=int(_env("HEIGHT", str(cls.height))),
            fps=int(_env("FPS", str(cls.fps))),
            jpeg_quality=int(_env("JPEG_QUALITY", str(cls.jpeg_quality))),
            host=_env("HOST", cls.host),
            port=int(_env("PORT", str(cls.port))),
            certfile=_env("CERTFILE", cls.certfile),
            keyfile=_env("KEYFILE", cls.keyfile),
            detector=_env("DETECTOR", cls.detector),
            model=_env("MODEL", cls.model),
            conf_thres=float(_env("CONF", str(cls.conf_thres))),
            iou_thres=float(_env("IOU", str(cls.iou_thres))),
        )
