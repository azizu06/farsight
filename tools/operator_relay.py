"""Serve Quest over USB and relay the rover through a private network.

python -m tools.operator_relay --upstream https://jetson.tailnet.ts.net:8443
Plain HTTP upstream must be selected explicitly, for example for a local demo.
"""

import argparse
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import ssl

import aiohttp
from aiohttp import web
from yarl import URL

DIST = Path(__file__).resolve().parents[1] / "quest" / "dist"
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
PROXY_PREFIXES = ("/ws/", "/map/", "/tts/", "/video/")
PROXY_PATHS = {"/health", "/camera.jpg", "/depth.jpg", "/stream.mjpg", "/ask", "/phone"}


def upstream_origin(value):
    url = URL(value)
    if (url.scheme not in ("http", "https") or not url.host or url.user or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Upstream must be an http(s) origin without credentials, path, or query")
    return url.origin()


def local_url(value, upstream):
    """Only rewrite this configured origin; never turn foreign URLs into proxy paths."""
    if not isinstance(value, str):
        return value
    try:
        url = URL(value)
        if url.is_absolute() and url.origin() == upstream:
            return str(url.relative())
    except ValueError:
        pass
    return value


def rewrite_json(value, upstream):
    if isinstance(value, dict):
        return {key: rewrite_json(item, upstream) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_json(item, upstream) for item in value]
    return local_url(value, upstream)


def forward_headers(headers):
    extra = {item.strip().lower() for item in headers.get("Connection", "").split(",")}
    return {key: value for key, value in headers.items()
            if key.lower() not in HOP_HEADERS | extra}


def create_app(upstream, dist=DIST, ca_file=None, send_timeout=0.5):
    origin = upstream_origin(upstream)
    dist = Path(dist).resolve()
    tls = ssl.create_default_context(cafile=ca_file)

    @web.middleware
    async def local_only(request, handler):
        # ADB reverse arrives as localhost. Reject DNS rebinding and foreign pages
        # attempting to open control sockets on the operator machine.
        if request.url.host not in ("localhost", "127.0.0.1", "::1"):
            raise web.HTTPForbidden(text="Relay accepts localhost only")
        supplied_origin = request.headers.get("Origin")
        if supplied_origin:
            try:
                if URL(supplied_origin).origin() != request.url.origin():
                    raise web.HTTPForbidden(text="Cross-origin relay request rejected")
            except ValueError:
                raise web.HTTPForbidden(text="Invalid browser origin")
        return await handler(request)

    app = web.Application(client_max_size=8 * 1024 * 1024, middlewares=[local_only])
    app["origin"] = origin

    async def sessions(app):
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=10)
        # Separate pools: video downloads cannot exhaust the control connection pool.
        app["http"] = aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
            connector=aiohttp.TCPConnector(limit=16, ssl=tls))
        app["control"] = aiohttp.ClientSession(timeout=timeout,
            connector=aiohttp.TCPConnector(limit=4, ssl=tls))
        yield
        await app["http"].close()
        await app["control"].close()

    app.cleanup_ctx.append(sessions)

    async def relay_health(request):
        return web.json_response({"relay": True, "upstream": str(origin),
                                  "quest_built": (dist / "index.html").is_file()},
                                 headers={"Cache-Control": "no-store"})

    async def websocket(request, target):
        control = request.path == "/ws/control"
        session = app["control"] if control else app["http"]
        # Do not accept the Quest socket until the upstream socket exists.
        try:
            remote = await session.ws_connect(target, heartbeat=10,
                max_msg_size=16384 if control else 8 * 1024 * 1024,
                autoclose=True, compress=0,
                timeout=aiohttp.ClientWSTimeout(ws_close=0.25))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise web.HTTPBadGateway(text=f"Rover unavailable: {type(exc).__name__}")
        local = web.WebSocketResponse(heartbeat=10, timeout=0.25,
            max_msg_size=16384 if control else 8 * 1024 * 1024, compress=False)
        try:
            await local.prepare(request)
            deadline = min(send_timeout, 0.1) if control else send_timeout

            async def pipe(source, destination, client_to_rover=False):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        data = message.data
                        # The relay never holds or replays drive input. A stale
                        # view is a stop even if a modified client sends motion.
                        if control and client_to_rover:
                            try:
                                command = json.loads(data)
                                if not isinstance(command, dict):
                                    raise ValueError("Expected command object")
                                if command.get("deadman") is True:
                                    age = command.get("video_age_ms")
                                    if (command.get("video_fresh") is not True
                                            or isinstance(age, bool)
                                            or not isinstance(age, (int, float))
                                            or not 0 <= age <= 500):
                                        command.update(deadman=False, v=0, w=0)
                                        data = json.dumps(command)
                            except (ValueError, TypeError):
                                await local.close(code=1008, message=b"Invalid control JSON")
                                return
                        if not client_to_rover:
                            # Assistant/telemetry messages may contain absolute audio URLs.
                            with suppress(ValueError, TypeError):
                                data = json.dumps(rewrite_json(json.loads(data), origin))
                        await asyncio.wait_for(destination.send_str(data), deadline)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        if control:
                            await local.close(code=1008, message=b"Control requires JSON")
                            return
                        await asyncio.wait_for(destination.send_bytes(message.data), deadline)
                    else:
                        return

            tasks = [asyncio.create_task(pipe(local, remote, True)),
                     asyncio.create_task(pipe(remote, local))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    with suppress(aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
                        task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # Total close deadline matters: a producer continuously sending binary
            # messages can otherwise keep aiohttp's per-receive close timer alive.
            with suppress(asyncio.TimeoutError, aiohttp.ClientError, ConnectionError):
                await asyncio.wait_for(remote.close(), 0.25)
            if local.prepared:
                with suppress(asyncio.TimeoutError, ConnectionError):
                    await asyncio.wait_for(local.close(code=1011,
                        message=b"Relay link ended; reconnect stopped"), 0.25)
        return local

    async def proxy(request):
        # origin and path are constructed separately; //host cannot change destination.
        target = origin.with_path(request.rel_url.raw_path, encoded=True).with_query(request.query)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket(request, target)
        response = None
        try:
            headers = forward_headers(request.headers)
            headers = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = "identity"
            async with app["http"].request(request.method, target,
                    headers=headers, data=request.content.iter_chunked(65536),
                    allow_redirects=False) as remote:
                headers = forward_headers(remote.headers)
                headers["Cache-Control"] = "no-store"
                if "Location" in headers:
                    redirect = origin.join(URL(headers["Location"]))
                    if redirect.origin() != origin:
                        raise web.HTTPBadGateway(text="Upstream redirect would bypass USB")
                    headers["Location"] = str(redirect.relative())
                if remote.content_type == "application/json":
                    chunks, size = [], 0
                    async for chunk in remote.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise web.HTTPBadGateway(text="Upstream JSON exceeds relay limit")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    # JSON must be decoded before URL replacement, never string-replaced.
                    data = json.dumps(rewrite_json(json.loads(data), origin)).encode()
                    headers.pop("Content-Encoding", None)
                    return web.Response(body=data, status=remote.status, headers=headers)
                response = web.StreamResponse(status=remote.status, headers=headers)
                await response.prepare(request)
                async for chunk in remote.content.iter_chunked(65536):
                    await asyncio.wait_for(response.write(chunk), send_timeout)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            raise web.HTTPBadGateway(text=f"Rover unavailable: {type(exc).__name__}")

    async def dispatch(request):
        if request.path in PROXY_PATHS or request.path.startswith(PROXY_PREFIXES):
            return await proxy(request)
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ("GET", "HEAD"))
        path = (dist / request.path.lstrip("/")).resolve()
        if not path.is_relative_to(dist):
            raise web.HTTPForbidden()
        if path.is_dir():
            path = path / "index.html"
        if not path.is_file():
            raise web.HTTPNotFound(text="Build Quest first: npm --prefix quest run build")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/relay/health", relay_health)
    app.router.add_route("*", "/{path:.*}", dispatch)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="Jetson http(s) origin on Tailscale")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dist", type=Path, default=DIST)
    parser.add_argument("--ca-file", help="Trusted CA PEM for a private upstream certificate")
    args = parser.parse_args()
    try:
        app = create_app(args.upstream, args.dist, args.ca_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None,
                shutdown_timeout=2)


if __name__ == "__main__":
    main()
