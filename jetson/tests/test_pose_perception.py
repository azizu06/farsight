import asyncio
import json
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from jetson.perception import PerceptionService
from jetson.camera import CameraFrame
from jetson.pose import Detection, make_pose_detector
from jetson.pose.boxes import detection_to_box
from jetson.pose.trt_backend import TensorRTPoseDetector
from jetson.tensorrt_detector import TensorRTError

MODEL = Path(os.environ.get("FARSIGHT_POSE_MODEL", Path(__file__).parents[2] / "models" / "yolo11n-pose.onnx"))


class FakePose:
    name = "fake-pose"

    def detect(self, frame):
        k = np.zeros((17, 3), np.float32)
        k[:, 0], k[:, 1], k[:, 2] = 320, 240, 0.9
        return [Detection(box=(64, 48, 128, 240), conf=0.91, kpts=k, down=True),
                Detection(box=(0, 0, 64, 48), conf=0.5, kpts=None, down=False)]


def jpeg(width=640, height=480):
    ok, buf = cv2.imencode(".jpg", np.full((height, width, 3), 90, np.uint8))
    assert ok
    return buf.tobytes()


class PoseBoxTests(unittest.TestCase):
    def test_detection_to_box_normalizes_and_adds_pose_fields(self):
        k = np.zeros((17, 3), np.float32)
        k[:, 0], k[:, 1], k[:, 2] = 320, 240, 0.9
        box = detection_to_box(Detection((64, 48, 128, 240), 0.91, k, True), 640, 480)
        self.assertEqual((box["x"], box["y"], box["w"], box["h"]), (0.1, 0.1, 0.2, 0.5))
        self.assertEqual((box["cls"], box["class_id"], box["conf"], box["down"]), ("person", 0, 0.91, True))
        self.assertEqual(len(box["kpts"]), 17)
        self.assertEqual(box["kpts"][0], [0.5, 0.5, 0.9])

    def test_box_without_keypoints_omits_kpts(self):
        box = detection_to_box(Detection((0, 0, 64, 48), 0.5), 640, 480)
        self.assertNotIn("kpts", box)
        self.assertIs(box["down"], False)


class PosePerceptionTests(unittest.TestCase):
    def service(self):
        service = PerceptionService(None, mode="pose")
        service.pose = FakePose()
        return service

    def test_pose_mode_boxes_carry_kpts_down_and_stable_track_ids(self):
        service = self.service()
        first = service.track(service._detect(jpeg()), 1.0)
        second = service.track(service._detect(jpeg()), 1.1)
        self.assertEqual([b["id"] for b in first], [b["id"] for b in second])
        self.assertTrue(first[0]["down"])
        self.assertEqual(len(first[0]["kpts"]), 17)
        self.assertEqual(first[0]["cls"], "person")

    def test_pose_mode_requires_a_model(self):
        service = PerceptionService(None, mode="pose")
        with self.assertRaisesRegex(ValueError, "pose"):
            service._setup()

    def test_tensorrt_pose_requires_an_actual_engine(self):
        with self.assertRaisesRegex(TensorRTError, "Engine missing"):
            TensorRTPoseDetector("models/yolo11n-pose.engine")
        service = PerceptionService(None, model="models/yolo11n-pose.engine", mode="pose")
        with self.assertRaisesRegex(TensorRTError, "Engine missing"):
            service._setup()

    def test_make_pose_detector_rejects_missing_model(self):
        with self.assertRaises(FileNotFoundError):
            make_pose_detector("/nonexistent/yolo11n-pose.onnx")

    @unittest.skipUnless(MODEL.exists(), "models/yolo11n-pose.onnx not present")
    def test_real_onnx_model_runs_through_perception(self):
        service = PerceptionService(None, model=str(MODEL), mode="pose")
        service._setup()
        boxes = service._detect(jpeg())
        self.assertIsInstance(boxes, list)
        self.assertEqual(service.mode, "pose")
        self.assertEqual(service.fps, 10)


class DetectorRateTests(unittest.TestCase):
    def test_rate_is_windowed_not_the_last_gap(self):
        service = PerceptionService(None, mode="none")
        self.assertEqual(service.windowed_rate(10.0), 0)
        times = [10.0 + 0.16 * i for i in range(1, 13)]
        times[5] = times[4] + 0.01  # two frames back to back: the last-gap rate read 100 fps
        rates = [service.windowed_rate(t) for t in times]
        self.assertTrue(all(4 < rate < 8 for rate in rates[6:]), rates)
        self.assertLessEqual(times[-1] - service.completions[0], service.FPS_WINDOW)


class FlakyGpu:
    """Stands in for a TensorRT pose detector whose CUDA context dies on the first call."""
    name = "pose-tensorrt"

    def __init__(self, healthy):
        self.healthy, self.closed = healthy, False

    def detect(self, frame):
        if not self.healthy:
            raise TensorRTError("cudaStreamSynchronize: CUDA error 700 (an illegal memory access)")
        return FakePose().detect(frame)

    def close(self):
        self.closed = True


class GpuRecoveryTests(unittest.TestCase):
    def service(self):
        service = PerceptionService(None, mode="pose")
        service.pose = FlakyGpu(healthy=False)
        service._rebuild = lambda: FlakyGpu(healthy=True)
        return service

    def test_cuda_fault_recreates_the_engine_and_detection_resumes(self):
        service = self.service()
        broken = service.pose
        with self.assertRaises(TensorRTError) as caught:
            service._detect(jpeg())
        self.assertTrue(service._recover_gpu(caught.exception))
        self.assertTrue(broken.closed)
        self.assertEqual(service.rebuilds, 1)
        self.assertEqual(len(service._detect(jpeg())), 2)

    def test_rebuilds_are_rate_limited_and_failures_stay_visible(self):
        service = self.service()
        fault = TensorRTError("cuda")
        self.assertTrue(service._recover_gpu(fault))
        self.assertFalse(service._recover_gpu(fault))  # inside REBUILD_INTERVAL
        service.REBUILD_INTERVAL = 0
        def fail():
            raise TensorRTError("engine deserialization failed")
        service._rebuild = fail
        self.assertFalse(service._recover_gpu(fault))
        self.assertIn("rebuild failed", service.error)

    def test_non_gpu_errors_never_rebuild(self):
        service = self.service()
        self.assertFalse(service._recover_gpu(ValueError("bad frame")))
        self.assertEqual(service.rebuilds, 0)


class DetectionAssociationTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_inference_retains_its_input_capture_and_orientation(self):
        key = ('phone', 1, 7, 1, 1.)
        image = jpeg()
        first = CameraFrame(image, time.monotonic(), 1, 'IPHONE LIDAR', mirrored=True,
                            native_jpeg=image, sensor_key=key)
        second = CameraFrame(image, time.monotonic(), 2, 'IPHONE LIDAR', mirrored=False,
                             native_jpeg=image, sensor_key=('phone', 1, 7, 2, 2.))
        camera = SimpleNamespace(current=first)
        camera.snapshot = lambda: camera.current
        service = PerceptionService(camera, mode='none')
        entered, release = threading.Event(), threading.Event()

        def detect(_):
            entered.set()
            release.wait(2)
            return []

        service._detect = detect
        service.task = asyncio.create_task(service._run())
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            camera.current = second
            release.set()
            async with asyncio.timeout(1):
                while service.packet['frame'] != 1:
                    await asyncio.sleep(.001)
            packet = json.loads(json.dumps(service.snapshot()))
            self.assertEqual(packet.get('sensor_key'), list(key))
            self.assertIs(packet.get('mirrored'), True)
            self.assertEqual(packet['source'], 'IPHONE LIDAR')
        finally:
            release.set()
            await service.close()


class GpuStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        frame = SimpleNamespace(source="FIXTURE", frame_id=1, received=time.monotonic(),
                                age_ms=0, jpeg=jpeg())
        camera = SimpleNamespace(snapshot=lambda: frame)
        service = PerceptionService(camera, model="fixture.engine", mode="pose")
        service.REBUILD_INTERVAL = .05
        return service

    async def wait(self, condition):
        try:
            async with asyncio.timeout(.5):
                while not condition():
                    await asyncio.sleep(.005)
        except TimeoutError:
            self.fail("detector startup did not recover within the fixture deadline")

    async def test_initial_cuda_allocation_failure_recovers_without_server_restart(self):
        service = self.service()
        detector = FlakyGpu(healthy=True)
        with patch("jetson.pose.make_pose_detector", side_effect=[TensorRTError("cudaMalloc: out of memory"), detector]):
            try:
                await service.start()
                self.assertIn("out of memory", service.health()["detector_error"])
                self.assertEqual(service.health()["detector_fps"], 0)
                await self.wait(lambda: bool(service.snapshot()["boxes"]))
                self.assertEqual(len(service.snapshot()["boxes"]), 2)
                self.assertEqual(service.health()["detector_backend"], "pose-tensorrt")
                self.assertIsNone(service.health()["detector_error"])
            finally:
                await service.close()
        self.assertTrue(detector.closed)

    async def test_persistent_startup_failure_is_paced_and_close_cancels_retry(self):
        service = self.service()
        attempts = []
        def factory(*args):
            attempts.append(time.monotonic())
            raise TensorRTError("cudaMalloc: out of memory")
        with patch("jetson.pose.make_pose_detector", side_effect=factory):
            try:
                await service.start()
                await self.wait(lambda: len(attempts) >= 2)
                self.assertGreaterEqual(attempts[1] - attempts[0], .04, "startup recovery busy-spun")
                self.assertIn("out of memory", service.health()["detector_error"])
                self.assertEqual(service.health()["detector_fps"], 0)
                self.assertIsNone(service.pose, "must not silently switch to a CPU detector")
            finally:
                await service.close()
            total = len(attempts)
            await asyncio.sleep(.075)
            self.assertEqual(len(attempts), total, "closed service retried setup")

    async def test_close_drains_inflight_setup_and_closes_the_late_detector(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()
        detector = FlakyGpu(healthy=True)
        attempts = 0
        def factory(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TensorRTError("cudaMalloc: out of memory")
            entered.set()
            release.wait(1)
            return detector
        with patch("jetson.pose.make_pose_detector", side_effect=factory):
            closing = None
            try:
                await service.start()
                await self.wait(entered.is_set)
                closing = asyncio.create_task(service.close())
                await asyncio.sleep(.01)
                self.assertFalse(closing.done(), "close abandoned the allocating worker")
                release.set()
                await asyncio.wait_for(closing, .5)
                self.assertTrue(detector.closed)
                self.assertFalse(service.snapshot()["fresh"], "closed service published detections")
            finally:
                release.set()
                if closing is not None:
                    await closing
                else:
                    await service.close()

    async def test_invalid_model_error_stays_visible_without_automatic_retry(self):
        service = self.service()
        with patch("jetson.pose.make_pose_detector", side_effect=ValueError("wrong model output shape")) as factory:
            try:
                await service.start()
                await asyncio.sleep(.075)
                self.assertEqual(factory.call_count, 1)
                self.assertIn("wrong model output shape", service.health()["detector_error"])
                self.assertEqual(service.health()["detector_fps"], 0)
                self.assertIsNone(service.task)
            finally:
                await service.close()

    async def test_close_drains_runtime_rebuild_after_startup_recovery(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()
        broken, fresh = FlakyGpu(healthy=False), FlakyGpu(healthy=True)
        attempts = 0

        def factory(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TensorRTError("cudaMalloc: out of memory")
            if attempts == 2:
                return broken
            entered.set()
            release.wait(1)
            return fresh

        with patch("jetson.pose.make_pose_detector", side_effect=factory):
            closing = None
            try:
                await service.start()
                await self.wait(entered.is_set)
                closing = asyncio.create_task(service.close())
                await asyncio.sleep(.01)
                self.assertFalse(closing.done(), "close abandoned the runtime allocating worker")
                release.set()
                await asyncio.wait_for(closing, .5)
                self.assertTrue(broken.closed)
                self.assertTrue(fresh.closed, "late runtime replacement remained open after close")
                self.assertFalse(service.snapshot()["fresh"], "closed service published detections")
            finally:
                release.set()
                if closing is not None:
                    await closing
                else:
                    await service.close()

    async def test_close_drains_active_inference_before_releasing_detector(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()

        class PausedGpu(FlakyGpu):
            used_after_close = False

            def detect(self, image):
                entered.set()
                release.wait(1)
                self.used_after_close = self.closed
                return FakePose().detect(image)

        detector = PausedGpu(healthy=True)
        with patch("jetson.pose.make_pose_detector", return_value=detector):
            closing = None
            try:
                await service.start()
                await self.wait(entered.is_set)
                closing = asyncio.create_task(service.close())
                await asyncio.sleep(.01)
                self.assertFalse(closing.done(), "close abandoned active detector inference")
                self.assertFalse(detector.closed, "detector closed while inference still used it")
                release.set()
                await asyncio.wait_for(closing, .5)
                self.assertTrue(detector.closed)
                self.assertFalse(detector.used_after_close)
                self.assertFalse(service.snapshot()["fresh"], "cancelled inference published detections")
            finally:
                release.set()
                if closing is not None:
                    await closing
                else:
                    await service.close()


class FaceGatingTests(unittest.TestCase):
    def test_face_pass_runs_only_while_wanted(self):
        class Faces:
            calls = 0
            def detect(self, image):
                Faces.calls += 1
                return [dict(cls="face", class_id=1, conf=.9, x=.1, y=.1, w=.1, h=.1)]
        service = PerceptionService(None, mode="pose")
        service.pose, service.face = FakePose(), Faces()
        wanted = [False]
        service.face_wanted = lambda: wanted[0]
        self.assertFalse(any(b["cls"] == "face" for b in service._detect(jpeg())))
        self.assertEqual(Faces.calls, 0)
        wanted[0] = True
        self.assertTrue(any(b["cls"] == "face" for b in service._detect(jpeg())))
        self.assertEqual(Faces.calls, 1)
