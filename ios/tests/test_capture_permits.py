from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("swiftc"), "Swift compiler required")
class CapturePermitTests(unittest.TestCase):
    def test_production_swift_gate_rejects_old_exposures_and_unqualified_clock(self):
        with tempfile.TemporaryDirectory(prefix="farsight-capture-gate-") as work:
            output = Path(work) / "capture-permits"
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/WirePacket.swift"),
                            str(ROOT / "ios/tests/CapturePermitFixture.swift"), "-o", str(output)],
                           check=True, capture_output=True, text=True)
            result = subprocess.run([str(output)], check=True, capture_output=True, text=True)
            self.assertIn("PASS", result.stdout)
