"""Farsight server: HTTP + WebSocket endpoints from docs/interfaces.md."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from .camera import Camera, open_camera
from .config import Config
from .detect import Detector, make_detector
from .detect.worker import DetectorWorker

BOUNDARY = "frame"


def create_app(camera: Camera, detector: Detector | None = None) -> FastAPI:
    worker = DetectorWorker(camera, detector) if detector else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()
        camera.stop()

    app = FastAPI(title="Farsight", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        return {
            "camera": camera.ok and not camera.is_mock,
            "camera_mock": camera.is_mock,
            "camera_fps": round(camera.fps, 1),
            "detector": worker.name if worker else "none",
            "detector_fps": round(worker.fps, 1) if worker else 0.0,
            "detector_ms": round(worker.latency_ms, 1) if worker else 0.0,
            "rover": False,
            "estop": False,
        }

    async def mjpeg(max_frames: int | None = None):
        last, sent = -1, 0
        while max_frames is None or sent < max_frames:
            seq, jpeg = camera.latest()
            if jpeg is None or seq == last:
                await asyncio.sleep(0.005)
                continue
            last, sent = seq, sent + 1
            yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode() \
                + jpeg + b"\r\n"

    app.state.mjpeg = mjpeg

    @app.get("/stream.mjpg")
    def stream() -> StreamingResponse:
        return StreamingResponse(
            mjpeg(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.websocket("/ws/detections")
    async def detections(ws: WebSocket) -> None:
        await ws.accept()
        last = 0
        try:
            while True:
                count, message = worker.latest() if worker else (0, None)
                if message is None or count == last:
                    await asyncio.sleep(0.01)
                    continue
                last = count
                await ws.send_json(message)
        except WebSocketDisconnect:
            pass

    return app


def main() -> None:
    import uvicorn

    cfg = Config.from_env()
    try:
        detector = make_detector(cfg)
    except Exception as e:  # missing model/onnxruntime must not take the video stream down
        logging.getLogger("farsight").warning("detector disabled: %s", e)
        detector = None
    app = create_app(open_camera(cfg), detector)
    ssl = {"ssl_certfile": cfg.certfile, "ssl_keyfile": cfg.keyfile} if cfg.certfile else {}
    uvicorn.run(app, host=cfg.host, port=cfg.port, **ssl)


if __name__ == "__main__":
    main()
