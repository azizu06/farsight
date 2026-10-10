"""Pure numpy pre/post-processing for YOLO11-pose (output 1x56x8400, no torch)."""

from __future__ import annotations

import cv2
import numpy as np

from .keypoints import is_down
from .types import Detection

PAD_COLOUR = 114


def letterbox(frame: np.ndarray, side: int = 640) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize keeping aspect, pad to side x side. Returns (image, scale, (pad_x, pad_y))."""
    h, w = frame.shape[:2]
    scale = min(side / w, side / h)
    nw, nh = round(w * scale), round(h * scale)
    resized = frame if (nw, nh) == (w, h) else cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    pad_x, pad_y = (side - nw) // 2, (side - nh) // 2
    out = np.full((side, side, 3), PAD_COLOUR, np.uint8)
    out[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return out, scale, (pad_x, pad_y)


def to_tensor(letterboxed_bgr: np.ndarray) -> np.ndarray:
    """BGR HWC uint8 -> RGB NCHW float32 0..1."""
    rgb = letterboxed_bgr[:, :, ::-1]
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float = 0.45) -> list[int]:
    """Greedy NMS over (x, y, w, h) boxes. Returns kept indices, best score first."""
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = x1 + boxes[:, 2], y1 + boxes[:, 3]
    areas = boxes[:, 2] * boxes[:, 3]
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        iw = np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
        ih = np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None)
        inter = iw * ih
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thres]
    return keep


def decode(raw: np.ndarray, frame_shape: tuple[int, ...], side: int = 640,
           conf_thres: float = 0.35, iou_thres: float = 0.45) -> list[Detection]:
    """Turn the raw 1x56x8400 tensor into Detections in original-frame pixels.

    Per anchor: cx, cy, w, h (input px), person conf, then 17 x (x, y, conf).
    """
    pred = np.asarray(raw)
    pred = pred[0] if pred.ndim == 3 else pred
    pred = pred.T  # (anchors, 56)
    pred = pred[pred[:, 4] >= conf_thres]
    if not len(pred):
        return []

    fh, fw = frame_shape[:2]
    scale = min(side / fw, side / fh)
    pad_x, pad_y = (side - round(fw * scale)) // 2, (side - round(fh * scale)) // 2

    cx = (pred[:, 0] - pad_x) / scale
    cy = (pred[:, 1] - pad_y) / scale
    w, h = pred[:, 2] / scale, pred[:, 3] / scale
    x1, y1 = np.clip(cx - w / 2, 0, fw), np.clip(cy - h / 2, 0, fh)
    x2, y2 = np.clip(cx + w / 2, 0, fw), np.clip(cy + h / 2, 0, fh)
    boxes = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
    scores = pred[:, 4]

    detections = []
    for i in nms(boxes, scores, iou_thres):
        if boxes[i, 2] <= 0 or boxes[i, 3] <= 0:
            continue
        kpts = pred[i, 5:56].reshape(17, 3).copy()
        kpts[:, 0] = (kpts[:, 0] - pad_x) / scale
        kpts[:, 1] = (kpts[:, 1] - pad_y) / scale
        box = tuple(float(v) for v in boxes[i])
        detections.append(Detection(box=box, conf=float(scores[i]), kpts=kpts, down=is_down(kpts, box)))
    return detections
