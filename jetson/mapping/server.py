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
from jetson.camera import CameraService
from jetson.video import VideoService
from jetson.perception import PerceptionService
from jetson.mission import Mission
from jetson.control import ControlService

DIST = Path(__file__).resolve().parents[2] / "quest" / "dist"


def create_app(demo=False, camera_device=None, serial_port=None, simulate_rover=False,
               detector="hog", detector_model=None, h264=False, mission_dir=None, max_pwm=100):
    if (demo or simulate_rover) and serial_port:
        raise ValueError("Simulated sources cannot be combined with a physical motor serial port")
    state = MapState(demo)
    camera_service = CameraService(state, device=camera_device)
    video_service = VideoService(camera_service, enabled=h264)
    mission = Mission(mission_dir)
    perception = PerceptionService(camera_service, model=detector_model, mode=detector, on_detection=mission.detect)
    control = ControlService(serial_port=serial_port, simulate=simulate_rover, max_pwm=max_pwm,
                             camera_ready=lambda: camera_service.status()["camera"])

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
        control.start()
        await camera_service.start()
        await video_service.start()
        await perception.start()
        try:
            yield
        finally:
            await asyncio.to_thread(control.close)
            await perception.close()
            await video_service.close()
            await camera_service.close()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Farsight RGB-D mapper", lifespan=lifespan)
    app.state.mapping = state
    app.state.phone_owner = None
    app.state.camera = camera_service
    app.state.video = video_service
    app.state.control = control
    app.state.perception = perception
    app.state.mission = mission
    app.include_router(control.router)

    @app.get("/health")
    async def health():
        status = state.status()
        status.update(camera_service.status())
        rover = control.status()
        status.update(rover=rover["rover"], estop=rover["estop"], control=rover,
                      detector=perception.mode, detector_error=perception.error,
                      detector_fps=perception.snapshot()["fps"],
                      video=dict(enabled=h264, ready=video_service.init is not None, error=video_service.error))
        return status

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
        frame = await asyncio.to_thread(camera_service.preview)
        if frame is None:
            raise HTTPException(503, "Waiting for a camera frame")
        return Response(frame.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store",
              "X-Frame-Id": str(frame.frame_id), "X-Frame-Age-Ms": str(frame.age_ms),
              "X-Frame-Source": frame.source})

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
                frame = await asyncio.to_thread(camera_service.preview)
                identity = (frame.source, frame.frame_id) if frame else None
                if frame and frame.age_ms < 500 and identity != last:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame.jpeg + b"\r\n"
                    last = identity
                await asyncio.sleep(0.1)
        return StreamingResponse(images(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.websocket("/ws/video")
    async def video(ws: WebSocket):
        await ws.accept()
        if not h264:
            await ws.send_json({"type": "video_error", "reason": "H264 disabled; use camera.jpg"})
            await ws.close(code=1008)
            return
        for _ in range(100):
            if video_service.init is not None or video_service.error:
                break
            await asyncio.sleep(.05)
        if video_service.init is None:
            await ws.send_json({"type": "video_error", "reason": video_service.error or "Waiting for camera"})
            await ws.close(code=1013)
            return
        queue = video_service.subscribe()
        try:
            frame = camera_service.snapshot()
            await ws.send_json(dict(type="video_init", mime=video_service.mime, source=frame.source if frame else "WAITING"))
            await ws.send_bytes(video_service.init)
            while True:
                request = await asyncio.wait_for(ws.receive_json(), 10)
                request_id = request.get("request_id") if isinstance(request, dict) else None
                if (not isinstance(request, dict) or request.get("type") != "next"
                        or type(request_id) is not int or not 0 <= request_id <= 2**53 - 1):
                    await ws.close(code=1008, reason="Expected next video fragment request")
                    return
                fragment = await asyncio.wait_for(queue.get(), 2)
                if fragment is None:
                    await ws.close(code=1013, reason="Video consumer fell behind; reconnect for fresh video")
                    return
                metadata = {key: value for key, value in fragment.items() if key not in ("payload", "received")}
                metadata.update(type="video_segment", request_id=request_id,
                                age_ms=max(0, round((time.monotonic() - fragment["received"]) * 1000)))
                await asyncio.wait_for(ws.send_json(metadata), .5)
                await asyncio.wait_for(ws.send_bytes(fragment["payload"]), .5)
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError, ValueError):
            with suppress(RuntimeError):
                await ws.close(code=1013)
        finally:
            video_service.subscribers.discard(queue)

    @app.websocket("/ws/detections")
    async def detections(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await asyncio.wait_for(ws.send_json(perception.snapshot()), .5)
                await asyncio.sleep(.3)
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            pass

    @app.get("/mission/report")
    async def report():
        return mission.report()

    @app.get("/mission/snapshots/{name}")
    async def mission_snapshot(name: str):
        if not mission.directory or "/" in name or "\\" in name or not name.startswith("detection-") or not name.endswith(".jpg"):
            raise HTTPException(404, "Mission snapshot unavailable")
        path = mission.directory / name
        if not path.is_file():
            raise HTTPException(404, "Mission snapshot unavailable")
        return Response(await asyncio.to_thread(path.read_bytes), media_type="image/jpeg")

    class DrillRequest(BaseModel):
        duration_s: int = 90

    @app.post("/mission/drill")
    async def start_drill(body: DrillRequest):
        if not 10 <= body.duration_s <= 300:
            raise HTTPException(422, "Drill duration must be 10..300 seconds")
        return mission.start_drill(body.duration_s)

    @app.get("/mission/drill")
    async def drill_status():
        return mission.drill()

    class AskRequest(BaseModel):
        text: str

    @app.post("/ask")
    async def ask(body: AskRequest):
        if not body.text.strip() or len(body.text) > 2000:
            raise HTTPException(422, "Ask a question of at most 2000 characters")
        detected = perception.snapshot()
        live = camera_service.status()
        rover = control.status()
        people = sum(box["cls"] == "person" for box in detected["boxes"])
        answer = (f"The {live['camera_source'].lower()} view is {'live' if live['camera'] else 'stale or unavailable'}. "
                  + (f"The {detected['detector']} detector currently reports {people} person detections. "
                     if detected["fresh"] and not detected["error"] else "No fresh person detection result is available. ")
                  + (f"The ultrasonic range is {rover['range_cm']} centimetres. " if rover["range_cm"] is not None else "Ultrasonic range is unavailable. ")
                  + "Motion is controlled only by the held deadman; this answer does not move the rover.")
        return dict(question=body.text, answer=answer, action=None, model="local-observation-summary", ms=0,
                    source=live["camera_source"], simulated=demo or simulate_rover)

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
            camera_service.phone_rgb = None
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

    @app.websocket("/phone/rgb")
    async def phone_rgb(ws: WebSocket):
        await ws.accept()
        if demo or app.state.phone_owner is not None or camera_device is not None:
            await ws.close(code=1008, reason="Another camera source is active")
            return
        owner = object()
        app.state.phone_owner = owner
        previous = -1
        try:
            hello = Hello.model_validate_json(await asyncio.wait_for(ws.receive_text(), 10))
            camera_service.phone_rgb = None
            while True:
                packet = await ws.receive_bytes()
                previous = await asyncio.to_thread(camera_service.accept_phone_rgb, packet, hello.session_id, previous)
        except WebSocketDisconnect:
            pass
        except (ValueError, TypeError, KeyError, ValidationError, asyncio.TimeoutError) as exc:
            with suppress(RuntimeError):
                await ws.close(code=1008, reason=str(exc)[:100])
        finally:
            if app.state.phone_owner is owner:
                app.state.phone_owner = None
                # Preserve the last image for inspection; its receipt age keeps increasing.

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
    parser.add_argument("--camera", help="USB camera index or /dev/video path; default is phone RGB-D input")
    parser.add_argument("--serial-port", help="Uno serial path; omitted means physical motors disabled")
    parser.add_argument("--simulate-rover", action="store_true", help="Explicit virtual rover; never opens serial")
    parser.add_argument("--max-pwm", type=int, default=100, help="Initial motor PWM ceiling, 1..255")
    parser.add_argument("--detector", choices=("hog", "none"), default="hog", help="Bundled CPU person detector")
    parser.add_argument("--detector-model", help="YOLOv8/11 detection ONNX path; overrides bundled HOG")
    parser.add_argument("--h264", action="store_true", help="Enable ffmpeg H264 fragmented MP4 WebSocket")
    parser.add_argument("--mission-dir", help="Persist detection evidence beneath this directory")
    args = parser.parse_args()
    if args.simulate_rover and args.serial_port:
        parser.error("--simulate-rover cannot be combined with --serial-port")
    if args.demo and args.serial_port:
        parser.error("A simulated camera cannot be used to drive physical motors")
    if args.demo and args.camera:
        parser.error("Choose --demo or a real --camera, not both")
    uvicorn.run(create_app(args.demo, camera_device=args.camera, serial_port=args.serial_port,
                simulate_rover=args.simulate_rover, detector=args.detector, detector_model=args.detector_model,
                h264=args.h264, mission_dir=args.mission_dir, max_pwm=args.max_pwm), host=args.host, port=args.port,
                ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
                ws_max_size=8 * 1024 * 1024, access_log=False,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
