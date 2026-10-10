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
    return dict(active=True, nav_command=None, nav_at=-100,
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
        args.update(nav_command=(0.3, 0) if i % 20 < 12 else (0, 0), nav_at=now)
        # Small measured noise should not turn clear corridors into standstills.
        args['depth'][:] = 3 + 0.03 * np.sin(i)
        command = motion.command(now=now, pose_at=now, depth_at=now, map_at=now, **args)
        assert command is not None and any(command)


def test_missing_depth_stops_obstacles_back_off_and_creep_never_expires():
    motion = local_motion.LocalMotion()
    args = scene()
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)[0] > 0
    args['depth'][:] = 0.25  # wall inside the body disc: back off over observed floor
    assert motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args) == (-motion.creep_mps, 0)
    args['depth'][:] = 0
    assert motion.command(now=0.2, pose_at=0.2, depth_at=0.2, map_at=0.2, **args) == (0, 0)
    args['depth'][:] = 3
    assert motion.command(now=1, pose_at=1, depth_at=0, map_at=1, **args) == (0, 0)
    assert motion.command(now=4, pose_at=4, depth_at=4, map_at=4, **args)[0] > 0
    assert motion.command(now=14, pose_at=14, depth_at=14, map_at=14, **args)[0] > 0  # no budget: keep creeping


def test_controller_zero_keeps_moving_and_release_hands_back():
    motion = local_motion.LocalMotion()
    args = scene()
    args.update(nav_command=(0.5, 0.2), nav_at=0)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (0.5, 0.2)
    args.update(nav_command=(0, 0), nav_at=0.05)  # controller stop or recovery hand-off: never stand still
    assert motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args)[0] > 0
    args.update(active=False)
    assert motion.command(now=0.2, pose_at=0.2, depth_at=0.2, map_at=0.2, **args) is None


def test_blocked_corridor_reorients_only_in_observed_safe_footprint():
    motion = local_motion.LocalMotion()
    args = scene()
    blocked = np.zeros((80, 100), np.int16)
    blocked[:, 29] = 100  # wall at x=0.9; body footprint itself is clear
    args['support'] = frontiers.clearance_map(blocked != 0, 0.1)
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0 and command[1] > 0
    # Unknown under the body and behind it: no back-off, but never a freeze either.
    blocked[40, 20] = -1
    args['support'] = frontiers.clearance_map(blocked != 0, 0.1)
    command = motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args)
    assert command[0] == 0 and command[1] != 0


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


def test_blind_ring_keeps_scanning_through_and_past_a_full_turn():
    motion = local_motion.LocalMotion()
    args = scene()
    args.update(support=np.zeros((80, 100)), body_clear=True)
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
        assert command[0] == 0 and command[1] > 0  # a full look-around restarts, never stops
    assert not motion.scan_exhausted


def test_thin_new_pole_between_coverage_rays_never_gets_creep():
    motion = local_motion.LocalMotion()
    args = scene()
    # A single measured pole pixel between the sparse coverage probes, before
    # the costmap catches up. All proposed arcs would sweep over it.
    args['depth'][40, 61] = 0.7
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0


def test_too_close_backs_off_over_observed_floor_then_turns_away():
    motion = local_motion.LocalMotion()
    args = scene()
    args['depth'][:] = 0.25
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (-motion.creep_mps, 0)
    motion.backoff_from = (motion.BACKOFF_M + 0.01, 0)  # still blocked after backing the full budget
    command = motion.command(now=1, pose_at=1, depth_at=1, map_at=1, **args)
    assert command[0] == 0 and command[1] != 0  # turn away instead of freezing
    args['robot'] = (-0.4, 0, 0)  # backed clear of it: the scan turn may start again
    assert motion.command(now=1.1, pose_at=1.1, depth_at=1.1, map_at=1.1, **args)[1] != 0
    assert motion.backoff_from is None
    fresh = local_motion.LocalMotion()
    unseen = np.zeros((80, 100), np.int16)
    unseen[:, :20] = -1  # nothing observed behind the rover
    args.update(robot=(0, 0, 0), support=frontiers.clearance_map(unseen != 0, 0.1))
    command = fresh.command(now=2, pose_at=2, depth_at=2, map_at=2, **args)
    assert command[0] == 0 and command[1] != 0  # never reverse blind; turn away
