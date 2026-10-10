"""Frontier detection and selection on a Nav2 occupancy grid (no ROS imports; unit-tested).

A frontier cell is known-free and touches unknown space. Cells are grouped into
8-connected clusters; each cluster's goal is its free cell nearest the cluster
centroid. Selection prefers big, near frontiers roughly ahead of the rover and
skips goals that already failed.
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
        frontiers.append(Frontier(goal=to_xy(goal_cell), centroid=to_xy(centre), size_m=len(cluster) * resolution))
    return frontiers


def choose(frontiers, robot, blacklist=(), blacklist_radius=0.5, min_distance=0.25):
    """Best frontier for robot (x, y, yaw), or None. Lower score wins."""
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


def beyond(frontier, robot, distance=0.3):
    """Goal a little past the frontier, into the unknown, so the rover arrives facing it."""
    dx, dy = frontier.goal[0] - robot[0], frontier.goal[1] - robot[1]
    norm = math.hypot(dx, dy) or 1.0
    return frontier.goal[0] + distance * dx / norm, frontier.goal[1] + distance * dy / norm
