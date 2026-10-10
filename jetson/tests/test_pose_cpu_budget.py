"""The CPU pose backend shares the host with camera, encoder and control work."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from jetson.pose.onnx_backend import OnnxPoseDetector


class PoseCpuBudgetTests(unittest.TestCase):
    def test_cpu_pose_session_reserves_headroom_and_still_decodes(self):
        created = []
        class Session:
            def __init__(self, path, **kwargs):
                created.append(kwargs)
            def get_inputs(self):
                return [SimpleNamespace(name="images")]
            def run(self, outputs, inputs):
                return [np.zeros((1, 56, 8400), dtype=np.float32)]
        fake_ort = SimpleNamespace(InferenceSession=Session,
                                   SessionOptions=lambda: SimpleNamespace(intra_op_num_threads=0))
        with patch.dict("sys.modules", onnxruntime=fake_ort):
            detector = OnnxPoseDetector("test.onnx")
        options = created[0].get("sess_options")
        self.assertIsNotNone(options, "CPU inference must bound its pool instead of using every physical core")
        self.assertEqual(options.intra_op_num_threads, 2)
        self.assertEqual(created[0]["providers"], ["CPUExecutionProvider"])
        self.assertEqual(detector.detect(np.zeros((480, 640, 3), dtype=np.uint8)), [])
