from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"), "Apple Compression/Swift required")
class GeometryWireTests(unittest.TestCase):
    def test_native_brotli_interoperability_and_binary_layout(self):
        import brotli
        with tempfile.TemporaryDirectory(prefix="farsight-geometry-wire-") as work:
            path = Path(work)
            binary = path / "fixture"
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/GeometryWire.swift"),
                            str(ROOT / "ios/tests/GeometryWireFixture.swift"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(binary), work], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn("PASS native geometry wire", result.stdout)
            raw = (path / "raw.bin").read_bytes()
            packed = (path / "br.bin").read_bytes()
            header = struct.unpack("<BBHQQ16s16s16fII", packed[:124])
            self.assertEqual(header[:5], (1, 0, 2, 44, 2))
            self.assertEqual(header[5], uuid.UUID("123e4567-e89b-12d3-a456-426614174000").bytes)
            self.assertEqual(header[6], uuid.UUID("223e4567-e89b-12d3-a456-426614174000").bytes)
            self.assertEqual(header[7 + 12:7 + 15], (1, 2, 3))
            self.assertEqual(header[-2:], (3, 3))
            body = brotli.decompress(packed[124:])
            self.assertEqual(body, raw[124:])
            self.assertEqual(struct.unpack("<9f3I", body), (0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 2))
            reset = (path / "reset.bin").read_bytes()
            self.assertEqual(len(reset), 124)
            self.assertEqual(struct.unpack("<BBH", reset[:4]), (1, 2, 0))
