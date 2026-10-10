"""The demo producer must bound fusion work and drain it during shutdown."""

import asyncio
import threading
import unittest

from jetson.camera import CameraService
from jetson.mapping.demo import stream_demo
from jetson.mapping.state import MapState


class DemoStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_fusion_drops_candidates_and_shutdown_drains_worker(self):
        class GatedMap(MapState):
            def __init__(self):
                super().__init__(demo=True)
                self.entered = threading.Event()
                self.resume = threading.Event()
                self.inputs = []

            def process(self, frame):
                self.inputs.append(frame.header["frame_id"])
                self.entered.set()
                if not self.resume.wait(5):
                    raise TimeoutError("Test did not release fusion")
                super().process(frame)

        state = GatedMap()
        camera = CameraService(state)
        stream = asyncio.create_task(stream_demo(state, camera))
        try:
            self.assertTrue(await asyncio.to_thread(state.entered.wait, 2))
            deadline = asyncio.get_running_loop().time() + 2
            while camera.snapshot().frame_id < 3 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.01)
            self.assertGreaterEqual(camera.snapshot().frame_id, 3)
            self.assertEqual(state.inputs, [0])  # no queued/executing stale jobs
            self.assertLess(camera.snapshot().age_ms, 500)
            stream.cancel()
            await asyncio.sleep(.03)
            self.assertFalse(stream.done())  # cancellation must drain the thread
        finally:
            state.resume.set()
            if not stream.cancelling():
                stream.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(stream, 2)
        self.assertEqual(state.frames, 1)
        self.assertEqual(state.inputs, [0])
