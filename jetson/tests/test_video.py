import asyncio
import shutil
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from jetson.camera import CameraFrame, CameraService, adjusted_receipt
from jetson.mapping.demo import simulated_frame
from jetson.mapping.state import MapState
from jetson.video import VideoService, codec_from_init


class FreshnessTests(unittest.TestCase):
    def test_producer_encoding_and_receiver_queue_delay_do_not_renew_freshness(self):
        state = MapState(demo=True)
        frame = simulated_frame(0, 0)
        frame.header.update(producer_delay_ms=300, _received_monotonic=time.monotonic() - .3)
        state.process(frame)
        self.assertGreater(CameraService(state).snapshot().age_ms, 550)
        for delay in (-1, float("nan"), True, "10"):
            with self.assertRaises(ValueError):
                adjusted_receipt({"producer_delay_ms": delay})
    def test_pose_heartbeat_does_not_refresh_camera(self):
        state = MapState(demo=True)
        frame = simulated_frame(0, 0)
        frame.header["_received_monotonic"] = time.monotonic() - 1
        state.process(frame)
        camera = CameraService(state)
        state.observe_pose(dict(frame.header, frame_id=1, t_capture=1), frame.transform)
        self.assertGreater(camera.snapshot().age_ms, 900)
        self.assertFalse(camera.status()["camera"])

    def test_camera_readiness_does_not_wait_for_map_fusion_lock(self):
        state = MapState(demo=True)
        state.process(simulated_frame(0, 0))
        camera = CameraService(state)
        with ThreadPoolExecutor(max_workers=1) as workers:
            with state.lock:
                future = workers.submit(camera.snapshot)
                # Another thread must be able to read the camera while this
                # thread owns the map lock, not only after fusion releases it.
                frame = future.result(timeout=.2)
                self.assertEqual(frame.source, "SIMULATED")
                self.assertIsNotNone(frame.jpeg)

    def test_preview_preserves_source_frame_and_receipt(self):
        state = MapState(demo=True)
        state.process(simulated_frame(0, 7))
        camera = CameraService(state)
        original, preview = camera.snapshot(), camera.preview(max_width=80)
        self.assertEqual(preview.frame_id, original.frame_id)
        self.assertEqual(preview.received, original.received)
        self.assertEqual(preview.source, "SIMULATED")

    def test_codec_metadata_and_invalid_init(self):
        self.assertEqual(codec_from_init(b"avcC\x01\x42\xc0\x1e"), "avc1.42C01E")
        with self.assertRaises(ValueError):
            codec_from_init(b"invalid")


class VideoTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required for actual encoder crash recovery")
    async def test_actual_encoder_crash_restarts_and_clears_old_readiness(self):
        jpeg = simulated_frame(0, 0).jpeg
        class Camera:
            def snapshot(self):
                return CameraFrame(jpeg, time.monotonic(), 0, "SIMULATED")
        service = VideoService(Camera(), enabled=True, width=160, height=120, fps=5, bitrate=100000)
        await service.start()
        try:
            for _ in range(300):
                if service.ready:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(service.ready)
            previous = service.process.pid
            service.process.kill()
            await service.process.wait()
            self.assertFalse(service.ready)
            for _ in range(500):
                if service.ready and service.process.pid != previous:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(service.ready)
            self.assertNotEqual(service.process.pid, previous)
            self.assertIsNone(service.error)
        finally:
            await service.close()
    async def test_slow_consumer_is_reset_instead_of_corrupted(self):
        service = VideoService(None)
        queue = service.subscribe()
        for _ in range(queue.maxsize + 1):
            service.publish(b"fragment")
        self.assertIsNone(queue.get_nowait())
        self.assertNotIn(queue, service.subscribers)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required for real encoding check")
    async def test_real_ffmpeg_produces_decodable_mp4(self):
        jpeg = simulated_frame(0, 0).jpeg
        class Camera:
            def snapshot(self):
                return CameraFrame(jpeg, time.monotonic(), 0, "SIMULATED")
        service = VideoService(Camera(), enabled=True, width=160, height=120, fps=5, bitrate=100000)
        await service.start()
        try:
            for _ in range(300):
                if service.init is not None or service.error:
                    break
                await asyncio.sleep(.01)
            self.assertIsNone(service.error)
            self.assertIn(b"moov", service.init)
            queue = service.subscribe()
            fragments = [await asyncio.wait_for(queue.get(), 3) for _ in range(7)]
            self.assertTrue(all(b"moof" in fragment["payload"] and b"mdat" in fragment["payload"] for fragment in fragments))
            self.assertTrue(all(fragment["received"] <= time.monotonic() for fragment in fragments))
            self.assertTrue(all(fragment["media_end_s"] > fragment["media_start_s"] for fragment in fragments))
            decoder = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", "pipe:0",
                       "-f", "null", "-", stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, errors = await decoder.communicate(service.init + b"".join(fragment["payload"] for fragment in fragments))
            self.assertEqual(decoder.returncode, 0, errors.decode())
        finally:
            await service.close()


if __name__ == "__main__":
    unittest.main()
