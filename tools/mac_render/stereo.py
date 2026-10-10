"""Explicit packed-view contract. Metadata alone does not prove stereo pixels."""
from .protocol import eye_views

SBS = "sbs-left-right"


def capture_format(meta, layout, width, height):
    if layout is None:
        return None if meta.get("stereo") is True else {"stereo": False}
    if (layout != SBS or meta.get("stereo") is not True or meta.get("layout") != SBS
            or type(meta.get("eye_width")) is not int or meta["eye_width"] * 2 != width
            or type(meta.get("eye_height")) is not int or meta["eye_height"] != height):
        return None
    pose = meta.get("poseEcho")
    if (not isinstance(pose, dict) or not isinstance(pose.get("session_id"), str)
            or not pose["session_id"] or type(pose.get("frame_id")) is not int or pose["frame_id"] < 0):
        return None
    try:
        eye_views(pose.get("eyes"), require_pair=True)
    except ValueError:
        return None
    return dict(stereo=True, layout=SBS, eye_width=width // 2, eye_height=height)
