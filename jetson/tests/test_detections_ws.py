import time

import numpy as np
from fastapi.testclient import TestClient

from farsight.app import create_app
from farsight.camera import MockCamera
from farsight.config import Config
from farsight.detect import Detection


class FakeDetector:
    name = "fake"

    def detect(self, frame):
        k = np.zeros((17, 3), np.float32)
        k[:, 0], k[:, 1], k[:, 2] = 320, 240, 0.9
        return [Detection(box=(64, 48, 128, 240), conf=0.91, kpts=k, down=True),
                Detection(box=(0, 0, 64, 48), conf=0.5, kpts=None, down=False)]


def wait_frame(cam):
    end = time.monotonic() + 2
    while time.monotonic() < end and not cam.ok:
        time.sleep(0.01)
    assert cam.ok


def test_ws_detections_message_shape():
    cam = MockCamera(Config(fps=60)).start()
    wait_frame(cam)
    with TestClient(create_app(cam, FakeDetector())) as client:
        with client.websocket_connect("/ws/detections") as ws:
            msg = ws.receive_json()
    cam.stop()
    assert set(msg) == {"ts", "frame", "fps", "boxes"}
    assert isinstance(msg["ts"], float) and isinstance(msg["frame"], int)
    b0, b1 = msg["boxes"]
    assert {"id", "cls", "conf", "x", "y", "w", "h", "kpts", "down"} <= set(b0)
    assert b0["id"] == 0 and b0["cls"] == "person" and b0["down"] is True
    assert (b0["x"], b0["y"], b0["w"], b0["h"]) == (0.1, 0.1, 0.2, 0.5)  # 640x480 frame
    assert len(b0["kpts"]) == 17 and b0["kpts"][0] == [0.5, 0.5, 0.9]
    assert "kpts" not in b1 and b1["down"] is False and b1["id"] == 1


def test_health_reports_detector_fps():
    cam = MockCamera(Config(fps=60)).start()
    wait_frame(cam)
    with TestClient(create_app(cam, FakeDetector())) as client:
        deadline = time.monotonic() + 3
        body = client.get("/health").json()
        while body["detector_fps"] == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
            body = client.get("/health").json()
    cam.stop()
    assert body["detector_fps"] > 0
    assert body["detector"] == "fake" and body["detector_ms"] >= 0


def test_no_detector_keeps_health_zero_and_ws_open():
    cam = MockCamera(Config(fps=60)).start()
    wait_frame(cam)
    with TestClient(create_app(cam)) as client:
        assert client.get("/health").json()["detector_fps"] == 0.0
    cam.stop()
