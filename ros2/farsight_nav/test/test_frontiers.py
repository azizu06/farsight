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



def open_space_scene():
    """Near 1 m opening into table legs, farther 2 m hallway; 10 cm cells."""
    grid = np.zeros((80, 100), np.int16)
    grid[:, 75:] = -1
    grid[10:35, 45:] = -1
    for row in (12, 20, 28, 36):
        for col in (32, 39, 46):
            grid[row, col] = 100
    legs = frontiers.Frontier((4.45, 2.05), (4.45, 2.05), 1.0)
    hallway = frontiers.Frontier((7.45, 5.05), (7.45, 5.05), 2.0)
    return grid, legs, hallway


def test_broad_hallway_beats_near_forest_of_legs():
    grid, legs, hallway = open_space_scene()
    robot = (2.05, 4.05, 0.0)
    # The old distance/length/turn heuristic picks the nearby clutter.
    assert frontiers.choose([legs, hallway], robot) is legs
    selection = frontiers.Explorer()
    choice = selection.choose([legs, hallway], robot, grid, 0.1, (0, 0), now=0)
    assert choice.centroid == hallway.centroid
    r, c = int(choice.goal[1] / 0.1), int(choice.goal[0] / 0.1)
    assert grid[r, c] == 0


def test_narrow_gap_is_rejected_even_when_frontier_is_large():
    grid = np.zeros((50, 70), np.int16)
    grid[:, 55:] = -1
    grid[:23, 42:55] = 99  # inscribed is as blocking as lethal
    grid[27:, 42:55] = 100
    edge = frontiers.Frontier((5.45, 2.55), (5.45, 2.55), 3.0)
    assert frontiers.Explorer().choose([edge], (2.05, 2.55, 0), grid, 0.1, (0, 0), now=0) is None


def test_hysteresis_holds_direction_until_time_and_score_margin_allow_switch():
    grid = np.zeros((80, 100), np.int16)
    grid[:, 80:] = -1
    left = frontiers.Frontier((7.95, 3.05), (7.95, 3.05), 1.5)
    right = frontiers.Frontier((7.95, 5.05), (7.95, 5.05), 1.5)
    chooser = frontiers.Explorer()
    robot = (2.05, 4.05, -0.1)
    assert chooser.choose([left, right], robot, grid, 0.1, (0, 0), now=0).centroid == left.centroid
    assert chooser.choose([right, left], (2.05, 4.05, 0.1), grid, 0.1, (0, 0), now=2).centroid == left.centroid
    assert chooser.choose([right, left], (2.05, 4.05, 0.1), grid, 0.1, (0, 0), now=6).centroid == left.centroid
    broad_right = frontiers.Frontier(right.goal, right.centroid, 3.0)
    assert chooser.choose([left, broad_right], robot, grid, 0.1, (0, 0), now=7).centroid == right.centroid


def test_revisit_penalty_prefers_new_patch_and_decays():
    grid = np.zeros((80, 100), np.int16)
    grid[:, 80:] = -1
    left = frontiers.Frontier((7.95, 2.05), (7.95, 2.05), 1.5)
    right = frontiers.Frontier((7.95, 6.05), (7.95, 6.05), 1.5)
    chooser = frontiers.Explorer(frontiers.ExploreConfig(commitment_s=0, switch_margin=0))
    # Traverse the left patch before returning to the fork.
    for i, y in enumerate(np.arange(1.55, 2.8, 0.2)):
        chooser.choose([], (7.45, y, 0), grid, 0.1, (0, 0), now=i)
    robot = (2.05, 4.05, -0.03)
    assert chooser.choose([left, right], robot, grid, 0.1, (0, 0), now=10).centroid == right.centroid
    chooser.release()
    assert chooser.choose([left, right], robot, grid, 0.1, (0, 0), now=500).centroid == left.centroid


def test_unknown_approach_is_not_invented_free_space():
    grid = np.zeros((50, 70), np.int16)
    grid[20:30, 40:55] = -1
    edge = frontiers.Frontier((5.45, 2.55), (5.45, 2.55), 3.0)
    assert frontiers.Explorer().choose([edge], (2.05, 2.55, 0), grid, 0.1, (0, 0), now=0) is None


def test_visibility_gain_prefers_large_unseen_patch_and_respects_wall_occlusion():
    grid = np.zeros((100, 120), np.int16)
    grid[5:45, 80:] = -1
    grid[60:70, 80:86] = -1
    large = frontiers.Frontier((7.95, 2.55), (7.95, 2.55), 1.5)
    sliver = frontiers.Frontier((7.95, 6.55), (7.95, 6.55), 1.5)
    config = frontiers.ExploreConfig(distance_weight=0, turn_weight=0, clearance_weight=0,
                                    size_weight=0, clutter_weight=0, revisit_weight=0,
                                    commitment_s=0, switch_margin=0)
    chooser = frontiers.Explorer(config)
    robot = (2.05, 4.55, 0)
    assert chooser.choose([large, sliver], robot, grid, 0.1, (0, 0), now=0).centroid == large.centroid
    # A lethal wall occludes the large patch, without blocking the approach.
    grid[5:45, 80] = 100
    assert chooser.choose([large, sliver], robot, grid, 0.1, (0, 0), now=1).centroid == sliver.centroid


def test_committed_goal_is_dropped_immediately_if_approach_becomes_narrow():
    grid, legs, hallway = open_space_scene()
    chooser = frontiers.Explorer()
    robot = (2.05, 4.05, 0)
    assert chooser.choose([hallway], robot, grid, 0.1, (0, 0), now=0) is not None
    grid[48:54, 65:75] = 100
    assert chooser.choose([hallway], robot, grid, 0.1, (0, 0), now=1) is None


def test_curved_camera_frontier_uses_broad_forward_sample_not_close_centroid():
    grid = np.full((100, 120), -1, np.int16)
    # Observed fan whose near side boundaries belong to the same cluster as
    # its distant broad edge. No invented free space behind the camera.
    for col in range(20, 75):
        half = min(24, col - 18)
        grid[50-half:51+half, col] = 0
    found = frontiers.find(grid, 0.1, (0, 0))
    assert len(found) == 1
    choice = frontiers.Explorer().choose(found, (2.05, 5.05, 0), grid, 0.1, (0, 0), now=0)
    assert choice is not None and choice.goal[0] > 6


def test_actual_body_path_fills_blind_ring_without_erasing_measured_obstacles():
    grid = np.full((50, 60), -1, np.int16)
    grid[25, 25] = 100
    trail = frontiers.SweptFootprint(radius=0.35)
    trail.record((2.05, 2.55))
    trail.record((2.25, 2.55))
    sensed = trail.apply(grid, 0.1, (0, 0))
    assert sensed[25, 20] == 0 and sensed[25, 22] == 0
    assert sensed[25, 25] == 100  # a new obstacle beats historical swept space
    assert sensed[25, 30] == -1   # never extend the trail ahead of the actual rover
    assert grid[25, 20] == -1


def test_useful_short_wide_advance_is_not_discarded_by_preplan_distance():
    grid = np.zeros((50, 60), np.int16)
    grid[:, 30:] = -1
    edge = frontiers.Frontier((2.95, 2.55), (2.95, 2.55), 1.5)
    choice = frontiers.Explorer().choose([edge], (2.05, 2.55, 0), grid, 0.1, (0, 0), now=0)
    assert choice is not None and 0.25 < math.dist(choice.goal, (2.05, 2.55)) < 0.6


def test_next_broad_patch_is_preselected_before_current_goal_arrives():
    grid = np.full((100, 100), -1, np.int16)
    grid[10:80, 10:80] = 0
    east = frontiers.Frontier((7.95, 4.05), (7.95, 4.05), 2.0)
    north = frontiers.Frontier((4.05, 7.95), (4.05, 7.95), 1.5)
    chooser = frontiers.Explorer()
    assert chooser.choose([east, north], (6.05, 4.05, 0), grid, 0.1, (0, 0), now=0).centroid == east.centroid
    assert chooser.choose([east, north], (7.2, 4.05, 0), grid, 0.1, (0, 0), now=2).centroid == north.centroid


def test_committed_waypoint_survives_resampling_but_is_revalidated():
    grid = np.zeros((80, 100), np.int16)
    grid[:, 80:] = -1
    edge = frontiers.Frontier((7.95, 4.05), (7.95, 4.05), 2.0)
    chooser = frontiers.Explorer()
    first = chooser.choose([edge], (2.05, 4.05, 0), grid, 0.1, (0, 0), now=0)
    # Frontier clustering/sample positions can disappear for one score cycle.
    # Keep the exact waypoint, including its original setback, while it is safe.
    retained = chooser.choose([], (2.45, 4.25, 0), grid, 0.1, (0, 0), now=2)
    assert retained is not None and retained.goal == first.goal
    grid[38:44, 65:78] = 100
    assert chooser.choose([], (2.45, 4.25, 0), grid, 0.1, (0, 0), now=3) is None


def test_blacklist_and_release_drop_retained_waypoints():
    grid = np.zeros((80, 100), np.int16)
    grid[:, 80:] = -1
    edge = frontiers.Frontier((7.95, 4.05), (7.95, 4.05), 2.0)
    chooser = frontiers.Explorer()
    assert chooser.choose([edge], (2.05, 4.05, 0), grid, 0.1, (0, 0), now=0) is not None
    assert chooser.choose([], (2.05, 4.05, 0), grid, 0.1, (0, 0), now=1, blacklist=[edge.goal]) is None
    assert chooser.choose([edge], (2.05, 4.05, 0), grid, 0.1, (0, 0), now=2) is not None
    chooser.release()
    assert chooser.choose([], (2.05, 4.05, 0), grid, 0.1, (0, 0), now=3) is None
