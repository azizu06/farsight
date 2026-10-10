"""'Person down' rule from COCO-17 keypoints.

Body axis = shoulder midpoint -> hip midpoint (ankle midpoint if the hips are unseen).
The person is down when that axis is more than 60 degrees away from vertical, i.e. closer
to lying than standing. If the shoulders or both lower anchors are low confidence, fall back
to the box: wider than 1.2x its height means down. Work in pixels, not normalized coords,
so the 4:3 frame does not skew the angle.
"""

from __future__ import annotations

import math

import numpy as np

KPT_CONF = 0.3
DOWN_ANGLE_DEG = 60.0
DOWN_ASPECT = 1.2

L_SHOULDER, R_SHOULDER = 5, 6
L_HIP, R_HIP = 11, 12
L_ANKLE, R_ANKLE = 15, 16


def _midpoint(kpts: np.ndarray, idxs: tuple[int, int]) -> np.ndarray | None:
    pts = [kpts[i, :2] for i in idxs if kpts[i, 2] >= KPT_CONF]
    return np.mean(pts, axis=0) if pts else None


def is_down(kpts: np.ndarray, box: tuple[float, float, float, float]) -> bool:
    shoulders = _midpoint(kpts, (L_SHOULDER, R_SHOULDER))
    lower = _midpoint(kpts, (L_HIP, R_HIP))
    if lower is None:
        lower = _midpoint(kpts, (L_ANKLE, R_ANKLE))
    if shoulders is None or lower is None:
        return box[2] > DOWN_ASPECT * box[3]
    dx, dy = abs(lower[0] - shoulders[0]), abs(lower[1] - shoulders[1])
    return math.degrees(math.atan2(dx, dy)) > DOWN_ANGLE_DEG
