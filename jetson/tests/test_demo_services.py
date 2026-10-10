import json
import struct
import tempfile
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from jetson.camera import CameraService
from jetson.mapping.demo import simulated_frame
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.mission import Mission
from jetson.perception import PerceptionService, yolo_boxes


class ServicesTests(unittest.TestCase):
    def test_assistant_http_audio_dependency_and_upload_limits(self):
        with TestClient(create_app(detector="none")) as client:
            self.assertEqual(client.post("/ask", json={"text": ""}).status_code, 400)
            self.assertEqual(client.post("/ask", data="bad", headers={"Content-Type": "application/json"}).status_code, 400)
            self.assertEqual(client.post("/ask", files={"audio": ("sample.wav", b"RIFF1234WAVE", "audio/wav")}).status_code, 503)
            oversized = b"RIFF1234WAVE" + b"0" * (9 * 1024 * 1024)
            self.assertEqual(client.post("/ask", files={"audio": ("sample.wav", oversized, "audio/wav")}).status_code, 413)
            self.assertEqual(client.get("/tts/reply-missing.wav").status_code, 404)
    def test_simulated_video_cannot_drive_physical_motors(self):
        with self.assertRaises(ValueError):
            create_app(demo=True, serial_port="/dev/ttyACM0")
        with self.assertRaises(ValueError):
            create_app(simulate_rover=True, serial_port="/dev/ttyACM0")
    def test_yolo_letterbox_person_coordinates_and_class_nms(self):
        prediction = np.zeros((1, 84, 8400), np.float32)
        prediction[0, :4, 0] = [320, 320, 320, 160]
        prediction[0, 4, 0] = .9
        prediction[0, :4, 1] = [320, 320, 320, 160]
        prediction[0, 4, 1] = .8
        boxes = yolo_boxes(prediction, (480, 640, 3))
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["cls"], "person")
        self.assertAlmostEqual(boxes[0]["x"], .25)
        self.assertAlmostEqual(boxes[0]["y"], 1 / 3)
        self.assertAlmostEqual(boxes[0]["h"], 1 / 3)

    def test_track_ids_persist_with_overlap_and_expire(self):
        service = PerceptionService(None)
        box = dict(cls="person", x=.1, y=.1, w=.2, h=.4)
        first = service.track([dict(box)], 0)[0]["id"]
        self.assertEqual(service.track([dict(box, x=.12)], 1)[0]["id"], first)
        self.assertNotEqual(service.track([dict(box)], 4)[0]["id"], first)

    def test_stale_detection_is_not_presented_as_live(self):
        service = PerceptionService(None)
        service.packet.update(received=time.monotonic() - 2, boxes=[{"cls": "person"}])
        self.assertFalse(service.snapshot()["fresh"])
        self.assertEqual(service.snapshot()["boxes"], [])

    def test_mission_preserves_real_source_and_snapshot(self):
        state = MapState(demo=True)
        state.process(simulated_frame(0, 0))
        frame = CameraService(state).snapshot()
        box = dict(id=1, cls="person", x=.1, y=.1, w=.2, h=.4)
        with tempfile.TemporaryDirectory() as directory:
            mission = Mission(directory)
            mission.start_drill()
            mission.detect({"boxes": [box]}, frame)
            mission.detect({"boxes": [box]}, frame)
            report = mission.report()
            self.assertEqual(len(report["events"]), 1)
            self.assertEqual(report["events"][0]["source"], "SIMULATED")
            self.assertEqual(report["drill"]["person_encounters"], 1)
            self.assertTrue((mission.directory / report["events"][0]["snapshot"]).is_file())

    def test_rgb_phone_without_lidar_and_local_assistant(self):
        app = create_app(detector="none")
        jpeg = simulated_frame(0, 0).jpeg
        header = dict(version=1, type="rgb_frame", session_id="rgb-test", frame_id=0,
                      t_capture=1.0, jpeg_len=len(jpeg))
        encoded = json.dumps(header).encode()
        packet = struct.pack("<I", len(encoded)) + encoded + jpeg
        with TestClient(app) as client:
            with client.websocket_connect("/phone/rgb") as phone:
                phone.send_json(dict(version=1, type="hello", session_id="rgb-test", map_epoch=1,
                                     device="iPhone RGB", supports_scene_depth=False))
                phone.send_bytes(packet)
                for _ in range(100):
                    if app.state.camera.snapshot():
                        break
                    time.sleep(.01)
                image = client.get("/camera.jpg")
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.headers["X-Frame-Source"], "PHONE RGB")
                self.assertTrue(client.get("/health").json()["camera"])
                self.assertEqual(client.get("/health").json()["points"], 0)
                reply = client.post("/ask", json={"text": "What do you see?"}).json()
                self.assertIsNone(reply["action"])
                self.assertEqual(reply["model"], "local-observation-summary")
                self.assertEqual(client.post("/mission/drill", json={"duration_s": 90}).status_code, 200)
                self.assertEqual(client.get("/mission/report").status_code, 200)
            app.state.camera.phone_rgb = app.state.camera.phone_rgb.__class__(jpeg, time.monotonic() - 1, 0, "PHONE RGB")
            self.assertFalse(client.get("/health").json()["camera"])


if __name__ == "__main__":
    unittest.main()
