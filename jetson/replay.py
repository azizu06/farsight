"""Timestamped mission playback. No serial link or motor control is constructed."""

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .camera import CameraFrame
from .perception import PerceptionService


class ReplayRun:
    def __init__(self, directory):
        root = Path(directory).resolve()
        manifest = json.loads((root / "replay.json").read_text())
        if manifest["version"] != 1:
            raise ValueError("Unsupported replay version")
        self.events = [json.loads(line) for line in (root / "replay.jsonl").read_text().splitlines()]
        self.events.sort(key=lambda event: event["t"])
        if not self.events or not any(event["kind"] == "frame" for event in self.events):
            raise ValueError("Replay has no recorded frames")
        for event in self.events:
            if event["kind"] == "frame":
                path = (root / event["file"]).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Replay frame escapes mission directory")
                if not path.is_file():
                    raise ValueError(f"Missing replay frame: {event["file"]}")
                event["path"] = path
        self.frame_span = 1 + max(event["frame"] for event in self.events if event["kind"] == "frame")
        self.duration = max(manifest["duration_s"], self.events[-1]["t"] + .1)

    async def play(self, publish, sleep=asyncio.sleep, clock=time.monotonic):
        while True:
            started = clock()
            publish(None)  # Clear all streams at the loop boundary.
            for event in self.events:
                await sleep(max(0, started + event["t"] - clock()))
                publish(event)
            await sleep(max(0, started + self.duration - clock()))


class ReplayPerception(PerceptionService):
    def __init__(self, camera):
        super().__init__(camera, mode="replay")

    async def start(self):
        pass  # Recorded inference only; never rerun a detector.


class ReplayControl:
    def __init__(self):
        self.packet = {}
        self.router = APIRouter()
        self.router.add_api_route("/control/status", self.public_status, methods=["GET"])
        self.router.add_api_websocket_route("/ws/control", self.websocket)

    def start(self):
        pass

    def close(self):
        pass

    def status(self):
        recorded = {k: v for k, v in self.packet.items() if k not in ("permit", "session_id")}
        return dict(recorded, type="control_status", replay=True, rover=False,
                    permit="", permit_ms=0, session_id="replay", requires_release=True,
                    reason="replay_read_only", estop=True, left_pwm=0, right_pwm=0,
                    recorded_left_pwm=self.packet.get("left_pwm", 0),
                    recorded_right_pwm=self.packet.get("right_pwm", 0))

    async def public_status(self):
        status = self.status()
        status.pop("session_id")
        return status

    async def websocket(self, ws: WebSocket):
        await ws.accept()

        async def send():
            while True:
                await asyncio.wait_for(ws.send_json(self.status()), .5)
                await asyncio.sleep(.1)

        async def discard():
            while True:
                await ws.receive_text()  # Commands are intentionally never executed.

        tasks = [asyncio.create_task(send()), asyncio.create_task(discard())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            pass
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
                    await task
            with suppress(RuntimeError, OSError, WebSocketDisconnect):
                await ws.close()


class ReplaySession:
    def __init__(self, directory, camera, perception, control):
        self.loop = 0
        self.run = ReplayRun(directory)
        self.camera, self.perception, self.control = camera, perception, control

    def publish(self, event):
        if event is None:
            self.loop += 1
            self.camera.phone_rgb = None
            self.control.packet = {}
            self.perception.packet = dict(ts=0, frame=-1, fps=0, boxes=[], source="REPLAY")
        elif event["kind"] == "frame":
            self.camera.phone_rgb = CameraFrame(event["path"].read_bytes(), time.monotonic(),
                                                event["frame"] + (self.loop - 1) * self.run.frame_span, "REPLAY: " + event["source"])
        elif event["kind"] == "detections":
            self.perception.packet = dict(event["packet"], replay=True, replay_loop=self.loop,
                                           frame=event["packet"]["frame"] + (self.loop - 1) * self.run.frame_span,
                                           recorded_frame=event["packet"]["frame"], received=time.monotonic(),
                                           source="REPLAY: " + event["packet"].get("source", "unknown"))
        elif event["kind"] == "control":
            self.control.packet = event["packet"]
