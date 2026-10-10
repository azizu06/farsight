"""Visual map wire caps, byte-for-byte with the Rust server (rust/farsight-server/src/geometry.rs).

Only the served/exported payload is bounded; the fused map, Nav2 depth/occupancy and
labels keep full resolution. A cap of 0 disables decimation.
"""

import os
import struct
import numpy as np

POINTS_ENV, TRIANGLES_ENV = "FARSIGHT_MAP_POINTS_CAP", "FARSIGHT_MAP_TRIANGLES_CAP"
DEFAULT_POINTS_CAP, DEFAULT_TRIANGLES_CAP = 30_000, 40_000


def cap_from_env(name, default):
    """Non-negative cap from the environment; unset or empty uses the default."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    if not raw.isdigit():
        raise ValueError(f"{name} must be a non-negative integer")
    return int(raw)


def decimate_points_bin(raw, cap):
    """Stride-sample points.bin records i*n/cap to at most `cap`; otherwise the same bytes."""
    n = struct.unpack_from("<I", raw)[0] if len(raw) >= 4 else 0
    if cap == 0 or n <= cap:
        return raw
    records = np.frombuffer(raw, np.uint8, 16 * n, 4).reshape(n, 16)
    return struct.pack("<I", cap) + records[np.arange(cap, dtype=np.int64) * n // cap].tobytes()


def decimate_mesh_bin(raw, cap):
    """Stride-sample mesh.bin triangles i*tris/cap, keep only referenced vertices in first-use
    order and remap indices so the wire format stays valid; otherwise the same bytes."""
    vertices, indices = struct.unpack_from("<II", raw) if len(raw) >= 8 else (0, 0)
    triangles = indices // 3
    if cap == 0 or triangles <= cap:
        return raw
    records = np.frombuffer(raw, np.uint8, 16 * vertices, 8).reshape(vertices, 16)
    faces = np.frombuffer(raw, "<u4", indices, 8 + 16 * vertices)[:triangles * 3].reshape(triangles, 3)
    chosen = faces[np.arange(cap, dtype=np.int64) * triangles // cap].ravel()
    used, first, inverse = np.unique(chosen, return_index=True, return_inverse=True)
    order = np.argsort(first, kind="stable")  # compact ids follow first appearance
    rank = np.empty(len(used), np.uint32)
    rank[order] = np.arange(len(used), dtype=np.uint32)
    return (struct.pack("<II", len(used), cap * 3) + records[used[order]].tobytes()
            + rank[inverse.ravel()].astype("<u4").tobytes())
