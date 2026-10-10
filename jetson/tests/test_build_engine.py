import subprocess
from pathlib import Path
import tempfile
import unittest

from jetson.build_engine import BuildError, build_engine, main


class FakeTrtexec:
    def __init__(self, returncode=0, write=True):
        self.calls, self.returncode, self.write = [], returncode, write

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.write:
            target = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--saveEngine="))
            Path(target).write_bytes(b"ENGINE " + str(len(self.calls)).encode())
        return subprocess.CompletedProcess(command, self.returncode, stdout="trtexec log\n&&&& FAILED" if self.returncode else "ok")


class BuildEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.onnx = self.dir / "yolo11n-pose.onnx"
        self.onnx.write_bytes(b"onnx v1")
        self.trtexec = self.dir / "trtexec"
        self.trtexec.write_text("#!/bin/sh\n")

    def tearDown(self):
        self.temp.cleanup()

    def build(self, run, **options):
        return build_engine(self.onnx, trtexec=self.trtexec, run=run, trt_version="10.13.3", **options)

    def test_first_build_uses_fp16_trtexec_then_cache_is_instant(self):
        run = FakeTrtexec()
        engine, built = self.build(run)
        self.assertEqual((engine, built), (self.dir / "yolo11n-pose.engine", True))
        command = run.calls[0]
        self.assertEqual(command[0], str(self.trtexec))
        self.assertIn(f"--onnx={self.onnx}", command)
        self.assertIn("--fp16", command)
        self.assertFalse(any(arg.startswith("--minShapes") for arg in command))  # static ONNX fixes its shapes
        self.assertEqual(engine.read_bytes(), b"ENGINE 1")
        self.assertEqual(self.build(run), (engine, False))
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["trtexec", "yolo11n-pose.engine", "yolo11n-pose.engine.json", "yolo11n-pose.onnx"])

    def test_onnx_change_tensorrt_upgrade_precision_or_force_rebuilds(self):
        run = FakeTrtexec()
        self.build(run)
        self.onnx.write_bytes(b"onnx v2")
        self.assertTrue(self.build(run)[1])
        self.assertTrue(build_engine(self.onnx, trtexec=self.trtexec, run=run, trt_version="10.14.0")[1])
        self.assertTrue(build_engine(self.onnx, trtexec=self.trtexec, run=run, trt_version="10.14.0", fp16=False)[1])
        self.assertNotIn("--fp16", run.calls[-1])
        self.assertTrue(build_engine(self.onnx, trtexec=self.trtexec, run=run, trt_version="10.14.0", fp16=False,
                                     force=True)[1])
        self.assertEqual(len(run.calls), 5)

    def test_deleted_engine_is_rebuilt_even_with_a_matching_stamp(self):
        run = FakeTrtexec()
        engine, _ = self.build(run)
        engine.unlink()
        self.assertTrue(self.build(run)[1])

    def test_dynamic_models_get_an_exact_single_shape_profile(self):
        run = FakeTrtexec()
        self.build(run, shapes="images:1x3x640x640")
        for flag in ("--minShapes", "--optShapes", "--maxShapes"):
            self.assertIn(f"{flag}=images:1x3x640x640", run.calls[0])

    def test_failed_build_leaves_no_engine_stamp_or_partial_file(self):
        for run in (FakeTrtexec(returncode=1), FakeTrtexec(write=False)):
            with self.subTest(returncode=run.returncode, write=run.write):
                with self.assertRaisesRegex(BuildError, "yolo11n-pose"):
                    self.build(run)
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["trtexec", "yolo11n-pose.onnx"])

    def test_missing_inputs_fail_clearly(self):
        with self.assertRaisesRegex(BuildError, "trtexec not found"):
            build_engine(self.onnx, trtexec=self.dir / "absent", run=FakeTrtexec(), trt_version="10")
        with self.assertRaisesRegex(BuildError, "ONNX model not found"):
            build_engine(self.dir / "absent.onnx", trtexec=self.trtexec, run=FakeTrtexec(), trt_version="10")

    def test_cli_continues_past_a_failed_model_and_exits_nonzero(self):
        with self.assertRaises(SystemExit) as exit_:
            main([str(self.dir / "absent.onnx"), "--trtexec", str(self.dir / "absent")])
        self.assertEqual(exit_.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
