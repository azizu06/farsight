"""python -m jetson.mapping.server --demo (or live phone mode by default)."""

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
import uvicorn

from .demo import simulated_frame
from .phone import Hello, decode_frame, validate_pose
from .state import MapState

DIST = Path(__file__).resolve().parents[2] / "quest" / "dist"


def create_app(demo=False):
    state = MapState(demo)

    async def simulate():
        started, frame_id = time.monotonic(), 0
        while True:
            frame = await asyncio.to_thread(simulated_frame, time.monotonic() - started, frame_id)
            await asyncio.to_thread(state.process, frame)
            frame_id += 1
            await asyncio.sleep(0.16)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(simulate()) if demo else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Farsight RGB-D mapper", lifespan=lifespan)
    app.state.mapping = state
    app.state.phone_owner = None

    @app.get("/health")
    async def health():
        return state.status()

    @app.get("/map/points.bin")
    async def points():
        return Response(await asyncio.to_thread(state.point_packet), media_type="application/octet-stream")

    @app.get("/map/mesh.bin")
    async def mesh():
        return Response(await asyncio.to_thread(state.mesh_packet), media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    @app.get("/map/export.ply")
    async def export():
        return Response(await asyncio.to_thread(state.ply), media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="farsight-scan.ply"'})

    @app.post("/map/reset")
    async def reset():
        state.clear()
        return state.status()

    class Pause(BaseModel):
        paused: bool

    @app.post("/map/pause")
    async def pause(body: Pause):
        with state.lock:
            state.paused = body.paused
        return state.status()

    @app.get("/camera.jpg")
    async def camera():
        if state.jpeg is None:
            raise HTTPException(503, "Waiting for a camera frame")
        return Response(state.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/depth.jpg")
    async def depth():
        if state.depth_jpeg is None:
            raise HTTPException(503, "Waiting for measured depth")
        return Response(state.depth_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/stream.mjpg")
    async def stream():
        async def images():
            last = None
            while True:
                jpeg = state.jpeg
                if jpeg is not None and jpeg is not last:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    last = jpeg
                await asyncio.sleep(0.1)
        return StreamingResponse(images(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.websocket("/ws/map")
    async def viewer(ws: WebSocket):
        await ws.accept()

        async def send_snapshots():
            revision = -1
            try:
                while True:
                    status = state.status()
                    # Capture data and revision together; every reconnect gets
                    # a complete authoritative snapshot, including an empty map.
                    if status["revision"] != revision:
                        def snapshot():
                            with state.lock:
                                return state.status(), state.point_packet()
                        status, packet = await asyncio.to_thread(snapshot)
                        await ws.send_json(status)
                        await ws.send_bytes(packet)
                        revision = status["revision"]
                    else:
                        await ws.send_json(status)
                    await asyncio.sleep(0.5)
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass

        async def wait_for_disconnect():
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        sender = asyncio.create_task(send_snapshots())
        receiver = asyncio.create_task(wait_for_disconnect())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, receiver):
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await task

    @app.websocket("/phone")
    async def phone(ws: WebSocket):
        await ws.accept()
        if demo or app.state.phone_owner is not None:
            await ws.close(code=1008, reason="Demo is active or another phone owns the stream")
            return
        owner = object()
        app.state.phone_owner = owner
        worker = None
        queue = asyncio.Queue(maxsize=1)
        try:
            hello = Hello.model_validate_json(await asyncio.wait_for(ws.receive_text(), timeout=10))
            if not hello.supports_scene_depth:
                raise ValueError("This mapper requires measured scene depth")
            identity = hello.session_id, hello.map_epoch
            state.start_session(identity, hello.device)

            async def consume():
                while True:
                    packet = await queue.get()
                    try:
                        frame = await asyncio.to_thread(decode_frame, packet, identity)
                        await asyncio.to_thread(state.process, frame)
                    except (ValueError, TypeError, KeyError, ValidationError) as exc:
                        with state.lock:
                            state.rejected += 1
                            state.reason = f"Frame rejected: {str(exc)[:180]}"

            worker = asyncio.create_task(consume())
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if queue.full():
                        queue.get_nowait()
                        state.dropped += 1
                    queue.put_nowait(message["bytes"])
                elif message.get("text"):
                    header = json.loads(message["text"])
                    if header.get("type") != "pose":
                        raise ValueError("Expected a pose message or binary RGB-D frame")
                    state.observe_pose(header, validate_pose(header, identity))
        except (ValueError, TypeError, KeyError, ValidationError, asyncio.TimeoutError) as exc:
            with suppress(RuntimeError):
                await ws.close(code=1008, reason=str(exc)[:100])
        except WebSocketDisconnect:
            pass
        finally:
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if app.state.phone_owner is owner:
                app.state.phone_owner = None
                with state.lock:
                    state.connected = False
                    # Invalidate any CPU worker still finishing after disconnect.
                    state.processing_generation += 1

    if DIST.is_dir():
        app.mount("/", StaticFiles(directory=DIST, html=True), name="viewer")
    else:
        @app.get("/")
        async def unbuilt():
            return {"message": "Build the viewer: npm --prefix quest install && npm --prefix quest run build"}
    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Ray-cast room; no hardware needed")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--ssl-certfile")
    parser.add_argument("--ssl-keyfile")
    args = parser.parse_args()
    uvicorn.run(create_app(args.demo), host=args.host, port=args.port,
                ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
                ws_max_size=8 * 1024 * 1024, access_log=False,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
