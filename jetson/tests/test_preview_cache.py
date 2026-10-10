"""Preview work is shared without refreshing the capture evidence."""

from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from jetson.camera import CameraFrame, CameraService


class PreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.camera = CameraService(None)
        okay, jpeg = cv2.imencode(".jpg", np.full((48, 80, 3), 90, np.uint8))
        assert okay
        self.camera.phone_rgb = CameraFrame(jpeg.tobytes(), time.monotonic() - .2, 7, "PHONE RGB")

    def test_concurrent_requests_encode_once_and_keep_capture_age(self):
        original = self.camera.snapshot()
        with patch("jetson.camera.cv2.imencode", wraps=cv2.imencode) as encode:
            with ThreadPoolExecutor(max_workers=8) as workers:
                frames = list(workers.map(lambda _: self.camera.preview(), range(16)))
            self.assertEqual(encode.call_count, 1)
        self.assertTrue(all(f.jpeg == frames[0].jpeg for f in frames))
        self.assertTrue(all(f.received == original.received for f in frames))
        self.assertGreaterEqual(frames[-1].age_ms, 200)

    def test_profile_and_restarted_capture_invalidate_cached_bytes(self):
        with patch("jetson.camera.cv2.imencode", wraps=cv2.imencode) as encode:
            self.camera.preview(max_width=40, quality=30)
            self.camera.preview(max_width=40, quality=30)
            self.camera.preview(max_width=80, quality=50)
            old = self.camera.snapshot()
            # Phone counters and source names may repeat after reconnect.
            self.camera.phone_rgb = CameraFrame(old.jpeg, old.received + .1, old.frame_id, old.source)
            new = self.camera.preview(max_width=80, quality=50)
            self.assertEqual(encode.call_count, 3)
        self.assertEqual(new.received, old.received + .1)

    def test_cached_pixels_never_reuse_old_permit_binding(self):
        old = self.camera.snapshot()
        binding = object()
        self.camera.phone_rgb = CameraFrame(old.jpeg, old.received, old.frame_id, old.source, True, binding)
        self.camera.preview()
        self.camera.phone_rgb = old
        fresh = self.camera.preview()
        self.assertFalse(fresh.capture_verified)
        self.assertIsNone(fresh.capture_binding)

    def test_disconnect_never_serves_the_cached_frame(self):
        self.camera.preview()
        self.camera.phone_rgb = None
        self.camera.mapping = type("Empty", (), {"camera_snapshot": lambda _: (None, 0, 0, "WAITING")})()
        self.assertIsNone(self.camera.preview())


if __name__ == "__main__":
    unittest.main()
