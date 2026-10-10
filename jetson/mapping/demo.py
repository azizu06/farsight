"""Ray-cast RGB-D room. This goes through the same fusion as a real phone."""

import time
import cv2
import numpy as np

from .geometry import Calibration
from .phone import RGBDFrame


def simulated_frame(elapsed, frame_id, identity=("simulated-room", 1)):
    w, h = 160, 120
    k = Calibration(w, h, 135, 135, w / 2, h / 2)
    yaw = elapsed * 0.27
    cosine, sine = np.cos(yaw), np.sin(yaw)
    pose = np.eye(4)
    pose[:3, :3] = [[cosine, 0, sine], [0, 1, 0], [-sine, 0, cosine]]
    pose[:3, 3] = [0.65 * np.sin(elapsed * 0.12), 0, -1.4 + 0.5 * np.cos(elapsed * 0.12)]
    v, u = np.mgrid[:h, :w]
    rays = np.stack(((u - k.cx) / k.fx, -(v - k.cy) / k.fy, -np.ones((h, w))), -1)
    rays = rays @ pose[:3, :3].T
    origin = pose[:3, 3]
    nearest = np.full((h, w), np.inf)
    color = np.zeros((h, w, 3), np.uint8)
    bounds = np.array([[-3, -0.48, -6], [3, 2.4, 2]])
    for axis in range(3):
        for side in range(2):
            direction = rays[..., axis]
            t = (bounds[side, axis] - origin[axis]) / np.where(np.abs(direction) < 1e-8, 1e-8, direction)
            hit = origin + rays * t[..., None]
            valid = (t > 0.15) & (t < nearest) & (hit >= bounds[0] - 1e-4).all(-1) & (hit <= bounds[1] + 1e-4).all(-1)
            base = np.array([87, 105, 111]) if axis != 1 else np.array([48, 63, 66])
            pattern = ((np.floor(hit[..., 0] * 3) + np.floor(hit[..., 2] * 3)) % 2) * 10
            wall_band = ((hit[..., 1] > 0.55) & (hit[..., 1] < 0.64)) * 50 if axis != 1 else 0
            rgb = np.clip(base + pattern[..., None] + np.asarray(wall_band)[..., None], 0, 255).astype(np.uint8)
            color[valid], nearest[valid] = rgb[valid], t[valid]
    boxes = [([-1.9, -0.48, -3.6], [-0.9, 0.35, -2.5], [201, 146, 61]),
             ([0.5, -0.48, -4.6], [1.6, 0.75, -3.8], [66, 150, 145]),
             ([1.9, -0.48, -1.6], [2.6, 0.05, -0.8], [167, 92, 64])]
    safe_rays = np.where(np.abs(rays) < 1e-8, 1e-8, rays)
    for lower, upper, tint in boxes:
        first = (np.array(lower) - origin) / safe_rays
        last = (np.array(upper) - origin) / safe_rays
        entry = np.minimum(first, last).max(-1)
        exit = np.maximum(first, last).min(-1)
        valid = (entry > 0.15) & (entry <= exit) & (entry < nearest)
        color[valid], nearest[valid] = tint, entry[valid]
    depth = np.where(nearest <= 8, nearest, 0).astype(np.float32)
    confidence = np.where(depth > 0, 2, 0).astype(np.uint8)
    _, jpeg = cv2.imencode('.jpg', cv2.cvtColor(cv2.resize(color, (320, 240)), cv2.COLOR_RGB2BGR))
    header = dict(version=1, type="frame", session_id=identity[0], map_epoch=identity[1],
                  frame_id=frame_id, t_capture=elapsed, t_wall_ms=int(time.time() * 1000),
                  transform=pose.flatten(order="F").tolist(), tracking="normal")
    return RGBDFrame(header, color, depth, confidence, k, pose, jpeg.tobytes())
