"""Receiver binding publication; synthetic frames, no device clock qualification."""

import unittest
from unittest.mock import patch

from jetson.camera import CameraService
from jetson.capture_permits import CapturePermits
from jetson.mapping.state import MapState
from jetson.mapping.tests.test_mapping import flat_frame


class RGBDBindingTests(unittest.TestCase):
    def fixture(self, supported_clocks=("avcapture_sync_to_host",)):
        self.now = 10.2
        permits = CapturePermits("test", 1, True, clock=lambda: self.now,
                                 supported_clocks=supported_clocks)
        self.now = 10
        token = permits.issue()["token"]
        self.now = 10.2
        mapping = MapState()
        self.addCleanup(mapping.dense_worker.close)
        mapping.start_session(("test", 1), "synthetic fixture")
        frame = flat_frame()
        frame.header.update(t_capture=100.1, capture_permit_received_t=100,
            capture_permit=token, capture_clock="avcapture_sync_to_host",
            capture_clock_verified=True, capture_bound=True, _received_monotonic=10.2)
        return permits, mapping, frame, CameraService(mapping)

    def test_bound_origin_and_live_issuer_travel_with_same_immutable_frame(self):
        permits, mapping, frame, camera = self.fixture()
        with patch("jetson.camera.time.monotonic", side_effect=lambda: self.now):
            mapping.process(frame, permits=permits)
            snapshot = camera.snapshot()
            self.assertEqual(snapshot.received, 10)
            self.assertTrue(snapshot.capture_verified)
            self.assertIs(snapshot.capture_binding, permits)
            self.assertEqual(mapping.camera_snapshot(),
                             (snapshot.jpeg, snapshot.received, snapshot.frame_id, snapshot.source))
            self.assertTrue(camera.status()["camera_drive_ready"])
            permits.close()
            self.assertTrue(camera.status()["camera"])
            self.assertFalse(camera.status()["camera_drive_ready"])

    def test_public_rgbd_policy_cannot_claim_the_rgb_only_clock(self):
        permits, mapping, frame, camera = self.fixture(supported_clocks=())
        with patch("jetson.camera.time.monotonic", return_value=10.2):
            mapping.process(frame, permits=permits)
            self.assertTrue(camera.status()["camera"])
            self.assertFalse(camera.status()["camera_capture_verified"])
            self.assertFalse(camera.status()["camera_drive_ready"])

    def test_expiry_during_decode_or_unsupported_clock_never_binds(self):
        for failure in ("expiry", "arkit", "invalid_clock"):
            permits, mapping, frame, camera = self.fixture()
            if failure == "expiry":
                self.now = 10.3
            else:
                frame.header["capture_clock"] = "arkit_unverified" if failure == "arkit" else []
            with patch("jetson.camera.time.monotonic", side_effect=lambda: self.now):
                mapping.process(frame, permits=permits)
                self.assertTrue(camera.status()["camera"], failure)
                self.assertFalse(camera.status()["camera_capture_verified"], failure)

    def test_receipt_delay_is_conservative_and_session_reset_revokes_snapshot(self):
        permits, mapping, frame, camera = self.fixture()
        frame.header["producer_delay_ms"] = 400
        with patch("jetson.camera.time.monotonic", return_value=10.2):
            mapping.process(frame, permits=permits)
            self.assertAlmostEqual(camera.snapshot().received, 9.8)
            mapping.start_session(("new", 1), "new synthetic fixture")
            self.assertIsNone(camera.snapshot())
            self.assertFalse(camera.status()["camera_drive_ready"])

    def test_raw_verified_fields_without_receiver_issuer_are_not_authority(self):
        _, mapping, frame, camera = self.fixture()
        frame.header.update(_capture_verified=True, capture_binding="forged")
        with patch("jetson.camera.time.monotonic", return_value=10.2):
            mapping.process(frame)
            self.assertFalse(camera.snapshot().capture_verified)
            self.assertIsNone(camera.snapshot().capture_binding)


if __name__ == "__main__":
    unittest.main()
