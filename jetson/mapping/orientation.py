"""Upright normalisation for a rover phone mounted either way up.

ARKit delivers sensor-native landscape-right images, depth, confidence and
intrinsics, with a camera pose that already includes the phone's roll. Fusion is
therefore geometrically consistent in either mount, but every picture consumer
(Quest view, detector boxes, SBAR/identity crops) sees the sensor image upside
down when the phone sits rotated 180 degrees.

`UprightNormalizer` decides the mount from gravity in the camera frame and, while
inverted, rotates RGB, JPEG, depth and confidence 180 degrees together with the
principal point and the camera pose. A 180 degree rotation about the optical axis
leaves every back-projected world point, the camera position and the viewing
direction unchanged, so the map is not corrected twice; only roll moves.
"""

from dataclasses import replace
import math

import cv2
import numpy as np

ENTER_INVERTED = math.cos(math.radians(110))  # up-cosine below this => inverted
EXIT_INVERTED = math.cos(math.radians(70))  # up-cosine above this => upright
CONFIRM_FRAMES = 3  # consecutive frames past a threshold before switching
MIN_IN_PLANE = 0.35  # camera pitched past ~69 deg from level: roll is undefined, hold
FLIP = np.diag([-1.0, -1.0, 1.0, 1.0])


def up_cosine(transform):
    """(cos of image roll, in-plane gravity magnitude) from a camera-to-world pose.

    World is Y-up, so world-up in camera axes is row 1 of the rotation. Upright
    landscape gives (+1), rotated 180 degrees gives (-1).
    """
    x, y, _ = np.asarray(transform, float)[1, :3]
    in_plane = math.hypot(x, y)
    return (y / in_plane if in_plane > 1e-9 else 0.0), in_plane


class UprightNormalizer:
    """Per-session mount detector with hysteresis; `apply` returns an upright frame."""

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

    def apply(self, frame):
        if not self.observe(frame.transform):
            return frame
        return rotate_frame_180(frame)


def rotate_frame_180(frame):
    from .phone import RGBDFrame  # local: phone.py imports this module

    height, width = frame.depth.shape
    k = frame.calibration
    bgr = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    ok, jpeg = cv2.imencode(".jpg", cv2.rotate(bgr, cv2.ROTATE_180), [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("Upright JPEG re-encode failed")
    header = dict(frame.header, orientation_normalized="rotated_180")
    # Pixel index u maps to width-1-u, so (u - cx) negates exactly.
    calibration = replace(k, cx=width - 1 - k.cx, cy=height - 1 - k.cy)
    return RGBDFrame(header, np.ascontiguousarray(frame.rgb[::-1, ::-1]),
                     np.ascontiguousarray(frame.depth[::-1, ::-1]),
                     np.ascontiguousarray(frame.confidence[::-1, ::-1]),
                     calibration, frame.transform @ FLIP, jpeg.tobytes())
