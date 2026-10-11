"""Calibrated measured depth views used to refresh revisited visual geometry.

An old point may be replaced only where a current accepted native depth sample
sees through it. Unknown pixels, nearer occluders and points outside the camera
remain untouched. This helper changes no navigation or drive state.
"""

import base64
from dataclasses import dataclass
import math

import numpy as np

from .geometry import Calibration, valid_depth
from .phone import DENSE_EDGE_RATIO, DENSE_EDGE_WINDOW, facing_mask, rigid_transform


FORMAT = "farsight.depth-view"
VERSION = 1
MAX_WIDTH, MAX_HEIGHT = 128, 96
POINT_BATCH = 65_536
# Visual depth correspondence spans two 1 cm dense cells plus the compact
# codec's 1 mm downward rounding. It is unrelated to controller clearances.
DEPTH_CORRESPONDENCE_M = .021


@dataclass(frozen=True)
class DepthView:
    """A conservative depth image with exact native calibration and camera pose.

    ``width`` and ``height`` describe the native pixel grid. Each ``depth_mm``
    cell is the minimum accepted depth in its ``stride`` square native block,
    rounded down to millimetres. Zero means that at least one native sample was
    not accepted. Short blocks at the right/bottom image edge contain only the
    native pixels that actually exist.
    """

    width: int
    height: int
    calibration: Calibration
    transform: np.ndarray
    stride: int
    depth_mm: np.ndarray
    sensor_key: tuple | None = None

    @property
    def depth(self):
        """Conservatively pooled optical-axis metres; zero is unknown."""
        return self.depth_mm.astype(np.float32) * .001

    @classmethod
    def from_frame(cls, frame):
        height, width = frame.depth.shape
        # Match measured_points exactly, including its native edge and grazing
        # acceptance. No interpolation or new surfaces are introduced.
        accepted = valid_depth(frame.depth, near=.15, far=8, edge_ratio=.12)
        accepted &= valid_depth(frame.depth, near=.15, far=8,
                                edge_ratio=DENSE_EDGE_RATIO, window=DENSE_EDGE_WINDOW)
        accepted &= frame.confidence >= 1
        accepted &= facing_mask(frame)
        stride = max(1, math.ceil(width / MAX_WIDTH), math.ceil(height / MAX_HEIGHT))
        pooled_height, pooled_width = math.ceil(height / stride), math.ceil(width / stride)
        native = np.where(accepted, frame.depth, 0)
        # Padding is not a native sample and cannot invalidate a partial block.
        padded = np.pad(native, ((0, pooled_height * stride - height),
                                 (0, pooled_width * stride - width)),
                        mode="constant", constant_values=np.inf)
        pooled = padded.reshape(pooled_height, stride, pooled_width, stride).min(axis=(1, 3))
        depth_mm = np.floor(pooled.astype(np.float64) * 1000).astype("<u2")
        depth_mm.setflags(write=False)
        transform = np.array(frame.transform, dtype=np.float64, copy=True)
        transform.setflags(write=False)
        key = frame.header.get("_mapping_sensor_key")
        return cls(width, height, frame.calibration, transform, stride, depth_mm,
                   tuple(key) if key is not None else None)

    def _select(self, points, *, contradicted):
        points = np.asarray(points)
        if not len(points):
            return np.zeros(0, dtype=bool)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("Revisit points must have shape (N, 3)")
        selected = np.zeros(len(points), dtype=bool)
        rotation, translation = self.transform[:3, :3], self.transform[:3, 3]
        k = self.calibration
        for start in range(0, len(points), POINT_BATCH):
            stop = min(start + POINT_BATCH, len(points))
            # Row-vector inverse of the rigid camera-to-world transform. ARKit
            # camera coordinates are x-right/y-up/z-backward and look along -Z.
            local = (points[start:stop].astype(np.float64) - translation) @ rotation
            optical_depth = -local[:, 2]
            valid = np.isfinite(local).all(axis=1) & (optical_depth > 0)
            with np.errstate(invalid="ignore", divide="ignore"):
                u = k.fx * local[:, 0] / optical_depth + k.cx
                v = k.cy - k.fy * local[:, 1] / optical_depth
            # Pixel footprints extend half a pixel from native centres. Always
            # select a native pixel before choosing its conservative block.
            valid &= ((u >= -.5) & (u < self.width - .5)
                      & (v >= -.5) & (v < self.height - .5))
            indices = np.flatnonzero(valid)
            if not len(indices):
                continue
            native_u = np.floor(u[indices] + .5).astype(np.int64)
            native_v = np.floor(v[indices] + .5).astype(np.int64)
            observed = self.depth_mm[native_v // self.stride,
                                     native_u // self.stride].astype(np.float64) * .001
            tolerance = DEPTH_CORRESPONDENCE_M
            old_depth = optical_depth[indices]
            comparison = (old_depth < observed - tolerance if contradicted
                          else old_depth <= observed + tolerance)
            selected[start + indices] = (observed > 0) & comparison
        return selected

    def contradicted(self, points):
        """Old foreground whose native view now measures farther free space."""
        return self._select(points, contradicted=True)

    def observed(self, points):
        """Currently observed region, including depth-matched old surfaces.

        Points behind the current surface by more than visual correspondence are
        occluded and remain outside the refresh region.
        """
        return self._select(points, contradicted=False)

    def to_metadata(self):
        """JSON-compatible v1 metadata; K and pose are explicitly row-major.

        The largest depth payload is 32,768 Base64 characters, leaving room in
        the existing 65,536-byte JSON header for calibration and other fields.
        ``sensor_key`` comes only from the accepted frame's trusted native key.
        """
        return dict(format=FORMAT, version=VERSION, width=self.width, height=self.height,
                    intrinsics=self.calibration.matrix.ravel().tolist(),
                    transform=self.transform.ravel().tolist(), matrix_order="row_major",
                    stride=self.stride, depth_format="uint16_mm_le",
                    depth=base64.b64encode(self.depth_mm.astype("<u2", copy=False).tobytes()).decode("ascii"),
                    sensor_key=list(self.sensor_key) if self.sensor_key is not None else None)

    @classmethod
    def from_metadata(cls, metadata):
        """Read the compact codec without expanding it to the native image."""
        if (metadata.get("format") != FORMAT or metadata.get("version") != VERSION
                or metadata.get("depth_format") != "uint16_mm_le"
                or metadata.get("matrix_order") != "row_major"):
            raise ValueError("Unsupported revisit depth view")
        width, height, stride = (metadata.get(name) for name in ("width", "height", "stride"))
        if (any(type(value) is not int or value < 1 for value in (width, height, stride))
                or math.ceil(width / stride) > MAX_WIDTH
                or math.ceil(height / stride) > MAX_HEIGHT):
            raise ValueError("Invalid revisit dimensions")
        k = np.asarray(metadata.get("intrinsics"), np.float64)
        if k.size != 9:
            raise ValueError("Invalid revisit intrinsics")
        k = k.reshape(3, 3)
        if (not np.allclose(k[2], [0, 0, 1]) or abs(k[0, 1]) > 1e-5 or abs(k[1, 0]) > 1e-5):
            raise ValueError("Unsupported revisit intrinsics")
        calibration = Calibration(width, height, k[0, 0], k[1, 1], k[0, 2], k[1, 2])
        values = np.asarray(metadata.get("transform"), np.float64)
        if values.size != 16:
            raise ValueError("Invalid revisit transform")
        transform = rigid_transform(values.reshape(4, 4).flatten(order="F"))
        transform.setflags(write=False)
        encoded = metadata.get("depth")
        if not isinstance(encoded, str) or len(encoded) > MAX_WIDTH * MAX_HEIGHT * 8 // 3:
            raise ValueError("Invalid revisit depth payload")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid revisit depth payload") from exc
        shape = (math.ceil(height / stride), math.ceil(width / stride))
        if len(raw) != shape[0] * shape[1] * 2:
            raise ValueError("Invalid revisit depth payload size")
        depth_mm = np.frombuffer(raw, "<u2").reshape(shape)
        if np.any((depth_mm != 0) & ((depth_mm < 149) | (depth_mm > 8000))):
            raise ValueError("Invalid revisit depth range")
        key = metadata.get("sensor_key")
        if key is not None and not isinstance(key, (list, tuple)):
            raise ValueError("Invalid revisit sensor key")
        return cls(width, height, calibration, transform, stride, depth_mm,
                   tuple(key) if key is not None else None)
