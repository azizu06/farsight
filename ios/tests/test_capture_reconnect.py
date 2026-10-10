from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("swiftc"), "Swift compiler required")
class CaptureReconnectTests(unittest.TestCase):
    def test_production_recovery_policy(self):
        with tempfile.TemporaryDirectory(prefix="farsight-capture-reconnect-") as work:
            binary = Path(work) / "fixture"
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/CaptureReconnect.swift"),
                            str(ROOT / "ios/tests/CaptureReconnectFixture.swift"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS capture recovery", result.stdout)
