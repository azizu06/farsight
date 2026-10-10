"""ARKit / voxel-map world <-> ROS frames. Pure numpy so it is testable without ROS.

ARKit world (= the voxel map): metres, Y up, the phone starts looking toward -Z.
ARKit camera axes: x right, y up, z backward (the optical axis is -z).

ROS (REP 103/105): odom is X forward (the rover's start heading), Y left, Z up
with Z = 0 on the floor. base_link sits on the floor under the camera, yaw only.
camera_link is the ROS body convention (x forward, y left, z up) at the phone's
optical centre; camera_optical is x right, y down, z forward.

    odom (X, Y, Z) = (-z, -x, y - floor_y)
"""

import math

import numpy as np

# odom_from_arkit: rotates ARKit world vectors into odom axes.
M = np.array([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
# arkit_camera_from_link: camera_link axes expressed in ARKit camera axes.
N = np.array([[0.0, -1.0, 0.0], [0.0, 0.0, 1.0], [-1.0, 0.0, 0.0]])
# camera_link -> camera_optical rotation (ROS optical convention), as a quaternion (x, y, z, w).
OPTICAL_QUATERNION = (-0.5, 0.5, -0.5, 0.5)


def world_to_odom(x, z):
    return -z, -x


def odom_to_world(X, Y):
    """ROS odom (X, Y) to voxel-map world (x, z)."""
    return -Y, -X


def transform_from_column_major(values):
    matrix = np.asarray(values, float).reshape(4, 4, order="F")
    if not np.isfinite(matrix).all():
        raise ValueError("Transform must be finite")
    return matrix


def camera_in_odom(camera_to_world, floor_y):
    """(position, rotation) of camera_link in odom from an ARKit camera-to-world transform."""
    rotation = M @ camera_to_world[:3, :3] @ N
    position = M @ camera_to_world[:3, 3]
    position[2] -= floor_y  # odom Z = 0 on the floor
    return position, rotation


def base_in_odom(camera_position, camera_rotation, yaw_offset=0.0):
    """base_link on the floor under the camera, heading along the chassis.

    yaw_offset is the drive profile's camera_yaw_offset_rad (travel heading minus
    camera heading, counter-clockwise); 0 means the phone looks straight ahead.
    """
    forward = camera_rotation[:, 0]
    if math.hypot(forward[0], forward[1]) >= 0.2:
        yaw = math.atan2(forward[1], forward[0])
    else:  # phone pitched near vertical: forward is 90 degrees clockwise of camera left
        left = camera_rotation[:, 1]
        yaw = math.atan2(left[1], left[0]) - math.pi / 2
    yaw = math.atan2(math.sin(yaw + yaw_offset), math.cos(yaw + yaw_offset))
    return np.array([camera_position[0], camera_position[1], 0.0]), yaw


def yaw_matrix(yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def camera_in_base(camera_position, camera_rotation, base_position, yaw):
    """camera_link relative to base_link: height plus the phone's pitch/roll."""
    inverse = yaw_matrix(yaw).T
    return inverse @ (camera_position - base_position), inverse @ camera_rotation


def quaternion(rotation):
    """Rotation matrix -> (x, y, z, w), numerically stable branch selection."""
    m = np.asarray(rotation, float)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    return x / norm, y / norm, z / norm, w / norm


def yaw_quaternion(yaw):
    return 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)


def depth_points(depth, fx, fy, cx, cy):
    """Metres (0 = no return) -> N x 3 float32 points in camera_optical (x right, y down, z forward)."""
    v, u = np.nonzero(depth > 0)
    z = depth[v, u].astype(np.float32)
    x = (u.astype(np.float32) - cx) * z / fx
    y = (v.astype(np.float32) - cy) * z / fy
    return np.column_stack((x, y, z)).astype(np.float32)
