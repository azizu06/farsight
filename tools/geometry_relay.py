"""Opt-in local geometry assembly; Jetson remains pose/camera/control authority."""

import asyncio
from contextlib import suppress
import ipaddress
import json
import math
import struct
import time
import uuid

import aiohttp
from aiohttp import web
from yarl import URL

MAX_MESH = 12 * 1024 * 1024
MAX_JSON = 65536
IDENTITY_MATRIX = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def geometry_origin(value):
    url = URL(value)
    try:
        local = url.host == "localhost" or ipaddress.ip_address(url.host).is_loopback
    except (ValueError, TypeError):
        local = False
    if (url.scheme not in ("http", "https") or not local or url.user or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Geometry upstream must be a credential-free loopback HTTP(S) origin")
    return url.origin()


def identity(session, epoch):
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 0 <= epoch < 2**64:
        return None
    try:
        parsed = uuid.UUID(session)
        return (str(parsed), epoch) if parsed.int else None
    except (ValueError, AttributeError, TypeError):
        return None


def stats(packet):
    if not isinstance(packet, dict):
        raise ValueError("Geometry status must be an object")
    for key in ("revision", "anchors", "triangles", "last_seq"):
        value = packet.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64:
            raise ValueError("Invalid geometry counters")
    if packet["triangles"] > 160000 or packet["anchors"] > 4096:
        raise ValueError("Geometry exceeds viewer limits")
    if not isinstance(packet.get("lane"), dict):
        raise ValueError("Geometry lane status missing")
    if "snapshot_complete" in packet and not isinstance(packet["snapshot_complete"], bool):
        raise ValueError("Invalid snapshot completeness")
    return packet


def validate_mesh(body):
    if not 8 <= len(body) <= MAX_MESH:
        raise ValueError("Geometry mesh size invalid")
    vertices, indices = struct.unpack_from("<II", body)
    end = 8 + vertices * 16
    if vertices > 600000 or indices > 480000 or indices % 3 or end + indices * 4 != len(body):
        raise ValueError("Geometry mesh counts invalid")
    for x, y, z, *_ in struct.iter_unpack("<3f4B", memoryview(body)[8:end]):
        if not all(map(math.isfinite, (x, y, z))):
            raise ValueError("Non-finite mesh coordinate")
    if any(index >= vertices for index, in struct.iter_unpack("<I", memoryview(body)[end:])):
        raise ValueError("Geometry index out of bounds")
    return vertices, indices // 3


def export_ply(body):
    vertices, indices = struct.unpack_from("<II", body)
    header = ("ply\nformat binary_little_endian 1.0\ncomment ARKit reconstructed mesh; neutral display colors\n"
              f"element vertex {vertices}\nproperty float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n"
              f"element face {indices // 3}\nproperty list uchar uint vertex_indices\nend_header\n").encode()
    records = memoryview(body)[8:8 + vertices * 16]
    faces = memoryview(body)[8 + vertices * 16:]
    return header + b"".join(records[i:i + 15] for i in range(0, len(records), 16)) + \
        b"".join(b"\x03" + faces[i:i + 12] for i in range(0, len(faces), 12))


class GeometryRelay:
    def __init__(self, upstream, geometry, *, interval=.5, timeout=1.0, ssl_context=True):
        self.upstream, self.origin = upstream, geometry_origin(geometry)
        self.interval, self.timeout = interval, timeout
        self.ssl_context = ssl_context
        self.client = None
        self.task = None
        self.jetson = None
        self.jetson_error = "Waiting for Jetson map status"
        self.geometry_error = "Waiting for matching phone geometry"
        self.geometry = None
        self.published_geometry = None
        self.mesh = None
        self.mesh_key = None
        self.points = None
        self.ply = None
        self.export_task = None
        self.export_mesh = None
        self.vertices = self.triangles = 0
        self.revision = self.generation = 0
        self.source_key = None
        self.fetched_at = 0.0

    async def start(self):
        self.client = aiohttp.ClientSession(auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=self.timeout, connect=min(self.timeout, .5)),
            connector=aiohttp.TCPConnector(limit=2, ssl=self.ssl_context))
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.client:
            await self.client.close()
        if self.export_task:
            with suppress(asyncio.CancelledError):
                await self.export_task

    async def read(self, origin, path, maximum):
        async with self.client.get(origin.with_path(path), headers={"Accept-Encoding": "identity"},
                                   allow_redirects=False) as response:
            if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("Geometry/status response failed or changed encoding")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > maximum:
                    raise ValueError("Geometry/status response exceeds limit")
                chunks.append(chunk)
            return b"".join(chunks)

    async def json(self, origin, path):
        return json.loads(await self.read(origin, path, MAX_JSON))

    def clear_mesh(self):
        if self.mesh is not None:
            self.revision += 1
            # A zero/incomplete replacement must also clear old Three surfaces.
            self.generation += 1
        self.mesh = self.mesh_key = self.points = self.ply = None
        self.published_geometry = None
        self.vertices = self.triangles = 0

    async def refresh(self):
        try:
            packet = await self.json(self.upstream(), "/map/status")
            if not isinstance(packet, dict) or packet.get("type") != "status":
                raise ValueError("Jetson map status malformed")
            new_key = (identity(packet.get("session_id"), packet.get("map_epoch")), packet.get("generation"), packet.get("source"))
            if new_key != self.source_key:
                self.source_key = new_key
                self.generation += 1
                self.clear_mesh()
            self.jetson = packet
            self.fetched_at = time.monotonic()
            self.jetson_error = None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, web.HTTPException) as exc:
            self.jetson_error = f"Jetson map status unavailable: {type(exc).__name__}"
            return
        wanted = self.source_key[0]
        try:
            geo = stats(await self.json(self.origin, "/geometry/status"))
            self.geometry = geo
            actual = identity(geo.get("session"), geo.get("epoch"))
            if wanted is None or wanted != actual or self.jetson.get("scan"):
                self.clear_mesh()
                self.geometry_error = "Phone geometry session/epoch does not match Jetson pose"
                return
            if geo.get("snapshot_complete") is False:
                self.clear_mesh()
                self.geometry_error = "Phone geometry snapshot incomplete"
                return
            key = (actual, geo["revision"])
            if self.mesh_key != key:
                body = await self.read(self.origin, "/geometry/mesh.bin", MAX_MESH)
                vertices, triangles = await asyncio.to_thread(validate_mesh, body)
                after = stats(await self.json(self.origin, "/geometry/status"))
                if (identity(after.get("session"), after.get("epoch")), after["revision"]) != key:
                    self.clear_mesh()
                    self.geometry_error = "Geometry changed during download; retrying"
                    return
                if after.get("snapshot_complete") is False or triangles != after["triangles"]:
                    raise ValueError("Mesh does not match published status")
                if self.triangles and not triangles:
                    self.generation += 1
                self.geometry = after
                self.published_geometry = after
                self.mesh, self.mesh_key = body, key
                self.points = struct.pack("<I", vertices) + body[8:8 + vertices * 16]
                self.ply = None
                self.vertices, self.triangles = vertices, triangles
                self.revision += 1
            self.geometry_error = None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.geometry_error = f"Phone geometry unavailable: {type(exc).__name__}"

    async def run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)

    def status(self):
        packet = dict(self.jetson or dict(type="status", source="IPHONE LIDAR", device="Waiting for Jetson",
            session_id=None, map_epoch=None, transform=IDENTITY_MATRIX, position=[0, 0, 0], tracking="not_available",
            frames=0, rejected=0, dropped=0, processing_ms=0, paused=False, depth_range=None, age_ms=None))
        if self.jetson_error:
            packet.update(fresh=False, connected=False)
        elif isinstance(packet.get("age_ms"), (int, float)):
            packet["age_ms"] += round((time.monotonic() - self.fetched_at) * 1000)
            packet["fresh"] = packet.get("fresh", False) and packet["age_ms"] < 2000
        geo = self.geometry or {}
        published = self.published_geometry or {}
        connected = bool(geo.get("lane", {}).get("upstream") and not self.geometry_error)
        retained = bool(self.mesh is not None and (self.geometry_error or self.jetson_error or not connected))
        complete = geo.get("snapshot_complete")
        packet.update(generation=self.generation, revision=self.revision, mesh_revision=self.revision,
            points=self.vertices, triangles=self.triangles, points_transport="http",
            geometry_source="IPHONE ARKIT MESH",
            geometry=dict(source="IPHONE ARKIT MESH", matched=self.mesh is not None,
                retained=retained, connected=connected, snapshot_complete=complete,
                partial=not complete if isinstance(complete, bool) else None,
                anchors=published.get("anchors", 0),
                revision=published.get("revision"), receiver_revision=geo.get("revision"), last_seq=geo.get("last_seq"),
                reason=self.geometry_error or self.jetson_error or
                    ("Geometry lane disconnected; reconstructed mesh retained" if self.mesh is not None and not connected else
                     "Snapshot completeness not reported" if complete is None else None)))
        if self.geometry_error:
            packet["reason"] = " · ".join(filter(None, (packet.get("reason"), self.geometry_error)))
        return packet

    async def handle(self, request):
        if request.path in ("/map/reset", "/map/pause"):
            raise web.HTTPConflict(text="Phone reconstruction cannot be reset or paused here. Stop and restart phone capture to reset its AR world; native geometry commands are not available yet.")
        if request.path == "/ws/map":
            return await self.websocket(request)
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ("GET", "HEAD"))
        if request.path in ("/map/status", "/geometry/status"):
            return web.json_response(self.status(), headers={"Cache-Control": "no-store"})
        if self.mesh is None:
            if self.geometry_error and "does not match" in self.geometry_error:
                raise web.HTTPConflict(text=self.geometry_error)
            raise web.HTTPServiceUnavailable(text=self.geometry_error or "Matching geometry not available")
        if request.path in ("/map/mesh.bin", "/geometry/mesh.bin"):
            body = self.mesh
        elif request.path == "/map/points.bin":
            body = self.points
        else:
            original = self.mesh
            if self.ply is None:
                # Concurrent Export clicks share one bounded CPU job/cache.
                if self.export_task is None or self.export_task.done():
                    self.export_mesh = original
                    self.export_task = asyncio.create_task(asyncio.to_thread(export_ply, original))
                exported = await asyncio.shield(self.export_task)
                if self.export_mesh is not original:
                    raise web.HTTPConflict(text="Geometry changed during export; retry")
                if self.mesh is not original:
                    raise web.HTTPConflict(text="Geometry changed during export; retry")
                self.ply = exported
            body = self.ply
        tag = f'"geometry-{self.generation}-{self.revision}-{request.path}"'
        headers = {"Cache-Control": "no-store", "ETag": tag,
                   "X-Geometry-Source": "IPHONE ARKIT MESH", "X-Geometry-Retained": str(self.status()["geometry"]["retained"]).lower(),
                   "X-Map-Generation": str(self.generation), "X-Map-Revision": str(self.revision),
                   "X-Map-Session": self.mesh_key[0][0], "X-Map-Epoch": str(self.mesh_key[0][1])}
        if request.headers.get("If-None-Match") == tag:
            return web.Response(status=304, headers=headers)
        if request.path == "/map/export.ply":
            headers["Content-Disposition"] = 'attachment; filename="farsight-phone-mesh.ply"'
        return web.Response(body=body, content_type="application/octet-stream", headers=headers)

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=10, timeout=.25, max_msg_size=16384)
        await ws.prepare(request)
        async def send():
            revision = None
            while not ws.closed:
                packet = self.status()
                await asyncio.wait_for(ws.send_json(packet), .5)
                if request.query.get("points") != "http" and revision != self.revision:
                    await asyncio.wait_for(ws.send_bytes(self.points or struct.pack("<I", 0)), .5)
                    revision = self.revision
                await asyncio.sleep(self.interval)
        sender = asyncio.create_task(send())
        async def receive():
            async for _ in ws:
                pass
        receiver = asyncio.create_task(receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            with suppress(asyncio.TimeoutError, ConnectionError):
                await asyncio.wait_for(ws.close(), .25)
        return ws
