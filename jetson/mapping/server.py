"""python -m jetson.mapping.server --demo (or live phone mode by default)."""

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.datastructures import UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
import uvicorn

from .compress import EncodedCache
from .demo import stream_demo
from .phone import Hello, decode_frame, validate_pose
from .state import MapState
from jetson.camera import CameraService
from jetson.capture_permits import CapturePermits
from jetson.video import VideoService
from jetson.perception import PerceptionService
from jetson.mission import Mission
from jetson.replay import ReplayControl, ReplayPerception, ReplaySession
from jetson.control import ControlService
from jetson.assistant import SceneAssistant, AssistantError
from jetson.phrases import PhraseSpeaker, SpeechError
from jetson.triage import TriageBusy, TriageService
from jetson.drive_profile import DriveProfile
from jetson.scan import ScanService
from jetson.bulk import BulkPacer, BulkPacketTooLarge
from jetson.bulk_response import bulk_response, PacedStreamingResponse

DIST = Path(__file__).resolve().parents[2] / "quest" / "dist"


def create_app(demo=False, camera_device=None, serial_port=None, simulate_rover=False,
               detector="hog", detector_model=None, h264=False, mission_dir=None, max_pwm=100, min_pwm=0,
               video_fps=10, video_bitrate=500000, assistant_url=None, whisper_bin=None, whisper_model=None,
               piper_bin=None, piper_model=None, tts_dir=None, face_model=None, whisper_url=None,
               piper_model_es=None, speaker_cmd="auto", triage_interval=3.0,
               depth_model=None, scan_pan=False, scan_fov=70, scan_intrinsics=None, scan_turn_dps=90,
               record=False, replay=None, bulk_limit_mbps=0, drive_profile=None, allow_calibration=False):
    if isinstance(drive_profile, (str, Path)):
        drive_profile = DriveProfile.load(drive_profile)
        if drive_profile.meta.get("source") == "simulated":
            logging.getLogger(__name__).warning("Drive profile came from a SIMULATED rover; it does not describe real motors")
    bulk = BulkPacer(bulk_limit_mbps)
    if replay is not None and (serial_port or camera_device is not None or demo or simulate_rover or record):
        raise ValueError("Replay cannot be combined with serial, live/simulated sources, or recording")
    if (demo or simulate_rover) and serial_port:
        raise ValueError("Simulated sources cannot be combined with a physical motor serial port")
    state = MapState(demo)
    camera_service = CameraService(state, device=camera_device)
    video_service = VideoService(camera_service, enabled=h264, fps=video_fps, bitrate=video_bitrate)
    mission = Mission(mission_dir, record=record)
    perception = ReplayPerception(camera_service) if replay is not None else PerceptionService(camera_service, model=detector_model, mode=detector, on_detection=mission.detect, face_model=face_model)
    control = ReplayControl() if replay is not None else ControlService(serial_port=serial_port, simulate=simulate_rover, max_pwm=max_pwm, min_pwm=min_pwm,
                             drive_profile=drive_profile, allow_calibration=allow_calibration,
                             camera_ready=lambda: camera_service.status()["camera_drive_ready" if serial_port else "camera"],
                             detections=perception.snapshot)
    replay_session = ReplaySession(replay, camera_service, perception, control) if replay is not None else None
    if record:
        control.on_status = lambda packet: mission.record("control", packet)

    async def record_run():
        previous = None
        next_status = 0
        while True:
            frame = camera_service.snapshot()
            identity = (frame.source, frame.frame_id, frame.received) if frame else None
            if frame and identity != previous:
                await asyncio.to_thread(mission.record, "frame", frame)
                previous = identity
            if time.monotonic() >= next_status:
                await asyncio.to_thread(mission.record, "control", control.status())
                next_status = time.monotonic() + .1
            await asyncio.sleep(.01)

    async def sample_mission():
        # Read-only: mission telemetry never issues permits or commands.
        def sample():
            mission.observe_control(control.status(), camera=camera_service.status()["camera"])
        while True:
            with suppress(Exception):
                await asyncio.to_thread(sample)
            await asyncio.sleep(1 / mission.telemetry.SAMPLE_HZ)

    if hasattr(control, "state"):  # recorded replays have no live control loop
        control.state.on_output = mission.drill_motion
    assistant = SceneAssistant(camera_service, perception, control, server_url=assistant_url,
                               whisper_bin=whisper_bin, whisper_model=whisper_model,
                               piper_bin=piper_bin, piper_model=piper_model, output_dir=tts_dir,
                               whisper_url=whisper_url)
    triage = TriageService(mission, server_url=assistant.server_url, min_interval=triage_interval,
                           range_cm=lambda: control.status().get("range_cm"))
    phrases = PhraseSpeaker(tts_dir, voices=dict(en=piper_model, es=piper_model_es), piper_bin=piper_bin,
                            speaker_cmd=speaker_cmd)
    scanner = ScanService(state, camera_service, control, perception, model=depth_model,
                          physical_pan=scan_pan, horizontal_fov=scan_fov,
                          intrinsics=scan_intrinsics, turn_dps=scan_turn_dps)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(stream_demo(state, camera_service)) if demo else None
        replay_task = asyncio.create_task(replay_session.run.play(replay_session.publish)) if replay_session else None
        record_task = asyncio.create_task(record_run()) if record else None
        sample_task = asyncio.create_task(sample_mission())
        control.start()
        await camera_service.start()
        await video_service.start()
        await perception.start()
        triage.start()
        try:
            yield
        finally:
            await scanner.close()
            for stream_task in (sample_task, record_task, replay_task):
                if stream_task:
                    stream_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stream_task
            await asyncio.to_thread(mission.finish_recording)
            await asyncio.to_thread(control.close)
            await assistant.close()
            await phrases.close()
            await asyncio.to_thread(triage.close)
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
    mission.face_status = perception.face_status
    perception.face_wanted = mission.drill_score.running
    app.state.mission = mission
    app.state.assistant = assistant
    app.state.triage = triage
    app.state.phrases = phrases
    app.state.scanner = scanner
    app.state.bulk = bulk
    app.include_router(control.router)

    class AskBodyLimit:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            count = 0
            async def bounded_receive():
                nonlocal count
                message = await receive()
                count += len(message.get("body", b""))
                if count > SceneAssistant.MAX_AUDIO + 65536:
                    raise HTTPException(413, "Audio upload exceeds 8 MiB plus framing")
                return message
            await self.app(scope, bounded_receive if scope["type"] == "http" and scope.get("path") == "/ask" else receive, send)

    app.add_middleware(AskBodyLimit)

    @app.get("/health")
    async def health():
        status = await asyncio.to_thread(state.status)
        status.update(camera_service.status())
        status["scan"] = scanner.result
        rover = control.status()
        status.update(perception.health())
        status.update(replay=replay is not None, recording=record, rover=rover["rover"], estop=rover["estop"], control=rover,
                      camera_pan=scan_pan,
                      detector=perception.mode, detector_error=perception.error,
                      detector_fps=perception.snapshot()["fps"] if perception.snapshot()["fresh"] else 0,
                      video=dict(enabled=h264, ready=video_service.ready, error=video_service.error,
                                 fps=video_fps, bitrate=video_bitrate),
                      assistant=dict(vision_model=bool(assistant_url), whisper=bool(whisper_url or (whisper_bin and whisper_model)),
                                     piper=bool(piper_model)),
                      speech=await asyncio.to_thread(phrases.status), triage=triage.status())
        return status

    class ScanRequest(BaseModel):
        session_id: str
        permit: str

    @app.get("/scan")
    async def scan_status():
        return scanner.result

    @app.post("/scan", status_code=202)
    async def scan(body: ScanRequest):
        if replay is not None:
            raise HTTPException(409, "Replay is read-only; live scans are disabled")
        try:
            return scanner.start(body.session_id, body.permit)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    map_cache = EncodedCache()

    def map_version():
        return state.generation, state.revision, state.surfaces.revision

    def map_response(route, packet, accept_encoding, headers=None):
        def build():
            with state.lock:  # version and bytes from the same fused frame
                return map_version(), packet()
        body, coding = map_cache.get(route, map_version, build, accept_encoding)
        headers = dict(headers or {}, Vary="Accept-Encoding")
        if coding:
            headers["Content-Encoding"] = coding
        return bulk_response(body, bulk, media_type="application/octet-stream", headers=headers)

    @app.get("/map/points.bin")
    async def points(request: Request):
        return await asyncio.to_thread(map_response, "points", state.point_packet,
                                       request.headers.get("accept-encoding"))

    @app.get("/map/mesh.bin")
    async def mesh(request: Request):
        return await asyncio.to_thread(map_response, "mesh", state.mesh_packet,
                                       request.headers.get("accept-encoding"), {"Cache-Control": "no-store"})

    @app.get("/map/export.ply")
    async def export(request: Request):
        return await asyncio.to_thread(map_response, "ply", state.ply, request.headers.get("accept-encoding"),
                                       {"Content-Disposition": 'attachment; filename="farsight-scan.ply"'})

    @app.post("/map/reset")
    async def reset():
        await asyncio.to_thread(state.clear)
        return await asyncio.to_thread(state.status)

    class Pause(BaseModel):
        paused: bool

    @app.post("/map/pause")
    async def pause(body: Pause):
        def change():
            with state.lock:
                if not body.paused and state.scan:
                    state.clear()
                state.paused = body.paused
            return state.status()
        return await asyncio.to_thread(change)

    @app.get("/camera.jpg")
    async def camera():
        frame = await asyncio.to_thread(camera_service.preview)
        if frame is None:
            raise HTTPException(503, "Waiting for a camera frame")
        return bulk_response(frame.jpeg, bulk, media_type="image/jpeg", headers={"Cache-Control": "no-store",
              "X-Frame-Id": str(frame.frame_id), "X-Frame-Age-Ms": str(frame.age_ms),
              "X-Frame-Source": frame.source})

    @app.get("/depth.jpg")
    async def depth():
        if state.depth_jpeg is None:
            raise HTTPException(503, "Waiting for measured depth")
        return bulk_response(state.depth_jpeg, bulk, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/stream.mjpg")
    async def stream():
        async def images():
            last = None
            while True:
                latest = camera_service.snapshot()  # cheap; only re-encode a frame we have not sent
                if latest and latest.age_ms < 500 and (latest.source, latest.frame_id) != last:
                    frame = await asyncio.to_thread(camera_service.preview)
                    if frame:
                        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame.jpeg + b"\r\n"
                        last = (frame.source, frame.frame_id)
                await asyncio.sleep(0.02)
        if bulk.enabled:
            return PacedStreamingResponse(images(), bulk, media_type="multipart/x-mixed-replace; boundary=frame")
        return StreamingResponse(images(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.websocket("/ws/video")
    async def video(ws: WebSocket):
        await ws.accept()
        if not h264:
            await ws.send_json({"type": "video_error", "reason": "H264 disabled; use camera.jpg"})
            await ws.close(code=1008)
            return
        for _ in range(100):
            if video_service.ready or video_service.error:
                break
            await asyncio.sleep(.05)
        if not video_service.ready:
            await ws.send_json({"type": "video_error", "reason": video_service.error or "Waiting for camera"})
            await ws.close(code=1013)
            return
        queue = video_service.subscribe()
        tasks = []
        try:
            frame = camera_service.snapshot()
            await ws.send_json(dict(type="video_init", mime=video_service.mime, source=frame.source if frame else "WAITING"))
            async def send_initialization(payload):
                if bulk.enabled:
                    await asyncio.wait_for(ws.send_bytes(payload), .5)
                else:
                    await ws.send_bytes(payload)
            requests = asyncio.Queue(maxsize=4)
            async def receive_requests():
                while True:
                    reading = asyncio.create_task(ws.receive_json())
                    try:
                        # Keep outer cancellation authoritative even if a read
                        # completes in the same turn as init admission fails.
                        done, _ = await asyncio.wait((reading,), timeout=10)
                        if not done:
                            raise asyncio.TimeoutError()
                        request = reading.result()
                    finally:
                        reading.cancel()
                        await asyncio.gather(reading, return_exceptions=True)
                    request_id = request.get("request_id") if isinstance(request, dict) else None
                    if (not isinstance(request, dict) or request.get("type") != "next"
                            or type(request_id) is not int or not 0 <= request_id <= 2**53 - 1 or requests.full()):
                        raise ValueError("Invalid or excessive video request window")
                    requests.put_nowait((request_id, time.monotonic()))

            # Monitor requests/disconnection while initialization waits for the
            # shared budget, rather than retaining a cancelled client's slot.
            receiver = asyncio.create_task(receive_requests())
            initialization = asyncio.create_task(bulk.send_packet(video_service.init, send_initialization))
            tasks = [receiver, initialization]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if receiver in done:
                return

            async def send_fragments():
                while True:
                    request_id, waiting_at = await requests.get()
                    fragment = await asyncio.wait_for(queue.get(), 2)
                    wait_ms = (time.monotonic() - waiting_at) * 1000
                    if fragment is None:
                        return
                    async def send_fragment(payload):
                        metadata = {key: value for key, value in fragment.items() if key not in ("payload", "received")}
                        metadata.update(type="video_segment", request_id=request_id,
                                        server_wait_ms=wait_ms,
                                        age_ms=max(0, round((time.monotonic() - fragment["received"]) * 1000)))
                        await asyncio.wait_for(ws.send_json(metadata), .5)
                        await asyncio.wait_for(ws.send_bytes(payload), .5)
                    await bulk.send_packet(fragment["payload"], send_fragment)

            tasks = [receiver, asyncio.create_task(send_fragments())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except BulkPacketTooLarge:
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1009, reason="Oversized/invalid video packet; reduce bitrate/resolution for the bulk atomic cap")
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError, TypeError, ValueError):
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1013)
        finally:
            video_service.subscribers.discard(queue)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1013)

    @app.websocket("/ws/detections")
    async def detections(ws: WebSocket):
        await ws.accept()
        previous = None
        last_sent = 0.0
        try:
            while True:
                packet = perception.snapshot()
                identity = (packet["frame"], packet["source"], packet["fresh"], packet["error"], packet.get("replay_loop"))
                if identity != previous or time.monotonic() - last_sent > 2:
                    await asyncio.wait_for(ws.send_json(packet), .5)
                    previous, last_sent = identity, time.monotonic()
                await asyncio.sleep(.08)
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
        return bulk_response(await asyncio.to_thread(path.read_bytes), bulk, media_type="image/jpeg")

    class DrillRequest(BaseModel):
        duration_s: int = 90
        victims: int = 3

    @app.post("/mission/drill")
    async def start_drill(body: DrillRequest):
        if not 10 <= body.duration_s <= 300:
            raise HTTPException(422, "Drill duration must be 10..300 seconds")
        if not 1 <= body.victims <= 20:
            raise HTTPException(422, "Drill victims must be 1..20")
        return mission.start_drill(body.duration_s, body.victims)

    @app.post("/mission/triage/{encounter}")
    async def mission_triage(encounter: str):
        try:
            return await asyncio.to_thread(triage.generate, encounter)
        except KeyError as exc:
            raise HTTPException(404, "Unknown encounter; use a key from /mission/report") from exc
        except TriageBusy as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/mission/drill")
    async def drill_status():
        return mission.drill()

    @app.post("/ask")
    async def ask(request: Request):
        try:
            content_type = request.headers.get("content-type", "").split(";")[0]
            if content_type == "application/json":
                body = await request.json()
                if not isinstance(body, dict):
                    raise HTTPException(400, "Expected a text question object")
                return await assistant.ask(text=body.get("text"))
            if content_type == "multipart/form-data":
                async with request.form(max_files=1, max_fields=1) as form:
                    uploaded = form.get("audio")
                    if not isinstance(uploaded, UploadFile):
                        raise HTTPException(400, "Expected an audio file")
                    return await assistant.ask(audio=await uploaded.read(SceneAssistant.MAX_AUDIO + 1))
            raise HTTPException(415, "Use JSON text or multipart audio")
        except AssistantError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid question request") from exc

    class SpeakRequest(BaseModel):
        phrase: str
        lang: str

    @app.get("/speak")
    async def speak_options():
        return dict(phrases=phrases.phrases(), **await asyncio.to_thread(phrases.status))

    @app.post("/speak")
    async def speak(body: SpeakRequest):
        try:
            return await phrases.speak(body.phrase, body.lang)
        except SpeechError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    @app.get("/tts/{name}")
    async def speech(name: str):
        if not assistant.output_dir or not name.startswith(("reply-", "phrase-")) or not name.endswith(".wav") or "/" in name or "\\" in name:
            raise HTTPException(404, "Speech reply unavailable")
        path = assistant.output_dir / name
        if not path.is_file():
            raise HTTPException(404, "Speech reply unavailable")
        return bulk_response(await asyncio.to_thread(path.read_bytes), bulk, media_type="audio/wav")

    @app.websocket("/ws/map")
    async def viewer(ws: WebSocket):
        await ws.accept()
        http_points = ws.query_params.get("points") == "http"

        async def send_snapshots():
            revision = -1
            try:
                while True:
                    status = await asyncio.to_thread(state.status)
                    if http_points:
                        # Opt-in clients pull compressed HTTP snapshots at their
                        # own pace; legacy subscribers retain binary pushes.
                        status["points_transport"] = "http"
                        await ws.send_json(status)
                    # Capture data and revision together; every reconnect gets
                    # a complete authoritative snapshot, including an empty map.
                    elif status["revision"] != revision:
                        def snapshot():
                            with state.lock:
                                return state.status(), state.point_packet()
                        status, packet = await asyncio.to_thread(snapshot)
                        await ws.send_json(status)
                        async def send_packet(payload):
                            if bulk.enabled:
                                await asyncio.wait_for(ws.send_bytes(payload), .5)
                            else:
                                await ws.send_bytes(payload)
                        await bulk.send_packet(packet, send_packet)
                        revision = status["revision"]
                    else:
                        await ws.send_json(status)
                    await asyncio.sleep(0.5)
            except BulkPacketTooLarge:
                with suppress(RuntimeError, WebSocketDisconnect):
                    await ws.close(code=1009, reason="Bulk map packet too large; subscribe /ws/map?points=http and pull HTTP snapshots")
            except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
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
        if replay is not None or demo or app.state.phone_owner is not None:
            await ws.close(code=1008, reason="Demo is active or another phone owns the stream")
            return
        owner = object()
        app.state.phone_owner = owner
        worker = None
        permits = None
        queue = asyncio.Queue(maxsize=1)
        try:
            hello = Hello.model_validate_json(await asyncio.wait_for(ws.receive_text(), timeout=10))
            if not hello.supports_scene_depth:
                raise ValueError("This mapper requires measured scene depth")
            identity = hello.session_id, hello.map_epoch
            permits = CapturePermits(*identity, enabled=hello.supports_capture_permits)
            camera_service.phone_rgb = None
            await asyncio.to_thread(state.start_session, identity, hello.device)

            async def consume():
                while True:
                    packet, received_at = await queue.get()
                    try:
                        frame = await asyncio.to_thread(decode_frame, packet, identity)
                        frame.header["_received_monotonic"] = received_at
                        await asyncio.to_thread(state.process, frame)
                    except (ValueError, TypeError, KeyError, ValidationError) as exc:
                        with state.lock:
                            state.rejected += 1
                            state.reason = f"Frame rejected: {str(exc)[:180]}"

            worker = asyncio.create_task(consume())
            async def receive_frames():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        if queue.full():
                            queue.get_nowait()
                            state.dropped += 1
                        queue.put_nowait((message["bytes"], time.monotonic()))
                    elif message.get("text"):
                        header = json.loads(message["text"])
                        if header.get("type") != "pose":
                            raise ValueError("Expected a pose message or binary RGB-D frame")
                        await asyncio.to_thread(state.observe_pose, header, validate_pose(header, identity))
            await permits.run(ws, receive_frames)
        except (ValueError, TypeError, KeyError, ValidationError, asyncio.TimeoutError) as exc:
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1008, reason=str(exc)[:100])
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            if permits:
                permits.close()
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if app.state.phone_owner is owner:
                app.state.phone_owner = None
                def disconnect_sensor():
                    with state.lock:
                        state.connected = False
                        # Invalidate any CPU worker still finishing after disconnect.
                        state.processing_generation += 1
                await asyncio.to_thread(disconnect_sensor)

    @app.websocket("/phone/rgb")
    async def phone_rgb(ws: WebSocket):
        await ws.accept()
        if replay is not None or demo or app.state.phone_owner is not None or camera_device is not None:
            await ws.close(code=1008, reason="Another camera source is active")
            return
        owner = object()
        app.state.phone_owner = owner
        previous = -1
        permits = None
        try:
            hello = Hello.model_validate_json(await asyncio.wait_for(ws.receive_text(), 10))
            permits = CapturePermits(hello.session_id, hello.map_epoch, enabled=hello.supports_capture_permits)
            camera_service.phone_rgb = None
            async def receive_frames():
                nonlocal previous
                while True:
                    packet = await ws.receive_bytes()
                    previous = await asyncio.to_thread(camera_service.accept_phone_rgb, packet, hello.session_id,
                                                       previous, time.monotonic(), permits)
            await permits.run(ws, receive_frames)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        except (ValueError, TypeError, KeyError, ValidationError, asyncio.TimeoutError) as exc:
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1008, reason=str(exc)[:100])
        finally:
            if permits:
                permits.close()
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
    parser.add_argument("--min-pwm", type=int, default=0, help="Measured wheel starting PWM floor; default 0 until calibrated")
    parser.add_argument("--drive-profile", help="Opt-in measured drive profile (tools.calibrate_drive output); enables mode \"velocity\" in m/s, rad/s")
    parser.add_argument("--allow-calibration", action="store_true",
                        help="Accept raw-PWM mode \"calibrate\" from a held-deadman controller; for tools.calibrate_drive on a cleared floor only")
    parser.add_argument("--detector", choices=("hog", "pose", "none"), default="hog",
                        help="hog: bundled CPU detector; pose: YOLO11-pose (needs --detector-model); none")
    parser.add_argument("--detector-model", help="YOLOv8/11 ONNX or TensorRT engine path; overrides bundled HOG. With --detector pose: YOLO11-pose model")
    parser.add_argument("--face-model", help="Enable extra UltraFace RFB-320 ONNX pass; omitted means off")
    parser.add_argument("--depth-model", help="Metric Indoor Small 308x406 ONNX or target-built TensorRT engine")
    parser.add_argument("--scan-pan", action="store_true", help="Physical camera pan installed; default is approximate rover turn")
    parser.add_argument("--scan-fov", type=float, default=70, help="Camera horizontal FOV degrees; ignored with calibrated intrinsics")
    parser.add_argument("--scan-intrinsics", type=float, nargs=4, metavar=("FX", "FY", "CX", "CY"), help="Calibrated intrinsics for the transmitted RGB pixel grid")
    parser.add_argument("--scan-turn-dps", type=float, default=90, help="Measured turn degrees/sec at w=.3; approximate open-loop fallback")
    parser.add_argument("--h264", action="store_true", help="Enable ffmpeg H264 fragmented MP4 WebSocket")
    parser.add_argument("--video-fps", type=int, default=10)
    parser.add_argument("--video-bitrate", type=int, default=500000, help="H264 bit/s; start low for cellular headroom")
    parser.add_argument("--bulk-limit-mbps", type=float, default=0,
                        help="Optional shared bulk payload Mbps cap; 0 disables, control/permits remain unpaced")
    parser.add_argument("--record", action="store_true", help="Record JPEG, detections and control in --mission-dir")
    parser.add_argument("--replay", help="Loop a recorded mission directory; motors always disabled")
    parser.add_argument("--mission-dir", help="Persist detection evidence beneath this directory")
    parser.add_argument("--assistant-url", help="Local llama-server origin, e.g. http://127.0.0.1:8082")
    parser.add_argument("--whisper-url", help="Running whisper-server origin (model stays loaded), e.g. http://127.0.0.1:8091")
    parser.add_argument("--whisper-bin")
    parser.add_argument("--whisper-model")
    parser.add_argument("--piper-bin")
    parser.add_argument("--piper-model")
    parser.add_argument("--piper-model-es", help="Spanish Piper .onnx voice for survivor phrases")
    parser.add_argument("--speaker-cmd", default="auto",
                        help="Rover speaker player, e.g. 'aplay -q' or 'paplay'; auto probes ALSA/Pulse; none disables")
    parser.add_argument("--triage-interval", type=float, default=3.0, help="Minimum seconds between SBAR model calls")
    parser.add_argument("--tts-dir", default="/tmp/farsight-tts", help="Bounded locally generated speech replies")
    args = parser.parse_args()
    try:
        BulkPacer(args.bulk_limit_mbps)
    except ValueError as exc:
        parser.error(str(exc))
    if args.record and not args.mission_dir:
        parser.error("--record requires --mission-dir")
    if args.replay and (args.serial_port or args.camera is not None or args.demo or args.simulate_rover or args.record):
        parser.error("Replay cannot be combined with serial, live/simulated sources, or recording")
    if args.simulate_rover and args.serial_port:
        parser.error("--simulate-rover cannot be combined with --serial-port")
    if args.demo and args.serial_port:
        parser.error("A simulated camera cannot be used to drive physical motors")
    if args.demo and args.camera:
        parser.error("Choose --demo or a real --camera, not both")
    uvicorn.run(create_app(args.demo, camera_device=args.camera, serial_port=args.serial_port,
                simulate_rover=args.simulate_rover, detector=args.detector, detector_model=args.detector_model,
                h264=args.h264, mission_dir=args.mission_dir, max_pwm=args.max_pwm, min_pwm=args.min_pwm,
                video_fps=args.video_fps, video_bitrate=args.video_bitrate, assistant_url=args.assistant_url,
                whisper_bin=args.whisper_bin, whisper_model=args.whisper_model, piper_bin=args.piper_bin,
                piper_model=args.piper_model, tts_dir=args.tts_dir, face_model=args.face_model,
                whisper_url=args.whisper_url, piper_model_es=args.piper_model_es,
                speaker_cmd=args.speaker_cmd, triage_interval=args.triage_interval,
                depth_model=args.depth_model, scan_pan=args.scan_pan, scan_fov=args.scan_fov,
                scan_intrinsics=args.scan_intrinsics, scan_turn_dps=args.scan_turn_dps,
                record=args.record, replay=args.replay, bulk_limit_mbps=args.bulk_limit_mbps,
                drive_profile=args.drive_profile, allow_calibration=args.allow_calibration), host=args.host, port=args.port,
                ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
                ws_max_size=8 * 1024 * 1024, access_log=False,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
