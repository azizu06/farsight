"""Fresh implementation of the Pip Scout v1–v3 RGB-D wire format.

ARKit camera coordinates are x-right, y-up, z-backward; world is Y-up.
Matrices on the wire are column-major. Depth is optical-axis metres.
"""

from dataclasses import dataclass
import json
import struct

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .geometry import Calibration, valid_depth

MAX_PACKET = 8 * 1024 * 1024


class Hello(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: int = Field(ge=1, le=1)
    type: str = Field(pattern="^hello$")
    session_id: str = Field(min_length=1, max_length=256)
    map_epoch: int = Field(ge=0)
    device: str = Field(default="iPhone", max_length=256)
    supports_scene_depth: bool
    supports_mesh: bool = False
    supports_capture_permits: bool = False


def rigid_transform(values):
    transform = np.asarray(values, np.float64)
    if transform.size != 16 or not np.isfinite(transform).all():
        raise ValueError("Camera transform must contain 16 finite numbers")
    transform = transform.reshape(4, 4, order="F")
    rotation = transform[:3, :3]
    if (not np.allclose(transform[3], [0, 0, 0, 1], atol=1e-4)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=0.005)
            or not np.isclose(np.linalg.det(rotation), 1, atol=0.005)):
        raise ValueError("Camera transform must be rigid and right-handed")
    return transform


def validate_pose(header, identity):
    if not isinstance(header, dict):
        raise ValueError("Frame header must be an object")
    if (header.get("session_id"), header.get("map_epoch")) != identity:
        raise ValueError("Session changed: reconnect with a new hello")
    if header.get("version") not in (1, 2, 3):
        raise ValueError("Unsupported phone protocol version")
    if header.get("tracking") not in ("normal", "limited", "not_available"):
        raise ValueError("Unknown ARKit tracking state")
    frame_id, capture = header.get("frame_id"), header.get("t_capture")
    if not isinstance(frame_id, int) or frame_id < 0:
        raise ValueError("Invalid frame ID")
    if not isinstance(capture, (int, float)) or not np.isfinite(capture) or capture < 0:
        raise ValueError("Invalid capture timestamp")
    return rigid_transform(header.get("transform"))


def dimensions(spec, limit):
    if not isinstance(spec, dict):
        raise ValueError("Missing image/depth metadata")
    width, height = spec.get("width"), spec.get("height")
    if (not isinstance(width, int) or not isinstance(height, int)
            or min(width, height) < 2 or max(width, height) > limit):
        raise ValueError("Invalid sensor dimensions")
    return width, height


@dataclass
class RGBDFrame:
    header: dict
    rgb: np.ndarray
    depth: np.ndarray
    confidence: np.ndarray
    calibration: Calibration
    transform: np.ndarray
    jpeg: bytes


def decode_frame(packet, identity, *, camera_only=False):
    """Validate the wire packet; camera_only skips depth copying and reconstruction.

    The camera worker can validate its JPEG without waiting for the map worker.
    """
    if not 4 < len(packet) <= MAX_PACKET:
        raise ValueError("Frame exceeds the 8 MiB limit or is empty")
    header_size = struct.unpack_from("<I", packet)[0]
    if not 0 < header_size <= 65536 or 4 + header_size > len(packet):
        raise ValueError("Invalid frame header length")
    header = json.loads(packet[4:4 + header_size])
    transform = validate_pose(header, identity)
    if header.get("type") != "frame":
        raise ValueError("Binary packets must be RGB-D frames")
    image, depth, confidence = (header.get(key) for key in ("image", "depth", "confidence"))
    iw, ih = dimensions(image, 1920)
    dw, dh = dimensions(depth, 640)
    if dimensions(confidence, 640) != (dw, dh):
        raise ValueError("Depth and confidence dimensions differ")
    if abs(iw / ih / (dw / dh) - 1) > 0.01:
        raise ValueError("RGB and depth aspect ratios differ")
    if (image.get("orientation") != "landscape_right"
            or depth.get("format") != "float32_m"
            or confidence.get("format") != "uint8_0_2"):
        raise ValueError("Unsupported orientation or depth/confidence format")
    lengths = (image.get("jpeg_len"), depth.get("len"), confidence.get("len"))
    if any(not isinstance(n, int) or n <= 0 for n in lengths):
        raise ValueError("Invalid payload lengths")
    if lengths[1:] != (dw * dh * 4, dw * dh):
        raise ValueError("Depth/confidence payload length mismatch")
    start = 4 + header_size
    if start + sum(lengths) != len(packet):
        raise ValueError("Truncated frame or trailing payload")
    intrinsics = np.asarray(image.get("intrinsics"), np.float64)
    if intrinsics.size != 9 or not np.isfinite(intrinsics).all():
        raise ValueError("Invalid camera intrinsics")
    k = intrinsics.reshape(3, 3, order="F")
    if (not np.allclose(k[2], [0, 0, 1]) or abs(k[0, 1]) > 1e-5 or abs(k[1, 0]) > 1e-5
            or not (0 <= k[0, 2] < iw and 0 <= k[1, 2] < ih)):
        raise ValueError("Unsupported camera intrinsic matrix")
    # ARKit principal points scale directly with the sensor image, unlike an
    # OpenCV image resize's half-pixel convention.
    calibration = Calibration(dw, dh, k[0, 0] * dw / iw, k[1, 1] * dh / ih,
                              k[0, 2] * dw / iw, k[1, 2] * dh / ih)
    jpeg = packet[start:start + lengths[0]]
    bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None or bgr.shape[:2] != (ih, iw):
        raise ValueError("JPEG dimensions do not match the frame header")
    confidence_data = np.frombuffer(packet, np.uint8, dw * dh, start + lengths[0] + lengths[1])
    if np.any(confidence_data > 2):
        raise ValueError("Confidence values must be 0, 1 or 2")
    if camera_only:
        return header, jpeg
    rgb = cv2.cvtColor(cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    start += lengths[0]
    depth_data = np.frombuffer(packet, "<f4", dw * dh, start).reshape(dh, dw).copy()
    confidence_data = confidence_data.reshape(dh, dw).copy()
    return RGBDFrame(header, rgb, depth_data, confidence_data, calibration, transform, jpeg)


def world_grid(frame):
    height, width = frame.depth.shape
    v, u = np.mgrid[:height, :width]
    z, k = frame.depth, frame.calibration
    local = np.stack(((u - k.cx) * z / k.fx, -(v - k.cy) * z / k.fy, -z), axis=-1)
    return (local @ frame.transform[:3, :3].T + frame.transform[:3, 3]).astype(np.float32)


def measured_points(frame, max_points=None, phase=0):
    """Observed native pixels with RGB; bounded samples rotate across captures."""
    usable = valid_depth(frame.depth, near=0.15, far=8, edge_ratio=0.12)
    usable &= frame.confidence >= 1
    indices = np.flatnonzero(usable)
    if max_points is not None and len(indices) > max_points:
        step = (len(indices) + max_points - 1) // max_points
        indices = indices[phase % step::step][:max_points]
    v, u = np.divmod(indices, frame.depth.shape[1])
    z, k = frame.depth[v, u], frame.calibration
    local = np.stack(((u - k.cx) * z / k.fx, -(v - k.cy) * z / k.fy, -z), axis=-1)
    xyz = local @ frame.transform[:3, :3].T + frame.transform[:3, 3]
    return xyz.astype(np.float32), frame.rgb[v, u]


# Coarser grid strides kept per keyframe for the capped visual mesh export.
VISUAL_LODS = (4, 8)


def grid_mesh(positions, colors, usable, depths):
    """Triangulate one structured depth grid, only across observed, continuous surfaces."""
    h, w = usable.shape
    vertices = np.arange(h * w).reshape(h, w)
    a, b, c, d = (vertices[:-1, :-1], vertices[:-1, 1:], vertices[1:, :-1], vertices[1:, 1:])
    triangles = np.concatenate((np.stack((a, c, b), -1).reshape(-1, 3),
                                np.stack((b, c, d), -1).reshape(-1, 3)))
    mask = usable.ravel()[triangles].all(axis=1)
    samples = depths.ravel()[triangles]
    mask &= np.ptp(samples, axis=1) <= 0.10 + 0.04 * np.min(samples, axis=1)
    triangles = triangles[mask]
    # Remove unreferenced and invalid vertices from the mesh.
    referenced, indices = np.unique(triangles, return_inverse=True)
    return (positions.reshape(-1, 3)[referenced], colors.reshape(-1, 3)[referenced],
            indices.astype(np.uint32).reshape(-1, 3))


def reconstruct(frame, stride=2, min_confidence=1, lods=()):
    """Project measured samples and build triangles only across observed surfaces.

    With `lods`, also returns {stride: mesh} triangulated from coarser samplings of
    the same grid (same depth-edge rule), for a connected low-detail visual export.
    """
    usable = valid_depth(frame.depth, near=0.15, far=8, edge_ratio=0.12)
    usable &= frame.confidence >= min_confidence
    grid = world_grid(frame)
    sample = lambda step: (grid[::step, ::step], frame.rgb[::step, ::step],
                           usable[::step, ::step], frame.depth[::step, ::step])
    positions, colors, fine, depths = sample(stride)
    mesh = grid_mesh(positions, colors, fine, depths)
    if not lods:
        return positions[fine], colors[fine], mesh
    return positions[fine], colors[fine], mesh, {step: grid_mesh(*sample(step)) for step in lods}
