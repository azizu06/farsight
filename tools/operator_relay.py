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

from .control_closures import ControlClosures

DIST = Path(__file__).resolve().parents[1] / "quest" / "dist"
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
PROXY_PREFIXES = ("/ws/", "/map/", "/tts/", "/video/", "/mission/", "/phone/", "/nav/")
# Binary map downloads keep the client's Accept-Encoding so br/gzip crosses the cellular leg.
# Everything else asks upstream for identity because the relay decodes and rewrites JSON.
ENCODED_PATHS = {"/map/points.bin", "/map/mesh.bin", "/map/export.ply"}
PROXY_PATHS = {"/health", "/control/status", "/camera.jpg", "/depth.jpg", "/stream.mjpg", "/ask", "/phone", "/scan", "/speak"}
RENDER_PATHS = ("/ws/macview", "/ws/macview-pose")


def is_proxied(path):
    return path in PROXY_PATHS or path.startswith(PROXY_PREFIXES)


def upstream_origin(value):
    url = URL(value)
    if (url.scheme not in ("http", "https") or not url.host or url.user or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Upstream must be an http(s) origin without credentials, path, or query")
    return url.origin()


def render_origin(value, jetson_origins=()):
    from .geometry_relay import geometry_origin
    try:
        render = geometry_origin(value)
    except (ValueError, TypeError):
        raise ValueError("Render upstream must be a credential-free loopback HTTP(S) origin") from None
    for jetson in jetson_origins:
        if jetson is None:
            continue
        try:
            local_jetson = geometry_origin(str(jetson))
        except ValueError:
            continue
        # localhost/127.0.0.1/::1 may reach the same listener. Different
        # spelling or scheme must not turn reserved render paths into Jetson.
        if render.port == local_jetson.port:
            raise ValueError("Render upstream must use a different port from loopback Jetson routes")
    return render


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


def create_app(upstream, dist=DIST, ca_file=None, send_timeout=0.5, bonded_upstream=None, geometry_upstream=None,
               render_upstream=None):
    origin = upstream_origin(upstream)
    second = upstream_origin(bonded_upstream) if bonded_upstream is not None else None
    if second == origin:
        raise ValueError("Bonded upstreams must be distinct routes to the same Jetson")
    if geometry_upstream is not None:
        from .geometry_relay import geometry_origin
        geometry_upstream = geometry_origin(geometry_upstream)
    render = render_origin(render_upstream, (origin, second)) if render_upstream is not None else None
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
    control_closures = ControlClosures()
    render_connections = 0

    async def sessions(app):
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=10)
        # Separate pools: video downloads cannot exhaust the control connection pool.
        app["http"] = aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
            connector=aiohttp.TCPConnector(limit=16, ssl=tls))
        app["control"] = aiohttp.ClientSession(timeout=timeout,
            connector=aiohttp.TCPConnector(limit=4, ssl=tls))
        if render is not None:
            # Frame/pose sockets cannot consume camera or control pool slots.
            app["render"] = aiohttp.ClientSession(timeout=timeout,
                connector=aiohttp.TCPConnector(limit=4, ssl=tls))
        if second is not None:
            from .bonded_relay import BondedRelay
            app["bonded"] = BondedRelay((origin, second), app["control"], app["http"])
        if geometry_upstream is not None:
            from .geometry_relay import GeometryRelay
            app["geometry"] = GeometryRelay(
                lambda: app["bonded"].preferred_origin() if second is not None else origin,
                geometry_upstream)
            await app["geometry"].start()
        try:
            yield
        finally:
            if geometry_upstream is not None:
                await app["geometry"].close()
            if second is not None:
                await app["bonded"].close()
            if render is not None:
                await app["render"].close()
            await app["http"].close()
            await app["control"].close()

    app.cleanup_ctx.append(sessions)

    async def relay_health(request):
        packet = {"relay": True, "upstream": str(origin),
                  "quest_built": (dist / "index.html").is_file(),
                  "control_closures": control_closures.status(enabled=second is None),
                  "render": {"configured": render is not None, "endpoints": list(RENDER_PATHS),
                             "transport_only": True}}
        if second is not None:
            packet["transport"] = app["bonded"].status()
        if geometry_upstream is not None:
            packet["geometry"] = app["geometry"].status()["geometry"]
        return web.json_response(packet,
                                 headers={"Cache-Control": "no-store"})

    async def websocket(request, target, render_lane=False):
        control = request.path == "/ws/control"
        session = app["render"] if render_lane else app["control"] if control else app["http"]
        pose = render_lane and request.path == "/ws/macview-pose"
        message_limit = 16384 if control or pose else 8 * 1024 * 1024
        diagnostic = control_closures.begin() if control else None
        # Do not accept the Quest socket until the upstream socket exists.
        try:
            remote = await session.ws_connect(target, heartbeat=10,
                max_msg_size=message_limit,
                autoclose=True, compress=0,
                timeout=aiohttp.ClientWSTimeout(ws_close=0.25))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if diagnostic is not None:
                diagnostic.end("upstream", "connect_failure", exception=exc, operation="connect")
            raise web.HTTPBadGateway(text=f"{'Mac render' if render_lane else 'Rover'} unavailable: {type(exc).__name__}")
        except BaseException as exc:
            if diagnostic is not None:
                diagnostic.end("upstream", "connect_failure", exception=exc, operation="connect")
            raise
        local = web.WebSocketResponse(heartbeat=10, timeout=0.25,
            max_msg_size=message_limit, compress=False)
        try:
            try:
                await local.prepare(request)
            except BaseException as exc:
                if diagnostic is not None:
                    diagnostic.end("quest", "prepare_failure", local.close_code, remote.close_code, exc, operation="prepare")
                raise
            if diagnostic is not None:
                diagnostic.opened()
            deadline = min(send_timeout, 0.1) if control or pose else min(send_timeout, 0.5) if render_lane else send_timeout

            async def send(operation, client_to_rover):
                try:
                    await asyncio.wait_for(operation, deadline)
                except BaseException as exc:
                    if diagnostic is not None:
                        diagnostic.end("upstream" if client_to_rover else "quest",
                                       "send_timeout" if isinstance(exc, asyncio.TimeoutError) else "error",
                                       local.close_code, remote.close_code, exc, operation="send")
                    raise

            async def pipe_body(source, destination, client_to_rover=False):
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
                            except (ValueError, TypeError) as exc:
                                if diagnostic is not None:
                                    diagnostic.end("quest", "invalid_control", local.close_code, remote.close_code, exc)
                                await local.close(code=1008, message=b"Invalid control JSON")
                                return
                        if not client_to_rover and not render_lane:
                            # Assistant/telemetry messages may contain absolute audio URLs.
                            with suppress(ValueError, TypeError):
                                data = json.dumps(rewrite_json(json.loads(data), target.origin()))
                        await send(destination.send_str(data), client_to_rover)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        if control:
                            if diagnostic is not None:
                                diagnostic.end("quest" if client_to_rover else "upstream", "invalid_control",
                                               local.close_code, remote.close_code)
                            await local.close(code=1008, message=b"Control requires JSON")
                            return
                        await send(destination.send_bytes(message.data), client_to_rover)
                    else:
                        return

            async def pipe(source, destination, client_to_rover=False):
                side = "quest" if client_to_rover else "upstream"
                try:
                    await pipe_body(source, destination, client_to_rover)
                except BaseException as exc:
                    if diagnostic is not None:
                        diagnostic.end(side, "send_timeout" if isinstance(exc, asyncio.TimeoutError) else "error",
                                       local.close_code, remote.close_code, exc)
                    raise
                finally:
                    if diagnostic is not None and not diagnostic.finished:
                        error = source.exception()
                        diagnostic.end(side, "error" if error is not None or source.close_code == 1006 else "normal",
                                       local.close_code, remote.close_code, error)

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

    async def render_websocket(request):
        nonlocal render_connections
        if render is None:
            raise web.HTTPServiceUnavailable(text="Mac render transport is not configured")
        if render.port == request.url.port:
            raise web.HTTPServiceUnavailable(text="Mac render upstream points back to this relay")
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ("GET",))
        if request.headers.get("Upgrade", "").lower() != "websocket":
            raise web.HTTPBadRequest(text="Mac render endpoint requires a WebSocket upgrade")
        if render_connections >= 4:
            raise web.HTTPServiceUnavailable(text="Mac render connection limit reached")
        render_connections += 1
        try:
            target = render.with_path(request.rel_url.raw_path, encoded=True).with_query(request.query)
            return await websocket(request, target, render_lane=True)
        finally:
            render_connections -= 1

    async def proxy(request):
        # origin and path are constructed separately; //host cannot change destination.
        route = origin
        if second is not None:
            bond = app["bonded"]
            if request.path == "/ws/control" and request.headers.get("Upgrade", "").lower() == "websocket":
                return await bond.control(request)
            if request.path in ("/ws/video", "/stream.mjpg") or request.path.startswith("/video/"):
                raise web.HTTPNotImplemented(text="Bonded prototype supports complete JPEG frames only")
            if request.method == "GET" and request.path in ("/camera.jpg", "/health"):
                reply = await bond.race_get(request.path)
                headers = forward_headers(reply.headers)
                headers["Cache-Control"] = "no-store"
                if request.path == "/health":
                    packet = rewrite_json(json.loads(reply.body), reply.origin)
                    packet["relay_transport"] = bond.status()
                    headers.pop("Content-Encoding", None)
                    return web.Response(body=json.dumps(packet).encode(), headers=headers)
                return web.Response(body=reply.body, headers=headers)
            route = bond.preferred_origin()
        target = route.with_path(request.rel_url.raw_path, encoded=True).with_query(request.query)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket(request, target)
        response = None
        try:
            headers = forward_headers(request.headers)
            headers = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
            headers["Accept-Encoding"] = ((request.headers.get("Accept-Encoding") or "identity")
                                          if request.path in ENCODED_PATHS else "identity")
            async with app["http"].request(request.method, target,
                    headers=headers, data=request.content.iter_chunked(65536),
                    allow_redirects=False) as remote:
                headers = forward_headers(remote.headers)
                headers["Cache-Control"] = "no-store"
                if "Location" in headers:
                    redirect = route.join(URL(headers["Location"]))
                    if redirect.origin() != route:
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
                    data = json.dumps(rewrite_json(json.loads(data), route)).encode()
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
        if request.path in RENDER_PATHS:
            return await render_websocket(request)
        if request.path == "/ws/control/bonded":
            raise web.HTTPForbidden(text="Private upstream attachment endpoint")
        if geometry_upstream is not None and request.path in (
                "/ws/map", "/map/status", "/map/mesh.bin", "/map/points.bin",
                "/map/export.ply", "/geometry/status", "/geometry/mesh.bin", "/map/reset", "/map/pause"):
            return await app["geometry"].handle(request)
        if is_proxied(request.path):
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
    parser.add_argument("--bonded-upstream", help="Opt-in second route to the same --bonded-control Jetson; JPEG only")
    parser.add_argument("--geometry-upstream", help="Opt-in loopback Mac geometry assembler origin, e.g. http://127.0.0.1:8484")
    parser.add_argument("--render-upstream", help="Opt-in loopback Mac renderer origin; reserved frame/pose WebSockets only")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dist", type=Path, default=DIST)
    parser.add_argument("--ca-file", help="Trusted CA PEM for a private upstream certificate")
    args = parser.parse_args()
    try:
        app = create_app(args.upstream, args.dist, args.ca_file, bonded_upstream=args.bonded_upstream,
                         geometry_upstream=args.geometry_upstream, render_upstream=args.render_upstream)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None,
                shutdown_timeout=2)


if __name__ == "__main__":
    main()
