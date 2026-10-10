"""Camera receipt must advance while the map worker is deliberately stalled."""
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app
from jetson.mapping.tests.test_mapping import bundle, flat_frame


class CameraFastLaneTests(unittest.TestCase):
    def test_rgb_decoder_keeps_only_latest_pending_frame(self):
        from jetson.tests.test_capture_permits import rgb_packet
        app = create_app(detector="none")
        camera = app.state.camera
        entered, release = threading.Event(), threading.Event()
        seen = []
        accept = camera.accept_phone_rgb

        def slow(packet, session_id, previous_frame, received_at, permits):
            if not seen:
                entered.set()
                release.wait(2)
            result = accept(packet, session_id, previous_frame, received_at, permits)
            seen.append(result)
            return result

        try:
            with patch.object(camera, "accept_phone_rgb", slow), TestClient(app) as client:
                with client.websocket_connect("/phone/rgb") as phone:
                    phone.send_json(dict(version=1, type="hello", session_id="test", map_epoch=1,
                                         supports_scene_depth=False))
                    phone.send_bytes(rgb_packet(frame=0))
                    self.assertTrue(entered.wait(1))
                    for frame in range(1, 21):
                        phone.send_bytes(rgb_packet(frame=frame))
                    deadline = time.monotonic() + .3
                    while camera.ingest_dropped < 19 and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertEqual(camera.ingest_dropped, 19)
                    release.set()
                    deadline = time.monotonic() + .3
                    while camera.status()["camera_frame"] != 20 and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertEqual(seen, [0, 20])
        finally:
            release.set()

    def test_rgbd_camera_and_tracking_advance_while_fusion_is_blocked(self):
        app = create_app(detector="none", lidar_drive=True)
        camera, state = app.state.camera, app.state.mapping
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)

        try:
            with patch.object(state, "process", blocked), TestClient(app) as client:
                with client.websocket_connect("/phone") as phone:
                    phone.send_json(dict(version=1, type="hello", session_id="test", map_epoch=1,
                                         supports_scene_depth=True))
                    frame = flat_frame()
                    phone.send_bytes(bundle(frame))
                    self.assertTrue(entered.wait(1))
                    # Pose heartbeats must not hold up the next camera packet.
                    with state.lock:
                        phone.send_json(dict(frame.header, type="pose", frame_id=1, t_capture=1))
                        frame.header.update(frame_id=2, t_capture=2, tracking="limited")
                        sent = time.monotonic()
                        phone.send_bytes(bundle(frame))
                        while time.monotonic() - sent < .3:
                            if camera.status()["camera_frame"] == 2:
                                break
                            time.sleep(.005)
                        status = camera.status()
                        self.assertEqual(status["camera_frame"], 2)
                        self.assertLess(status["camera_age_ms"], 300)
                        self.assertFalse(status["camera_drive_ready"])
                        self.assertFalse(status["camera_capture_verified"])
                        state.processing_ms = 900
                        result = client.get("/video/status").json()
                        self.assertEqual(result["mapping_processing_ms"], 900)
                        self.assertEqual(result["camera_frame"], 2)
                        self.assertLess(result["camera_age_ms"], 300)
                    frame.header.update(frame_id=3, t_capture=3, tracking="normal")
                    phone.send_bytes(bundle(frame))
                    deadline = time.monotonic() + .3
                    while camera.status()["camera_frame"] != 3 and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertTrue(camera.status()["camera_drive_ready"])
                    with state.lock:
                        phone.send_json(dict(frame.header, type="pose", frame_id=4,
                                             t_capture=4, tracking="limited"))
                        deadline = time.monotonic() + .3
                        while camera.status()["camera_drive_ready"] and time.monotonic() < deadline:
                            time.sleep(.005)
                        self.assertFalse(camera.status()["camera_drive_ready"])
                    release.set()
                    phone.close()
                    deadline = time.monotonic() + .3
                    while app.state.phone_owner is not None and time.monotonic() < deadline:
                        time.sleep(.005)
                deadline = time.monotonic() + .3
                while camera.status()["camera_drive_ready"] and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertFalse(camera.status()["camera_drive_ready"])
        finally:
            release.set()
