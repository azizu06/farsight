"""ARKit / voxel-map world <-> ROS frames. Pure numpy so it is testable without ROS.

ARKit world (= the voxel map): metres, Y up, the phone starts looking toward -Z.
ARKit camera axes: x right, y up, z backward (the optical axis is -z).

ROS (REP 103/105): odom is X forward (the rover's start heading), Y left, Z up
with Z = 0 on the floor. base_link sits on the floor under the camera, yaw only.
camera_link is the ROS body convention (x forward, y left, z up) at the phone's
optical centre; camera_optical is x right, y down, z forward.

    odom (X, Y) = (-z, -x); camera_link Z is the mount height -floor_y.
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
    """(position, rotation) of camera_link in odom from an ARKit camera-to-world transform.

    The phone is fixed to the chassis, so the camera always sits -floor_y above the floor
    under it. ARKit's y is relative to wherever the session started and drifts during fast
    spins (floor test 2026-10-11: -0.6 to +1.2 m), which put the costmap's raytrace origin
    outside its 0-0.5 m voxel band: no clearing, and the wrong height slice marked.
    """
    rotation = M @ camera_to_world[:3, :3] @ N
    position = M @ camera_to_world[:3, 3]
    position[2] = -floor_y  # odom Z = 0 on the floor
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


class VelocityEstimator:
    """Body velocity (forward m/s, yaw rate rad/s) from base_link poses over a fixed window.

    Differencing consecutive 30 Hz poses amplifies stamp jitter (a 20 ms stamp gap
    over 33 ms of motion reads 1.6x the true rate), and MPPI seeds its rollouts
    with this speed, so spikes make it believe the rover is still spinning. A
    window of ~150 ms keeps lag small and the noise an order of magnitude lower.
    """

    def __init__(self, window=0.15, max_linear=1.5, max_angular=4.0):
        self.window, self.max_linear, self.max_angular = window, max_linear, max_angular
        self.samples = []   # (t, x, y, unwrapped yaw)

    def add(self, t, x, y, yaw):
        if self.samples:
            last = self.samples[-1]
            if t <= last[0]:
                return self.estimate()
            if t - last[0] > 1.0:
                self.samples = []  # a pose gap: restart rather than average across it
            else:
                yaw = last[3] + math.atan2(math.sin(yaw - last[3]), math.cos(yaw - last[3]))
        self.samples.append((t, x, y, yaw))
        while len(self.samples) > 2 and t - self.samples[1][0] >= self.window:
            self.samples.pop(0)
        return self.estimate()

    def estimate(self):
        if len(self.samples) < 2:
            return 0.0, 0.0
        t0, x0, y0, yaw0 = self.samples[0]
        t1, x1, y1, yaw1 = self.samples[-1]
        dt = t1 - t0
        if dt < self.window / 3:
            return 0.0, 0.0
        heading = (yaw0 + yaw1) / 2
        forward = ((x1 - x0) * math.cos(heading) + (y1 - y0) * math.sin(heading)) / dt
        angular = (yaw1 - yaw0) / dt
        return (max(-self.max_linear, min(self.max_linear, forward)),
                max(-self.max_angular, min(self.max_angular, angular)))

