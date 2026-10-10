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


def cmd_vel(linear, angular):
    return dict(type="cmd_vel", linear=float(linear), angular=float(angular))


def status(state, goal_id, *, path=(), frontiers=(), message="", **extra):
    return dict(type="status", state=state, goal_id=goal_id,
                path=[[round(float(x), 3), round(float(y), 3)] for x, y in path],
                frontiers=[[round(float(x), 3), round(float(y), 3)] for x, y in frontiers],
                message=message, **extra)


def encode(message):
    return json.dumps(message, separators=(",", ":"))
