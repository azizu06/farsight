"""Read-only desktop dense lab. Build first; never changes the running rig relay.

node quest/node_modules/vite/bin/vite.js build --config tools/dense_viewer/vite.config.mjs
python -m tools.dense_viewer --port 8086
"""
import argparse
import asyncio
from pathlib import Path

import aiohttp
from aiohttp import web

DENSE_READS = frozenset(("status", "manifest", "chunk.bin"))
DIST = Path(__file__).resolve().parents[1] / ".cache/dense-viewer-dist"


def create_app(upstream="http://127.0.0.1:8080", dist=DIST, raw_enabled=False):
    app = web.Application()

    async def lifecycle(app):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            app["session"] = session
            yield
    app.cleanup_ctx.append(lifecycle)

    async def dense(request):
        name = request.match_info.get("name", "frame.bin")
        capture = request.path == "/capture/frame.bin"
        if capture and not raw_enabled:
            raise web.HTTPServiceUnavailable(text="Raw capture disabled; --raw requires an approved link-budget window")
        if not capture and name not in DENSE_READS:
            raise web.HTTPNotFound()
        try:
            path = "/phone/frame.bin" if capture else f"/dense/{name}"
            async with app["session"].get(f"{upstream.rstrip('/')}{path}",
                    params=request.query, allow_redirects=False) as reply:
                limit = 8 * 1024 * 1024 if capture else 2_000_000
                parts, size = [], 0
                async for part in reply.content.iter_chunked(65536):
                    size += len(part)
                    if size > limit:
                        raise web.HTTPBadGateway(text="Dense response exceeds chunk budget")
                    parts.append(part)
                body = b"".join(parts)
                return web.Response(body=body, status=reply.status,
                                    content_type=reply.content_type, headers={k:v for k,v in reply.headers.items()
                                      if k.lower() in ("x-capture-key", "x-capture-age-ms")})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text="Dense relay unavailable")

    async def index(request):
        return web.FileResponse(Path(dist) / "index.html")

    app.router.add_get("/", index)
    app.router.add_get("/dense/{name}", dense)
    app.router.add_get("/capture/frame.bin", dense)
    app.router.add_static("/assets/", Path(dist) / "assets")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8086)
    parser.add_argument("--upstream", default="http://127.0.0.1:8080")
    parser.add_argument("--raw", action="store_true", help="Enable raw pulls in an approved link-budget window (default off)")
    args = parser.parse_args()
    web.run_app(create_app(args.upstream, raw_enabled=args.raw), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
