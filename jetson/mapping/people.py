"""Depth-backed 3D positions for tracked person detections (display markers only).

A person box gets `world: [x, y, z]` (map frame, metres, Y up) only when the exact
RGB-D frame it was detected on (same JPEG object) is still held and has enough confident LiDAR depth
inside the box. Nothing here touches the fused map, navigation or motion.
"""

from collections import OrderedDict
import threading

import numpy as np

HELD_FRAMES = 12  # ~2 s of phone RGB-D at 6 fps


def torso_point(box, frame):
    """Map-frame torso centre from the median measured depth in the box's central patch, or None."""
    depth = np.asarray(frame.depth, np.float32)
    height, width = depth.shape
    u0, u1 = int((box["x"] + 0.3 * box["w"]) * width), int(np.ceil((box["x"] + 0.7 * box["w"]) * width))
    v0, v1 = int((box["y"] + 0.25 * box["h"]) * height), int(np.ceil((box["y"] + 0.6 * box["h"]) * height))
    rows, cols = slice(max(0, v0), min(height, v1)), slice(max(0, u0), min(width, u1))
    patch = depth[rows, cols]
    valid = np.isfinite(patch) & (patch > 0.15) & (patch <= 8) & (np.asarray(frame.confidence)[rows, cols] >= 1)
    if valid.sum() < 3:
        return None
    d = float(np.median(patch[valid]))
    k = frame.calibration
    u, v = (u0 + u1) / 2, (v0 + v1) / 2
    point = np.asarray(frame.transform, float) @ np.array([(u - k.cx) / k.fx * d, -(v - k.cy) / k.fy * d, -d, 1.0])
    return [round(float(c), 3) for c in point[:3]] if np.isfinite(point[:3]).all() else None


class PersonLocator:
    """Holds the newest normal-tracking RGB-D frames by frame id; MapState frame observer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = OrderedDict()

    def offer_frame(self, frame):
        if frame.header.get("tracking") != "normal":
            return
        with self.lock:
            self.frames[frame.header["frame_id"]] = frame
            self.frames.move_to_end(frame.header["frame_id"])
            while len(self.frames) > HELD_FRAMES:
                self.frames.popitem(last=False)

    def annotate(self, boxes, camera_frame):
        """Add `world` to tracked person boxes detected on a held RGB-D frame."""
        with self.lock:
            held = self.frames.get(camera_frame.frame_id)
        if held is None or held.jpeg is not camera_frame.jpeg:
            return boxes  # RGB-only or estimated-depth sources never get a 3D position
        for box in boxes:
            if box.get("cls") == "person" and isinstance(box.get("id"), int):
                world = torso_point(box, held)
                if world is not None:
                    box["world"] = world
        return boxes
