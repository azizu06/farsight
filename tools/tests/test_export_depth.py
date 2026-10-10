import subprocess
import sys
import unittest
from types import SimpleNamespace

import numpy as np

from tools.export_depth import ExportError, INPUT_SHAPE, OUTPUT_SHAPE, validate_contract, validate_prediction


class Session:
    def __init__(self, input_shape=INPUT_SHAPE, output_shape=OUTPUT_SHAPE, dtype="tensor(float)"):
        self.input = SimpleNamespace(shape=input_shape, type=dtype)
        self.output = SimpleNamespace(shape=output_shape, type=dtype)

    def get_inputs(self):
        return [self.input]

    def get_outputs(self):
        return [self.output]


class ExportDepthTests(unittest.TestCase):
    def test_import_and_help_do_not_load_export_dependencies(self):
        program = "from tools.export_depth import main; import sys; assert not any(x in sys.modules for x in ['torch', 'transformers', 'onnx', 'onnxruntime']); main(['--help'])"
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--local-only", result.stdout)

    def test_fixed_metric_contract_and_wrong_relative_contract(self):
        validate_contract(Session())
        for session in (Session(input_shape=(1, 3, 518, 518)),
                        Session(output_shape=(1, 1, 308, 406)),
                        Session(dtype="tensor(float16)")):
            with self.assertRaises(ExportError):
                validate_contract(session)

    def test_near_equal_metric_output_preserves_units(self):
        reference = np.full(OUTPUT_SHAPE, 12, np.float32)
        actual = reference + np.float32(.0001)
        self.assertLess(validate_prediction(actual, reference), .0002)

    def test_rejects_relative_depth_rescaling(self):
        reference = np.full(OUTPUT_SHAPE, 12, np.float32)
        with self.assertRaises(ExportError):
            validate_prediction(reference / 20, reference)

    def test_rejects_invalid_depth(self):
        good = np.ones(OUTPUT_SHAPE, np.float32)
        for value in (np.nan, np.inf, -1):
            bad = good.copy()
            bad[0, 0, 0] = value
            with self.assertRaises(ExportError):
                validate_prediction(bad, good)

    def test_rejects_wrong_prediction_shape(self):
        with self.assertRaises(ExportError):
            validate_prediction(np.ones((1, 308, 406, 1)), np.ones(OUTPUT_SHAPE))


if __name__ == "__main__":
    unittest.main()
