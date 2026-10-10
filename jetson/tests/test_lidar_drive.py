"""Opt-in --lidar-drive: fresh /phone LiDAR frames with normal tracking may drive."""

import sys
import unittest
from unittest.mock import patch

from jetson.camera import CameraService
from jetson.control.state import ControlState
from jetson.mapping import server
from jetson.mapping.state import MapState
from jetson.mapping.tests.test_mapping import flat_frame


class LidarDriveTests(unittest.TestCase):
    def fixture(self, lidar_drive=True, tracking="normal"):
        mapping = MapState()
        mapping.start_session(("test", 1), "synthetic fixture")
        frame = flat_frame()
        frame.header.update(t_capture=100.0, tracking=tracking, _received_monotonic=10.0)
        with patch("jetson.camera.time.monotonic", return_value=10.0):
            mapping.process(frame)
        return mapping, CameraService(mapping, lidar_drive=lidar_drive)

    def status(self, camera, now=10.1):
        with patch("jetson.camera.time.monotonic", return_value=now):
            return camera.status()

    def test_default_off_keeps_lidar_view_only(self):
        _, camera = self.fixture(lidar_drive=False)
        status = self.status(camera)
        self.assertTrue(status["camera"])
        self.assertEqual(status["camera_source"], "IPHONE LIDAR")
        self.assertFalse(status["camera_drive_ready"])

    def test_fresh_normal_tracking_frame_is_drive_ready_without_claiming_verification(self):
        _, camera = self.fixture()
        status = self.status(camera)
        self.assertTrue(status["camera_drive_ready"])
        self.assertFalse(status["camera_capture_verified"])

    def test_stale_frame_is_never_drive_ready(self):
        _, camera = self.fixture()
        self.assertFalse(self.status(camera, now=10.5)["camera_drive_ready"])

    def test_tracking_loss_disconnect_and_new_session_revoke(self):
        for tracking in ("limited", "not_available"):
            _, camera = self.fixture(tracking=tracking)
            self.assertFalse(self.status(camera)["camera_drive_ready"], tracking)
        mapping, camera = self.fixture()
        mapping.connected = False
        self.assertFalse(self.status(camera)["camera_drive_ready"])
        mapping, camera = self.fixture()
        mapping.start_session(("new", 1), "new synthetic fixture")
        self.assertFalse(self.status(camera)["camera_drive_ready"])

    def test_lidar_drives_under_a_lease_then_camera_stale_stops_and_needs_release(self):
        _, camera = self.fixture()
        now = [10.1]
        state = ControlState(camera_ready=lambda: self.status(camera, now=now[0])["camera_drive_ready"],
                             proximity_stop=False)
        state.set_connected(True)
        session = state.begin()
        seq = [0]

        def send(**values):
            seq[0] += 1
            message = {"session_id": session, "permit": state.status(issue_permit=True)["permit"],
                       "seq": seq[0], "deadman": True, "v": 0.5, "w": 0, "pan": 0, "mode": "teleop",
                       "video_fresh": True, "video_age_ms": 0}
            message.update(values)
            return state.accept(message, session)

        send(deadman=False)
        self.assertTrue(send())
        self.assertEqual(state.output()[:2], (50, 50))
        now[0] = 10.6  # frame older than 500 ms
        self.assertEqual(state.output()[:2], (0, 0))
        self.assertEqual(state.reason, "camera_stale")
        self.assertTrue(state.requires_release)

    def test_flag_reaches_the_camera_service(self):
        with patch.object(sys, "argv", ["jetson.server", "--lidar-drive"]), \
                patch.object(server, "create_app") as create_app, patch.object(server.uvicorn, "run"):
            server.main()
        self.assertTrue(create_app.call_args.kwargs["lidar_drive"])
        with patch.object(sys, "argv", ["jetson.server"]), \
                patch.object(server, "create_app") as create_app, patch.object(server.uvicorn, "run"):
            server.main()
        self.assertFalse(create_app.call_args.kwargs["lidar_drive"])


if __name__ == "__main__":
    unittest.main()
