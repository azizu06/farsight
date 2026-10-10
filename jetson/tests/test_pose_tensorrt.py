"""CPU CUDA/TensorRT fixtures exercise the executable pose path, not real GPU inference.

JetsonHardwareTests runs only on the Jetson, with FARSIGHT_POSE_ENGINE set.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

from jetson.perception import PerceptionService
from jetson.pose import make_pose_detector
from jetson.pose.decode import letterbox, to_tensor
from jetson.pose.trt_backend import TensorRTPoseDetector
from jetson.tensorrt_detector import INPUT_SHAPE, POSE_OUTPUT_SHAPE, TensorRTError
from jetson.tests.test_tensorrt_detector import FakeCuda, FakeEngine, fake_trt


class PoseTensorRTTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "pose.engine"
        self.path.write_bytes(b"CPU fixture only: not a TensorRT plan")
        self.cuda = FakeCuda()
        self.engine = FakeEngine(self.cuda)
        self.engine.output_shape = POSE_OUTPUT_SHAPE
        self.prediction = np.zeros(POSE_OUTPUT_SHAPE, np.float32)
        self.prediction[0, :5, 0] = [320, 320, 160, 80, .9]
        points = np.tile(np.array([320, 320, .8], np.float32), (17, 1))
        points[5:7, :2] = [280, 300]
        points[11:13, :2] = [360, 305]  # mostly horizontal torso axis
        self.prediction[0, 5:, 0] = points.ravel()

        def execute(stream_handle):
            self.cuda.events.append(("execute", stream_handle))
            values = self.prediction.astype(self.engine.dtype)
            self.cuda.buffers[self.engine.context.addresses["output0"]][:] = values.tobytes()
            return True

        self.engine.context.execute_async_v3 = execute
        self.trt = fake_trt(self.engine)

    def tearDown(self):
        self.directory.cleanup()

    def create(self):
        return TensorRTPoseDetector(str(self.path), cuda=self.cuda, trt_module=self.trt, warmup=1)

    def test_fp32_and_fp16_pose_i_o_letterbox_unprojection_and_cleanup(self):
        for dtype in (np.float32, np.float16):
            with self.subTest(dtype=dtype):
                self.engine.dtype = dtype
                frame = np.zeros((240, 320, 3), np.uint8)
                frame[:, :, 0] = 255  # blue BGR must become blue RGB input channel 2
                with self.create() as detector:
                    detections = detector.detect(frame)
                    self.assertEqual(detector.name, "pose-tensorrt")
                    self.assertEqual(detector.engine.output_shape, (1, 56, 8400))
                    self.assertEqual(detector.engine.score_channels, 1)
                    self.assertEqual(len(detections), 1)
                    person = detections[0]
                    np.testing.assert_allclose(person.box, [120, 100, 80, 40], atol=.1)
                    self.assertAlmostEqual(person.conf, .9, places=3)
                    self.assertEqual(person.kpts.shape, (17, 3))
                    np.testing.assert_allclose(person.kpts[0], [160, 120, .8], atol=.01)
                    self.assertTrue(person.down)
                    blob = np.frombuffer(self.cuda.buffers[detector.engine.device_buffers[0]],
                                         dtype=dtype).reshape(INPUT_SHAPE)
                    np.testing.assert_allclose(blob[0, :, 320, 320], [0, 0, 1], atol=.001)
                    np.testing.assert_allclose(blob[0, :, 0, 0], [114 / 255] * 3, atol=.001)
                    self.assertEqual(detector.engine.host_output.shape, POSE_OUTPUT_SHAPE)
                self.assertEqual(self.cuda.buffers, {})
                self.assertEqual(self.cuda.host, {})
                detector.close()  # idempotent
                with self.assertRaises(TensorRTError):
                    detector.detect(frame)

    def test_nms_uses_pose_person_score_and_rejects_low_confidence_anchor(self):
        self.prediction[0, :, 1] = self.prediction[0, :, 0]
        self.prediction[0, 4, 1] = .8  # duplicate overlapping candidate should be suppressed
        self.prediction[0, :, 2] = self.prediction[0, :, 0]
        self.prediction[0, 4, 2] = .1
        with self.create() as detector:
            self.assertEqual(len(detector.detect(np.zeros((480, 640, 3), np.uint8))), 1)

    def test_invalid_camera_input_never_enqueues_inference(self):
        with self.create() as detector:
            initial = len(self.cuda.events)
            for frame in (np.zeros((0, 5, 3), np.uint8), np.zeros((240, 320), np.uint8),
                          np.zeros((240, 320, 4), np.uint8), np.zeros((240, 320, 3), np.float32)):
                with self.assertRaises(ValueError):
                    detector.detect(frame)
            self.assertEqual(len(self.cuda.events), initial)

    def test_missing_tensorrt_bindings_fail_clearly_through_the_factory(self):
        with mock.patch("jetson.tensorrt_detector.importlib.import_module", side_effect=ImportError("no trt")):
            with self.assertRaisesRegex(TensorRTError, "bindings unavailable"):
                make_pose_detector(str(self.path))

    def test_perception_health_reports_tensorrt_backend_rate_and_latency(self):
        service = PerceptionService(None, model=str(self.path), mode="pose")
        detector = self.create()
        with mock.patch("jetson.pose.make_pose_detector", return_value=detector):
            service._setup()
        self.assertEqual(service.fps, 15)
        health = service.health()
        self.assertEqual((health["detector"], health["detector_backend"]), ("pose", "pose-tensorrt"))
        self.assertEqual((health["detector_fps"], health["detector_inference_ms"]), (0, None))
        service.packet = dict(service.packet, fps=14.8, inference_ms=21.5, received=time.monotonic())
        self.assertEqual((service.health()["detector_fps"], service.health()["detector_inference_ms"]), (14.8, 21.5))
        asyncio.run(service.close())
        self.assertTrue(detector.engine.closed)

    def test_missing_engine_surfaces_in_health(self):
        service = PerceptionService(None, model=str(self.path.with_name("absent.engine")), mode="pose")
        asyncio.run(service.start())
        self.assertIn("Engine missing", service.health()["detector_error"])


MODELS = Path(os.environ.get("FARSIGHT_MODELS", Path(__file__).parents[2] / "models"))
ONNX_MODEL = Path(os.environ.get("FARSIGHT_POSE_MODEL", MODELS / "yolo11n-pose.onnx"))
ENGINE = os.environ.get("FARSIGHT_POSE_ENGINE")


@unittest.skipUnless(ONNX_MODEL.exists() and importlib.util.find_spec("onnxruntime"),
                     "yolo11n-pose.onnx or onnxruntime not present")
class OnnxParityTests(unittest.TestCase):
    def test_real_model_output_through_tensorrt_path_matches_onnx_detector(self):
        from jetson.pose.onnx_backend import OnnxPoseDetector
        import onnxruntime as ort
        image = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)  # noise scores ~0.004
        session = ort.InferenceSession(str(ONNX_MODEL), providers=["CPUExecutionProvider"])
        raw = session.run(None, {"images": to_tensor(letterbox(image)[0])})[0]
        self.assertEqual(raw.shape, POSE_OUTPUT_SHAPE)
        self.assertLessEqual(float(raw[0, 7::3].max()), 1)  # keypoint confidences are sigmoid outputs
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.engine"
            path.write_bytes(b"CPU fixture only")
            cuda = FakeCuda()
            engine = FakeEngine(cuda)
            engine.output_shape = POSE_OUTPUT_SHAPE
            def execute(stream_handle):
                cuda.buffers[engine.context.addresses["output0"]][:] = raw.astype(np.float32).tobytes()
                return True
            engine.context.execute_async_v3 = execute
            with TensorRTPoseDetector(str(path), .002, cuda=cuda, trt_module=fake_trt(engine), warmup=1) as detector:
                got = detector.detect(image)
        expected = OnnxPoseDetector(str(ONNX_MODEL), conf_thres=.002).detect(image)
        self.assertGreater(len(expected), 0)
        self.assertEqual(len(got), len(expected))
        for a, b in zip(got, expected):
            np.testing.assert_allclose(a.box, b.box, atol=1e-3)
            np.testing.assert_allclose(a.kpts, b.kpts, atol=1e-3)
            self.assertEqual((round(a.conf, 5), a.down), (round(b.conf, 5), b.down))


@unittest.skipUnless(ENGINE and Path(ENGINE).is_file() and importlib.util.find_spec("tensorrt"),
                     "real hardware only: set FARSIGHT_POSE_ENGINE on the Jetson")
class JetsonHardwareTests(unittest.TestCase):
    def test_real_engine_runs_on_a_camera_sized_frame(self):
        with TensorRTPoseDetector(ENGINE) as detector:
            self.assertEqual(detector.engine.output_shape, POSE_OUTPUT_SHAPE)
            self.assertIsInstance(detector.detect(np.full((480, 640, 3), 90, np.uint8)), list)


if __name__ == "__main__":
    unittest.main()
