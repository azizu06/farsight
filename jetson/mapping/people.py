"""Depth-backed 3D positions for tracked person detections (display markers only).

A person box gets `world: [x, y, z]` (map frame, metres, Y up) only when its exact
native RGB-D capture is still held and has enough confident LiDAR depth
inside the box. Nothing here touches the fused map, navigation or motion.
"""

from collections import OrderedDict
import threading

import numpy as np

from .orientation import upright_view

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
    """Holds normal-tracking RGB-D captures by trusted sensor key; MapState observer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = OrderedDict()

    def offer_frame(self, frame):
        if frame.header.get("tracking") != "normal":
            return
        with self.lock:
            key = frame.header.get("_mapping_sensor_key", frame.header["frame_id"])
            self.frames[key] = frame
            self.frames.move_to_end(key)
            while len(self.frames) > HELD_FRAMES:
                self.frames.popitem(last=False)

    def annotate(self, boxes, camera_frame):
        """Add `world` to tracked person boxes detected on a held RGB-D frame."""
        native_jpeg = getattr(camera_frame, "native_jpeg", None) or camera_frame.jpeg
        key = getattr(camera_frame, "sensor_key", None)
        with self.lock:
            held = self.frames.get(key if key is not None else camera_frame.frame_id)
            if key is None and held is None:
                # Direct/simulated map snapshots retain the exact sensor JPEG object.
                held = next((frame for frame in self.frames.values()
                             if frame.header["frame_id"] == camera_frame.frame_id and frame.jpeg is native_jpeg), None)
        same_picture = (held is not None and camera_frame.source in ("IPHONE LIDAR", "SIMULATED")
                        and (held.jpeg is native_jpeg or held.jpeg == native_jpeg)
                        and (key is None or held.header.get("_mapping_sensor_key") == key))
        if not same_picture:
            return boxes  # RGB-only or estimated-depth sources never get a 3D position
        for box in boxes:
            if box.get("cls") == "person" and isinstance(box.get("id"), int):
                # Boxes live on the upright picture; sensor-native depth is viewed rotated to match.
                world = torso_point(box, upright_view(held) if camera_frame.mirrored else held)
                if world is not None:
                    box["world"] = world
        return boxes
