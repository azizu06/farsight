"""Planning transitions with measured-depth fixtures; no ROS/hardware claims."""
import math
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "..", "..", ".."))  # repo root: jetson.nav codec
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


@pytest.mark.parametrize("nominal, expected", [(0.02, 0.15), (0.15, 0.15), (0.3, 0.3)])
def test_configured_creep_has_the_same_minimum_forward_and_reverse(nominal, expected):
    motion = local_motion.LocalMotion(creep_mps=nominal)
    args = scene()
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (expected, 0)
    args['depth'][:] = 0.25
    assert motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args) == (-expected, 0)


@pytest.mark.parametrize("nominal", [0.02, 0.15])
def test_curved_coverage_projects_the_executed_minimum_speed(nominal):
    motion = local_motion.LocalMotion(creep_mps=nominal, min_width=0.36)
    motion.heading = 0.2  # smooth continuation of a previously chosen left arc
    checked = []
    project_depth = motion.depth_clear

    def record_projection(points, depth, header):
        clear = project_depth(points, depth, header)
        checked.append((np.asarray(points), clear))
        return clear

    motion.depth_clear = record_projection
    args = scene()
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (0.15, 0.1)
    # A 0.15 m/s, 0.1 rad/s turn has a 1.5 m radius. The body centres after
    # 0.55, 0.75 and 0.95 m of travel must be the ones checked against depth.
    centres = np.array([[0.5377585064, 0.0997086772],
                        [0.7191383079, 0.1836261572],
                        [0.8877523574, 0.2909111894]])
    assert any(clear and np.allclose(points.reshape(3, 7, 3, 3).mean(axis=(1, 2))[:, :2], centres)
               for points, clear in checked)


@pytest.mark.parametrize("command", [(0.02, 0.004), (-0.02, 0.004), (0.1, -0.2), (-0.1, -0.2)])
def test_slow_nav_commands_raise_translation_without_changing_curvature(command):
    motion = local_motion.LocalMotion()
    args = scene()
    args.update(nav_command=command, nav_at=0)
    linear, angular = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert abs(linear) == pytest.approx(0.15)
    assert np.sign(linear) == np.sign(command[0])
    assert angular / linear == pytest.approx(command[1] / command[0])


@pytest.mark.parametrize("command", [(0.15, 0.2), (0.3, -0.5), (-0.3, 0.5), (0, 0.82), (0, -0.82)])
def test_fast_nav_commands_and_pivots_are_forwarded_unchanged(command):
    motion = local_motion.LocalMotion()
    args = scene()
    args.update(nav_command=command, nav_at=0)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == command


def test_nav_zero_uses_measured_fallback_at_the_minimum_speed():
    motion = local_motion.LocalMotion(creep_mps=0.02)
    args = scene()
    args.update(nav_command=(0, 0), nav_at=0)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (0.15, 0)


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


def scene_depth(wall=3.0):
    """A measured image: the floor under the camera, a wall at `wall` metres."""
    args = scene()
    v = np.arange(80)[:, None]
    ray_y = -(v - 40) / 60
    floor = np.where(ray_y < -1e-9, 0.15 / -np.minimum(ray_y, -1e-9), np.inf)
    args["depth"] = np.broadcast_to(np.where(floor <= wall, floor, wall), (80, 120)).copy()
    return args


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


def test_open_space_past_four_metres_stays_unknown_not_clear():
    motion = local_motion.LocalMotion()
    args = render(pitch_deg=0, wall=8.0)  # far wall past LiDAR range: upper rays return 0
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0 and command[1] != 0  # no return is not evidence of space: look around


def test_handoff_seeds_the_look_around_from_nav2_last_turn():
    for turn in (-0.6, 0.6):
        motion = local_motion.LocalMotion()  # fresh start: the first thing it sees is Nav2 driving
        args = blocked()
        args.update(nav_command=(0.3, turn), nav_at=0)
        assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args) == (0.3, turn)
        args.update(nav_command=(0.3, 0.0), nav_at=0.05)  # a straight command carries no side
        assert motion.command(now=0.05, pose_at=0.05, depth_at=0.05, map_at=0.05, **args) == (0.3, 0.0)
        args.update(nav_command=None, nav_at=-100)  # Nav2 has gone quiet: the look-around takes over
        command = motion.command(now=0.1, pose_at=0.1, depth_at=0.1, map_at=0.1, **args)
        assert command[0] == 0 and math.copysign(1, command[1]) == math.copysign(1, turn), turn


# Production counterexamples from docs/qualification/nav-open-space-review-20261010
# (witness.py), with its exact inputs: the real encoder and bridge decoder, a
# formally fresh (100 ms) free map that the current depth contradicts.

def witness_scene(height=.15, drop=None, wall=6):
    v, u = np.mgrid[0:80, 0:120]
    ray = np.stack(((u - 60) / 60, -(v - 40) / 60, -np.ones_like(u, float)), axis=-1)
    floor = np.where(ray[..., 1] < 0, -height / np.minimum(ray[..., 1], -1e-9), np.inf)
    if drop is not None:
        floor = np.where(-ray[..., 2] * floor <= drop, floor, np.inf)
    wall_depth = np.where(height + ray[..., 1] * wall >= 0, wall, np.inf)
    depth = np.minimum(floor, wall_depth)
    depth = np.where(depth * np.linalg.norm(ray, axis=-1) <= 5, depth, 0)
    transform = np.eye(4)
    transform[1, 3] = height
    return dict(active=True, nav_command=None, nav_at=-100, robot=(0, 0, 0), depth=depth,
                header=dict(transform=transform.flatten(order='F').tolist(), floor_y=0, fx=60, fy=60, cx=60, cy=40),
                support=np.full((80, 100), 10.0), resolution=.1, origin=(-2, -4))


def witness_command(args):
    return local_motion.LocalMotion().command(now=10, pose_at=10, depth_at=10, map_at=9.9, **args)


def test_low_confidence_obstacle_the_codec_zeroes_never_gets_creep():
    from farsight_nav.protocol import decode_depth
    from jetson.nav import encode_depth

    args = witness_scene()
    args['depth'][:30] = .2  # 20 cm obstacle across the upper rows
    frame = SimpleNamespace(depth=args['depth'], confidence=np.ones_like(args['depth'], np.uint8),
                            calibration=SimpleNamespace(fx=60, fy=60, cx=60, cy=40),
                            transform=np.asarray(args['header']['transform']).reshape(4, 4, order='F'),
                            header={'frame_id': 1, 'tracking': 'normal'})
    commands = {}
    for confidence in (1, 0):
        frame.confidence[:30] = confidence
        header, depth = decode_depth(encode_depth(frame, floor_y=0, source='CPU fixture', age_ms=0))
        commands[confidence] = (float(depth[10, 60]), witness_command({**args, 'depth': depth, 'header': header}))
    assert commands[1][0] == pytest.approx(.2) and commands[1][1][0] < 0  # measured: back off
    assert commands[0][0] == 0  # the real codec censors it to "no return"
    linear, angular = commands[0][1]
    assert linear == 0 and angular != 0  # unknown stays blocked: turn, never drive at it


def test_low_camera_with_a_floor_edge_ahead_never_gets_creep():
    args = witness_scene(height=.08, drop=.6, wall=8)  # camera 8 cm up, floor ends 60 cm ahead
    assert int(((args['depth'] > .15) & (args['depth'] <= 4)).sum()) >= 16  # depth is fresh, not stale
    linear, angular = witness_command(args)
    assert linear == 0 and angular != 0


def test_invalid_upper_samples_are_not_clear():
    header = witness_scene()['header']
    for value in (float('nan'), float('inf'), -1, 0):
        depth = np.full((80, 120), value)
        assert not local_motion.LocalMotion.depth_clear([(0.55, 0, .25)], depth, header), value


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


def lagged(args, behind):
    """The rover turned `behind` rad past the heading its newest depth frame was captured at."""
    args["robot"] = (0, 0, behind)


def test_turning_past_a_confirmed_open_heading_turns_back_and_creeps_without_a_new_scan():
    motion = local_motion.LocalMotion()
    args = scene_depth(wall=3.0)  # floor and a wall at 3 m: open straight ahead of the capture camera
    lagged(args, 0.6)  # a fast turn outran the depth stream: arcs from this nose leave the frame
    command = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert command[0] == 0 and command[1] < 0  # straight back toward the confirmed heading, not onward
    assert motion.target is not None and motion.scan_since is None  # a directed turn, not a look-around
    lagged(args, 0.05)  # turned back onto it
    linear, angular = motion.command(now=0.3, pose_at=0.3, depth_at=0.3, map_at=0.3, **args)
    assert linear >= local_motion.LocalMotion.MIN_TRANSLATION_MPS  # the normal arcs pass from the new nose
    assert motion.target is None


def test_reaching_the_heading_waits_for_one_depth_frame_from_there_then_rescans():
    motion = local_motion.LocalMotion()
    args = scene_depth(wall=3.0)
    lagged(args, 0.6)
    assert motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)[1] < 0
    args.update(depth=np.full((80, 120), 0.4))  # the frame from the new heading shows a wall
    lagged(args, 0.02)
    assert motion.command(now=0.2, pose_at=0.2, depth_at=0.1, map_at=0.2, **args) == (0, 0)  # older frame
    linear, angular = motion.command(now=0.4, pose_at=0.4, depth_at=0.35, map_at=0.4, **args)
    assert linear == 0 and angular != 0 and motion.target is None  # blocked from here: look around again


def test_no_confirmed_heading_is_invented_in_open_space_or_witness_scenes():
    for args in (render(pitch_deg=0, wall=8.0), witness_scene(height=.08, drop=.6, wall=8)):
        motion = local_motion.LocalMotion()
        lagged(args, 0.6)
        linear, angular = motion.command(now=10, pose_at=10, depth_at=10, map_at=9.9, **args)
        assert linear == 0 and angular != 0 and motion.target is None


def test_look_around_records_why_the_nose_is_blocked():
    motion = local_motion.LocalMotion()
    args = render(pitch_deg=0, wall=8.0)  # far wall past LiDAR range
    motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert motion.blocked.get("no return", 0) > 0


def walled(args, rows=None, cols=None, padding=0):
    """A lethal map line beside the rover (rows: y cells, cols: x cells of the 0.1 m test map),
    with `padding` cells of Nav2 inflation (cost 60, above FREE_MAX) around it, as the bridge passes them."""
    grid = np.zeros((80, 100), np.int16)
    for index, axis in ((rows, 0), (cols, 1)):
        if index is None:
            continue
        for offset in range(-padding, padding + 1):
            line = [slice(None), slice(None)]
            line[axis] = index + offset
            grid[tuple(line)] = np.maximum(grid[tuple(line)], 60)
        line = [slice(None), slice(None)]
        line[axis] = index
        grid[tuple(line)] = 100
    args.update(support=frontiers.clearance_map(~((grid >= 0) & (grid <= frontiers.FREE_MAX)), 0.1),
                lethal=frontiers.clearance_map(grid >= 100, 0.1), unseen=frontiers.clearance_map(grid < 0, 0.1))
    return args


@pytest.mark.parametrize("padding", [0, 2])
def test_within_a_radius_of_a_wall_drives_away_instead_of_turning_forever(padding):
    # 2026-10-11 wander sim: beside a box, inside its inflation, every arc failed the map
    # check and the look-around turned in place for 25-90 s; no turn changes that.
    motion = local_motion.LocalMotion()
    args = walled(scene(), cols=17, padding=padding)  # a wall 0.2-0.3 m behind, nothing measured ahead
    linear, angular = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert linear >= local_motion.LocalMotion.MIN_TRANSLATION_MPS


@pytest.mark.parametrize("padding", [0, 2])
def test_beside_a_wall_never_arcs_closer_to_it(padding):
    motion = local_motion.LocalMotion()
    args = walled(scene(), rows=42, padding=padding)  # a wall 0.2-0.3 m to the left
    linear, angular = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert linear >= local_motion.LocalMotion.MIN_TRANSLATION_MPS and angular <= 0  # along or away, never into it


def test_facing_a_wall_inside_its_inflation_still_turns():
    motion = local_motion.LocalMotion()
    args = walled(scene(), cols=23, padding=2)  # the wall 0.3 m ahead
    linear, angular = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert linear == 0 and angular != 0


def test_escape_arcs_never_enter_unknown_cells():
    motion = local_motion.LocalMotion()
    args = walled(scene(), cols=17, padding=2)
    grid = np.zeros((80, 100), np.int16)
    grid[:, 17] = 100
    grid[:, 22:] = -1  # unknown from 0.2 m ahead
    args.update(support=frontiers.clearance_map(~((grid >= 0) & (grid <= frontiers.FREE_MAX)), 0.1),
                lethal=frontiers.clearance_map(grid >= 100, 0.1), unseen=frontiers.clearance_map(grid < 0, 0.1))
    linear, angular = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert linear == 0


def test_a_heading_reached_without_an_open_arc_is_not_chased_again():
    motion = local_motion.LocalMotion()
    args = scene_depth(wall=3.0)
    open_depth = args["depth"]
    lagged(args, 0.6)
    motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    tried = motion.target[0]
    args.update(depth=np.full((80, 120), 0.4))  # from there the newest frame shows a wall
    lagged(args, 0.02)
    motion.command(now=0.2, pose_at=0.2, depth_at=0.1, map_at=0.2, **args)
    motion.command(now=0.4, pose_at=0.4, depth_at=0.35, map_at=0.4, **args)
    assert motion.target is None
    args.update(depth=open_depth)  # an older-looking open frame confirms the same heading again
    lagged(args, 0.6)
    motion.command(now=0.5, pose_at=0.5, depth_at=0.5, map_at=0.5, **args)
    assert motion.target is None or abs(motion.target[0] - tried) >= motion.TARGET_SPACING


def test_a_measured_return_past_four_metres_is_open_space():
    motion = local_motion.LocalMotion()
    args = render(pitch_deg=0, wall=4.1, max_range=5.0)  # far wall measured inside the LiDAR's 5 m
    linear, angular = motion.command(now=0, pose_at=0, depth_at=0, map_at=0, **args)
    assert linear >= local_motion.LocalMotion.MIN_TRANSLATION_MPS
