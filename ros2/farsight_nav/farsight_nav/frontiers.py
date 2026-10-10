"""Frontier detection and selection on a Nav2 occupancy grid (no ROS imports; unit-tested).

A frontier cell is known-free and touches unknown space. Cells are grouped into
8-connected clusters; each cluster's goal is its free cell nearest the cluster
centroid. Explorer scores observed wide approaches, visible unknown area, clutter and
revisits, then commits to a broad direction. Nav2 owns complete-route planning.
"""

from collections import deque
from dataclasses import dataclass
import math

import numpy as np

FREE_MAX = 50        # OccupancyGrid values 0..FREE_MAX count as traversable free space
MIN_CELLS = 6        # ignore specks: 6 cells at 5 cm is a 30 cm opening


@dataclass
class Frontier:
    goal: tuple       # (x, y) in the grid frame, on a free frontier cell
    centroid: tuple   # (x, y) in the grid frame
    size_m: float     # frontier length (cells * resolution)
    samples: tuple = ()  # bounded observed boundary samples; centroid alone misses broad curved edges


def find(grid, resolution, origin, min_cells=MIN_CELLS):
    """grid: 2D int array (rows = y), -1 unknown, 0..100 cost. origin: (x, y) of cell (0, 0)."""
    grid = np.asarray(grid)
    free = (grid >= 0) & (grid <= FREE_MAX)
    unknown = grid < 0
    padded = np.pad(unknown, 1, constant_values=False)
    touches = (padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2] | padded[1:-1, 2:])
    cells = free & touches
    rows, cols = np.nonzero(cells)
    remaining = set(zip(rows.tolist(), cols.tolist()))
    frontiers = []
    while remaining:
        seed = remaining.pop()
        cluster, queue = [seed], deque([seed])
        while queue:
            r, c = queue.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    neighbour = (r + dr, c + dc)
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        cluster.append(neighbour)
                        queue.append(neighbour)
        if len(cluster) < min_cells:
            continue
        points = np.array(cluster, float)
        centre = points.mean(axis=0)
        goal_cell = points[np.argmin(((points - centre) ** 2).sum(axis=1))]
        to_xy = lambda rc: (origin[0] + (rc[1] + 0.5) * resolution, origin[1] + (rc[0] + 0.5) * resolution)
        ordered = sorted(cluster)
        indices = np.linspace(0, len(ordered) - 1, min(32, len(ordered)), dtype=int)
        samples = tuple(to_xy(ordered[i]) for i in indices)
        frontiers.append(Frontier(goal=to_xy(goal_cell), centroid=to_xy(centre),
                                  size_m=len(cluster) * resolution, samples=samples))
    return frontiers


def choose(frontiers, robot, blacklist=(), blacklist_radius=0.5, min_distance=0.25):
    """Legacy geometric chooser (comparison/compatibility); runtime uses Explorer."""
    best, best_score = None, math.inf
    for frontier in frontiers:
        dx, dy = frontier.goal[0] - robot[0], frontier.goal[1] - robot[1]
        distance = math.hypot(dx, dy)
        if distance < min_distance:
            continue  # already there: its unknown side gets seen from here or it is unreachable
        if any(math.dist(frontier.goal, bad) < blacklist_radius for bad in blacklist):
            continue
        turn = abs(math.atan2(math.sin(math.atan2(dy, dx) - robot[2]), math.cos(math.atan2(dy, dx) - robot[2])))
        score = distance - 1.5 * min(frontier.size_m, 2.0) + 0.4 * turn
        if score < best_score:
            best, best_score = frontier, score
    return best


def still_frontier(frontiers, goal, radius=0.6):
    """True while some frontier cell cluster goal stays near the current target."""
    return any(math.dist(f.goal, goal) < radius or math.dist(f.centroid, goal) < radius for f in frontiers)



@dataclass
class ExploreConfig:
    """Metres, seconds and dimensionless score weights; lower score wins."""

    min_corridor_width: float = 0.7
    approach_length: float = 1.2
    sensor_range: float = 4.0
    clearance_weight: float = 2.5
    information_weight: float = 3.0
    size_weight: float = 1.5
    distance_weight: float = 0.35
    turn_weight: float = 0.8
    clutter_weight: float = 3.0
    revisit_weight: float = 3.0
    revisit_decay_s: float = 60.0
    commitment_s: float = 4.0
    switch_margin: float = 0.8
    transition_distance: float = 0.6

    def __post_init__(self):
        for name, value in vars(self).items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Explore {name} must be finite and nonnegative")
        if self.min_corridor_width < 0.34 or self.approach_length <= 0 or self.revisit_decay_s <= 0:
            raise ValueError("Explore needs a rover-width corridor, positive approach and visit decay")
        if not 0.3 <= self.sensor_range <= 4.0:
            raise ValueError("Explore sensor_range must be within measured LiDAR range 0.3..4 m")


def clearance_map(obstacles, resolution, limit=2.0):
    """Capped Euclidean distance transform, NumPy only, including map edges.

    The horizontal nearest obstacle is computed with cumulative minima; vertical
    shifts finish the separable squared transform. O(cells * limit/resolution),
    with no Python loop over pixels. Subtract half a cell diagonal so clearance
    bounds occupied cell *area*, rather than just occupied cell centres.
    """
    obstacles = np.pad(obstacles, 1, constant_values=True)
    cols = np.arange(obstacles.shape[1])[None, :]
    left = np.maximum.accumulate(np.where(obstacles, cols, -1000000), axis=1)
    right = np.minimum.accumulate(np.where(obstacles, cols, 1000000)[:, ::-1], axis=1)[:, ::-1]
    horizontal = np.minimum(cols - left, right - cols).astype(float) ** 2
    squared = np.minimum(horizontal, (limit / resolution + 1) ** 2)
    for offset in range(1, min(obstacles.shape[0], math.ceil(limit / resolution) + 2)):
        np.minimum(squared[offset:], horizontal[:-offset] + offset ** 2, out=squared[offset:])
        np.minimum(squared[:-offset], horizontal[offset:] + offset ** 2, out=squared[:-offset])
    return np.maximum(0, np.sqrt(squared[1:-1, 1:-1]) * resolution - resolution / math.sqrt(2))


def thin_obstacles(obstacles, resolution, max_width=0.35):
    """Mask small 8-connected lethal/inscribed blobs; inflation is not a pole."""
    remaining = set(zip(*np.nonzero(obstacles)))
    thin = np.zeros_like(obstacles)
    while remaining:
        seed = remaining.pop()
        blob, queue = [seed], deque([seed])
        while queue:
            r, c = queue.popleft()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    other = (r + dr, c + dc)
                    if other in remaining:
                        remaining.remove(other)
                        blob.append(other)
                        queue.append(other)
        rows, cols = zip(*blob)
        if max(max(rows) - min(rows) + 1, max(cols) - min(cols) + 1) * resolution <= max_width:
            thin[rows, cols] = True
    return thin


class SweptFootprint:
    """Physical body support through the depth sensor's near-field blind ring.

    Retain actual sampled poses in odom, not extrapolated commands or a guessed
    future path. Fill unknown only: current measured costs always take priority.
    """

    def __init__(self, radius=0.35):
        if not math.isfinite(radius) or not 0.17 <= radius <= 0.35:
            raise ValueError("Swept body radius must fit the physical rover: 0.17..0.35 m")
        self.radius = radius
        self.poses = {}

    def record(self, position):
        if np.isfinite(position[:2]).all():
            key = tuple(np.floor(np.asarray(position[:2]) / 0.05).astype(int))
            self.poses[key] = tuple(position[:2])

    def apply(self, grid, resolution, origin, to_map=None):
        result = np.array(grid, copy=True)
        extent = math.ceil(self.radius / resolution) + 1
        offsets = np.arange(-extent, extent + 1)
        dr, dc = np.meshgrid(offsets, offsets, indexing="ij")
        for point in self.poses.values():
            point = to_map(*point)[:2] if to_map else point
            centre = np.floor((np.asarray(point) - origin) / resolution).astype(int)
            rr, cc = centre[1] + dr, centre[0] + dc
            x, y = origin[0] + (cc + 0.5) * resolution, origin[1] + (rr + 0.5) * resolution
            inside = ((rr >= 0) & (rr < grid.shape[0]) & (cc >= 0) & (cc < grid.shape[1])
                      & ((x - point[0]) ** 2 + (y - point[1]) ** 2 <= self.radius ** 2))
            rows, cols = rr[inside], cc[inside]
            unknown = result[rows, cols] < 0
            result[rows[unknown], cols[unknown]] = 0
        return result


def observed_window(grid, resolution, origin, margin=4.0):
    """Discard distant all-unknown map padding, retaining the full gain horizon.

    The default global map is 800x800 cells although initially only ~600 cells
    are observed. All observed costs plus a sensor-range rim remain unchanged.
    """
    rows, cols = np.nonzero(grid >= 0)
    if not len(rows):
        return grid, origin
    pad = math.ceil(margin / resolution)
    r0, r1 = max(0, int(rows.min()) - pad), min(grid.shape[0], int(rows.max()) + pad + 1)
    c0, c1 = max(0, int(cols.min()) - pad), min(grid.shape[1], int(cols.max()) + pad + 1)
    return grid[r0:r1, c0:c1], (origin[0] + c0 * resolution, origin[1] + r0 * resolution)


class Explorer:
    """Costmap-backed open-space chooser with decaying path visits and hysteresis.

    Only observed traversable cells support a goal or its short approach. Nav2
    still plans the complete route and rejects unreachable candidates. Unknown
    cells contribute prospective information only, never clearance/free space.
    """

    def __init__(self, config=None):
        self.config = config or ExploreConfig()
        self.visits = {}  # world-aligned 25 cm cells survive rolling costmap origins
        self.last_robot = None
        self.committed = None
        self.committed_at = 0.0
        self.committed_frontier = None
        self.committed_waypoint = None

    def release(self):
        self.committed = None
        self.committed_frontier = None
        self.committed_waypoint = None

    def _visit(self, robot, now):
        decay = max(0.1, self.config.revisit_decay_s)
        self.visits = {cell: at for cell, at in self.visits.items() if now - at < 5 * decay}
        # Fill the segment between 1 Hz poses so a moving rover records a path.
        start = self.last_robot if self.last_robot is not None else robot[:2]
        steps = max(1, math.ceil(math.dist(start, robot[:2]) / 0.125))
        for t in np.linspace(0, 1, min(steps + 1, 200)):
            point = np.asarray(start) * (1 - t) + np.asarray(robot[:2]) * t
            self.visits[tuple(np.floor(point / 0.25).astype(int))] = now
        self.last_robot = robot[:2]

    def choose(self, candidates, robot, grid, resolution, origin, now, blacklist=()):
        cfg = self.config
        grid = np.asarray(grid)
        if grid.ndim != 2 or resolution <= 0:
            self.release()
            return None
        self._visit(robot, now)
        free = (grid >= 0) & (grid <= FREE_MAX)
        # Nav2 OccupancyGrid translates lethal to 100 and inscribed to 99.
        clearance = clearance_map(grid >= 99, resolution)
        support = clearance_map(~free, resolution)
        thin = thin_obstacles(grid >= 99, resolution)
        half_width = cfg.min_corridor_width / 2
        scored = []
        # Boundary samples/centroids move as the camera clears cells. Keep the
        # exact active waypoint as an independent candidate and revalidate its
        # full current approach on every score pass, even during a missing cluster.
        approaches = [self.committed_frontier] if self.committed_frontier is not None else []
        for candidate in candidates:
            approaches.append(candidate)
            approaches.extend(Frontier(point, point, candidate.size_m) for point in candidate.samples)
        for candidate in approaches:
            if any(math.dist(candidate.goal, bad) < 0.5 for bad in blacklist):
                continue
            dx, dy = np.asarray(candidate.goal) - robot[:2]
            distance = math.hypot(dx, dy)
            if distance < 0.25:
                continue
            direction = np.array((dx, dy)) / distance
            # Stop one footprint short of the unseen boundary, never beyond it.
            if candidate is self.committed_frontier:
                goal = np.asarray(self.committed_waypoint)
            else:
                goal = np.asarray(candidate.goal) - direction * (half_width + resolution)
            if math.dist(goal, robot[:2]) < 0.25:
                continue
            approach_distance = math.dist(goal, robot[:2])
            approach_direction = (goal - robot[:2]) / approach_distance
            start = goal - approach_direction * min(cfg.approach_length, approach_distance)
            approach = np.linspace(start, goal, max(2, math.ceil(cfg.approach_length / resolution) + 1))
            rc = np.floor((approach - origin) / resolution).astype(int)[:, ::-1]
            if (rc < 0).any() or (rc[:, 0] >= grid.shape[0]).any() or (rc[:, 1] >= grid.shape[1]).any():
                continue
            rr, cc = rc.T
            if not free[rr, cc].all() or support[rr, cc].min() < half_width:
                continue
            width = min(float(clearance[rr, cc].min()), 1.5)
            turn = abs(math.atan2(math.sin(math.atan2(dy, dx) - robot[2]),
                                  math.cos(math.atan2(dy, dx) - robot[2])))
            information = self._information(grid, goal, resolution, origin)
            rows, cols = np.nonzero(thin)
            clutter = 0.0
            if len(rows):
                points = np.column_stack((cols + 0.5, rows + 0.5)) * resolution + origin
                relative = points - start
                along = np.clip(relative @ approach_direction, 0, math.dist(start, goal))
                near = np.linalg.norm(relative - along[:, None] * approach_direction, axis=1) < 0.8
                # Density of occupied thin-clutter area, scaled to several 10 cm legs.
                clutter = min(1.0, float(near.sum()) * resolution ** 2 / 0.12)
            revisit = 0.0
            for cell, at in self.visits.items():
                point = (np.asarray(cell) + 0.5) * 0.25
                if math.dist(point, goal) < 0.75:
                    revisit += math.exp(-(now - at) / max(0.1, cfg.revisit_decay_s))
            revisit = min(1.0, revisit / 4)
            score = (cfg.distance_weight * distance + cfg.turn_weight * turn
                     + cfg.clutter_weight * clutter + cfg.revisit_weight * revisit
                     - cfg.clearance_weight * width - cfg.information_weight * information
                     - cfg.size_weight * min(candidate.size_m, 3.0))
            scored.append((score, Frontier(tuple(goal), candidate.centroid, candidate.size_m), candidate.goal, candidate))
        if not scored:
            self.release()
            return None
        scored.sort(key=lambda item: (item[0], item[2]))
        best = scored[0]
        if self.committed is not None:
            current = next((item for item in scored if item[3] is self.committed_frontier),
                           min(scored, key=lambda item: math.dist(item[2], self.committed)))
            if math.dist(current[2], self.committed) < 0.6:
                next_patch = [item for item in scored if math.dist(item[1].goal, robot[:2]) >= cfg.transition_distance]
                if math.dist(current[1].goal, robot[:2]) < cfg.transition_distance and next_patch:
                    best = next_patch[0]  # preselect ahead; do not impose a minimum 60 cm stride
                elif now - self.committed_at < cfg.commitment_s or best[0] >= current[0] - cfg.switch_margin:
                    best = current
        if self.committed is None or math.dist(best[2], self.committed) >= 0.6:
            self.committed_at = now
        self.committed = best[2]
        self.committed_frontier, self.committed_waypoint = best[3], best[1].goal
        return best[1]

    def _information(self, grid, goal, resolution, origin):
        """Unknown area in a range disc, occluded by known nontraversable cells.

        Bounded polar ray sampling avoids a per-goal full-map visibility pass.
        Unknown is prospective gain; it never enters the approach support mask.
        """
        radius = self.config.sensor_range
        if radius <= 0:
            return 0.0
        angles = np.linspace(-math.pi, math.pi, 96, endpoint=False)
        distances = np.arange(resolution, radius + resolution / 2, resolution)
        points = np.asarray(goal) + distances[:, None, None] * np.column_stack((np.cos(angles), np.sin(angles)))
        rc = np.floor((points - origin) / resolution).astype(int)[..., ::-1]
        valid = ((rc[..., 0] >= 0) & (rc[..., 0] < grid.shape[0])
                 & (rc[..., 1] >= 0) & (rc[..., 1] < grid.shape[1]))
        rr = np.clip(rc[..., 0], 0, grid.shape[0] - 1)
        cc = np.clip(rc[..., 1], 0, grid.shape[1] - 1)
        values = grid[rr, cc]
        visible = np.logical_and.accumulate(valid & (values <= FREE_MAX), axis=0)
        unknown = visible & (values < 0)
        cells = np.unique(rr[unknown] * grid.shape[1] + cc[unknown])
        return min(1.0, len(cells) * resolution ** 2 / (math.pi * radius ** 2))
