"""UltraFace output contract and optional real-model CPU rehearsal."""
import os
from pathlib import Path

import numpy as np
import pytest

from jetson.face import decode


def test_decode_normalized_boxes_suppresses_overlap_and_rejects_invalid():
    scores = np.array([[[.1, .9], [.2, .8], [.9, .1], [.1, .9], [.1, .9]]])
    boxes = np.array([[[.1, .2, .4, .6], [.11, .21, .41, .61],
                       [.6, .6, .8, .8], [np.nan, 0, 1, 1], [.8, .8, .7, .9]]])
    result = decode(scores, boxes)
    assert len(result) == 1
    assert result[0].pop("cls") == "face"
    assert result[0] == pytest.approx(dict(conf=.9, x=.1, y=.2, w=.3, h=.4))


def test_decode_clips_and_handles_empty_and_bad_shapes():
    assert decode(np.array([[[.1, .9]]]), np.array([[[-.1, .2, 1.2, .8]]]))[0]["w"] == 1
    assert decode(np.zeros((1, 0, 2)), np.zeros((1, 0, 4))) == []
    with pytest.raises(ValueError, match="UltraFace"):
        decode(np.zeros((1, 2, 2)), np.zeros((1, 3, 4)))


def test_extra_pass_preserves_person_boxes_and_tracks_by_class():
    import cv2
    from jetson.perception import PerceptionService
    service = PerceptionService(None, mode="none", face_model="optional.onnx")
    class FakeFace:
        def detect(self, image):
            return [dict(cls="face", conf=.9, x=.1, y=.2, w=.3, h=.4)]
    service.face = FakeFace()
    service._detect_primary = lambda image: [dict(cls="person", conf=.8, x=.1, y=.2, w=.3, h=.4)]
    jpeg = cv2.imencode('.jpg', np.zeros((480, 640, 3), np.uint8))[1].tobytes()
    boxes = service.track(service._detect(jpeg), 1)
    assert [b["cls"] for b in boxes] == ["person", "face"]
    assert boxes[0]["id"] != boxes[1]["id"]
    assert [b["id"] for b in service.track(service._detect(jpeg), 1.1)] == [b["id"] for b in boxes]


def test_real_model_smoke():
    from jetson.face import FaceDetector
    model = Path(os.environ.get("FARSIGHT_FACE_MODEL", "models/version-RFB-320.onnx"))
    if not model.is_file():
        pytest.skip("Set FARSIGHT_FACE_MODEL to the downloaded UltraFace ONNX")
    result = FaceDetector(model).detect(np.zeros((480, 640, 3), np.uint8))
    assert result == []  # blank image must not invent rescue victims
