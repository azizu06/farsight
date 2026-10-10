from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"), "Apple Network framework/Swift required")
class PhoneBridgeTests(unittest.TestCase):
    def test_real_network_loopback_transport_and_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="farsight-phone-bridge-") as work:
            binary = Path(work) / "bridge-fixture"
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/PhoneBridge.swift"),
                            str(ROOT / "ios/tests/PhoneBridgeFixture.swift"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS", result.stdout)
