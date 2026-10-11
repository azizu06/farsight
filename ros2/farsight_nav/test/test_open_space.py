"""The human's central-obstacle sketch, represented as a mapped-room fixture."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from farsight_nav import frontiers, open_space


def sketch():
    resolution, origin = 0.05, (-4.0, -3.0)
    y, x = np.indices((120, 160))
    x, y = origin[0] + (x + .5) * resolution, origin[1] + (y + .5) * resolution
    occupied = ((x * x + y * y < .8 ** 2)
                | ((x + 3) ** 2 + (y - 2.2) ** 2 < .85 ** 2)
                | ((x - 3) ** 2 + (y - 2.2) ** 2 < .85 ** 2)
                | ((x < .7) & (y < -1.2) & (y > -2.0)))
    return np.where(occupied, 100, 0).astype(np.int16), resolution, origin, (-2.0, 0.0, 0.0)


def test_sketch_commits_to_open_space_and_routes_around_the_central_obstacle():
    grid, resolution, origin, robot = sketch()
    assert frontiers.find(grid, resolution, origin) == [], 'Fully mapped room has no frontier'
    goal = open_space.choose(grid, resolution, origin, robot)
    assert goal is not None
    assert goal.goal[0] > .8, 'Commit to the open region beyond the central obstacle'
    assert len(goal.route) > 20
    for x, y in goal.route[1:]:
        row, col = int((y - origin[1]) / resolution), int((x - origin[0]) / resolution)
        assert grid[row, col] == 0, 'The route follows open cells around the blobs'
    assert any(y > .8 for x, y in goal.route if -.8 < x < .8), 'Use the broad upper passage'


def test_wider_disconnected_space_is_not_selected_as_a_reachable_goal():
    grid = np.zeros((60, 100), np.int16)
    grid[:, 25] = 100
    goal = open_space.choose(grid, .1, (-2, -3), (-1, 0, 0))
    assert goal is not None
    assert goal.goal[0] < .5
    assert all(x < .5 for x, _ in goal.route)


def test_active_open_space_goal_survives_small_map_and_pose_changes():
    grid, resolution, origin, robot = sketch()
    goal = open_space.choose(grid, resolution, origin, robot)
    grid[5, 5] = 100
    kept = open_space.choose(grid, resolution, origin, (-1.95, .02, 0), current_goal=goal.goal)
    assert kept.goal == goal.goal


def test_a_stalled_blacklisted_open_space_goal_is_replaced():
    grid, resolution, origin, robot = sketch()
    first = open_space.choose(grid, resolution, origin, robot)
    other = open_space.choose(grid, resolution, origin, robot,
                              current_goal=first.goal, blacklist=[(*first.goal, 0)])
    assert other is not None
    assert np.linalg.norm(np.asarray(other.goal) - first.goal) >= .5


def test_unknown_or_disconnected_cells_do_not_invent_an_open_destination():
    grid = np.full((40, 40), -1, np.int16)
    grid[20, 20] = 0
    assert open_space.choose(grid, .1, (-2, -2), (0, 0, 0)) is None
