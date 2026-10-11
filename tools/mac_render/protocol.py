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
import uuid
import re

VERSION = 1
MAX_POSE_HZ = 72
# A pose older than this is not applied. It does not stretch any motor lease.
POSE_STALE_MS = 250
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def vec(value, size):
    if not isinstance(value, list) or len(value) != size or any(not finite(item) for item in value):
        raise ValueError(f"need {size} finite numbers")
    return [float(item) for item in value]


def quaternion(value):
    raw = vec(value, 4)
    length = math.sqrt(sum(item * item for item in raw))
    if not math.isfinite(length) or length < 1e-6:
        raise ValueError("zero quaternion")
    return [item / length for item in raw]


def eye_views(eyes, require_pair=False):
    if not isinstance(eyes, list) or len(eyes) > 2:
        raise ValueError("invalid eye views")
    result = []
    for eye in eyes:
        if not isinstance(eye, dict):
            raise ValueError("invalid eye view")
        view = dict(position=vec(eye.get("position"), 3),
                    quaternion=quaternion(eye.get("quaternion")), projection=vec(eye.get("projection"), 16))
        if require_pair:
            matrix = view["projection"]
            scale = max(map(abs, matrix))
            if not scale:
                raise ValueError("singular eye projection")
            rows = [[matrix[col * 4 + row] / scale for col in range(4)] for row in range(4)]
            for col in range(4):
                pivot = max(range(col, 4), key=lambda row: abs(rows[row][col]))
                if abs(rows[pivot][col]) < 1e-12:
                    raise ValueError("singular eye projection")
                rows[col], rows[pivot] = rows[pivot], rows[col]
                for row in range(col + 1, 4):
                    factor = rows[row][col] / rows[col][col]
                    for at in range(col, 4):
                        rows[row][at] -= factor * rows[col][at]
        if "eye" in eye:
            if eye["eye"] not in ("left", "right"):
                raise ValueError("invalid eye label")
            view["eye"] = eye["eye"]
        result.append(view)
    labels = [view.get("eye") for view in result]
    if len([label for label in labels if label]) != len(set(label for label in labels if label)):
        raise ValueError("duplicate eye label")
    if require_pair and (len(result) != 2 or set(labels) != {"left", "right"}):
        raise ValueError("stereo requires labeled left and right eyes")
    if require_pair and labels != ["left", "right"]:
        raise ValueError("stereo wire eyes must be ordered left then right")
    return result


def parse_pose(message, now_mono_ms):
    """Validate a pose. Returns the applied record, or raises ValueError."""
    if not isinstance(message, dict) or message.get("type") not in ("pose", "pose_untracked"):
        raise ValueError("not a pose")
    if type(message.get("version")) is not int or message.get("version") != VERSION:
        raise ValueError("unsupported pose version")
    identity = {}
    if "session_id" in message or "frame_id" in message or "pose_id" in message:
        session = message.get("session_id")
        if not isinstance(session, str) or not _SESSION_ID.fullmatch(session):
            raise ValueError("invalid pose session")
        identity["session_id"] = session
        seq = message.get("frame_id", message.get("pose_id"))
        if type(seq) is not int or not 0 <= seq < 2**53:
            raise ValueError("invalid pose frame")
        identity["frame_id"] = seq
        identity["pose_id"] = seq
    nonce = message.get("client_nonce")
    if nonce is not None:
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 128 or not nonce.isascii():
            raise ValueError("invalid pose nonce")
        identity["client_nonce"] = nonce
    if message["type"] == "pose_untracked":
        return dict(identity, tracked=False, received_mono_ms=float(now_mono_ms))
    if message.get("tracked", True) is not True:
        raise ValueError("pose is not tracked")
    sent = message.get("sent_mono_ms")
    wall = message.get("sent_wall_ms")
    if not finite(sent) or not finite(wall):
        raise ValueError("pose timestamps must be finite")
    head = message.get("head")
    if not isinstance(head, dict):
        raise ValueError("head required")
    pose = {
        **identity, "version": VERSION, "tracked": True,
        "sent_mono_ms": float(sent),
        "sent_wall_ms": float(wall),
        "received_mono_ms": float(now_mono_ms),
        "position": vec(head.get("position"), 3),
        "quaternion": quaternion(head.get("quaternion")),
        "ray": None,
    }
    if "space" in message:
        if message["space"] not in ("ar_map", "xr_local_floor", "local-floor"):
            raise ValueError("unsupported pose coordinate space")
        pose["space"] = message["space"]
    for key, low, high in (("fov_y_deg", 1, 170), ("aspect", .1, 5)):
        if key in message:
            if not finite(message[key]) or not low <= message[key] <= high:
                raise ValueError(f"invalid {key}")
            pose[key] = float(message[key])
    if "map_revision" in message:
        value = message["map_revision"]
        if value is not None and (type(value) is not int or not 0 <= value < 2**53):
            raise ValueError("invalid map revision")
        pose["map_revision"] = value
    if "eyes" in message:
        pose["eyes"] = eye_views(message["eyes"])
    if "stereo" in message:
        if type(message["stereo"]) is not bool:
            raise ValueError("invalid stereo request")
        pose["stereo"] = message["stereo"]
        if message["stereo"]:
            eye_views(pose.get("eyes"), require_pair=True)
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
    echoed = None if pose is None else {key: pose[key] for key in
        ("session_id", "frame_id", "pose_id", "client_nonce", "sent_mono_ms", "sent_wall_ms", "received_mono_ms",
         "space", "fov_y_deg", "aspect", "map_revision", "eyes") if key in pose}
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
