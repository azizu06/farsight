"""Pure-numpy indoor test world for the SLAM loop test (no ROS imports).

The world is planar in ROS conventions: x forward/east, y left/north, z up,
metres. Obstacles are vertical extrusions of 2D segments with a height, which is
enough to raycast an iPhone-like LiDAR cone and to give the scan matcher walls,
corners and low clutter that pointcloud_to_laserscan's height band must keep or
drop.
"""

from dataclasses import dataclass
import math

import numpy as np

CEILING = 2.6

# iPhone LiDAR as streamed in landscape_right: 960x720 at fx = fy = 800 gives a
# 62 x 48 degree cone. Depth is trusted to about 5 m indoors.
LIDAR_HFOV = 2 * math.atan(480 / 800)
LIDAR_VFOV = 2 * math.atan(360 / 800)
LIDAR_MAX_RANGE = 5.0
LIDAR_MIN_RANGE = 0.15


@dataclass(frozen=True)
class Box:
    """Axis-aligned box on the floor (x0..x1, y0..y1) with a height."""
    x0: float
    y0: float
    x1: float
    y1: float
    height: float = CEILING

    def segments(self):
        a = (self.x0, self.y0)
        b = (self.x1, self.y0)
        c = (self.x1, self.y1)
        d = (self.x0, self.y1)
        return [(a, b, self.height), (b, c, self.height), (c, d, self.height), (d, a, self.height)]

    def contains(self, x, y, margin=0.0):
        return (self.x0 - margin <= x <= self.x1 + margin) and (self.y0 - margin <= y <= self.y1 + margin)


class World:
    """Closed room plus interior boxes, flattened to height-tagged wall segments."""

    def __init__(self, width, depth, boxes):
        self.width = width
        self.depth = depth
        self.boxes = list(boxes)
        hx, hy = width / 2, depth / 2
        room = [((-hx, -hy), (hx, -hy), CEILING), ((hx, -hy), (hx, hy), CEILING),
                ((hx, hy), (-hx, hy), CEILING), ((-hx, hy), (-hx, -hy), CEILING)]
        segs = room + [s for b in self.boxes for s in b.segments()]
        self.seg_a = np.array([s[0] for s in segs], dtype=float)
        self.seg_b = np.array([s[1] for s in segs], dtype=float)
        self.seg_h = np.array([s[2] for s in segs], dtype=float)

    def raycast(self, ox, oy, sensor_z, azimuths, elevations, max_range=LIDAR_MAX_RANGE):
        """Return 3D ranges for rays from (ox, oy, sensor_z); inf where nothing is hit.

        azimuths are world-frame headings (rad, CCW from +x); elevations are
        angles above horizontal. Floor, ceiling and every segment are tested and
        the nearest valid hit wins; a ray that clears a low segment's top keeps
        going.
        """
        az = np.asarray(azimuths, dtype=float)
        el = np.asarray(elevations, dtype=float)
        dx, dy = np.cos(az), np.sin(az)
        tan_el = np.tan(el)
        az, tan_el = np.broadcast_arrays(az, tan_el)
        best = np.full(az.shape, np.inf)

        # Floor (z = 0) and ceiling, as horizontal distances.
        with np.errstate(divide="ignore", invalid="ignore"):
            floor_d = np.where(tan_el < 0, sensor_z / -tan_el, np.inf)
            ceil_d = np.where(tan_el > 0, (CEILING - sensor_z) / tan_el, np.inf)
        best = np.minimum(best, np.minimum(floor_d, ceil_d))

        # Ray/segment intersection in 2D, vectorised over rays for each segment.
        for a, b, h in zip(self.seg_a, self.seg_b, self.seg_h):
            ex, ey = b[0] - a[0], b[1] - a[1]
            denom = dx * ey - dy * ex
            wx, wy = a[0] - ox, a[1] - oy
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (wx * ey - wy * ex) / denom   # distance along ray
                u = (wx * dy - wy * dx) / denom   # fraction along segment
                z = sensor_z + t * tan_el
            hit = (np.abs(denom) > 1e-12) & (t > 1e-6) & (u >= 0) & (u <= 1) & (z >= 0) & (z <= h)
            best = np.where(hit & (t < best), t, best)

        ranges = best / np.cos(el)
        ranges[(ranges > max_range) | (ranges < LIDAR_MIN_RANGE)] = np.inf
        return ranges

    def free(self, x, y, margin):
        hx, hy = self.width / 2 - margin, self.depth / 2 - margin
        if not (-hx <= x <= hx and -hy <= y <= hy):
            return False
        return not any(b.contains(x, y, margin) for b in self.boxes)


def loop_world():
    """8 x 6 m room with a central island the rover circles, plus wall clutter.

    The island forces a closed loop; shelves, a pillar and low crates give the
    narrow LiDAR cone distinct corners so scan matching has something to lock to.
    Two crates are 0.12 m tall, below the scan band, so they must not appear in
    the map.
    """
    boxes = [
        Box(-1.5, -0.7, 1.5, 0.7),            # island
        Box(-3.9, -1.0, -3.5, 0.6),           # west shelf
        Box(1.0, 2.6, 2.4, 3.0),              # north cabinet
        Box(-2.2, 2.5, -1.6, 3.0),            # north shelf stub
        Box(3.3, -0.4, 3.7, 0.0),             # east pillar
        Box(-0.6, -3.0, 0.2, -2.6, 0.9),      # south desk (in band)
        Box(2.6, -2.9, 3.6, -2.4, 0.5),       # south-east crate (in band)
        Box(-3.2, -2.9, -2.8, -2.5, 0.12),    # floor clutter (below band)
        Box(0.6, -1.4, 0.8, -1.2, 0.12),      # floor clutter on the path edge (below band)
    ]
    return World(8.0, 6.0, boxes)


def loop_route():
    """Closed rounded-rectangle route around the island, counter-clockwise."""
    corners = [(-2.6, -1.8), (2.6, -1.8), (2.6, 1.8), (-2.6, 1.8)]
    pts = []
    radius = 0.6
    for i, (cx, cy) in enumerate(corners):
        px, py = corners[i - 1]
        nx, ny = corners[(i + 1) % len(corners)]
        # Points `radius` before and after the corner, joined by a 5-point arc.
        def toward(x0, y0, x1, y1, d):
            length = math.hypot(x1 - x0, y1 - y0)
            return x0 + (x1 - x0) * d / length, y0 + (y1 - y0) * d / length
        start = toward(cx, cy, px, py, radius)
        end = toward(cx, cy, nx, ny, radius)
        for k in range(5):
            s = k / 4
            # Quadratic Bezier through the corner keeps the turn smooth.
            x = (1 - s) ** 2 * start[0] + 2 * (1 - s) * s * cx + s ** 2 * end[0]
            y = (1 - s) ** 2 * start[1] + 2 * (1 - s) * s * cy + s ** 2 * end[1]
            pts.append((x, y))
    return densify(pts, 0.1)


def densify(route, step):
    """Resample a closed route so consecutive points are at most `step` apart."""
    out = []
    for i, a in enumerate(route):
        b = route[(i + 1) % len(route)]
        n = max(1, math.ceil(math.dist(a, b) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    return out


def route_length(route):
    return sum(math.dist(route[i], route[(i + 1) % len(route)]) for i in range(len(route)))


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


class PurePursuit:
    """Follows a closed route; returns (v, w) for a unicycle at (x, y, yaw)."""

    def __init__(self, route, speed, lookahead=0.5):
        self.route = route
        self.speed = speed
        self.lookahead = lookahead
        self.index = 0

    def command(self, x, y, yaw):
        n = len(self.route)
        # Advance past waypoints closer than the lookahead.
        for _ in range(n):
            tx, ty = self.route[self.index]
            if math.hypot(tx - x, ty - y) > self.lookahead:
                break
            self.index = (self.index + 1) % n
        tx, ty = self.route[self.index]
        alpha = wrap(math.atan2(ty - y, tx - x) - yaw)
        curvature = 2 * math.sin(alpha) / max(math.hypot(tx - x, ty - y), 1e-3)
        return self.speed, max(-1.5, min(1.5, self.speed * curvature))


class DriftingOdometry:
    """ARKit-like odometry: integrates true motion with scale error and heading drift.

    `scale_error` multiplies travelled distance, `yaw_drift_per_m` adds heading
    error per metre travelled, and `yaw_noise` is a per-step random walk (rad).
    With all three at zero the odometry equals ground truth.
    """

    def __init__(self, scale_error=0.0, yaw_drift_per_m=0.0, yaw_noise=0.0, seed=7):
        self.scale_error = scale_error
        self.yaw_drift_per_m = yaw_drift_per_m
        self.yaw_noise = yaw_noise
        self.rng = np.random.default_rng(seed)
        self.x = self.y = self.yaw = 0.0

    def reset(self, x, y, yaw):
        self.x, self.y, self.yaw = x, y, yaw

    def step(self, ds, dyaw):
        noise = self.rng.normal(0.0, self.yaw_noise) if self.yaw_noise else 0.0
        self.yaw = wrap(self.yaw + dyaw + self.yaw_drift_per_m * abs(ds) + noise)
        d = ds * (1 + self.scale_error)
        self.x += d * math.cos(self.yaw)
        self.y += d * math.sin(self.yaw)
