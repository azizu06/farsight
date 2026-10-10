"""FastAPI integration. Call start/close from the application's lifespan."""

import asyncio
from contextlib import suppress
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .serial_link import SerialPump
from .state import ControlState


class ControlService:
    # Permit/status publication period. The 250 ms lease is fixed; a shorter
    # period only offers a fresh permit sooner. At 40 Hz a renewal needs a
    # permit round trip under ~225 ms instead of ~200 ms (docs/link-lab.md).
    STATUS_INTERVAL = 0.025
    # Mission replay keeps its 20 Hz control sampling.
    RECORD_INTERVAL = 0.05

    def __init__(self, serial_port=None, simulate=False, max_pwm=100, camera_ready=None, min_pwm=0,
                 detections=None, proximity_stop=True, navigator=None, simulated_range=None,
                 drive_profile=None, allow_calibration=False):
        self.on_status = None
        self.state = ControlState(simulated=simulate, max_pwm=max_pwm, camera_ready=camera_ready,
                                  min_pwm=min_pwm, detections=detections, proximity_stop=proximity_stop,
                                  navigator=navigator, drive_profile=drive_profile,
                                  allow_calibration=allow_calibration)
        self.pump = SerialPump(self.state, port=serial_port, simulate=simulate, simulated_range=simulated_range)
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/control", self.websocket)
        self.router.add_api_route("/control/status", self.public_status, methods=["GET"])

    def start(self):
        self.pump.start()

    def close(self):
        self.pump.close()

    def status(self):
        return self.state.status()

    async def public_status(self):
        status = self.status()
        status.pop("session_id", None)
        return status

    async def websocket(self, ws: WebSocket):
        await ws.accept()
        session_id = self.state.begin()
        if session_id is None:
            await ws.close(code=1008, reason="Another controller owns the rover")
            return

        async def send_status():
            recorded = -float("inf")
            while True:
                started = asyncio.get_running_loop().time()
                # A congested downstream status path makes continued control
                # unsafe; closing causes a stop instead of queued status tokens.
                status = self.state.status(issue_permit=True)
                await asyncio.wait_for(ws.send_json(status), 0.1)
                if self.on_status and started - recorded >= self.RECORD_INTERVAL - 0.005:
                    recorded = started
                    await asyncio.to_thread(self.on_status, status)
                # Account for send/record time without queuing catch-up ticks.
                await asyncio.sleep(max(0, self.STATUS_INTERVAL - (asyncio.get_running_loop().time() - started)))

        async def receive_commands():
            while True:
                envelope = await ws.receive()
                if envelope["type"] == "websocket.disconnect":
                    return
                text = envelope.get("text")
                if text is None:
                    self.state.stop("binary_control_rejected")
                    await ws.close(code=1003)
                    return
                if len(text) > 2048:
                    self.state.stop("oversized_command")
                    await ws.close(code=1009)
                    return
                try:
                    message = json.loads(text)
                    self.state.accept(message, session_id)
                except (ValueError, TypeError):
                    self.state.stop("invalid_json")

        tasks = [asyncio.create_task(send_status()), asyncio.create_task(receive_commands())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, OSError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            self.state.end(session_id)
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, OSError, RuntimeError, asyncio.TimeoutError):
                    await task
            with suppress(RuntimeError, OSError, WebSocketDisconnect):
                await ws.close()
