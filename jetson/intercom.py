"""Bounded, opt-in full-duplex PCM relay between the Quest and capture iPhone."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2
QUEUE_FRAMES = 4


@dataclass(eq=False)
class Peer:
    role: str
    socket: WebSocket
    outgoing: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_FRAMES))


class IntercomHub:
    """One Quest and one iPhone; audio is forwarded only while Quest Talk is on."""

    def __init__(self):
        self.peers: dict[str, Peer | None] = {"phone": None, "quest": None}
        self.active = False

    def _queue(self, peer: Peer | None, payload, binary=False):
        if peer is None:
            return
        queue = peer.outgoing
        if queue.full():
            items = []
            while not queue.empty():
                items.append(queue.get_nowait())
            replace = next((index for index, item in enumerate(items) if item[0]), None)
            if replace is None:
                if binary:
                    for item in items:
                        queue.put_nowait(item)
                    return
                replace = 0
            items.pop(replace)
            for item in items:
                queue.put_nowait(item)
        queue.put_nowait((binary, payload))

    def _control(self, kind: str, value: bool):
        payload = {"type": kind, "connected" if kind == "peer" else "active": value}
        for peer in self.peers.values():
            self._queue(peer, payload)

    async def connect(self, socket: WebSocket, role: str) -> Peer | None:
        await socket.accept()
        if role not in self.peers:
            await socket.close(code=1008, reason="Use role=quest or role=phone")
            return None
        if self.peers[role] is not None:
            await socket.close(code=1013, reason=f"A {role} intercom is already connected")
            return None
        peer = Peer(role, socket)
        self.peers[role] = peer
        other = self.peers["phone" if role == "quest" else "quest"]
        self._queue(peer, {"type": "peer", "connected": other is not None})
        self._queue(peer, {"type": "talk", "active": self.active})
        if other is not None:
            self._queue(other, {"type": "peer", "connected": True})
        return peer

    def set_talk(self, peer: Peer, text: str) -> bool:
        if peer.role != "quest":
            return False
        try:
            packet = json.loads(text)
        except (TypeError, ValueError):
            return False
        if (not isinstance(packet, dict) or set(packet) != {"type", "active"}
                or packet.get("type") != "talk" or type(packet.get("active")) is not bool):
            return False
        active = packet["active"]
        if self.active == active:
            return True
        self.active = active
        if not active:
            # Drop queued PCM immediately when the user turns talk off.
            for target in self.peers.values():
                if target is None:
                    continue
                controls = []
                while not target.outgoing.empty():
                    item = target.outgoing.get_nowait()
                    if not item[0]:
                        controls.append(item)
                for item in controls[-QUEUE_FRAMES:]:
                    self._queue(target, item[1], binary=item[0])
        self._control("talk", active)
        return True

    def forward_audio(self, peer: Peer, payload: bytes) -> bool:
        if not isinstance(payload, bytes) or len(payload) != FRAME_BYTES:
            return False
        if not self.active:
            return True
        other = self.peers["phone" if peer.role == "quest" else "quest"]
        if other is not None:
            self._queue(other, payload, binary=True)
        return True

    def disconnect(self, peer: Peer):
        if self.peers.get(peer.role) is not peer:
            return
        self.peers[peer.role] = None
        was_active = self.active
        self.active = False
        other = self.peers["phone" if peer.role == "quest" else "quest"]
        self._queue(other, {"type": "peer", "connected": False})
        if was_active:
            self._queue(other, {"type": "talk", "active": False})


def intercom_router(hub: IntercomHub) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/intercom")
    async def intercom(socket: WebSocket):
        peer = await hub.connect(socket, socket.query_params.get("role", ""))
        if peer is None:
            return

        async def send_pending():
            while True:
                binary, payload = await peer.outgoing.get()
                if binary:
                    await socket.send_bytes(payload)
                else:
                    await socket.send_json(payload)

        async def receive_incoming():
            while True:
                packet = await socket.receive()
                if packet["type"] == "websocket.disconnect":
                    return
                if packet.get("bytes") is not None:
                    if not hub.forward_audio(peer, packet["bytes"]):
                        await socket.close(code=1003, reason=f"Audio must be one {FRAME_BYTES}-byte PCM frame")
                        return
                elif packet.get("text") is not None:
                    if not hub.set_talk(peer, packet["text"]):
                        await socket.close(code=1008, reason="Only Quest may change the talk state")
                        return

        sender = asyncio.create_task(send_pending())
        receiver = asyncio.create_task(receive_incoming())
        try:
            done, pending = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, OSError, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            for task in (sender, receiver):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, OSError, RuntimeError):
                    await task
            hub.disconnect(peer)

    return router
