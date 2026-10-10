"""Production Swift framing consumed by production Python phone endpoints."""

import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import unittest

import cv2
from fastapi.testclient import TestClient
import numpy as np

from jetson.mapping.phone import decode_frame
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.camera import CameraService


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("swiftc"), "Swift compiler is required for native wire interop")
class NativeWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="farsight-swift-wire-")
        cls.directory = Path(cls.work.name)
        cls.fixture = cls.directory / "packet-fixture"
        subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/WirePacket.swift"),
                        str(ROOT / "ios/tests/PacketFixture.swift"), "-o", str(cls.fixture)],
                       check=True, capture_output=True, text=True)
        image = np.zeros((6, 8, 3), np.uint8)
        image[:, :4] = (10, 20, 230)
        image[:, 4:] = (200, 180, 10)
        _, jpeg = cv2.imencode(".jpg", image)
        cls.jpeg = jpeg.tobytes()

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def packet(self, header, *payloads):
        header_path = self.directory / "header.json"
        header_path.write_text(json.dumps(header))
        output = self.directory / "packet.bin"
        paths = []
        for number, data in enumerate(payloads):
            path = self.directory / f"payload-{number}.bin"
            path.write_bytes(data)
            paths.append(str(path))
        subprocess.run([str(self.fixture), "packet", str(header_path), str(output), *paths],
                       check=True, capture_output=True)
        return output.read_bytes()

    def rgb_header(self, frame_id=0):
        return dict(version=1, type="rgb_frame", session_id="native-test", frame_id=frame_id,
                    t_capture=12.5 + frame_id, jpeg_len=len(self.jpeg), width=8, height=6,
                    tracking="normal", t_wall_ms=1791600000000)

    def test_rgb_packet_header_little_endian_and_live_endpoint(self):
        packet = self.packet(self.rgb_header(), self.jpeg)
        length = struct.unpack_from("<I", packet)[0]
        self.assertEqual(json.loads(packet[4:4 + length]), self.rgb_header())
        self.assertEqual(packet[4 + length:], self.jpeg)
        app = create_app(detector="none")
        with TestClient(app) as client:
            with client.websocket_connect("/phone/rgb") as ws:
                ws.send_json(dict(version=1, type="hello", session_id="native-test", map_epoch=1,
                                  device="Native Swift fixture", supports_scene_depth=False))
                ws.send_bytes(packet)
                deadline = time.monotonic() + 2
                while not client.get("/health").json()["camera"]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                health = client.get("/health").json()
                self.assertEqual(health["camera_source"], "PHONE RGB")
                self.assertEqual(client.get("/camera.jpg").status_code, 200)
                self.assertEqual(health["points"], 0)  # RGB never invents a depth map

    def test_calibrated_rgbd_float32_and_column_major_wire(self):
        transform = np.eye(4)
        transform[:3, 3] = [1, 2, 3]
        depth = np.full((3, 4), 2.0, dtype="<f4")
        confidence = np.full((3, 4), 2, dtype=np.uint8)
        header = dict(version=1, type="frame", session_id="native-test", map_epoch=1,
                      frame_id=1, t_capture=12.5, tracking="normal",
                      transform=transform.flatten(order="F").tolist(),
                      image=dict(width=8, height=6, jpeg_len=len(self.jpeg),
                                 intrinsics=[8, 0, 0, 0, 8, 0, 4, 3, 1], orientation="landscape_right"),
                      depth=dict(width=4, height=3, format="float32_m", len=depth.nbytes),
                      confidence=dict(width=4, height=3, format="uint8_0_2", len=confidence.nbytes))
        packet = self.packet(header, self.jpeg, depth.tobytes(), confidence.tobytes())
        frame = decode_frame(packet, ("native-test", 1))
        np.testing.assert_array_equal(frame.depth, depth)
        np.testing.assert_array_equal(frame.confidence, confidence)
        np.testing.assert_array_equal(frame.transform, transform)
        self.assertEqual(frame.calibration.fx, 4)
        self.assertEqual(frame.calibration.cx, 2)
        app = create_app(detector="none")
        with TestClient(app) as client:
            with client.websocket_connect("/phone") as ws:
                ws.send_json(dict(version=1, type="hello", session_id="native-test", map_epoch=1,
                                  device="Native Swift fixture", supports_scene_depth=True))
                ws.send_bytes(packet)
                deadline = time.monotonic() + 2
                while client.get("/health").json()["frames"] < 1:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                health = client.get("/health").json()
                self.assertEqual(health["camera_source"], "IPHONE LIDAR")
                self.assertGreater(health["points"], 0)
                self.assertEqual(health["position"], [1, 2, 3])

    def test_optional_producer_interval_passes_through_native_framing(self):
        header = dict(self.rgb_header(), producer_delay_ms=37.25)
        packet = self.packet(header, self.jpeg)
        length = struct.unpack_from("<I", packet)[0]
        self.assertEqual(json.loads(packet[4:4 + length])["producer_delay_ms"], 37.25)
        self.assertEqual(packet[4 + length:], self.jpeg)
        camera = CameraService(MapState())
        self.assertEqual(camera.accept_phone_rgb(packet, "native-test"), 0)
        self.assertEqual(camera.status()["camera_source"], "PHONE RGB")


    def test_endpoint_mode_selection_and_invalid_urls(self):
        for supplied, mode, expected in (
                ("ws://172.20.10.2:8443/phone", "rgb", "ws://172.20.10.2:8443/phone/rgb"),
                ("wss://jetson.example/phone/rgb?ignored=1", "depth", "wss://jetson.example/phone")):
            result = subprocess.run([str(self.fixture), "endpoint", supplied, mode],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), expected)
        for invalid in ("https://jetson:8443", "", "ws://user:secret@jetson:8443"):
            result = subprocess.run([str(self.fixture), "endpoint", invalid, "rgb"], capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_oversize_packet_rejected_by_native_builder(self):
        header_path = self.directory / "oversize.json"
        header_path.write_text(json.dumps(self.rgb_header()))
        payload = self.directory / "oversize-payload.bin"
        payload.write_bytes(b"\0" * (8 * 1024 * 1024))
        result = subprocess.run([str(self.fixture), "packet", str(header_path),
                                 str(self.directory / "oversize-packet.bin"), str(payload)],
                                capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
