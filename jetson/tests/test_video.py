import asyncio
import shutil
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from jetson.camera import CameraFrame, CameraService, adjusted_receipt
from jetson.mapping.demo import simulated_frame
from jetson.mapping.state import MapState
from jetson.video import PROFILES, VideoService, choose_encoder, codec_from_init, ffmpeg_encoders, fit_frame, video_settings


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


class ProfileTests(unittest.TestCase):
    def test_low_is_todays_encoder_and_hq_is_opt_in(self):
        low, hq = video_settings("low"), video_settings("hq")
        self.assertEqual((low["width"], low["height"], low["fps"], low["bitrate"]), (640, 480, 10, 500000))
        self.assertEqual((low["preset"], low["profile"], low["level"]), ("ultrafast", "baseline", "3.0"))
        self.assertEqual((hq["width"], hq["height"], hq["fps"], hq["bitrate"]), (1280, 720, 15, 1500000))
        self.assertEqual((hq["preset"], hq["profile"], hq["level"]), ("veryfast", "main", "3.1"))
        self.assertEqual(PROFILES["low"]["encoder"], PROFILES["hq"]["encoder"])

    def test_explicit_size_and_rate_override_the_profile(self):
        settings = video_settings("hq", width=320, fps=8)
        self.assertEqual((settings["width"], settings["height"], settings["fps"], settings["bitrate"]),
                         (320, 720, 8, 1500000))
        service = VideoService(None, profile="low", bitrate=250000)
        self.assertEqual((service.width, service.height, service.fps, service.bitrate, service.profile),
                         (640, 480, 10, 250000, "low"))

    def test_omitted_size_keeps_the_low_profile(self):
        service = VideoService(None)
        self.assertEqual((service.profile, service.width, service.height, service.fps, service.bitrate,
                          service.preset, service.h264_profile, service.level, service.encoder),
                         ("low", 640, 480, 10, 500000, "ultrafast", "baseline", "3.0", "libx264"))

    def test_unknown_profile_and_out_of_range_override_rejected(self):
        with self.assertRaises(ValueError):
            video_settings("cinema")
        with self.assertRaises(ValueError):
            VideoService(None, profile="hq", fps=60)
        with self.assertRaises(ValueError):
            VideoService(None, width=641)

    def test_encoder_list_parse_ignores_a_missing_ffmpeg(self):
        class Result:
            returncode, stdout = 0, " V..... libx264   H.264\n A..... aac      AAC\n"
        self.assertEqual(ffmpeg_encoders(runner=lambda *args, **kwargs: Result()), ("libx264",))
        self.assertEqual(ffmpeg_encoders(runner=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing"))), ())

    def test_hardware_encoder_only_when_requested_and_listed(self):
        listed = ("libx264", "h264_nvmpi", "h264_nvv4l2enc")
        self.assertEqual(choose_encoder("libx264", listed), "libx264")
        self.assertEqual(choose_encoder("nvenc", listed), "h264_nvmpi")
        self.assertEqual(choose_encoder("nvenc", ("h264_nvv4l2enc",)), "h264_nvv4l2enc")
        self.assertEqual(choose_encoder("nvenc", ("libx264",)), "libx264")
        self.assertEqual(choose_encoder("h264_nvv4l2enc", listed), "h264_nvv4l2enc")
        self.assertEqual(choose_encoder("missing", listed), "libx264")

    def test_fit_fills_the_frame_so_boxes_stay_on_the_picture(self):
        import numpy as np
        # A block at one quarter of the camera picture must land at one
        # quarter of the encoded frame. A single pixel smears under resize,
        # so the check reads the middle of a block wide enough to survive it.
        # Padding would slide that block inward and leave the left edge black.
        source = np.zeros((120, 160, 3), np.uint8)
        source[:, :8] = 200
        source[:, 40:48] = 150
        stretched = fit_frame(source, 640, 480)
        self.assertEqual(stretched.shape, (480, 640, 3))
        self.assertEqual(int(stretched[:, 0:16].min()), 200)
        self.assertEqual(int(stretched[:, 168:184].min()), 150)
        phone = np.zeros((480, 640, 3), np.uint8)
        phone[:, :8] = 180
        phone[:, 160:176] = 90
        hq = fit_frame(phone, 1280, 720)
        self.assertEqual(hq.shape, (720, 1280, 3))
        self.assertEqual(int(hq[:, 0:8].min()), 180)
        self.assertEqual(int(hq[:, 336:344].min()), 90)
        same = fit_frame(phone, 640, 480)
        self.assertIs(same, phone)

    def test_encoder_name_stays_software_until_probed(self):
        service = VideoService(None, encoder="nvenc")
        self.assertEqual(service.encoder, "libx264")
        self.assertEqual(service.requested_encoder, "nvenc")

    def test_hq_encoder_args_keep_zerolatency_and_the_short_buffer(self):
        service = VideoService(None, profile="hq")
        args = service.encoder_args("libx264")
        self.assertEqual(args[args.index("-preset") + 1], "veryfast")
        self.assertIn("-tune", args)
        self.assertEqual(args[args.index("-tune") + 1], "zerolatency")
        self.assertEqual(args[args.index("-profile:v") + 1], "main")
        self.assertEqual(args[args.index("-level") + 1], "3.1")
        self.assertEqual(args[args.index("-bufsize") + 1], str(1500000 // 5))
        self.assertEqual(args[args.index("-g") + 1], "15")
        hardware = service.encoder_args("h264_nvmpi")
        self.assertEqual(hardware[:2], ["-c:v", "h264_nvmpi"])
        self.assertNotIn("-preset", hardware)
        self.assertEqual(hardware[hardware.index("-bufsize") + 1], str(1500000 // 5))


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
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required for the fallback check")
    async def test_listed_hardware_encoder_that_cannot_open_falls_back(self):
        jpeg = simulated_frame(0, 0).jpeg
        class Camera:
            def snapshot(self):
                return CameraFrame(jpeg, time.monotonic(), 0, "SIMULATED")
        service = VideoService(Camera(), enabled=True, width=160, height=120, fps=5, bitrate=100000,
                               encoder="h264_nvmpi")
        # Listed, but this ffmpeg cannot open it. The supervisor must switch to
        # libx264 instead of restarting the dead encoder forever.
        service._encoders_known = ("libx264", "h264_nvmpi")
        await service.start()
        try:
            for _ in range(400):
                if service.ready and service.encoder == "libx264":
                    break
                await asyncio.sleep(.025)
            self.assertTrue(service._encoder_failed)
            self.assertEqual(service.encoder, "libx264")
            self.assertTrue(service.ready)
            self.assertIsNone(service.error)
        finally:
            await service.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required for the reopen fallback check")
    async def test_failed_hardware_open_on_restart_falls_back_once(self):
        jpeg = simulated_frame(0, 0).jpeg
        class Camera:
            def snapshot(self):
                return CameraFrame(jpeg, time.monotonic(), 0, "SIMULATED")
        service = VideoService(Camera(), enabled=True, width=160, height=120, fps=5, bitrate=100000,
                               encoder="libx264")
        starts = []
        real_args = service.encoder_args

        def refuse_after_open(encoder):
            # First start is real libx264 and produces an init. After it dies
            # the supervisor clears "opened", and the next start asks for a
            # listed chip that ffmpeg cannot open. That must fall back once,
            # not get retried on every later restart.
            starts.append(encoder)
            if encoder == "libx264":
                return real_args("libx264")
            return ["-c:v", "h264_nvmpi", "-b:v", "100000"]

        service.encoder_args = refuse_after_open
        service._encoders_known = ("libx264", "h264_nvmpi")
        await service.start()
        try:
            for _ in range(400):
                if service.ready and service._encoder_opened:
                    break
                await asyncio.sleep(.025)
            self.assertTrue(service._encoder_opened, "the first open never produced an init")
            self.assertFalse(service._encoder_failed)
            service.requested_encoder = "h264_nvmpi"
            service.process.kill()
            await service.process.wait()
            for _ in range(400):
                if service.ready and service.encoder == "libx264" and service._encoder_failed:
                    break
                await asyncio.sleep(.025)
            self.assertGreaterEqual(starts.count("h264_nvmpi"), 1)
            self.assertTrue(service._encoder_failed)
            self.assertEqual(service.encoder, "libx264")
            self.assertTrue(service.ready)
            self.assertIsNone(service.error)
            asked = starts.count("h264_nvmpi")
            await asyncio.sleep(.6)
            self.assertEqual(starts.count("h264_nvmpi"), asked)
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
