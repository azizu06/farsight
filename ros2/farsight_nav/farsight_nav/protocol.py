"""jetson.server /ws/nav bridge protocol v1 (docs/navigation.md). No ROS imports."""

import json
import struct

import numpy as np

VERSION = 1


def decode_depth(packet):
    """Binary depth packet -> (header dict, float32 metres with 0 = no return)."""
    if len(packet) < 4:
        raise ValueError("Depth packet too short")
    length = struct.unpack_from("<I", packet)[0]
    if not 0 < length <= 65536 or 4 + length > len(packet):
        raise ValueError("Invalid depth header length")
    header = json.loads(packet[4:4 + length])
    width, height = header.get("width"), header.get("height")
    if header.get("type") != "depth" or not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("Expected a depth packet")
    if len(packet) != 4 + length + width * height * 2:
        raise ValueError("Depth payload length mismatch")
    raw = np.frombuffer(packet, "<u2", width * height, 4 + length)
    return header, raw.reshape(height, width).astype(np.float32) / 1000.0


def hello(**fields):
    return dict(type="hello", version=VERSION, node="farsight_bridge", **fields)


def cmd_vel(linear, angular, goal_id=None, generation=None):
    """Capture goal and sensor generation before enqueueing; the server rejects stale authority."""
    return dict(type="cmd_vel", linear=float(linear), angular=float(angular),
                goal_id=goal_id, generation=generation)


def status(state, goal_id, *, path=(), frontiers=(), message="", **extra):
    return dict(type="status", state=state, goal_id=goal_id,
                path=[[round(float(x), 3), round(float(y), 3)] for x, y in path],
                frontiers=[[round(float(x), 3), round(float(y), 3)] for x, y in frontiers],
                message=message, **extra)


def encode(message):
    return json.dumps(message, separators=(",", ":"))


def accept_generation(message, generation):
    """Motion intent requires an explicit matching integer sensor generation."""
    value = message.get("generation")
    return type(value) is int and type(generation) is int and value >= 0 and value == generation


def accept_sample(message, generation):
    """True if a pose/depth sample may renew TF, odometry or costmaps.

    Only normal ARKit tracking counts, and only for the server's current sensor
    generation (announced in hello/reset); a missing field means an older server
    and is accepted for that field only.
    """
    if message.get("tracking", "normal") != "normal":
        return False
    sample = message.get("generation")
    return sample is None or generation is None or sample == generation

