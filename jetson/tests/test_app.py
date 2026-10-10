import asyncio
import time

from fastapi.testclient import TestClient

from farsight.app import create_app
from farsight.camera import MockCamera, open_camera
from farsight.config import Config


def wait_for_frame(cam, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cam.ok:
        time.sleep(0.01)
    assert cam.ok


def test_health_reports_mock_camera_honestly():
    cam = MockCamera(Config(fps=60)).start()
    wait_for_frame(cam)
    body = TestClient(create_app(cam)).get("/health").json()
    assert body["camera"] is False
    assert body["camera_mock"] is True
    cam.stop()


def test_mjpeg_yields_jpeg_parts():
    cam = MockCamera(Config(fps=60)).start()
    wait_for_frame(cam)
    app = create_app(cam)

    async def first_two():
        return [part async for part in app.state.mjpeg(max_frames=2)]

    parts = asyncio.run(first_two())
    assert len(parts) == 2
    for part in parts:
        assert part.startswith(b"--frame\r\nContent-Type: image/jpeg")
        assert b"\xff\xd8" in part  # JPEG start marker
    cam.stop()


def test_missing_camera_falls_back_to_mock():
    cam = open_camera(Config(camera="/dev/does-not-exist", fps=60))
    assert cam.is_mock
    cam.stop()


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("FARSIGHT_PORT", "9000")
    monkeypatch.setenv("FARSIGHT_CAMERA", "mock")
    cfg = Config.from_env()
    assert cfg.port == 9000 and cfg.camera == "mock"
