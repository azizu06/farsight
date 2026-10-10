"""Calibration and projection. Internal coordinates are OpenCV x-right/y-down/z-forward."""

from dataclasses import dataclass
import json
from pathlib import Path
import struct

import cv2
import numpy as np


@dataclass(frozen=True)
class Calibration:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    distortion: tuple[float, ...] = ()
    approximate: bool = False

    def __post_init__(self):
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Calibration dimensions must be positive")
        if not np.isfinite([self.fx, self.fy, self.cx, self.cy, *self.distortion]).all():
            raise ValueError("Calibration must contain finite numbers")
        if min(self.fx, self.fy) <= 0:
            raise ValueError("Focal lengths must be positive")
        if self.distortion and len(self.distortion) not in (4, 5, 8, 12, 14):
            raise ValueError("Unsupported OpenCV distortion coefficients")

    @property
    def matrix(self):
        return np.array([[self.fx, 0, self.cx], [0, self.fy, self.cy], [0, 0, 1]], np.float64)

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @classmethod
    def from_fov(cls, width, height, horizontal_fov):
        if not 10 < horizontal_fov < 170:
            raise ValueError("Horizontal FOV must be between 10 and 170 degrees")
        focal = width / (2 * np.tan(np.deg2rad(horizontal_fov) / 2))
        return cls(width, height, focal, focal, (width - 1) / 2, (height - 1) / 2,
                   approximate=True)

    def scaled(self, width, height):
        # Resizing is supported; cropping/changing the camera zoom is not.
        if abs(width / height / (self.width / self.height) - 1) > 0.01:
            raise ValueError("Camera aspect ratio changed; recalibrate at the capture resolution")
        sx, sy = width / self.width, height / self.height
        return Calibration(width, height, self.fx * sx, self.fy * sy,
                           (self.cx + 0.5) * sx - 0.5, (self.cy + 0.5) * sy - 0.5,
                           self.distortion, self.approximate)

    def rectifier(self):
        if not self.distortion or not np.any(self.distortion):
            return lambda image: image
        maps = cv2.initUndistortRectifyMap(self.matrix, np.array(self.distortion), None,
                                         self.matrix, (self.width, self.height), cv2.CV_32FC1)
        return lambda image: cv2.remap(image, *maps, interpolation=cv2.INTER_LINEAR)


def backproject(pixels, depth, calibration):
    """Pixels (u,v) and optical-axis depth in metres -> OpenCV camera points."""
    uv = np.asarray(pixels, np.float64)
    z = np.asarray(depth, np.float64)
    return np.column_stack(((uv[:, 0] - calibration.cx) * z / calibration.fx,
                            (uv[:, 1] - calibration.cy) * z / calibration.fy, z))


def valid_depth(depth, near=0.2, far=8.0, edge_ratio=0.08):
    """Reject invalid/range-clipped depths and samples near depth discontinuities."""
    d = np.asarray(depth, np.float32)
    valid = np.isfinite(d) & (d >= near) & (d <= far)
    clean = np.where(valid, d, 0)
    local_min = cv2.erode(clean, np.ones((3, 3), np.uint8))
    local_max = cv2.dilate(clean, np.ones((3, 3), np.uint8))
    return valid & ((local_max - local_min) <= edge_ratio * np.maximum(clean, near))


def project_cloud(rgb, depth, calibration, camera_to_world, stride=4):
    if rgb.shape[:2] != depth.shape or depth.shape != (calibration.height, calibration.width):
        raise ValueError("RGB, depth and calibration dimensions must agree")
    if stride < 1:
        raise ValueError("Point sampling stride must be positive")
    v, u = np.mgrid[0:depth.shape[0]:stride, 0:depth.shape[1]:stride]
    keep = valid_depth(depth)[v, u]
    u, v = u[keep], v[keep]
    points = backproject(np.column_stack((u, v)), depth[v, u], calibration)
    world = points @ camera_to_world[:3, :3].T + camera_to_world[:3, 3]
    # Existing /ws/points convention: x right, y up, z forward.
    world[:, 1] *= -1
    return world.astype(np.float32), rgb[v, u].astype(np.uint8)


POINT_DTYPE = np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,)), ("pad", "u1")])


def encode_points(points, colors):
    """uint32 count followed by 16-byte XYZ/RGB records, little endian."""
    records = np.zeros(len(points), dtype=POINT_DTYPE)
    records["xyz"], records["rgb"] = points, colors
    return struct.pack("<I", len(points)) + records.tobytes()


def export_ply(points, colors):
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {len(points)}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
    dtype = np.dtype([("xyz", "<f4", (3,)), ("rgb", "u1", (3,))])
    data = np.empty(len(points), dtype=dtype)
    data["xyz"], data["rgb"] = points, colors
    return header.encode() + data.tobytes()
