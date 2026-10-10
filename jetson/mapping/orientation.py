"""Upright camera picture for a rover phone mounted either way up.

ARKit delivers sensor-native landscape-right images, depth and intrinsics with a
camera pose that already includes the phone's roll. Fusion, Nav2 and the pose
published to the Quest are therefore geometrically consistent in either mount and
are left untouched. Only pixels shown to people or run through detectors see the
sensor image upside down when the phone sits rotated 180 degrees.

`UprightNormalizer` decides the mount from gravity in the camera frame (with
hysteresis); the camera lane then rotates the published JPEG. Depth lookups for a
box detected on the upright picture mirror the box back (see `people.py`).
"""

from dataclasses import replace
import math

import cv2
import numpy as np

ENTER_INVERTED = math.cos(math.radians(110))  # up-cosine below this => inverted
EXIT_INVERTED = math.cos(math.radians(70))  # up-cosine above this => upright
CONFIRM_FRAMES = 3  # consecutive frames past a threshold before switching
MIN_IN_PLANE = 0.35  # camera pitched past ~69 deg from level: roll is undefined, hold


def up_cosine(transform):
    """(cos of image roll, in-plane gravity magnitude) from a camera-to-world pose.

    World is Y-up, so world-up in camera axes is row 1 of the rotation. Upright
    landscape gives (+1), rotated 180 degrees gives (-1).
    """
    x, y, _ = np.asarray(transform, float)[1, :3]
    in_plane = math.hypot(x, y)
    return (y / in_plane if in_plane > 1e-9 else 0.0), in_plane


class UprightNormalizer:
    """Per-session mount detector with hysteresis."""

    def __init__(self):
        self.inverted = False
        self._pending = 0

    def observe(self, transform):
        cosine, in_plane = up_cosine(transform)
        if in_plane < MIN_IN_PLANE:
            self._pending = 0
            return self.inverted
        # The band between the thresholds (about portrait) never switches.
        wants = cosine > EXIT_INVERTED if self.inverted else cosine < ENTER_INVERTED
        self._pending = self._pending + 1 if wants else 0
        if self._pending >= CONFIRM_FRAMES:
            self.inverted, self._pending = not self.inverted, 0
        return self.inverted


def rotate_jpeg_180(jpeg):
    """Upright JPEG for display, detectors and crops (re-encoded; only used while inverted)."""
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    ok, encoded = cv2.imencode(".jpg", cv2.rotate(bgr, cv2.ROTATE_180), [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("Upright JPEG re-encode failed")
    return encoded.tobytes()


def upright_view(frame):
    """Sensor-native RGB-D frame seen through a 180 degree rotation (views, no copies).

    Depth, confidence, principal point and pose rotate together, so a pixel on the
    upright picture back-projects to the same world point as its sensor pixel.
    """
    height, width = frame.depth.shape
    k = frame.calibration
    flip = np.diag([-1.0, -1.0, 1.0, 1.0])
    return replace(frame, rgb=frame.rgb[::-1, ::-1], depth=frame.depth[::-1, ::-1],
                   confidence=frame.confidence[::-1, ::-1],
                   calibration=replace(k, cx=width - 1 - k.cx, cy=height - 1 - k.cy),
                   transform=frame.transform @ flip)
