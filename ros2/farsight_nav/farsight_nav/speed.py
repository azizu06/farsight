"""Clearance-adaptive Explore speed: full speed in open space, the AUTO floor in tight spots.

No ROS dependency. Clearance is metres from the body centre to the nearest measured
obstacle on the current map; the top speed is what the server can actually drive
(the drive profile's top, else the normalized full scale).
"""

import math

import numpy as np


def clearance(lethal, resolution, origin, xy, reach):
    """Metres from xy to the nearest lethal cell within reach (cell area, not centre); None past reach.

    Off-map space is unknown, not an obstacle: a small, freshly started map never slows the rover.
    """
    col, row = (xy[0] - origin[0]) / resolution, (xy[1] - origin[1]) / resolution
    span = math.ceil(reach / resolution) + 1
    r0, c0 = max(0, math.floor(row) - span), max(0, math.floor(col) - span)
    window = lethal[r0:max(0, math.floor(row) + span + 1), c0:max(0, math.floor(col) + span + 1)]
    cells = np.argwhere(window)
    if not len(cells):
        return None
    centres = (cells + (r0, c0) + 0.5) * resolution
    nearest = float(np.min(np.hypot(centres[:, 1] - col * resolution, centres[:, 0] - row * resolution)))
    nearest = max(0.0, nearest - resolution / math.sqrt(2))
    return nearest if nearest < reach else None


def top_speed(hello):
    """The server's fastest translation (m/s) from its hello, or None before one arrives."""
    if not isinstance(hello, dict):
        return None
    profile = hello.get("drive_profile") or {}
    top = profile.get("max_linear_mps") if isinstance(profile, dict) else None
    if not (isinstance(top, (int, float)) and math.isfinite(top) and top > 0):
        speed, max_v = hello.get("speed_mps"), hello.get("max_v")
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (speed, max_v)):
            return None
        top = speed * max_v
    return float(top)


def clearance_limit(clearance_m, top_mps, *, floor_mps=0.15, tight_m=0.4, open_m=1.0):
    """Fastest translation for this clearance: top from open_m out, floor_mps at tight_m and closer."""
    if clearance_m is None or not math.isfinite(clearance_m) or clearance_m >= open_m:
        return top_mps
    share = max(0.0, (clearance_m - tight_m) / (open_m - tight_m))
    return floor_mps + (max(top_mps, floor_mps) - floor_mps) * share


def limit(command, clearance_m, top_mps, **bands):
    """(v, w) scaled down together so |v| obeys the clearance limit (same arc); slower commands pass."""
    v, w = command
    if top_mps is None:
        return command
    cap = clearance_limit(clearance_m, top_mps, **bands)
    if abs(v) <= cap:
        return command
    return math.copysign(cap, v), w * cap / abs(v)
