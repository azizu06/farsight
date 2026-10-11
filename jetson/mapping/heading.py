"""Measured phone/chassis heading for operator head steering; no model or ROS dependency."""
import math
import time


def measured_heading(mapping, yaw_offset=0.0):
    # Like NavService.pose_snapshot, read replaced snapshots without the fusion lock:
    # a voxel integration must not stall the 40 Hz control stream.
    identity, generation = mapping.identity, mapping.socket_generation
    pose, received = mapping.pose, mapping.last_received
    age = time.monotonic() - received if received else math.inf
    if (identity is None or not mapping.connected or mapping.scan or
            mapping.tracking != 'normal' or age >= 1.0):
        return None
    # The optical forward axis is -Z. Roll about it does not change chassis heading.
    x, z = float(pose[0, 2]), float(pose[2, 2])
    if not math.isfinite(x) or not math.isfinite(z) or math.hypot(x, z) < .2:
        return None
    yaw = math.atan2(x, z) + yaw_offset
    if identity != mapping.identity or generation != mapping.socket_generation:
        return None
    return dict(yaw_rad=math.atan2(math.sin(yaw), math.cos(yaw)), age_ms=max(0, age * 1000),
                source='simulated' if mapping.demo else 'arkit', session_id=identity[0],
                map_epoch=identity[1], socket_generation=generation)
