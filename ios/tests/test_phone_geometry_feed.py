"""Actual production Swift feed/native Brotli with synthetic mesh; no devices."""
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"),
                     "Apple Compression/Swift required")
class PhoneGeometryFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="farsight-phone-geometry-feed-")
        cls.binary = Path(cls.work.name) / "fixture"
        try:
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/GeometryWire.swift"),
                            str(ROOT / "ios/FarsightCapture/PhoneGeometryFeed.swift"),
                            str(ROOT / "ios/tests/PhoneGeometryFeedFixture.swift"), "-o", str(cls.binary)],
                           check=True, capture_output=True, text=True, timeout=60)
        except BaseException:
            cls.work.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def run_fixture(self, mode):
        directory = Path(self.work.name) / mode
        directory.mkdir()
        result = subprocess.run([str(self.binary), mode, str(directory)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["passed"])
        self.assertTrue(summary["software_only"])
        return directory, summary

    def test_full_snapshots_reconnect_sequence_and_identical_native_payload(self):
        directory, summary = self.run_fixture("snapshot")
        packets = [(directory / f"packet-{i}.bin").read_bytes() for i in range(6)]
        self.assertEqual([p[1] for p in packets], [2, 0, 3, 2, 0, 3])
        self.assertEqual([struct.unpack_from("<Q", p, 4)[0] for p in packets], [2, 3, 4, 5, 6, 7])
        self.assertEqual(packets[1][124:], packets[4][124:])
        self.assertEqual([len(p) for p in (packets[0], packets[2], packets[3], packets[5])], [124]*4)
        self.assertEqual(struct.unpack_from("<II", packets[1], 116), (3, 3))
        self.assertEqual(summary["snapshot_records"], 6)

    def test_pending_latest_anchor_coalescing_replacement_and_neutral_removal(self):
        directory, summary = self.run_fixture("coalescing")
        self.assertEqual(summary["submitted_updates"], 100)
        self.assertEqual(summary["coalesced_records"], 2)
        packet = (directory / "remove.bin").read_bytes()
        self.assertEqual(packet[:4], b"\x01\x01\x00\x00")
        self.assertEqual(len(packet), 124)
        self.assertEqual(struct.unpack_from("<II", packet, 116), (0, 0))
        self.assertEqual(struct.unpack_from("<16f", packet, 52),
                         (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))

    def test_stop_new_session_and_old_pending_callback_are_fenced(self):
        _, summary = self.run_fixture("lifecycle")
        self.assertTrue(summary["stale_changes_ignored"])
        self.assertTrue(summary["new_session_sequence_reset"])

    def test_invalid_and_oversize_inputs_leave_retained_anchor_atomic(self):
        _, summary = self.run_fixture("invalid")
        self.assertEqual(summary["atomic_rejections"], 7)

    def test_retained_vertex_and_anchor_bounds_emit_ordered_removals(self):
        _, summary = self.run_fixture("bounds")
        self.assertEqual(summary["vertex_cap"], 300_000)
        self.assertEqual(summary["anchor_cap"], 256)
        self.assertEqual(summary["triangle_cap"], 100_000)
        self.assertEqual(summary["pending_budget_rejections"], 1)


if __name__ == "__main__":
    unittest.main()
