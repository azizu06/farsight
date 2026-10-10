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
  Optional (the Quest AUTO view, quest/src/mac-auto-pose.js, sends all of them):
   "pose_id" (alias "frame_id"): non-negative integer, echoed with every picture drawn from it
   "session_id": renderer view identity, string up to 64 chars (UUID from the Quest)
   "space": "ar_map" (head is in AR-map coordinates), "map_revision", "viewer"
   "fov_y_deg" 20..120 and "aspect" 0.5..3: the frustum the viewer wants drawn
   "eyes": [{"position","quaternion","projection"[16]}] per XR eye, map coordinates
  The renderer view identity (session_id, pose_id) is not the capture/map identity.
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


def pose_is_stale(pose, now_mono_ms, limit_ms=POSE_STALE_MS):
    """True when the Mac has held this pose longer than the limit.

    Both clocks are the Mac monotonic clock. The viewer's own timestamp is
    never compared against ours, because those clocks are not the same.
    """
    received = pose.get("received_mono_ms") if isinstance(pose, dict) else None
    if not finite(received) or not finite(now_mono_ms):
        return True
    return now_mono_ms - received > limit_ms


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
    pose_id = message.get("pose_id", message.get("frame_id"))
    if pose_id is not None and (not isinstance(pose_id, int) or isinstance(pose_id, bool) or pose_id < 0):
        raise ValueError("pose_id must be a non-negative integer")
    session = message.get("session_id")
    if session is not None and (not isinstance(session, str) or not 0 < len(session) <= 64):
        raise ValueError("session_id must be a string of 1..64 characters")
    space = message.get("space")
    if space is not None and space != "ar_map":
        raise ValueError("unsupported pose space")
    fov = message.get("fov_y_deg")
    if fov is not None and (not finite(fov) or not 20 <= fov <= 120):
        raise ValueError("fov_y_deg must be 20..120")
    aspect = message.get("aspect")
    if aspect is not None and (not finite(aspect) or not 0.5 <= aspect <= 3):
        raise ValueError("aspect must be 0.5..3")
    revision = message.get("map_revision")
    eyes = message.get("eyes") or []
    if not isinstance(eyes, list) or len(eyes) > 2:
        raise ValueError("eyes must be a list of at most two views")
    pose = {
        "version": VERSION,
        "pose_id": pose_id,
        "session_id": session,
        "space": space,
        "map_revision": revision if finite(revision) else None,
        "fov_y_deg": float(fov) if fov is not None else None,
        "aspect": float(aspect) if aspect is not None else None,
        "eyes": [{
            "position": vec(eye.get("position"), 3),
            "quaternion": quaternion(eye.get("quaternion")),
            "projection": vec(eye.get("projection"), 16),
        } for eye in eyes if isinstance(eye, dict)],
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


def oldest_of(samples, count):
    """The first frame inside a fragment, not the newest one stamped on it."""
    if not samples or count < 1:
        return None
    return samples[-count] if len(samples) >= count else samples[0]


def segment_sidecar(frame_id, pose, capture_mono_ms, capture_wall_ms, media_start_s, media_end_s,
                    request_id, source, encode_ms, render_ms, capture_ms):
    """JSON sent immediately before each fMP4 fragment."""
    echoed = None if pose is None else {
        "sent_mono_ms": pose["sent_mono_ms"],
        "sent_wall_ms": pose["sent_wall_ms"],
        "received_mono_ms": pose["received_mono_ms"],
        "pose_id": pose.get("pose_id"),
        "session_id": pose.get("session_id"),
        "fov_y_deg": pose.get("fov_y_deg"),
        "aspect": pose.get("aspect"),
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
        # The frustum the picture was drawn with, so the viewer can size it.
        "fov_y_deg": None if echoed is None else echoed["fov_y_deg"],
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
