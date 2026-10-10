"""Farsight server: HTTP + WebSocket endpoints from docs/interfaces.md."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from .camera import Camera, open_camera
from .config import Config

BOUNDARY = "frame"


def create_app(camera: Camera) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        camera.stop()

    app = FastAPI(title="Farsight", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        return {
            "camera": camera.ok and not camera.is_mock,
            "camera_mock": camera.is_mock,
            "camera_fps": round(camera.fps, 1),
            "detector_fps": 0.0,
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

    return app


def main() -> None:
    import uvicorn

    cfg = Config.from_env()
    app = create_app(open_camera(cfg))
    ssl = {"ssl_certfile": cfg.certfile, "ssl_keyfile": cfg.keyfile} if cfg.certfile else {}
    uvicorn.run(app, host=cfg.host, port=cfg.port, **ssl)


if __name__ == "__main__":
    main()
