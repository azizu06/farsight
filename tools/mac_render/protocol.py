"""Message shapes for the Mac visual offload. Visual only: no control, no motion.

/ws/macview (Mac -> viewer), request/response like /ws/video:
  {"type":"macview_init","version":1,"mime":"video/mp4; codecs=\\"avc1....\\"","source":"...","width":1280,"height":720,"fps":30,"stereo":false}
  then the fMP4 init bytes.
  viewer: {"type":"next","request_id":1}
  server: {"type":"macview_segment", ...} then the fMP4 fragment bytes.

/ws/macview-pose (viewer -> Mac), JSON, at most 72 Hz:
  {"type":"pose","version":1,"sent_mono_ms":12.5,"sent_wall_ms":1710000000000,
   "head":{"position":[x,y,z],"quaternion":[x,y,z,w]},
   "ray":{"origin":[x,y,z],"direction":[x,y,z]} | null}
"""

import math
import time

VERSION = 1
MAX_POSE_HZ = 72
# A pose older than this is not applied. It does not stretch any motor lease.
POSE_STALE_MS = 250


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def vec(value, size):
    if not isinstance(value, list) or len(value) != size or any(not finite(item) for item in value):
        raise ValueError(f"need {size} finite numbers")
    return [float(item) for item in value]


def quaternion(value):
    raw = vec(value, 4)
    length = math.sqrt(sum(item * item for item in raw))
    if length < 1e-6:
        raise ValueError("zero quaternion")
    return [item / length for item in raw]


def parse_pose(message, now_mono_ms):
    """Validate a pose. Returns the applied record, or raises ValueError."""
    if not isinstance(message, dict) or message.get("type") != "pose":
        raise ValueError("not a pose")
    if message.get("version") != VERSION:
        raise ValueError("unsupported pose version")
    sent = message.get("sent_mono_ms")
    wall = message.get("sent_wall_ms")
    if not finite(sent) or not finite(wall):
        raise ValueError("pose timestamps must be finite")
    head = message.get("head")
    if not isinstance(head, dict):
        raise ValueError("head required")
    pose = {
        "version": VERSION,
        "sent_mono_ms": float(sent),
        "sent_wall_ms": float(wall),
        "received_mono_ms": float(now_mono_ms),
        "position": vec(head.get("position"), 3),
        "quaternion": quaternion(head.get("quaternion")),
        "ray": None,
    }
    ray = message.get("ray", None)
    if ray is not None:
        if not isinstance(ray, dict):
            raise ValueError("ray must be an object or null")
        direction = vec(ray.get("direction"), 3)
        if math.sqrt(sum(item * item for item in direction)) < 1e-6:
            raise ValueError("zero ray")
        pose["ray"] = {"origin": vec(ray.get("origin"), 3), "direction": direction}
    return pose


def segment_sidecar(frame_id, pose, capture_mono_ms, capture_wall_ms, media_start_s, media_end_s,
                    request_id, source, encode_ms, render_ms, capture_ms):
    """JSON sent immediately before each fMP4 fragment."""
    echoed = None if pose is None else {
        "sent_mono_ms": pose["sent_mono_ms"],
        "sent_wall_ms": pose["sent_wall_ms"],
        "received_mono_ms": pose["received_mono_ms"],
    }
    return {
        # video_init/video_segment names match quest/src/h264.ts so the existing
        # player accepts the stream. macview_* fields are the extra measurements.
        "type": "video_segment",
        "macview": "macview_segment",
        "version": VERSION,
        "request_id": request_id,
        "age_ms": 0 if capture_wall_ms is None else max(0.0, _clock_ms() - capture_wall_ms),
        "frame_id": frame_id,
        "source": source,
        "stereo": False,
        "media_start_s": media_start_s,
        "media_end_s": media_end_s,
        "capture_mono_ms": capture_mono_ms,
        "capture_wall_ms": capture_wall_ms,
        "pose": echoed,
        "encode_ms": encode_ms,
        "render_ms": render_ms,
        "capture_ms": capture_ms,
    }


def _clock_ms():
    return time.time() * 1000


def frame_age_ms(capture_wall_ms, now_wall_ms):
    """How old the picture is: now minus the capture timestamp. Negative means clock skew."""
    if not finite(capture_wall_ms) or not finite(now_wall_ms):
        raise ValueError("frame age needs finite timestamps")
    return now_wall_ms - capture_wall_ms


def pose_to_frame_ms(pose, capture_mono_ms):
    """Mac-side pose-in to capture-out, both on the Mac monotonic clock."""
    if pose is None:
        return None
    received = pose.get("received_mono_ms")
    if not finite(received) or not finite(capture_mono_ms):
        raise ValueError("pose-to-frame needs finite monotonic timestamps")
    return capture_mono_ms - received


def percentile(values, fraction):
    ordered = sorted(item for item in values if finite(item))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    low = math.floor(index)
    high = math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)
