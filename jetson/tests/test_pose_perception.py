import os
import unittest
from pathlib import Path

import cv2
import numpy as np

from jetson.perception import PerceptionService
from jetson.pose import Detection, make_pose_detector
from jetson.pose.boxes import detection_to_box
from jetson.pose.trt_backend import TensorRTPoseDetector

MODEL = Path(os.environ.get("FARSIGHT_POSE_MODEL", Path(__file__).parents[2] / "models" / "yolo11n-pose.onnx"))


class FakePose:
    name = "fake-pose"

    def detect(self, frame):
        k = np.zeros((17, 3), np.float32)
        k[:, 0], k[:, 1], k[:, 2] = 320, 240, 0.9
        return [Detection(box=(64, 48, 128, 240), conf=0.91, kpts=k, down=True),
                Detection(box=(0, 0, 64, 48), conf=0.5, kpts=None, down=False)]


def jpeg(width=640, height=480):
    ok, buf = cv2.imencode(".jpg", np.full((height, width, 3), 90, np.uint8))
    assert ok
    return buf.tobytes()


class PoseBoxTests(unittest.TestCase):
    def test_detection_to_box_normalizes_and_adds_pose_fields(self):
        k = np.zeros((17, 3), np.float32)
        k[:, 0], k[:, 1], k[:, 2] = 320, 240, 0.9
        box = detection_to_box(Detection((64, 48, 128, 240), 0.91, k, True), 640, 480)
        self.assertEqual((box["x"], box["y"], box["w"], box["h"]), (0.1, 0.1, 0.2, 0.5))
        self.assertEqual((box["cls"], box["class_id"], box["conf"], box["down"]), ("person", 0, 0.91, True))
        self.assertEqual(len(box["kpts"]), 17)
        self.assertEqual(box["kpts"][0], [0.5, 0.5, 0.9])

    def test_box_without_keypoints_omits_kpts(self):
        box = detection_to_box(Detection((0, 0, 64, 48), 0.5), 640, 480)
        self.assertNotIn("kpts", box)
        self.assertIs(box["down"], False)


class PosePerceptionTests(unittest.TestCase):
    def service(self):
        service = PerceptionService(None, mode="pose")
        service.pose = FakePose()
        return service

    def test_pose_mode_boxes_carry_kpts_down_and_stable_track_ids(self):
        service = self.service()
        first = service.track(service._detect(jpeg()), 1.0)
        second = service.track(service._detect(jpeg()), 1.1)
        self.assertEqual([b["id"] for b in first], [b["id"] for b in second])
        self.assertTrue(first[0]["down"])
        self.assertEqual(len(first[0]["kpts"]), 17)
        self.assertEqual(first[0]["cls"], "person")

    def test_pose_mode_requires_a_model(self):
        service = PerceptionService(None, mode="pose")
        with self.assertRaisesRegex(ValueError, "pose"):
            service._setup()

    def test_tensorrt_pose_stub_fails_clearly(self):
        with self.assertRaisesRegex(NotImplementedError, "TensorRT"):
            TensorRTPoseDetector("models/yolo11n-pose.engine")
        service = PerceptionService(None, model="models/yolo11n-pose.engine", mode="pose")
        with self.assertRaisesRegex(NotImplementedError, "TensorRT"):
            service._setup()

    def test_make_pose_detector_rejects_missing_model(self):
        with self.assertRaises(FileNotFoundError):
            make_pose_detector("/nonexistent/yolo11n-pose.onnx")

    @unittest.skipUnless(MODEL.exists(), "models/yolo11n-pose.onnx not present")
    def test_real_onnx_model_runs_through_perception(self):
        service = PerceptionService(None, model=str(MODEL), mode="pose")
        service._setup()
        boxes = service._detect(jpeg())
        self.assertIsInstance(boxes, list)
        self.assertEqual(service.mode, "pose")
        self.assertEqual(service.fps, 10)
