"""Opt-in two-path control; camera replies require the same validated owner.

No command retries, queues or reconnects. Precontroller health is read-only;
camera GETs are rejected until an owner validates at least one route.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import math
import re

import aiohttp
from aiohttp import web
from yarl import URL
from multidict import CIMultiDict

CONNECT_TIMEOUT = 3
ATTACH_TIMEOUT = 3
SEND_TIMEOUT = .1
CLOSE_TIMEOUT = .25
MAX_GET_BODY = 2 * 1024 * 1024
MAX_HTTP_RACES = 4
MAX_COMMAND = 2048
SAFE_INTEGER = 2**53 - 1
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


@dataclass(frozen=True)
class CompleteReply:
    body: bytes
    headers: CIMultiDict
    origin: URL


@dataclass
class Path:
    origin: URL
    socket: aiohttp.ClientWebSocketResponse
    reader: asyncio.Task = None
    writer: asyncio.Task = None


def origin(value):
    url = URL(value)
    if (url.scheme not in ("http", "https") or not url.host or url.user or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Use credential-free HTTP(S) origins")
    return url.origin()


def safe_int(value):
    return type(value) is int and 0 <= value <= SAFE_INTEGER


def decode(data):
    def invalid_constant(_):
        raise ValueError("Non-finite JSON")
    value = json.loads(data, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


async def close_socket(socket):
    with suppress(aiohttp.ClientError, ConnectionError, RuntimeError, asyncio.TimeoutError):
        await asyncio.wait_for(socket.close(), CLOSE_TIMEOUT)


class BondedRelay:
    """One Quest owner and at most two authenticated paths; secrets stay private.

    No automatic reconnect: a restored path is used on a new explicitly stopped
    controller session. GET /health may race initial configured routes. Camera
    GETs require surviving attachments validated against the current owner,
    and a complete image cannot cross an owner/session change while in flight.
    """
    def __init__(self, origins, control_session, http_session=None):
        self.origins = tuple(origin(value) for value in origins)
        if len(self.origins) != 2 or self.origins[0] == self.origins[1]:
            raise ValueError("Bonded relay requires two distinct origins")
        self.control_session = control_session
        self.http_session = http_session or control_session
        self.paths = {}
        self.local = self.owner = None
        self.session_id = self.secret = None
        self.latest = None
        self.last_status_seq = self.last_command_seq = self.last_delivered_seq = -1
        self.status_lock = asyncio.Lock()
        self.disconnected = self.closed = False
        self.path_faults = 0
        self.get_tasks = set()
        self.http_races = 0
        self.writer_tasks = set()
        self.cleanup_tasks = set()

    def status(self):
        return dict(configured_paths=2, attached_paths=len(self.paths),
                    controller_active=self.owner is not None, disconnected=self.disconnected,
                    path_faults=self.path_faults, active_http_races=self.http_races, automatic_reconnect=False,
                    profile="bonded-jpeg")

    def preferred_origin(self):
        if self.closed or self.disconnected:
            raise web.HTTPServiceUnavailable(text="Bonded controller disconnected")
        if self.owner is not None:
            for route in self.origins:
                if route in self.paths:
                    return route
            raise web.HTTPServiceUnavailable(text="No validated bonded route")
        return self.origins[0]

    async def _connect(self, route):
        try:
            ws = await asyncio.wait_for(self.control_session.ws_connect(
                route.with_path("/ws/control/bonded"), heartbeat=10, max_msg_size=16384,
                compress=0, timeout=aiohttp.ClientWSTimeout(ws_close=CLOSE_TIMEOUT)), CONNECT_TIMEOUT)
            return route, ws
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            return route, None

    async def _attach(self, route, socket):
        async def exchange():
            hello = {"type": "attach", "version": 1}
            if self.secret is not None:
                hello["secret"] = self.secret
            await asyncio.wait_for(socket.send_str(json.dumps(hello)), SEND_TIMEOUT)
            message = await socket.receive()
            if message.type != aiohttp.WSMsgType.TEXT or len(message.data.encode()) > MAX_COMMAND:
                raise ValueError("Invalid attachment message")
            reply = decode(message.data)
            if (set(reply) != {"type", "version", "session_id", "secret", "path_id"}
                    or reply["type"] != "attached" or type(reply["version"]) is not int
                    or reply["version"] != 1 or not isinstance(reply["session_id"], str)
                    or not 1 <= len(reply["session_id"]) <= 256
                    or not isinstance(reply["path_id"], str) or not 1 <= len(reply["path_id"]) <= 128
                    or not isinstance(reply["secret"], str) or re.fullmatch(r"[0-9a-f]{64}", reply["secret"]) is None):
                raise ValueError("Invalid attachment reply")
            if self.session_id is not None and (reply["session_id"] != self.session_id or reply["secret"] != self.secret):
                raise ValueError("Different logical controller")
            if self.disconnected:
                raise ValueError("Logical controller already ended")
            self.session_id, self.secret = reply["session_id"], reply["secret"]
            path = Path(route, socket)
            self.paths[route] = path
            path.reader = asyncio.create_task(self._read(path))
            return True
        try:
            return await asyncio.wait_for(exchange(), ATTACH_TIMEOUT)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError, TypeError, RecursionError):
            self.path_faults += 1
            await close_socket(socket)
            return False

    def _status(self, data):
        status = decode(data)
        if (status.get("session_id") != self.session_id or not safe_int(status.get("status_seq"))
                or not isinstance(status.get("permit"), str) or not 1 <= len(status["permit"]) <= 256
                or type(status.get("permit_ms")) is not int or status["permit_ms"] != 250
                or type(status.get("rover")) is not bool or type(status.get("requires_release")) is not bool
                or any(key in status for key in ("secret", "attach_secret", "path_id"))):
            raise ValueError("Invalid bonded status")
        return status

    async def _read(self, path):
        try:
            async for message in path.socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise ValueError("Control status requires JSON")
                status = self._status(message.data)
                # At most two readers can await this lock; no status replay queue.
                async with self.status_lock:
                    if status["status_seq"] <= self.last_status_seq:
                        continue
                    self.last_status_seq = status["status_seq"]
                    encoded = json.dumps(status, allow_nan=False, separators=(",", ":"))
                    self.latest = (encoded, asyncio.get_running_loop().time(), status["status_seq"])
                    if self.local is not None and self.local.prepared and not self.local.closed:
                        try:
                            await asyncio.wait_for(self.local.send_str(encoded), SEND_TIMEOUT)
                        except (aiohttp.ClientError, OSError, RuntimeError, asyncio.TimeoutError):
                            self._end_controller()
                            return
                        self.last_delivered_seq = status["status_seq"]
        except (aiohttp.ClientError, OSError, ValueError, TypeError, RecursionError, asyncio.TimeoutError):
            pass
        finally:
            await self._lose(path)

    def _cleanup(self, operation):
        task = asyncio.create_task(operation)
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_tasks.discard)
        return task

    def _detach(self, path):
        if self.paths.pop(path.origin, None) is not path:
            return False
        self.path_faults += 1
        current = asyncio.current_task()
        for task in (path.reader, path.writer):
            if task is not None and task is not current:
                task.cancel()
        if not self.paths:
            self.disconnected = True
            if self.local is not None and self.local.prepared:
                self._cleanup(close_socket(self.local))
        return True

    async def _finish_path(self, path, excluded):
        tasks = [task for task in (path.reader, path.writer)
                 if task is not None and task is not excluded]
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_socket(path.socket)

    async def _lose(self, path):
        if self._detach(path):
            await self._finish_path(path, asyncio.current_task())

    def _end_controller(self):
        for path in tuple(self.paths.values()):
            if self._detach(path):
                self._cleanup(self._finish_path(path, asyncio.current_task()))

    def _command(self, text):
        if len(text.encode()) > MAX_COMMAND:
            raise ValueError("Oversized command")
        command = decode(text)
        if (command.get("session_id") != self.session_id or not safe_int(command.get("seq"))
                or command["seq"] <= self.last_command_seq or type(command.get("deadman")) is not bool
                or any(key in command for key in ("secret", "attach_secret", "path_id"))
                or command.get("type") == "attach"):
            raise ValueError("Invalid command")
        if command["deadman"]:
            age = command.get("video_age_ms")
            if (command.get("video_fresh") is not True or type(age) not in (int, float)
                    or not math.isfinite(age) or not 0 <= age <= 500):
                command.update(deadman=False, v=0, w=0)
        encoded = json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_COMMAND:
            raise ValueError("Oversized normalized command")
        self.last_command_seq = command["seq"]
        return encoded

    async def _send(self, path, encoded):
        try:
            # Each fresh command is attempted exactly once per currently live path.
            async with asyncio.timeout(SEND_TIMEOUT):
                await path.socket.send_str(encoded)
        except (aiohttp.ClientError, OSError, RuntimeError, asyncio.TimeoutError):
            await self._lose(path)

    async def control(self, request):
        if self.closed:
            raise web.HTTPServiceUnavailable(text="Bonded relay closed")
        if self.owner is not None:
            raise web.HTTPConflict(text="Another controller owns this relay")
        self.owner = asyncio.current_task()
        self.disconnected = False
        self.last_status_seq = self.last_command_seq = self.last_delivered_seq = -1
        self.latest = None
        candidates = []
        self.local = web.WebSocketResponse(heartbeat=10, timeout=CLOSE_TIMEOUT,
                                          max_msg_size=16384, compress=False)
        local = self.local
        try:
            # Each connected socket is attached immediately: leaving an already
            # connected secondary idle behind a slow first handshake would spend
            # its server-side attachment deadline. Both routes are tried, with
            # independent connect/attach deadlines and first-route preference.
            for configured in self.origins:
                route, socket = await self._connect(configured)
                candidates.append((route, socket))
                if socket is not None:
                    await self._attach(route, socket)
                if self.disconnected:
                    break  # never silently create a replacement owner after last loss
            if not self.paths or self.disconnected:
                raise web.HTTPBadGateway(text="No validated bonded path")
            # Startup readers retain one latest status, not seconds of permits.
            for _ in range(100):
                if self.latest is not None or not self.paths:
                    break
                await asyncio.sleep(.01)
            if self.latest is None or not self.paths:
                raise web.HTTPBadGateway(text="No bonded status")
            await local.prepare(request)
            async with self.status_lock:
                if (self.latest is not None and self.latest[2] > self.last_delivered_seq
                        and asyncio.get_running_loop().time() - self.latest[1] < SEND_TIMEOUT):
                    await asyncio.wait_for(local.send_str(self.latest[0]), SEND_TIMEOUT)
                    self.last_delivered_seq = self.latest[2]
            async for message in local:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    encoded = self._command(message.data)
                except (ValueError, TypeError, RecursionError):
                    break
                for path in tuple(self.paths.values()):
                    if path.writer is not None and not path.writer.done():
                        # Do not enqueue, replay, or wait for this path. Even a
                        # 100 ms failed send must not delay the survivor's next
                        # fresh heartbeat. Retire the pending path instead.
                        if self._detach(path):
                            self._cleanup(self._finish_path(path, asyncio.current_task()))
                        continue
                    path.writer = asyncio.create_task(self._send(path, encoded))
                    self.writer_tasks.add(path.writer)
                    path.writer.add_done_callback(self.writer_tasks.discard)
                if not self.paths:
                    break
                # A buffered client read can already contain several commands.
                # Let direct, healthy handoffs run before checking their next
                # packet; never wait for a genuinely blocked writer or queue it.
                await asyncio.sleep(0)
        except (aiohttp.ClientError, OSError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            # Closing the final authenticated path ends server ownership and its lease.
            paths = tuple(self.paths.values())
            self.paths.clear()
            readers = [path.reader for path in paths if path.reader is not None]
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            writers = tuple(self.writer_tasks)
            for task in writers:
                task.cancel()
            await asyncio.gather(*writers, return_exceptions=True)
            await asyncio.gather(*(close_socket(path.socket) for path in paths))
            await asyncio.gather(*tuple(self.cleanup_tasks), return_exceptions=True)
            await asyncio.gather(*(close_socket(socket) for _, socket in candidates if socket is not None))
            if local.prepared:
                await close_socket(local)
            self.disconnected = True
            self.session_id = self.secret = self.latest = self.local = self.owner = None
        return local

    async def _get(self, route, path):
        async def complete():
            async with self.http_session.get(route.with_path(path), allow_redirects=False,
                    headers={"Accept-Encoding": "identity", "Cache-Control": "no-store"}) as response:
                if response.status != 200:
                    raise ValueError("Non-200 response")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    if len(body) + len(chunk) > MAX_GET_BODY:
                        raise ValueError("Oversized body")
                    body.extend(chunk)
                payload = bytes(body)
                if path == "/health":
                    decode(payload)
                elif (response.content_type != "image/jpeg" or not payload.startswith(b"\xff\xd8")
                        or not payload.endswith(b"\xff\xd9")):
                    raise ValueError("Incomplete JPEG")
                connection = {part.strip().lower() for part in response.headers.get("Connection", "").split(",")}
                headers = CIMultiDict((key, value) for key, value in response.headers.items()
                                      if key.lower() not in HOP_HEADERS | connection)
                return CompleteReply(payload, headers, route)
        return await asyncio.wait_for(complete(), CONNECT_TIMEOUT)

    async def race_get(self, path):
        if path not in ("/health", "/camera.jpg"):
            raise ValueError("Only GET health/camera can be raced")
        if self.closed or self.disconnected:
            raise web.HTTPServiceUnavailable(text="Bonded controller disconnected")
        owner, session_id = self.owner, self.session_id
        if path == "/camera.jpg" and (owner is None or session_id is None or not self.paths):
            raise web.HTTPServiceUnavailable(text="Camera requires an owner-validated bonded path")
        routes = tuple(route for route in self.origins if route in self.paths) if owner is not None else self.origins
        if not routes:
            raise web.HTTPServiceUnavailable(text="No validated bonded HTTP path")
        if self.http_races >= MAX_HTTP_RACES:
            raise web.HTTPServiceUnavailable(text="Bonded GET capacity reached")
        self.http_races += 1
        tasks = [asyncio.create_task(self._get(route, path)) for route in routes]
        self.get_tasks.update(tasks)
        try:
            for done in asyncio.as_completed(tasks):
                try:
                    reply = await done
                    if self.closed or self.disconnected or (owner is not None and
                            (self.owner is not owner or self.session_id != session_id)):
                        raise web.HTTPServiceUnavailable(text="Bonded owner ended during GET")
                    if self.owner is not None and reply.origin not in self.paths:
                        continue  # route lost validation while its body was in flight
                    return reply
                except (aiohttp.ClientError, OSError, ValueError, TypeError, RecursionError, asyncio.TimeoutError):
                    continue
            raise web.HTTPBadGateway(text="No complete bonded response")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.get_tasks.difference_update(tasks)
            self.http_races -= 1

    async def close(self):
        self.closed = self.disconnected = True
        jobs = tuple(self.get_tasks)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.get_tasks.clear()
        task = self.owner
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for path in tuple(self.paths.values()):
            await self._lose(path)
