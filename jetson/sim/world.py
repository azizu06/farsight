"""Closed-loop indoor rover simulator: floor plans, pop-up obstacles, a walking person.

Everything is in the phone's ARKit world frame (metres, Y up, the rover starts
facing -Z), so simulated frames take the same path as a LiDAR iPhone. The rover
pose is (x, z, yaw); yaw is a counter-clockwise rotation about +Y, which equals
the ROS odom yaw used by the navigation bridge. Applied motor commands
(normalized v, w with positive w turning right) integrate a differential-drive
model; walls stop the rover and count as collisions.
"""

from dataclasses import dataclass, field
import math
import threading
import time

import cv2
import numpy as np

from jetson.mapping.geometry import Calibration

CEILING = 2.6


@dataclass
class Block:
    """Axis-aligned box on the floor: x0..x1, z0..z1 (metres), height above floor."""
    x0: float
    z0: float
    x1: float
    z1: float
    height: float = CEILING
    color: tuple = (120, 128, 132)
    # Pop-up obstacles: appear once the rover is within `appear_within` metres of
    # the box centre, or once `appear_after_s` seconds of motion have elapsed.
    appear_within: float | None = None
    appear_after_s: float | None = None
    visible: bool = True
    name: str = ""

    def __post_init__(self):
        self.x0, self.x1 = sorted((self.x0, self.x1))
        self.z0, self.z1 = sorted((self.z0, self.z1))
        if self.appear_within is not None or self.appear_after_s is not None:
            self.visible = False

    @property
    def centre(self):
        return (self.x0 + self.x1) / 2, (self.z0 + self.z1) / 2

    def distance(self, x, z):
        dx = max(self.x0 - x, 0, x - self.x1)
        dz = max(self.z0 - z, 0, z - self.z1)
        return math.hypot(dx, dz)


@dataclass
class Person:
    """Upright cylinder walking a closed route of (x, z) waypoints."""
    route: list
    speed: float = 0.3
    radius: float = 0.22
    height: float = 1.7
    distance: float = 0.0
    pause_s: float = 0.0
    paused_until: float = 0.0

    def position(self):
        points = self.route
        if len(points) == 1:
            return points[0]
        lengths = [math.dist(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]
        remaining = self.distance % sum(lengths)
        for i, length in enumerate(lengths):
            if remaining <= length:
                a, b = points[i], points[(i + 1) % len(points)]
                t = remaining / length if length else 0
                return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            remaining -= length
        return points[0]


@dataclass
class World:
    name: str
    blocks: list
    start: tuple = (0.0, 0.0, 0.0)  # x, z, yaw
    people: list = field(default_factory=list)
    description: str = ""


class RoverSim:
    """Thread-safe simulated rover in a World. Call apply() with every applied command."""

    CAMERA_HEIGHT = 0.15     # phone optical centre above the floor
    RADIUS = 0.12            # collision circle of the small chassis
    SPEED_MPS = 0.45         # forward speed at normalized v = 1
    TURN_RADPS = 3.0         # yaw rate at normalized w = 1
    MAX_DT = 0.25
    COVERAGE_CELL = 0.25     # metres; floor coverage is counted on this grid

    def __init__(self, world, *, width=160, height=120, fx=135.0, max_depth=5.0, seed=0,
                 clock=time.monotonic, speed_mps=None, turn_radps=None):
        self.world = world
        self.clock = clock
        self.lock = threading.RLock()
        self.calibration = Calibration(width, height, fx, fx, (width - 1) / 2, (height - 1) / 2)
        self.max_depth = max_depth
        self.rng = np.random.default_rng(seed)
        self.speed_mps = speed_mps or self.SPEED_MPS
        self.turn_radps = turn_radps or self.TURN_RADPS
        self.reset()

    def reset(self):
        with self.lock:
            self.x, self.z, self.yaw = self.world.start
            self.v = self.w = 0.0
            self.last = self.clock()
            self.moving_s = 0.0
            self.collisions = 0
            self.travelled = 0.0
            self.min_clearance = math.inf
            self.trace = [(round(self.x, 3), round(self.z, 3))]
            for block in self.world.blocks:
                block.visible = block.appear_within is None and block.appear_after_s is None
            for person in self.world.people:
                person.distance = 0.0
            self.appeared = []
            self.seen = set()        # floor cells (COVERAGE_CELL) any depth frame has hit

    # -- dynamics -------------------------------------------------------------------

    def apply(self, v, w, blocked=False):
        """ControlState.on_output hook: the command applied from now on."""
        with self.lock:
            self.advance()
            self.v, self.w = float(v), float(w)

    def advance(self, now=None):
        with self.lock:
            now = self.clock() if now is None else now
            dt = min(max(now - self.last, 0.0), self.MAX_DT)
            self.last = now
            if dt <= 0:
                return
            for person in self.world.people:
                person.distance += person.speed * dt
            steps = max(1, int(math.ceil(dt / 0.02)))
            for _ in range(steps):
                self._integrate(dt / steps)
            self._update_popups()

    def _integrate(self, dt):
        speed = self.v * self.speed_mps
        yaw = self.yaw - self.w * self.turn_radps * dt  # positive w turns right (clockwise)
        mid = (self.yaw + yaw) / 2
        x = self.x - math.sin(mid) * speed * dt
        z = self.z - math.cos(mid) * speed * dt
        if abs(self.v) > 1e-3 or abs(self.w) > 1e-3:
            self.moving_s += dt
        clearance = self.clearance(x, z)
        if clearance < self.RADIUS:
            if self.clearance(self.x, self.z) >= self.RADIUS or clearance < self.clearance(self.x, self.z):
                self.collisions += abs(speed) > 1e-3
                x, z = self.x, self.z  # wheels slip against the wall; rotation still applies
        self.travelled += math.hypot(x - self.x, z - self.z)
        self.x, self.z, self.yaw = x, z, math.atan2(math.sin(yaw), math.cos(yaw))
        self.min_clearance = min(self.min_clearance, self.clearance(self.x, self.z))
        if math.dist(self.trace[-1], (self.x, self.z)) > 0.05:
            self.trace.append((round(self.x, 3), round(self.z, 3)))
            del self.trace[:-4000]

    def _update_popups(self):
        for block in self.world.blocks:
            if block.visible:
                continue
            near = block.appear_within is not None and math.dist((self.x, self.z), block.centre) <= block.appear_within
            late = block.appear_after_s is not None and self.moving_s >= block.appear_after_s
            if near or late:
                block.visible = True
                self.appeared.append(dict(name=block.name, at_moving_s=round(self.moving_s, 2),
                                          rover=[round(self.x, 3), round(self.z, 3)]))

    def clearance(self, x, z):
        """Distance from (x, z) to the nearest visible block or person surface."""
        best = math.inf
        for block in self.world.blocks:
            if block.visible:
                best = min(best, block.distance(x, z))
        for person in self.world.people:
            px, pz = person.position()
            best = min(best, math.hypot(px - x, pz - z) - person.radius)
        return best

    # -- sensors --------------------------------------------------------------------

    def camera_pose(self, yaw_offset=0.0):
        with self.lock:
            yaw = self.yaw + yaw_offset
            c, s = math.cos(yaw), math.sin(yaw)
            pose = np.eye(4)
            pose[:3, :3] = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
            pose[:3, 3] = [self.x, 0.0, self.z]
            return pose

    def _scene(self):
        blocks = [b for b in self.world.blocks if b.visible]
        floor = -self.CAMERA_HEIGHT
        lo = np.array([[b.x0, floor, b.z0] for b in blocks]).reshape(-1, 3)
        hi = np.array([[b.x1, floor + b.height, b.z1] for b in blocks]).reshape(-1, 3)
        colors = np.array([b.color for b in blocks], np.float64).reshape(-1, 3)
        people = [(person.position(), person) for person in self.world.people]
        return lo, hi, colors, people, floor

    def render(self, yaw_offset=0.0):
        """Depth (optical-axis metres, 0 = no return), RGB uint8 and the camera-to-world pose."""
        k = self.calibration
        with self.lock:
            pose = self.camera_pose(yaw_offset)
            lo, hi, colors, people, floor = self._scene()
        v, u = np.mgrid[:k.height, :k.width]
        local = np.stack(((u - k.cx) / k.fx, -(v - k.cy) / k.fy, -np.ones((k.height, k.width))), -1)
        rays = (local @ pose[:3, :3].T).reshape(-1, 3)
        origin = pose[:3, 3]
        safe = np.where(np.abs(rays) < 1e-9, 1e-9, rays)
        nearest = np.full(len(rays), np.inf)
        color = np.zeros((len(rays), 3))
        # Floor and ceiling planes.
        floor_t = None
        for plane, tint in ((floor, (70, 74, 70)), (floor + CEILING, (205, 205, 200))):
            t = (plane - origin[1]) / safe[:, 1]
            hit = origin + rays * t[:, None]
            ok = (t > 0.05) & (t < nearest)
            checker = ((np.floor(hit[:, 0] * 2) + np.floor(hit[:, 2] * 2)) % 2) * 18
            nearest[ok] = t[ok]
            color[ok] = np.asarray(tint) + checker[ok, None]
            if floor_t is None:
                floor_t = np.where(ok, t, np.nan)
        if len(lo):
            first = (lo[None] - origin) / safe[:, None]
            last = (hi[None] - origin) / safe[:, None]
            entry = np.minimum(first, last).max(-1)
            exit_ = np.maximum(first, last).min(-1)
            entry = np.where((entry > 0.05) & (entry <= exit_), entry, np.inf)
            index = np.argmin(entry, axis=1)
            t = entry[np.arange(len(rays)), index]
            ok = t < nearest
            hit = origin + rays * t[:, None]
            hit = np.where(np.isfinite(hit), hit, 0)
            band = (((hit[:, 1] - floor) * 4) % 1 < .08) * 30 + ((np.floor(hit[:, 0] * 3) + np.floor(hit[:, 2] * 3)) % 2) * 8
            nearest[ok] = t[ok]
            color[ok] = colors[index[ok]] + band[ok, None]
        for (px, pz), person in people:
            ox, oz = origin[0] - px, origin[2] - pz
            a = rays[:, 0] ** 2 + rays[:, 2] ** 2
            b = 2 * (ox * rays[:, 0] + oz * rays[:, 2])
            c = ox ** 2 + oz ** 2 - person.radius ** 2
            disc = b * b - 4 * a * c
            root = np.sqrt(np.maximum(disc, 0))
            t = np.where(disc >= 0, (-b - root) / (2 * np.maximum(a, 1e-12)), np.inf)
            y = origin[1] + rays[:, 1] * t
            ok = (t > 0.05) & (t < nearest) & (y >= floor) & (y <= floor + person.height)
            nearest[ok] = t[ok]
            color[ok] = (176, 74, 58)
        floor_hit = (floor_t == nearest) & (nearest <= self.max_depth)
        if floor_hit.any():
            points = origin + rays[floor_hit] * nearest[floor_hit, None]
            cells = np.floor(points[:, [0, 2]] / self.COVERAGE_CELL).astype(int)
            with self.lock:
                self.seen.update(map(tuple, np.unique(cells, axis=0).tolist()))
        depth = np.where(nearest <= self.max_depth, nearest, 0).reshape(k.height, k.width)
        noise = self.rng.normal(0, 0.004, depth.shape) * (depth > 0)
        depth = np.where(depth > 0, depth + noise, 0).astype(np.float32)
        rgb = np.clip(color, 0, 255).astype(np.uint8).reshape(k.height, k.width, 3)
        return depth, rgb, pose

    def ultrasonic_cm(self, max_cm=206):
        """Forward HC-SR04 range; None without an echo.

        The Uno's 12 ms echo timeout ends at ~206 cm, so open space reads as no
        echo, which the proximity stop (when on) treats as unknown.
        """
        with self.lock:
            best = math.inf
            for spread in (-0.13, 0.0, 0.13):
                yaw = self.yaw + spread
                dx, dz = -math.sin(yaw), -math.cos(yaw)
                distance = 0.05
                while distance < max_cm / 100:
                    if self.clearance(self.x + dx * distance, self.z + dz * distance) <= 0.0:
                        best = min(best, distance)
                        break
                    distance += 0.03
            return round(best * 100) if best * 100 <= max_cm else None

    def person_boxes(self, depth, pose):
        """Ground-truth normalized boxes (tracker-style int ids) for people visible in ``depth``."""
        k = self.calibration
        boxes = []
        with self.lock:
            world_to_camera = np.linalg.inv(pose)
            floor = -self.CAMERA_HEIGHT
            for index, person in enumerate(self.world.people):
                px, pz = person.position()
                centre = world_to_camera @ np.array([px, floor + person.height / 2, pz, 1])
                if centre[2] >= -0.3:
                    continue  # behind or at the camera
                distance = -centre[2]
                us, vs = [], []
                for y in (floor, floor + person.height):
                    point = world_to_camera @ np.array([px, y, pz, 1])
                    for offset in (-person.radius, person.radius):  # cylinder silhouette edges
                        us.append(k.cx + k.fx * (point[0] + offset) / -point[2])
                        vs.append(k.cy - k.fy * point[1] / -point[2])
                x0, x1 = np.clip([min(us), max(us)], 0, k.width - 1)
                y0, y1 = np.clip([min(vs), max(vs)], 0, k.height - 1)
                if x1 - x0 < 2 or y1 - y0 < 4:
                    continue
                u, v = int((x0 + x1) / 2), int(np.clip((y0 + y1) / 2, 0, k.height - 1))
                seen = depth[v, u]
                if seen <= 0 or seen < distance - person.radius - 0.15:
                    continue  # occluded by a wall or box
                boxes.append(dict(id=index, cls="person", class_id=0, conf=0.99, simulated=True,
                                  x=float(x0 / k.width), y=float(y0 / k.height),
                                  w=float((x1 - x0) / k.width), h=float((y1 - y0) / k.height)))
        return boxes

    def reachable_cells(self):
        """Floor cells the chassis can reach from the start among the walls visible at start."""
        if getattr(self, "_reachable", None) is None:
            size = self.COVERAGE_CELL
            static = [b for b in self.world.blocks if b.appear_within is None and b.appear_after_s is None]
            def free(cell):
                x, z = (cell[0] + 0.5) * size, (cell[1] + 0.5) * size
                return all(b.distance(x, z) >= self.RADIUS for b in static)
            x0, z0 = min(b.x0 for b in static), min(b.z0 for b in static)
            x1, z1 = max(b.x1 for b in static), max(b.z1 for b in static)
            start = (math.floor(self.world.start[0] / size), math.floor(self.world.start[1] / size))
            seen, frontier = {start}, [start]
            while frontier:
                cx, cz = frontier.pop()
                for nx, nz in ((cx + 1, cz), (cx - 1, cz), (cx, cz + 1), (cx, cz - 1)):
                    if ((nx, nz) not in seen and x0 <= nx * size <= x1 and z0 <= nz * size <= z1
                            and free((nx, nz))):
                        seen.add((nx, nz))
                        frontier.append((nx, nz))
            self._reachable = frozenset(seen)
        return self._reachable

    def coverage(self):
        reachable = self.reachable_cells()
        with self.lock:
            return len(self.seen & reachable) / max(1, len(reachable))

    def state(self):
        with self.lock:
            return dict(world=self.world.name, x=round(self.x, 3), z=round(self.z, 3),
                        yaw_deg=round(math.degrees(self.yaw), 1), v=self.v, w=self.w,
                        collisions=self.collisions, travelled_m=round(self.travelled, 3),
                        min_clearance_m=None if math.isinf(self.min_clearance) else round(self.min_clearance, 3),
                        moving_s=round(self.moving_s, 2), appeared=list(self.appeared),
                        people=[[round(c, 3) for c in p.position()] for p in self.world.people],
                        obstacles=[dict(name=b.name, visible=b.visible, box=[b.x0, b.z0, b.x1, b.z1],
                                        height=b.height) for b in self.world.blocks if b.name],
                        trace=self.trace[-400:], speed_mps=self.speed_mps, turn_radps=self.turn_radps,
                        camera_height_m=self.CAMERA_HEIGHT, coverage=round(self.coverage(), 3))

    def jpeg(self, rgb):
        _, encoded = cv2.imencode(".jpg", cv2.cvtColor(cv2.resize(rgb, (320, 240)), cv2.COLOR_RGB2BGR))
        return encoded.tobytes()
