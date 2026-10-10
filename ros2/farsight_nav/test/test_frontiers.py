"""Frontier search on synthetic grids: python -m pytest ros2/farsight_nav/test."""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from farsight_nav import frontiers  # noqa: E402


def room_grid():
    """4 x 4 m at 5 cm: left half seen and free, right half unknown, a wall with a door at x = 1 m."""
    grid = np.full((80, 80), -1, np.int16)
    grid[:, :40] = 0
    grid[:, 20] = 100            # wall at x = 1.0 m ...
    grid[30:46, 20] = 0          # ... with a 0.8 m door
    grid[61, 7] = -1             # a one-cell unseen speck: not a frontier
    return grid


def test_find_clusters_and_goals_on_free_cells():
    found = frontiers.find(room_grid(), 0.05, (0.0, 0.0))
    assert len(found) == 1  # the edge of the seen half; the speck is too small
    edge = found[0]
    assert math.isclose(edge.goal[0], 39.5 * 0.05)
    assert edge.size_m >= 3.9
    grid = room_grid()
    row, col = int(edge.goal[1] / 0.05), int(edge.goal[0] / 0.05)
    assert 0 <= grid[row, col] <= frontiers.FREE_MAX


def test_choose_prefers_near_big_ahead_and_skips_blacklist():
    near = frontiers.Frontier(goal=(1.0, 0.0), centroid=(1.0, 0.0), size_m=1.0)
    behind = frontiers.Frontier(goal=(-1.0, 0.0), centroid=(-1.0, 0.0), size_m=1.0)
    far = frontiers.Frontier(goal=(4.0, 0.0), centroid=(4.0, 0.0), size_m=1.0)
    robot = (0.0, 0.0, 0.0)
    assert frontiers.choose([behind, far, near], robot) is near
    assert frontiers.choose([behind, near], robot, blacklist=[(1.1, 0.0)]) is behind
    assert frontiers.choose([frontiers.Frontier((0.1, 0), (0.1, 0), 1.0)], robot) is None
    assert frontiers.still_frontier([near], (1.2, 0.1))
    assert not frontiers.still_frontier([far], (1.2, 0.1))


def test_goal_goes_a_little_past_the_frontier():
    edge = frontiers.Frontier(goal=(1.0, 0.0), centroid=(1.0, 0.0), size_m=1.0)
    x, y = frontiers.beyond(edge, (0.0, 0.0, 0.0))
    assert math.isclose(x, 1.3) and math.isclose(y, 0.0)
