import unittest
from unittest import mock

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app


class StubFace:
    def __init__(self, path):
        self.path = path

    def detect(self, image):
        return []


class FaceIndicatorTests(unittest.TestCase):
    """/health and /mission/drill say whether the cls==face pass the drill scores on is loaded."""

    def fetch(self, **options):
        app = create_app(demo=True, simulate_rover=True, detector="hog", **options)
        with TestClient(app) as client:
            return app, client.get("/health").json(), client.get("/mission/drill").json()

    def test_without_face_model_reports_false(self):
        _, health, drill = self.fetch()
        for body in (health, drill):
            self.assertIs(body["face_detector"], False)
            self.assertIsNone(body["face_error"])

    def test_stub_face_detector_reports_true(self):
        with mock.patch("jetson.face.FaceDetector", StubFace):
            _, health, drill = self.fetch(face_model="stub.onnx")
        for body in (health, drill):
            self.assertIs(body["face_detector"], True)
            self.assertIsNone(body["face_error"])

    def test_bad_face_model_keeps_primary_detector_running(self):
        app, health, drill = self.fetch(face_model="/nonexistent/face.onnx")
        for body in (health, drill):
            self.assertIs(body["face_detector"], False)
            self.assertIn("Face model unavailable", body["face_error"])
        self.assertIsNone(health["detector_error"])
        self.assertEqual(health["detector"], "hog")


if __name__ == "__main__":
    unittest.main()
