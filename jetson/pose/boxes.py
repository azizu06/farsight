"""Detection -> the box dict used in detection packets (docs/interfaces.md)."""

from __future__ import annotations

from .types import Detection


def detection_to_box(d: Detection, width: int, height: int) -> dict:
    """Normalized 0..1, top-left origin. kpts is omitted when the detection has no pose."""
    x, y, w, h = d.box
    box = dict(cls="person", class_id=0, conf=round(d.conf, 3),
               x=x / width, y=y / height, w=w / width, h=h / height, down=bool(d.down))
    if d.kpts is not None:
        box["kpts"] = [[round(float(kx) / width, 4), round(float(ky) / height, 4), round(float(kc), 3)]
                       for kx, ky, kc in d.kpts]
    return box
