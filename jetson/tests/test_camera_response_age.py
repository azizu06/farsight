"""HTTP camera wait is measured separately without refreshing capture receipt."""

import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from jetson.camera import CameraFrame
from jetson.mapping.server import create_app


class CameraResponseAgeTests(unittest.IsolatedAsyncioTestCase):
    async def response_after_preview_wait(self, *, fresh):
        app = create_app(detector="none")
        camera = app.state.camera
        route = next(route for route in app.routes if getattr(route, "path", None) == "/camera.jpg")
        okay, encoded = cv2.imencode(".jpg", np.full((24, 32, 3), 90, np.uint8))
        self.assertTrue(okay)
        clock = SimpleNamespace(now=10.)
        time_source = SimpleNamespace(monotonic=lambda: clock.now)
        entered, finish = asyncio.Event(), threading.Event()
        loop, preview = asyncio.get_running_loop(), camera.preview

        def queued_preview():
            loop.call_soon_threadsafe(entered.set)
            if not finish.wait(2):
                raise RuntimeError("preview fixture did not release")
            return preview()  # actual preview snapshots after the queue wait

        with patch("jetson.mapping.server.time", time_source), patch("jetson.camera.time", time_source), \
                patch.object(camera, "preview", queued_preview):
            request = asyncio.create_task(route.endpoint())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                clock.now = 10.5009
                receipt = 10.48 if fresh else 9.3
                original = CameraFrame(encoded.tobytes(), receipt, 7, "PHONE RGB")
                camera.phone_rgb = original
                finish.set()
                response = await request
            finally:
                finish.set()
                await request
        self.assertIs(camera.snapshot(), original)
        self.assertEqual(camera.snapshot().received, receipt)
        self.assertEqual(response.headers["X-Frame-Id"], "7")
        self.assertEqual(response.headers["X-Frame-Source"], "PHONE RGB")
        self.assertEqual(response.headers["X-Frame-Server-Wait-Ms"], "500", "wait rounds down, never subtracting extra residence")
        return response

    async def test_fresh_capture_published_during_wait_keeps_its_actual_receipt(self):
        response = await self.response_after_preview_wait(fresh=True)
        self.assertEqual(response.headers["X-Frame-Age-Ms"], "21")

    async def test_wait_does_not_refresh_an_old_capture(self):
        response = await self.response_after_preview_wait(fresh=False)
        self.assertEqual(response.headers["X-Frame-Age-Ms"], "1201")
