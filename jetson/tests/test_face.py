"""Face boxes: pose head keypoints (default), UltraFace output contract, optional real-model CPU rehearsal."""
import os
from pathlib import Path

import numpy as np
import pytest

from jetson.face import decode, keypoint_faces
from jetson.pose import Detection

W, H = 640, 480
# Nose, left eye, right eye, left ear, right ear in pixels: 60 px wide, 20 px tall.
HEAD = [(320, 110), (330, 100), (310, 100), (350, 105), (290, 105)]


def person(head_conf, width=W, height=H, w=.05):
    """Packet-form person box (32 px wide by default) whose first five normalized keypoints are HEAD."""
    kpts = [[x / width, y / height, c] for (x, y), c in zip(HEAD, head_conf)] + [[.5, .5, .9]] * 12
    return dict(cls="person", class_id=0, conf=.9, x=.45, y=.2, w=w, h=.6, down=False, kpts=kpts)


def test_head_keypoints_give_a_square_1_4x_their_horizontal_span():
    (face,) = keypoint_faces([person([.9, .8, .8, .7, .7])], W, H)
    assert face.pop("cls") == "face"
    side = 1.4 * 60  # square in pixels, centred on the mean of the points (320, 104)
    assert face == pytest.approx(dict(conf=.78, x=(320 - side / 2) / W, y=(104 - side / 2) / H,
                                      w=side / W, h=side / H))


def test_fewer_than_three_confident_head_keypoints_give_no_face():
    assert keypoint_faces([person([.9, .9, .3, .2, .1])], W, H) == []
    assert keypoint_faces([person([0, 0, 0, 0, 0])], W, H) == []


def test_low_confidence_keypoints_are_ignored():
    # Only nose and both eyes (> 0.5) set the extent; the ears at 0.5 and 0.2 do not.
    (face,) = keypoint_faces([person([.9, .9, .9, .5, .2])], W, H)
    side = 1.4 * 20
    assert face["conf"] == .9
    assert face["w"] * W == pytest.approx(side) and face["h"] * H == pytest.approx(side)
    assert face["x"] * W == pytest.approx(320 - side / 2) and face["y"] * H == pytest.approx(310 / 3 - side / 2)


def test_narrow_heads_use_the_8_px_minimum_and_vertical_spread_is_ignored():
    narrow = person([.9] * 5)
    narrow["kpts"][:5] = [[x / W, y / H, .9] for x, y in ((320, 90), (321, 100), (322, 120))] + [[0, 0, .1]] * 2
    (face,) = keypoint_faces([narrow], W, H)  # 2 px wide, 30 px tall
    assert face["w"] * W == pytest.approx(1.4 * 8) and face["h"] * H == pytest.approx(1.4 * 8)
    assert face["x"] * W + 5.6 == pytest.approx(321) and face["y"] * H + 5.6 == pytest.approx(310 / 3)


def test_only_person_boxes_with_keypoints_count_and_edges_clip():
    boxes = [dict(cls="person", conf=.9, x=0, y=0, w=.1, h=.1), dict(person([.9] * 5), cls="chair")]
    assert keypoint_faces(boxes, W, H) == []
    edge = person([.9] * 5)
    edge["kpts"][:5] = [[0, .01, .9], [.02, 0, .9], [.03, .02, .9], [0, 0, .1], [0, 0, .1]]
    (face,) = keypoint_faces([edge], W, H)
    assert face["x"] == 0 and face["y"] == 0 and face["w"] > 0 and face["h"] > 0


class HeadPose:
    name = "fake-pose"

    def detect(self, frame):
        k = np.full((17, 3), .9, np.float32)
        k[:5, :2] = HEAD
        k[5:, :2] = (320, 300)
        return [Detection(box=(280, 80, 80, 380), conf=.91, kpts=k)]


def pose_service(**options):
    from jetson.perception import PerceptionService
    service = PerceptionService(None, mode="pose", model="pose.onnx", **options)
    service.pose = HeadPose()
    return service


def frame_jpeg():
    import cv2
    return cv2.imencode(".jpg", np.zeros((H, W, 3), np.uint8))[1].tobytes()


def test_pose_faces_keep_the_ultraface_packet_shape_and_track_ids():
    service = pose_service()
    boxes = service.track(service._detect(frame_jpeg()), 1)
    assert [b["cls"] for b in boxes] == ["person", "face"]
    ultraface = decode(np.array([[[.1, .9]]]), np.array([[[.1, .2, .4, .6]]]))[0]
    assert set(boxes[1]) == set(ultraface) | {"id"}
    assert boxes[1]["conf"] == .9 and type(boxes[1]["id"]) is int and boxes[1]["id"] != boxes[0]["id"]
    assert service.face_status() == dict(face_detector=True, face_error=None)


def test_pose_faces_follow_the_drill_gate():
    service = pose_service()
    service.face_wanted = lambda: False
    assert [b["cls"] for b in service._detect(frame_jpeg())] == ["person"]


def test_default_never_loads_ultraface_and_needs_the_pose_detector(monkeypatch):
    import jetson.face
    monkeypatch.setattr(jetson.face, "FaceDetector", lambda path: pytest.fail("UltraFace loaded"))
    monkeypatch.setattr("jetson.pose.make_pose_detector", lambda model: HeadPose())
    service = pose_service(face_model="version-RFB-320.onnx")
    service._setup()
    assert service.face is None and service.face_status()["face_detector"] is True
    from jetson.perception import PerceptionService
    hog = PerceptionService(None, mode="hog", face_model="version-RFB-320.onnx")
    hog._setup()
    status = hog.face_status()
    assert status["face_detector"] is False and "--detector pose" in status["face_error"]


def test_ultraface_stays_selectable_for_rollback(monkeypatch):
    import jetson.face
    from jetson.perception import PerceptionService
    loaded = []
    monkeypatch.setattr(jetson.face, "FaceDetector", lambda path: loaded.append(path) or "ultraface")
    service = PerceptionService(None, mode="none", face_model="version-RFB-320.onnx", face_source="ultraface")
    service._setup()
    assert loaded == ["version-RFB-320.onnx"] and service.face == "ultraface"
    assert service.face_status() == dict(face_detector=True, face_error=None)

    class UltraFace:
        def detect(self, image):
            return [dict(cls="face", conf=.8, x=.1, y=.1, w=.1, h=.1)]
    service = pose_service(face_source="ultraface")
    service.face = UltraFace()
    faces = [b for b in service._detect(frame_jpeg()) if b["cls"] == "face"]
    assert faces == [dict(cls="face", conf=.8, x=.1, y=.1, w=.1, h=.1)]  # no keypoint faces alongside


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
