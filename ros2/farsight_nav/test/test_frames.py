"""Frame math without ROS: python -m pytest ros2/farsight_nav/test (host or container)."""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from farsight_nav import frames, protocol  # noqa: E402


def arkit_pose(x, z, yaw, pitch=0.0, height=0.0):
    """ARKit camera-to-world: yaw about +Y (CCW from above), then pitch about camera x."""
    c, s = math.cos(yaw), math.sin(yaw)
    ry = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    cp, sp = math.cos(pitch), math.sin(pitch)
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    pose = np.eye(4)
    pose[:3, :3] = ry @ rx
    pose[:3, 3] = [x, height, z]
    return pose


def test_start_pose_is_odom_origin_facing_x():
    position, rotation = frames.camera_in_odom(arkit_pose(0, 0, 0), floor_y=-0.15)
    np.testing.assert_allclose(position, [0, 0, 0.15], atol=1e-12)
    np.testing.assert_allclose(rotation, np.eye(3), atol=1e-12)
    base, yaw = frames.base_in_odom(position, rotation)
    np.testing.assert_allclose(base, [0, 0, 0])
    assert abs(yaw) < 1e-12


def test_arkit_yaw_is_odom_yaw_and_positions_map():
    pose = arkit_pose(-1.0, -2.0, math.radians(30))
    position, rotation = frames.camera_in_odom(pose, floor_y=-0.15)
    np.testing.assert_allclose(position[:2], frames.world_to_odom(-1.0, -2.0))
    _, yaw = frames.base_in_odom(position, rotation)
    assert math.isclose(yaw, math.radians(30), abs_tol=1e-9)
    assert np.isclose(np.linalg.det(rotation), 1.0)


def test_camera_pitch_stays_in_base_to_camera_transform():
    pose = arkit_pose(0.5, 0.2, math.radians(-50), pitch=math.radians(-12))
    position, rotation = frames.camera_in_odom(pose, floor_y=-0.15)
    base, yaw = frames.base_in_odom(position, rotation)
    assert math.isclose(yaw, math.radians(-50), abs_tol=1e-9)
    relative, relative_rotation = frames.camera_in_base(position, rotation, base, yaw)
    np.testing.assert_allclose(relative, [0, 0, 0.15], atol=1e-12)
    # camera_link x (forward) points 12 degrees down in base_link.
    forward = relative_rotation[:, 0]
    assert math.isclose(math.degrees(math.atan2(-forward[2], forward[0])), 12, abs_tol=1e-6)


def test_drive_profile_yaw_offset_and_downward_phone():
    offset = -0.35  # chassis travels 20 degrees clockwise of where the phone looks
    pose = arkit_pose(0.0, 0.0, math.radians(30), pitch=math.radians(-85))
    position, rotation = frames.camera_in_odom(pose, floor_y=-0.15)
    base, yaw = frames.base_in_odom(position, rotation)
    assert math.isclose(yaw, math.radians(30), abs_tol=1e-9)  # forward axis nearly vertical: left axis used
    base, yaw = frames.base_in_odom(position, rotation, offset)
    assert math.isclose(yaw, math.radians(30) + offset, abs_tol=1e-9)
    _, relative_rotation = frames.camera_in_base(position, rotation, base, yaw)
    left = relative_rotation[:, 1]  # camera_link stays yawed by -offset inside base_link
    assert math.isclose(math.atan2(left[1], left[0]) - math.pi / 2, -offset, abs_tol=1e-9)


def test_quaternion_matches_rotation():
    rotation = frames.camera_in_odom(arkit_pose(0, 0, 1.1, pitch=0.3), -0.15)[1]
    x, y, z, w = frames.quaternion(rotation)
    rebuilt = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    np.testing.assert_allclose(rebuilt, rotation, atol=1e-9)


def test_optical_points_match_arkit_world_projection():
    """A depth pixel lands at the same odom point through TF as through ARKit geometry."""
    fx = fy = 100.0
    cx, cy = 80.0, 60.0
    depth = np.zeros((120, 160), np.float32)
    depth[30, 120] = 2.0
    pose = arkit_pose(0.3, -0.4, math.radians(25), pitch=math.radians(-8))
    floor_y = -0.15
    optical = frames.depth_points(depth, fx, fy, cx, cy)[0]
    # ARKit: camera x right, y up, z backward (jetson/mapping/phone.py world_grid).
    local = np.array([(120 - cx) * 2 / fx, -(30 - cy) * 2 / fy, -2.0])
    world = pose[:3, :3] @ local + pose[:3, 3]
    expected = frames.M @ world - np.array([0, 0, floor_y])
    position, rotation = frames.camera_in_odom(pose, floor_y)
    x, y, z, w = frames.OPTICAL_QUATERNION
    optical_rotation = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    through_tf = rotation @ optical_rotation @ optical + position
    np.testing.assert_allclose(through_tf, expected, atol=1e-5)


def test_camera_stays_at_mount_height_wherever_the_arkit_session_started():
    """Floor test 2026-10-11: ARKit y -0.75 (session began 0.75 m up) put the camera 0.6 m under the floor."""
    floor_y = -0.15
    for height in (-0.75, 0.0, 1.07):
        pose = arkit_pose(0.3, -0.4, math.radians(25), pitch=math.radians(-30), height=height)
        position, rotation = frames.camera_in_odom(pose, floor_y)
        assert math.isclose(position[2], 0.15, abs_tol=1e-12)
        # The floor 0.15 m under the camera lands at odom Z = 0 through TF.
        down = math.radians(30)
        depth = np.zeros((120, 160), np.float32)
        depth[60, 80] = 0.15 / math.sin(down)
        optical = frames.depth_points(depth, 100.0, 100.0, 80.0, 60.0)[0]
        x, y, z, w = frames.OPTICAL_QUATERNION
        optical_rotation = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        floor = rotation @ optical_rotation @ optical + position
        assert math.isclose(floor[2], 0.0, abs_tol=1e-6)


def test_depth_packet_decoding():
    import json
    import struct
    header = dict(type="depth", width=3, height=2, fx=1, fy=1, cx=1, cy=1, transform=list(np.eye(4).ravel()))
    data = json.dumps(header).encode()
    packet = struct.pack("<I", len(data)) + data + np.array([0, 1000, 2500, 0, 0, 65535], "<u2").tobytes()
    decoded, depth = protocol.decode_depth(packet)
    assert decoded["width"] == 3
    np.testing.assert_allclose(depth, [[0, 1.0, 2.5], [0, 0, 65.535]])


def test_velocity_estimator_rejects_stamp_jitter_and_wraps_yaw():
    rng = np.random.default_rng(3)
    estimator = frames.VelocityEstimator()
    rates = []
    for i in range(90):  # 3 s at 30 Hz: 0.2 m/s forward while spinning at -1.5 rad/s through +-pi
        t_true = i / 30
        yaw = math.atan2(math.sin(1.0 - 1.5 * t_true), math.cos(1.0 - 1.5 * t_true))
        x, y = 0.2 * t_true * math.cos(yaw), 0.2 * t_true * math.sin(yaw)  # position path barely matters here
        v, w = estimator.add(t_true + rng.uniform(-0.008, 0.008), x, y, yaw)
        if i > 10:
            rates.append(w)
    assert max(abs(w + 1.5) for w in rates) < 0.25  # raw differencing reaches ~1.6x on this jitter
    assert estimator.add(10.0, 0, 0, 0) == (0.0, 0.0)  # a pose gap restarts the estimate


def test_samples_need_normal_tracking_and_the_current_generation():
    assert protocol.accept_sample(dict(tracking="normal", generation=2), 2)
    assert not protocol.accept_sample(dict(tracking="limited", generation=2), 2)
    assert not protocol.accept_sample(dict(tracking="not_available"), 2)
    assert not protocol.accept_sample(dict(tracking="normal", generation=1), 2)
    assert protocol.accept_sample(dict(tracking="normal"), 2)  # pre-generation server
