from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("swiftc"), "Swift compiler required")
class CaptureCadenceTests(unittest.TestCase):
    def test_sensor_callback_cadence_and_send_gaps(self):
        with tempfile.TemporaryDirectory(prefix="farsight-capture-cadence-") as work:
            binary = Path(work) / "fixture"
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/CaptureCadence.swift"),
                            str(ROOT / "ios/tests/CaptureCadenceFixture.swift"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS capture cadence", result.stdout)


if __name__ == "__main__":
    unittest.main()
