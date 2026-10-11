"""Inferred people/crowd footprints, separate from measured reconstruction.

Torso locations come from the existing depth-matched detections. Projecting a
body onto the floor closes the false gap between its legs; nearby bodies form
a filled crowd hull. Nav2 supplies its usual robot inflation around this layer.
"""

import math
import time

import numpy as np


def bootstrap_parameters(params):
    """Match the initial semantic map to the selected Nav2 global map geometry."""
    grid = params["global_costmap"]["global_costmap"]["ros__parameters"]
    # Omitted fields use Costmap2DROS defaults, not this project's explicit
    # 40 m YAML values; otherwise a custom file could be resized on startup.
    return {"people_map_" + name: float(grid.get(name, default)) for name, default in
            (("width", 5), ("height", 5), ("resolution", .1), ("origin_x", 0), ("origin_y", 0))}


def capture_identity(key):
    """The existing native capture key's world identity (session, epoch, socket)."""
    return tuple(key[:3]) if isinstance(key, (list, tuple)) and len(key) == 5 else None


class People:
    """Latest detection snapshot in the same world as the bridge's native depth.

    The detector already uses a 1 s freshness window. Replacing rather than
    accumulating snapshots clears vacated positions and expired detections.
    This state never changes drive authority or requests a stopped interval.
    """

    def __init__(self):
        self.identity = None
        self.positions = ()
        self.expires_at = -math.inf
        self.revision = 0

    def clear(self):
        self.identity, self.positions = None, ()
        self.expires_at = -math.inf
        self.revision += 1

    def bind(self, sensor_key):
        identity = capture_identity(sensor_key)
        if identity != self.identity:
            self.clear()
            self.identity = identity

    def update(self, packet, now=None):
        identity = capture_identity(packet.get("sensor_key"))
        if identity is None or identity != self.identity:
            return False
        now = time.monotonic() if now is None else now
        positions = []
        age = float(packet.get("age_ms") or 0) / 1000.0
        if packet.get("fresh") and not packet.get("error") and 0 <= age < 1:
            for box in packet.get("boxes", []):
                world = box.get("world")
                if box.get("cls") != "person" or not isinstance(world, (list, tuple)) or len(world) != 3:
                    continue
                xyz = np.asarray(world, float)
                if np.isfinite(xyz).all():
                    positions.append((-float(xyz[2]), -float(xyz[0])))  # ARKit -> ROS odom
        self.positions = tuple(positions)
        self.expires_at = now + max(0, 1 - age) if positions else -math.inf
        self.revision += 1
        return True

    def points(self, now=None):
        now = time.monotonic() if now is None else now
        return self.positions if now < self.expires_at else ()


def _groups(points, distance):
    """Connected small crowds; separate groups do not share a room-wide hull."""
    parent = list(range(len(points)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(points)):
        delta = points[i + 1:] - points[i]
        for j in np.flatnonzero(np.sum(delta * delta, axis=1) <= distance * distance) + i + 1:
            parent[root(int(j))] = root(i)
    groups = {}
    for i in range(len(points)):
        groups.setdefault(root(i), []).append(points[i])
    return groups.values()


def _hull(points):
    ordered = sorted(set(map(tuple, points)))
    if len(ordered) < 3:
        return np.asarray(ordered, float)

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    halves = []
    for sequence in (ordered, reversed(ordered)):
        half = []
        for point in sequence:
            while len(half) >= 2 and cross(half[-2], half[-1], point) <= 0:
                half.pop()
            half.append(point)
        halves.append(half[:-1])
    return np.asarray(halves[0] + halves[1], float)


def rasterize(points, shape, resolution, origin, *, body_radius=0.3, crowd_distance=1.2):
    """Body disks and nearby groups' rounded convex hulls at cell centres.

    Only each hull's bounding region is evaluated, even for a large global map.
    Values are semantic occupancy, not new measured points or observed floor.
    """
    occupied = np.zeros(shape, bool)
    if not len(points):
        return occupied
    points = np.asarray(points, float).reshape(-1, 2)
    for group in _groups(points, crowd_distance):
        hull = _hull(group)
        lo = np.floor((hull.min(axis=0) - body_radius - origin) / resolution).astype(int)
        hi = np.ceil((hull.max(axis=0) + body_radius - origin) / resolution).astype(int)
        x0, y0 = np.maximum(lo, 0)
        x1, y1 = np.minimum(hi, (shape[1], shape[0]))
        if x0 >= x1 or y0 >= y1:
            continue
        xs = origin[0] + (np.arange(x0, x1)[None, :] + 0.5) * resolution
        ys = origin[1] + (np.arange(y0, y1)[:, None] + 0.5) * resolution
        near = np.zeros((y1 - y0, x1 - x0), bool)
        inside = np.ones_like(near) if len(hull) >= 3 else np.zeros_like(near)
        for a, b in zip(hull, np.roll(hull, -1, axis=0)):
            dx, dy = b - a
            norm = dx * dx + dy * dy
            t = np.clip(((xs - a[0]) * dx + (ys - a[1]) * dy) / norm, 0, 1) if norm else 0
            near |= (xs - a[0] - t * dx) ** 2 + (ys - a[1] - t * dy) ** 2 <= body_radius ** 2
            if len(hull) >= 3:
                inside &= dx * (ys - a[1]) - dy * (xs - a[0]) >= -1e-9
        occupied[y0:y1, x0:x1] |= near | inside
    return occupied


def overlay(cells, points, resolution, origin, **options):
    """Add people after driven-floor clearing, retaining all other map values."""
    result = cells.copy()
    result[rasterize(points, cells.shape, resolution, origin, **options)] = 100
    return result
