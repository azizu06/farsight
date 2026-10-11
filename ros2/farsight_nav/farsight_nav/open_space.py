"""Commit Explore to open mapped space, including rooms with no frontier.

Nav2's costmap already describes traversable space. This planner does not add
another corridor-width or camera-ray veto. Nav2 executes the resulting goal.
"""
from dataclasses import dataclass
import heapq
import math

import numpy as np

from . import frontiers


@dataclass
class OpenSpaceGoal:
    goal: tuple
    centroid: tuple
    size_m: float
    route: tuple


def choose(grid, resolution, origin, robot, *, current_goal=None, blacklist=(),
           visits=None, now=0.0, transition_m=0.6):
    """Choose an open destination through a connected mapped passage.

    Wide routes cost less than narrow routes; width is a preference, not a
    second footprint requirement. A running destination remains committed until
    approached, blocked or blacklisted. Unknown space is left to frontier/Nav2
    planning, rather than being called an open interior region.
    """
    free = (grid >= 0) & (grid <= frontiers.FREE_MAX)
    rows, cols = free.shape
    start_col = math.floor((robot[0] - origin[0]) / resolution)
    start_row = math.floor((robot[1] - origin[1]) / resolution)
    if not (0 <= start_row < rows and 0 <= start_col < cols):
        return None
    clearance = frontiers.clearance_map(~free, resolution)
    start = start_row * cols + start_col
    cost = np.full(rows * cols, np.inf)
    distance = np.full(rows * cols, np.inf)
    width = np.full(rows * cols, -np.inf)
    previous = np.full(rows * cols, -1, dtype=np.int32)
    cost[start] = distance[start] = 0.0
    width[start] = float(clearance[start_row, start_col]) if free[start_row, start_col] else np.inf
    queue = [(-width[start], 0.0, start)]
    while queue:
        negative_width, travelled, index = heapq.heappop(queue)
        if travelled != cost[index] or -negative_width != width[index]:
            continue
        row, col = divmod(index, cols)
        for dr, dc in ((-1, 0), (0, 1), (1, 0), (0, -1)):
            rr, cc = row + dr, col + dc
            if not (0 <= rr < rows and 0 <= cc < cols and free[rr, cc]):
                continue
            other = rr * cols + cc
            corridor = min(width[index], float(clearance[rr, cc]))
            candidate = travelled + resolution
            if corridor > width[other] or (corridor == width[other] and candidate < cost[other]):
                width[other] = corridor
                cost[other] = candidate
                distance[other] = distance[index] + resolution
                previous[other] = index
                heapq.heappush(queue, (-corridor, candidate, other))

    def xy(index):
        row, col = divmod(int(index), cols)
        return (origin[0] + (col + 0.5) * resolution,
                origin[1] + (row + 0.5) * resolution)

    def blocked(goal):
        return any(math.dist(goal, bad[:2]) < 0.5 for bad in blacklist)

    def result(index, goal=None):
        goal = tuple(goal) if goal is not None else xy(index)
        route, cursor = [], int(index)
        while cursor != start:
            route.append(xy(cursor))
            cursor = int(previous[cursor])
        route.append(tuple(robot[:2]))
        route.reverse()
        route[-1] = goal
        return OpenSpaceGoal(goal, goal, 2 * float(clearance.flat[index]), tuple(route))

    if current_goal is not None and math.dist(robot[:2], current_goal) > transition_m:
        cc = math.floor((current_goal[0] - origin[0]) / resolution)
        rr = math.floor((current_goal[1] - origin[1]) / resolution)
        if (0 <= rr < rows and 0 <= cc < cols and free[rr, cc]
                and np.isfinite(cost[rr * cols + cc]) and not blocked(current_goal)):
            return result(rr * cols + cc, current_goal)

    best, best_score = None, -math.inf
    visits = visits or {}
    for index in np.flatnonzero(np.isfinite(cost) & free.ravel()):
        goal = xy(index)
        # A destination inside the existing Nav2 goal tolerance causes churn.
        if math.dist(robot[:2], goal) < 0.25 or blocked(goal):
            continue
        cell = tuple(np.floor(np.asarray(goal) / 0.25).astype(int))
        revisit = math.exp(-(now - visits[cell]) / 60) if cell in visits else 0.0
        score = (2.5 * clearance.flat[index] + width[index] + 0.2 * min(distance[index], 4.0)
                 - 0.08 * cost[index] - 0.12 * frontiers.turn_to(robot, goal)
                 - 3.0 * revisit)
        if score > best_score:
            best, best_score = int(index), float(score)
    return result(best) if best is not None else None
