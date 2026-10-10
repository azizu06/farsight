from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class Detection:
    """One person in original-frame pixels. box = (x, y, w, h), top-left origin."""

    box: tuple[float, float, float, float]
    conf: float
    kpts: np.ndarray | None = None  # (17, 3) x, y, conf in frame pixels (COCO order)
    down: bool = False


class PoseDetector(Protocol):
    name: str

    def detect(self, frame: np.ndarray) -> list[Detection]:
        """BGR frame -> person detections."""
        ...
