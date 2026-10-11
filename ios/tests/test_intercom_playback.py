from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("swiftc"), "Swift compiler required")
class IntercomPlaybackTests(unittest.TestCase):
    def test_pcm_gate_and_speaker_queue_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="farsight-intercom-") as work:
            binary = Path(work) / "intercom-playback"
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/IntercomPlayback.swift"),
                            str(ROOT / "ios/tests/IntercomPlaybackFixture.swift"), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("PASS intercom playback protocol and bounded speaker queue", result.stdout)


if __name__ == "__main__":
    unittest.main()
