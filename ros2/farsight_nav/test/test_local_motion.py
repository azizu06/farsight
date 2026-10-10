"""Planning transitions with measured-depth fixtures; no ROS/hardware claims."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from farsight_nav import frontiers, local_motion  # noqa: E402


def scene():
    grid = np.zeros((80, 100), np.int16)
    depth = np.full((80, 120), 3.0)
    transform = np.eye(4)
    transform[1, 3] = 0.15
    return dict(active=True, planning=True, nav_command=None, nav_at=-100,
                robot=(0, 0, 0), depth=depth,
                header=dict(transform=transform.flatten(order="F").tolist(), floor_y=0,
                            fx=60, fy=60, cx=60, cy=40),
                support=frontiers.clearance_map(grid != 0, 0.1),
                resolution=0.1, origin=(-2, -4))


def test_no_idle_gap_during_goal_changes_and_noisy_depth():
    motion = local_motion.LocalMotion()
    args = scene()
    for i in range(80):
        now = i * 0.05
        # Alternate valid Nav2 driving and empty-plan waypoint transitions.
        args.update(planning=i % 20 >= 12, nav_command=(0.3, 0) if i % 20 < 12 else (0, 0), nav_at=now)
        # Small measured noise should not turn clear corridors into standstills.
        args['depth'][:] = 3 + 0.03 * np.sin(i)
        command = motion.command(now=now, pose_at=now, depth_at=now, map_at=now, **args)
        assert command is not None and any(command)


def test_missing_depth_real_obstacle_and_expired_budget_stop():
    motion = local_motion.LocalMotion()
    args = scene()
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)[0] > 0
    args['depth'][:] = 0.25
    assert motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args) == (0, 0)
    args['depth'][:] = 0
    assert motion.command(now=0.2, pose_at=0.2, depth_at=0.2, map_at=0.2, **args) == (0, 0)
    args['depth'][:] = 3
    assert motion.command(now=1, pose_at=1, depth_at=0, map_at=1, **args) == (0, 0)
    assert motion.command(now=4, pose_at=4, depth_at=4, map_at=4, **args)[1] > 0
    assert motion.command(now=14, pose_at=14, depth_at=14, map_at=14, **args) == (0, 0)


def test_controller_stop_and_release_are_not_overridden():
    motion = local_motion.LocalMotion()
    args = scene()
    args.update(planning=False, nav_command=(0, 0), nav_at=0)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (0, 0)
    args.update(active=False, planning=True)
    assert motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args) is None


def test_blocked_corridor_reorients_only_in_observed_safe_footprint():
    motion = local_motion.LocalMotion()
    args = scene()
    blocked = np.zeros((80, 100), np.int16)
    blocked[:, 29] = 100  # wall at x=0.9; body footprint itself is clear
    args['support'] = frontiers.clearance_map(blocked != 0, 0.1)
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0 and command[1] > 0
    # Unknown in the sweep footprint does not support turning, even with depth.
    blocked[40, 20] = -1
    args['support'] = frontiers.clearance_map(blocked != 0, 0.1)
    assert motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args) == (0, 0)


def test_missing_pose_and_map_or_release_never_replay_previous_creep():
    motion = local_motion.LocalMotion()
    args = scene()
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)[0] > 0
    assert motion.command(now=1, pose_at=0, depth_at=1, map_at=1, **args) == (0, 0)
    assert motion.command(now=3, pose_at=3, depth_at=3, map_at=0, **args) == (0, 0)
    args['active'] = False
    assert motion.command(now=3.1, pose_at=3.1, depth_at=3.1, map_at=3.1, **args) is None
    args['active'] = True
    assert motion.command(now=3.2, pose_at=0, depth_at=0, map_at=0, **args) == (0, 0)


def test_blind_ring_scans_only_actual_body_support_then_stops_after_full_turn():
    motion = local_motion.LocalMotion()
    args = scene()
    args.update(support=np.zeros((80, 100)), body_clear=True, scan_only=True)
    # No invented forward support: turn in place throughout the real yaw sweep.
    for i in range(80):
        now = i * 0.1
        args['robot'] = (0, 0, (i * 0.08 + np.pi) % (2 * np.pi) - np.pi)
        transform = np.eye(4)
        # Camera follows yaw in ARKit coordinates; measured fixture stays clear.
        yaw = args['robot'][2]
        transform[:3, :3] = ((np.cos(yaw), 0, np.sin(yaw)), (0, 1, 0), (-np.sin(yaw), 0, np.cos(yaw)))
        transform[1, 3] = 0.15
        args['header']['transform'] = transform.flatten(order='F').tolist()
        command = motion.command(now=now, pose_at=now, depth_at=now, map_at=now, **args)
        if i < 79:
            assert command[0] == 0 and command[1] > 0
        else:
            assert command == (0, 0) and motion.scan_exhausted


def test_thin_new_pole_between_coverage_rays_never_gets_creep():
    motion = local_motion.LocalMotion()
    args = scene()
    # A single measured pole pixel between the sparse coverage probes, before
    # the costmap catches up. All proposed arcs would sweep over it.
    args['depth'][40, 61] = 0.7
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0
