"""The human's central-obstacle sketch, represented as a mapped-room fixture."""
import math
import os
import sys

import numpy as np
import pytest

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


def known_room():
    grid = np.zeros((80, 100), np.int16)
    grid[[0, -1], :] = 100
    grid[:, [0, -1]] = 100
    return grid


@pytest.mark.parametrize('robot, current', [((3.4, 2, 0), (3.85, 2)),
                                           ((3.0, 2, 0), (3.8, 2))])
def test_reachable_goal_stays_committed_until_arrival(robot, current):
    goal = open_space.choose(known_room(), .05, (0, 0), robot,
                             current_goal=current, transition_m=.6)
    assert goal.goal == current
    assert goal.centroid == current
    assert goal.route[0] == robot[:2]
    assert goal.route[-1] == current


@pytest.mark.parametrize('current', [(3.7, 2), (3.75, 2)])
def test_arrived_open_space_goal_is_replaced(current):
    robot = (3.5, 2, 0)
    goal = open_space.choose(known_room(), .05, (0, 0), robot,
                             current_goal=current, transition_m=.6)
    assert goal.goal != current
    assert math.dist(robot[:2], goal.goal) > .6


def test_smaller_transition_distance_still_controls_goal_arrival():
    robot, current = (3.4, 2, 0), (3.55, 2)
    goal = open_space.choose(known_room(), .05, (0, 0), robot,
                             current_goal=current, transition_m=.1)
    assert goal.goal == current
    arrived = open_space.choose(known_room(), .05, (0, 0), (3.5, 2, 0),
                                current_goal=current, transition_m=.1)
    assert arrived.goal != current


@pytest.mark.parametrize('invalid', ['occupied', 'disconnected', 'blacklisted'])
def test_invalid_near_goal_is_replaceable(invalid):
    grid, robot, current = known_room(), (3.4, 2, 0), (3.85, 2)
    blacklist = []
    if invalid == 'occupied':
        grid[40, 77] = 100
    elif invalid == 'disconnected':
        grid[:, 74] = 100
    else:
        blacklist = [current]
    goal = open_space.choose(grid, .05, (0, 0), robot,
                             current_goal=current, blacklist=blacklist, transition_m=.6)
    assert goal is not None and goal.goal != current
    if invalid == 'blacklisted':
        assert math.dist(goal.goal, current) >= .5
    if invalid == 'disconnected':
        assert goal.goal[0] < 3.7
    for x, y in goal.route[1:]:
        assert grid[math.floor(y / .05), math.floor(x / .05)] == 0


def test_new_destination_outlives_its_transition_radius():
    robot = (2.7, 2, 0)
    goal = open_space.choose(known_room(), .05, (0, 0), robot, transition_m=.6)
    assert math.dist(robot[:2], goal.goal) > .6
    kept = open_space.choose(known_room(), .05, (0, 0), robot,
                             current_goal=goal.goal, transition_m=.6)
    assert kept.goal == goal.goal


@pytest.mark.parametrize('robot, direction', [((3.4, 2, 0), 1),
                                             ((1.6, 2, math.pi), -1)])
def test_reachable_forward_destination_is_preferred_in_known_room(robot, direction):
    goal = open_space.choose(known_room(), .05, (0, 0), robot)
    assert (goal.goal[0] - robot[0]) * direction > 0
    assert math.dist(robot[:2], goal.goal) > .6


@pytest.mark.parametrize('direction', ['side', 'rear'])
def test_reachable_side_or_rear_space_remains_a_destination(direction):
    grid = np.full((80, 100), 100, np.int16)
    if direction == 'side':
        grid[10:69, 68] = 0
    else:
        grid[40, 10:69] = 0
    robot = (3.425, 2.025, 0)
    goal = open_space.choose(grid, .05, (0, 0), robot)
    assert goal is not None
    assert math.dist(robot[:2], goal.goal) > .6
    if direction == 'side':
        assert goal.goal[0] == pytest.approx(robot[0])
    else:
        assert goal.goal[0] < robot[0]


def test_distant_rear_space_precedes_a_forward_goal_inside_release_radius():
    grid = np.full((80, 100), 100, np.int16)
    grid[40, 10:76] = 0
    robot = (3.425, 2.025, 0)
    goal = open_space.choose(grid, .05, (0, 0), robot, transition_m=.6)
    assert goal is not None and goal.goal[0] < robot[0] - .6


def test_blacklisted_forward_space_does_not_hide_a_reachable_rear_destination():
    grid = np.full((80, 100), 100, np.int16)
    grid[40, 10:83] = 0
    robot = (3.425, 2.025, 0)
    goal = open_space.choose(grid, .05, (0, 0), robot,
                             blacklist=[(3.9, 2.025)], transition_m=.6)
    assert goal is not None and goal.goal[0] < robot[0] - .6
    assert math.dist(goal.goal, (3.9, 2.025)) >= .5


def test_short_reachable_space_does_not_become_a_no_goal_condition():
    grid = np.full((3, 8), 100, np.int16)
    grid[1, 1:7] = 0
    robot = (.35, .15, 0)
    goal = open_space.choose(grid, .1, (0, 0), robot, transition_m=.6)
    assert goal is not None
    assert .25 <= math.dist(robot[:2], goal.goal) < .6
