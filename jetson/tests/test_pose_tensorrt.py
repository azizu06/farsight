"""CPU CUDA/TensorRT fixtures exercise the executable pose path, not real GPU inference."""

from pathlib import Path
import tempfile
import unittest

import numpy as np

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


if __name__ == "__main__":
    unittest.main()
