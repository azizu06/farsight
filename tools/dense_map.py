"""Mac retained measured cloud and bounded same-origin Quest view chunks.

Use mount(app, upstream, ca_file) before the operator relay starts, or run
python -m tools.dense_map --upstream https://jetson.example:8443 --port 8080.
The normal operator relay file is deliberately not modified.
"""

import argparse
import asyncio
from contextlib import suppress
import gzip
import json
import math
import ssl
import struct
import time
import uuid

import aiohttp
from aiohttp import web
from jetson.mapping.compress import negotiate, encode, brotli
from jetson.mapping.dense import DenseMap, RECORD, MAX_RECORDS, packet, unpack

CHUNK_MM = 2000
VIEW_CHUNKS = 32
DENSE_PATHS = frozenset(("/dense/status", "/dense/metrics", "/dense/manifest",
                         "/dense/chunk.bin", "/dense/delta.bin"))
READ_ERRORS = (aiohttp.ClientError, OSError, ValueError, KeyError, TypeError,
               asyncio.TimeoutError) + ((brotli.error,) if brotli else ())


class MacCloud(DenseMap):
    def __init__(self, retained_history=False, **kwargs):
        self.retained_history = retained_history
        if retained_history:
            kwargs["ttl"] = 86400  # compatible legacy metadata; history is session-retained
        super().__init__(capacity=5_000_000, **kwargs)
        self.chunks = {}
        self.chunk_revisions = {}
        self.view_cache = {}

    def expire(self):
        if not self.retained_history:
            return super().expire()
        # Snapshot pages still have a short lifetime; observed room history does
        # not. Source epoch resets and the capacity policy own its lifecycle.
        with self.lock:
            for saved in list(self.snapshots):
                if self.clock() - self.snapshot_times[saved] > 30:
                    del self.snapshots[saved]
                    del self.snapshot_times[saved]

    def apply(self, records):
        if self.retained_history:
            # RGB-D producer tombstones represent its expiring visual cache.
            # They do not erase samples already observed by the Mac. Native
            # geometry uses a separate cloud and retains its explicit deletes.
            records = (record for record in records if record[6] != 0)
        return super().apply(records)

    def delta(self, *args, **kwargs):
        body = super().delta(*args, **kwargs)
        if not self.retained_history:
            return body
        size = struct.unpack_from("<I", body)[0]
        metadata = json.loads(body[4:4 + size])
        metadata["retained_history"] = True
        return packet(metadata, (body[4 + size:],))

    def clear(self, source=None):
        super().clear(source)
        self.chunks.clear()
        self.chunk_revisions.clear()
        self.view_cache.clear()

    def _change(self, record):
        revision = super()._change(record)
        values = RECORD.unpack(record)
        chunk = tuple(v // CHUNK_MM for v in values[:3])
        key = tuple(v // 10 for v in values[:3])
        if values[6]:
            self.chunks.setdefault(chunk, set()).add(key)
        elif chunk in self.chunks:
            self.chunks[chunk].discard(key)
            if not self.chunks[chunk]:
                del self.chunks[chunk]
        self.chunk_revisions[chunk] = revision
        self.view_cache.pop(chunk, None)
        return revision

    def view_chunk(self, chunk, lod):
        with self.lock:
            revision = self.chunk_revisions.get(chunk, 0)
            cached = self.view_cache.get(chunk)
            if cached and cached[:2] == (revision, lod):
                return cached[2]
            # LOD chooses existing measured samples, never interpolated points.
            keys = sorted(self.chunks.get(chunk, ()))
            step = 1 << lod
            while True:
                samples = {}
                for key in keys:
                    coarse = tuple(v // step for v in key)
                    if coarse not in samples and key in self.cells:
                        samples[coarse] = self.cells[key][1]
                if len(samples) <= MAX_RECORDS:
                    break
                step *= 2
            body = packet(dict(format="xyz_mm_rgb8_op", epoch=self.epoch,
                               chunk=list(chunk), revision=revision, lod=lod,
                               sample_voxel_m=step * .01, points=len(samples)), samples.values())
            self.view_cache[chunk] = (revision, lod, body)
            return body

    def manifest(self, position):
        with self.lock:
            self.expire()
            nearest = sorted(self.chunks, key=lambda c: sum(
                ((c[i] + .5) * 2 - position[i]) ** 2 for i in range(3)))[:VIEW_CHUNKS]
            chunks = []
            for chunk in nearest:
                distance = math.sqrt(sum(((chunk[i] + .5) * 2 - position[i]) ** 2 for i in range(3)))
                if distance > 30:
                    continue
                lod = 0 if distance < 4 else 1 if distance < 10 else 2
                chunks.append(dict(id="_".join(map(str, chunk)), revision=self.chunk_revisions[chunk], lod=lod))
            # Keep only selected cached LOD packets, not duplicates of the entire cloud.
            selected = {tuple(map(int, c["id"].split("_"))) for c in chunks}
            self.view_cache = {k: v for k, v in self.view_cache.items() if k in selected}
            return dict(epoch=self.epoch, source=self.source, points=len(self.cells),
                        chunks=chunks, point_budget=VIEW_CHUNKS * MAX_RECORDS, ttl_s=self.ttl,
                        retained_history=self.retained_history)


class DenseAccumulator:
    def __init__(self, upstream, ca_file=None, geometry_upstream=None):
        from .operator_relay import upstream_origin
        self.upstream = upstream_origin(upstream)
        self.tls = ssl.create_default_context(cafile=ca_file) if ca_file else True
        self.cloud = MacCloud(retained_history=True)
        from .dense_geometry import GeometryConsumer
        from .geometry_relay import geometry_origin
        self.geometry_origin = geometry_origin(geometry_upstream) if geometry_upstream else None
        self.geometry_cloud = MacCloud(ttl=86400)
        self.geometry = GeometryConsumer(self.geometry_cloud)
        self.geometry_error = None
        self.geometry_retained = None
        self.geometry_observed = None
        self.geometry_poll = None
        self.cursor = dict(after=0)
        self.upstream_epoch = None
        self.jetson_points = self.bytes = self.received_points = 0
        self.started = time.monotonic()
        self.last_update = 0
        self.error = "Waiting for dense RGB-D"
        self.quest = {}
        self.poll = None

    async def run_geometry(self):
        from .dense_geometry import decode_geometry
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            while True:
                try:
                    async with session.get(self.geometry_origin.with_path("/geometry/status"),
                            allow_redirects=False) as response:
                        response.raise_for_status()
                        status = await response.json()
                        self.geometry_retained = status.get("retained")
                        self.geometry_observed = status.get("observed")
                        identity = (str(uuid.UUID(status["session"])), int(status["epoch"])) if status.get("session") else None
                        if identity != self.geometry.identity:
                            with self.geometry_cloud.lock:
                                self.geometry_cloud.clear()
                                self.geometry.after = 0
                        if identity is None or status.get("snapshot_complete") is False:
                            await asyncio.sleep(.25)
                            continue
                    async with session.get(self.geometry_origin.with_path("/geometry/chunks"),
                            params={"after": self.geometry.after, "limit": 16},
                            headers={"Accept-Encoding": "identity"}, allow_redirects=False) as response:
                        response.raise_for_status()
                        parts, size = [], 0
                        async for part in response.content.iter_chunked(65536):
                            size += len(part)
                            if size > 64 * 1024 * 1024:
                                raise ValueError("Geometry bootstrap exceeds 64 MiB")
                            parts.append(part)
                        updates = await asyncio.to_thread(decode_geometry, b"".join(parts))
                        bootstrap = response.headers.get("X-Geometry-Truncated") == "1"
                        await asyncio.to_thread(self.geometry.apply, updates, bootstrap)
                        if "X-Geometry-Next-Seq" in response.headers:
                            self.geometry.after = int(response.headers["X-Geometry-Next-Seq"])
                        response_session = response.headers.get("X-Geometry-Session")
                        if response_session:
                            actual = (str(uuid.UUID(response_session)), int(response.headers["X-Geometry-Epoch"]))
                            if actual != identity:
                                self.geometry.after = 0  # session swapped between status and chunk read
                    self.geometry_error = None
                except (aiohttp.ClientError, OSError, ValueError, KeyError, TypeError, asyncio.TimeoutError) as exc:
                    self.geometry_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(.25)

    def selected_cloud(self, source=None):
        if source == "geometry" or (source is None and not self.cloud.cells and self.geometry_cloud.cells):
            return self.geometry_cloud, "geometry"
        return self.cloud, "rgbd"

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
                connector=aiohttp.TCPConnector(limit=2, ssl=self.tls)) as session:
            while True:
                delay = .1
                try:
                    async with session.get(self.upstream.with_path("/map/dense.bin"),
                            params=self.cursor, headers={"Accept-Encoding": "br, gzip"}) as response:
                        response.raise_for_status()
                        parts, size = [], 0
                        async for part in response.content.iter_chunked(65536):
                            size += len(part)
                            if size > 1024 * 1024:
                                raise ValueError("Dense response exceeds budget")
                            parts.append(part)
                        raw = b"".join(parts)
                        self.bytes += len(raw)
                        coding = response.headers.get("Content-Encoding")
                        if coding == "br":
                            raw = brotli.decompress(raw)
                        elif coding == "gzip":
                            raw = gzip.decompress(raw)
                        if len(raw) > 1024 * 1024:
                            raise ValueError("Decoded dense response exceeds budget")
                        metadata, records = unpack(raw)
                        await asyncio.to_thread(self.accept, metadata, records)
                        self.error = None
                        delay = .01 if metadata["more"] else .1
                except READ_ERRORS as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    delay = 1
                await asyncio.sleep(delay)

    def accept(self, metadata, records):
        with self.cloud.lock:
            # A same-epoch reset only bootstraps the producer's bounded cache
            # after a missed journal page. It cannot reconstruct older history.
            if metadata["epoch"] != self.upstream_epoch:
                self.cloud.clear(metadata.get("source"))
            self.upstream_epoch = metadata["epoch"]
            self.cloud.source = metadata.get("source")
            self.cloud.apply(records)
            self.jetson_points = metadata["points"]
            self.received_points += sum(r[6] == 1 for r in records)
            self.last_update = time.monotonic()
            self.cursor = dict(epoch=metadata["epoch"], after=metadata["cursor"],
                               snapshot="true" if metadata["snapshot"] else "false")
            if metadata.get("anchor") is not None:
                self.cursor["anchor"] = metadata["anchor"]

    def status(self):
        return dict(jetson_points=self.jetson_points, mac_points=len(self.cloud.cells),
                    retained_history=self.cloud.retained_history,
                    tailnet_bytes=self.bytes,
                    tailnet_bytes_per_s=self.bytes / max(.001, time.monotonic() - self.started),
                    age_s=time.monotonic() - self.last_update if self.last_update else None,
                    error=self.error, quest=self.quest)

    async def handle(self, request):
        if request.path == "/dense/status":
            result = self.status()
            result.update(geometry_mac_points=len(self.geometry_cloud.cells),
                          geometry_error=self.geometry_error, geometry_retained=self.geometry_retained)
            result["geometry_observed"] = self.geometry_observed
            return web.json_response(result)
        if request.path == "/dense/metrics" and request.method == "POST":
            data = await request.json()
            self.quest = {key: data[key] for key in ("points", "uploaded_points", "fps", "xr")
                          if isinstance(data.get(key), (int, float, bool))}
            self.quest["reported_at"] = time.time()
            return web.json_response({"ok": True})
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        try:
            if request.path == "/dense/manifest":
                position = tuple(float(request.query.get(k, 0)) for k in ("x", "y", "z"))
                if not all(math.isfinite(v) and abs(v) < 10000 for v in position):
                    raise ValueError("Invalid view position")
                cloud, kind = self.selected_cloud(request.query.get("source"))
                result = await asyncio.to_thread(cloud.manifest, position)
                result["source_kind"] = kind
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            if request.path == "/dense/chunk.bin":
                chunk = tuple(map(int, request.query["id"].split("_")))
                lod = int(request.query["lod"])
                if len(chunk) != 3 or not 0 <= lod <= 4:
                    raise ValueError("Invalid chunk or LOD")
                cloud, _ = self.selected_cloud(request.query.get("source"))
                body = await asyncio.to_thread(cloud.view_chunk, chunk, lod)
            elif request.path == "/dense/delta.bin":
                # Full retained detail for the Mac GPU renderer, also paginated.
                cloud, _ = self.selected_cloud(request.query.get("source", "rgbd"))
                body = await asyncio.to_thread(cloud.delta, request.query.get("epoch"),
                    int(request.query.get("after", 0)), request.query.get("snapshot") == "true",
                    MAX_RECORDS, int(request.query["anchor"]) if "anchor" in request.query else None)
            else:
                raise web.HTTPNotFound()
        except (ValueError, KeyError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        coding = negotiate(request.headers.get("Accept-Encoding"))
        headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
        if coding:
            body = await asyncio.to_thread(encode, coding, body)
            headers["Content-Encoding"] = coding
        return web.Response(body=body, content_type="application/octet-stream", headers=headers)


def mount(app, upstream, ca_file=None, geometry_upstream=None):
    """Mount before relay startup; middleware runs behind its local-origin guard."""
    accumulator = DenseAccumulator(upstream, ca_file, geometry_upstream)
    @web.middleware
    async def dense_routes(request, handler):
        if request.path.startswith("/dense/"):
            return await accumulator.handle(request)
        return await handler(request)
    app.middlewares.append(dense_routes)
    async def lifecycle(app):
        accumulator.poll = asyncio.create_task(accumulator.run())
        if accumulator.geometry_origin:
            accumulator.geometry_poll = asyncio.create_task(accumulator.run_geometry())
        yield
        accumulator.poll.cancel()
        with suppress(asyncio.CancelledError):
            await accumulator.poll
        if accumulator.geometry_poll:
            accumulator.geometry_poll.cancel()
            with suppress(asyncio.CancelledError):
                await accumulator.geometry_poll
    app.cleanup_ctx.append(lifecycle)
    return accumulator


def main():
    from .operator_relay import create_app
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ca-file")
    parser.add_argument("--bonded-upstream")
    parser.add_argument("--geometry-upstream")
    parser.add_argument("--render-upstream")
    parser.add_argument("--demo-mode", action="store_true")
    args = parser.parse_args()
    app = create_app(args.upstream, ca_file=args.ca_file,
                     bonded_upstream=args.bonded_upstream, geometry_upstream=args.geometry_upstream,
                     render_upstream=args.render_upstream, demo_mode=args.demo_mode)
    mount(app, args.upstream, args.ca_file, args.geometry_upstream)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None, shutdown_timeout=2)


if __name__ == "__main__":
    main()
