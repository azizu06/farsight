"""Planning transitions with measured-depth fixtures; no ROS/hardware claims."""
import math
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


def scene_depth(wall=3.0, drop_above=None):
    """A measured image: the floor under the camera, a wall at `wall` metres.

    Rows above `drop_above` get no return, the way a far wall or open ceiling
    does. The camera sits 15 cm up, so those rows are level with or above it.
    """
    args = scene()
    depth = np.zeros((80, 120))
    fx, fy, cx, cy = 60, 60, 60, 40
    cam_y = 0.15
    for v in range(80):
        for u in range(120):
            ray_y = -(v - cy) / fy
            ray_z = -1.0
            if ray_y < -1e-9:
                t = cam_y / -ray_y  # hits the floor
                if t * -ray_z <= wall:
                    depth[v, u] = t
                    continue
            depth[v, u] = wall / -ray_z
    if drop_above is not None:
        depth[:drop_above, :] = 0
    args["depth"] = depth
    return args


def test_open_hallway_arc_passes_when_far_wall_and_upper_rays_return_nothing():
    motion = local_motion.LocalMotion()
    args = scene_depth(wall=6.0, drop_above=30)  # wall past 4 m; upper rays see nothing
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] > 0  # open hallway is driveable, not a spin


def test_near_obstacle_on_the_low_rays_still_blocks():
    motion = local_motion.LocalMotion()
    args = scene_depth(wall=6.0, drop_above=30)
    args["depth"][42:, :] = 0.5  # something close, only on rays that look down
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0


def blocked():
    """A wall ahead, outside the body circle, with no mapped free corridor."""
    args = scene()
    args.update(depth=np.full((80, 120), 0.4), support=np.zeros((80, 100)))
    return args


def yawed(args, yaw):
    """Rover and capture camera share a yaw (ARKit rotates about world y)."""
    transform = np.eye(4)
    transform[:3, :3] = ((np.cos(yaw), 0, np.sin(yaw)), (0, 1, 0), (-np.sin(yaw), 0, np.cos(yaw)))
    transform[1, 3] = 0.15
    args["robot"] = (0, 0, yaw)
    args["header"]["transform"] = transform.flatten(order="F").tolist()


def open_right_only():
    """Floor plus a wall at 1.5 m, and a measured post just left of the nose.

    The post blocks the straight and left arcs; the gentle right arc passes.
    """
    args = scene_depth(wall=1.5)
    args["depth"][:, 34:42] = 0.9
    args["support"] = frontiers.clearance_map(np.zeros((80, 100), bool), 0.1)
    return args["depth"], args["support"]


def scan_signs(motion, args, glimpse_tick=6, ticks=90):
    """Scan while blocked, with one tick of a right-only open arc at `glimpse_tick`."""
    signs = []
    for i in range(ticks):
        now = 0.1 + i * 0.1
        yawed(args, (i * motion.turn_radps * 0.1 + np.pi) % (2 * np.pi) - np.pi)
        if i == glimpse_tick:
            args["depth"], args["support"] = open_right_only()
        else:
            args["depth"], args["support"] = np.full((80, 120), 0.4), np.zeros((80, 100))
        command = motion.command(now=now, pose_at=now, depth_at=now, map_at=now, **args)
        if i == glimpse_tick:
            assert command[0] > 0 and command[1] < 0  # the momentary arc really passes, to the right
        elif command[0] == 0 and command[1]:
            signs.append(math.copysign(1, command[1]))
    return signs


def test_look_around_keeps_one_direction_through_a_momentary_opposite_arc():
    motion = local_motion.LocalMotion()
    args = blocked()
    args["body_clear"] = True
    args.update(nav_command=(0.2, 0.5), nav_at=0)  # Nav2 was turning left: seed the scan left
    motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    args.update(nav_command=None, nav_at=-100)
    signs = scan_signs(motion, args)
    # More than a full turn (2 pi / 0.82 rad/s / 0.1 s = 77 ticks), one direction, no flip.
    assert len(signs) > 80 and set(signs) == {1.0}


def test_fresh_look_around_picks_its_side_once_without_a_nav2_seed():
    motion = local_motion.LocalMotion()  # fresh Explore press: no Nav2 turn to seed from
    args = blocked()
    args["body_clear"] = True
    signs = scan_signs(motion, args)
    assert len(signs) > 80 and set(signs) == {1.0}  # started left, a right glimpse never flips it


def test_steady_arc_for_a_second_reseeds_the_look_around():
    motion = local_motion.LocalMotion()
    args = blocked()
    args["body_clear"] = True
    yawed(args, 0)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)[1] > 0  # scan picks left
    args["depth"], args["support"] = open_right_only()
    for i in range(12):  # 1.1 s of the same right arc
        now = 0.1 + i * 0.1
        assert motion.command(now=now, pose_at=now, depth_at=now, map_at=now, **args)[1] < 0
    args["depth"], args["support"] = np.full((80, 120), 0.4), np.zeros((80, 100))
    assert motion.command(now=1.4, pose_at=1.4, depth_at=1.4, map_at=1.4, **args)[1] < 0


def render(pitch_deg, wall, drop=None, max_range=5.0):
    """Ray-cast a tilted phone 15 cm above a floor, with a wall `wall` m ahead.

    Positive pitch tilts the phone back (nose up). The floor ends `drop` m ahead
    (a stair edge: nothing below comes back). Returns past `max_range` are lost,
    like the iPhone LiDAR, and stored as 0 = no return.
    """
    args = scene()
    a = math.radians(pitch_deg)
    rotation = np.array(((1, 0, 0), (0, math.cos(a), -math.sin(a)), (0, math.sin(a), math.cos(a))))
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[1, 3] = 0.15
    args["header"]["transform"] = transform.flatten(order="F").tolist()
    v, u = np.mgrid[0:80, 0:120]
    ray = np.stack(((u - 60) / 60, -(v - 40) / 60, -np.ones_like(u, float)), axis=-1) @ rotation.T
    t_floor = np.where(ray[..., 1] < 0, -0.15 / np.minimum(ray[..., 1], -1e-9), np.inf)
    floor_ahead = -ray[..., 2] * t_floor
    if drop is not None:
        t_floor = np.where(floor_ahead <= drop, t_floor, np.inf)
    t_wall = np.where(ray[..., 2] < 0, wall / np.maximum(-ray[..., 2], 1e-9), np.inf)
    wall_height = 0.15 + ray[..., 1] * t_wall
    t_wall = np.where(wall_height >= 0, t_wall, np.inf)
    t = np.minimum(t_floor, t_wall)  # camera ray z is -1, so t is the z depth
    t = np.where(t * np.linalg.norm(ray, axis=-1) <= max_range, t, 0)
    args["depth"] = t
    args["support"] = frontiers.clearance_map(np.zeros((80, 100), bool), 0.1)
    return args


def test_phone_tilted_forward_still_blocks_a_floor_that_drops_away():
    motion = local_motion.LocalMotion()
    args = render(pitch_deg=-10, wall=3.0, drop=0.9)  # stair edge 0.9 m ahead, map not caught up
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0  # low rays come back with nothing: never creep over the edge


def test_phone_tilted_back_still_sees_an_open_hallway():
    motion = local_motion.LocalMotion()
    args = render(pitch_deg=6, wall=8.0)  # far wall past LiDAR range: upper rays return nothing
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] > 0


def test_open_hallway_survives_arkit_camera_height_wobble():
    for drift in (-0.002, 0.002, 0.005, 0.009):
        motion = local_motion.LocalMotion()
        args = render(pitch_deg=0, wall=8.0)  # no returns past 5 m
        transform = np.eye(4)
        transform[1, 3] = 0.15 + drift  # ARKit reports the camera a few mm off
        args["header"]["transform"] = transform.flatten(order="F").tolist()
        command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
        assert command[0] > 0, drift


def test_level_tolerance_is_a_centimetre_not_a_free_pass():
    points = [(0.55, 0.0, 0.15)]  # one sample at body height, straight ahead
    depth = np.zeros((80, 120))  # nothing comes back anywhere
    header = scene()["header"]
    for camera_y, clear in ((0.155, True), (0.17, False)):
        transform = np.eye(4)
        transform[1, 3] = camera_y
        header["transform"] = transform.flatten(order="F").tolist()
        assert local_motion.LocalMotion.depth_clear(points, depth, header) is clear, camera_y


def test_a_return_right_at_the_lens_blocks_even_on_upper_rays():
    motion = local_motion.LocalMotion()
    args = scene_depth(wall=6.0, drop_above=30)
    args["depth"][:30, :] = 0.1  # something touching the lens, only on rays looking up
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0


def test_handoff_seeds_the_look_around_from_nav2_last_turn():
    motion = local_motion.LocalMotion()
    args = blocked()
    args.update(nav_command=(0.3, -0.6), nav_at=0)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (0.3, -0.6)
    args.update(nav_command=None, nav_at=-100)  # Nav2 has gone quiet: the look-around takes over
    command = motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args)
    assert command[0] == 0 and command[1] < 0  # Nav2 was turning right, so the scan starts right


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
