"""Mac visual offload server. Renders the map, encodes H.264, never sends /ws/control.

python -m tools.mac_render --upstream http://127.0.0.1:8080 --port 8090
python -m tools.mac_render --sim-world --port 8090
"""

import argparse
import asyncio
import base64
import json
import shutil
import time
from contextlib import suppress
from pathlib import Path

from aiohttp import WSMsgType, web

from .encode import screencast_to_ffmpeg, videobox_available
from .mp4 import Mp4Reader, fragment_timing
from .protocol import (
    POSE_STALE_MS,
    parse_pose,
    pose_to_frame_ms,
    segment_sidecar,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


class RenderEncoder:
    def __init__(self, page, width, height, fps, bitrate, hardware):
        self.page = page
        self.width, self.height, self.fps, self.bitrate = width, height, fps, bitrate
        self.hardware = hardware
        self.process = None
        self.reader = Mp4Reader()
        self.init = None
        self.mime = None
        self.encoder = None
        self.error = None
        self.frame_id = 0
        self.fragments = asyncio.Queue(maxsize=4)
        self.latest_pose = None
        self.samples = []
        self._tasks = []
        self._screencast = None

    async def start(self):
        if not shutil.which("ffmpeg"):
            self.error = "ffmpeg unavailable"
            return
        args, self.encoder = screencast_to_ffmpeg(
            self.width, self.height, self.fps, self.bitrate, self.hardware)
        self.process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._tasks = [
            asyncio.create_task(self._capture()),
            asyncio.create_task(self._read()),
            asyncio.create_task(self._stderr()),
        ]

    def apply_pose(self, message, now_mono_ms):
        pose = parse_pose(message, now_mono_ms)
        if now_mono_ms - pose["sent_mono_ms"] > POSE_STALE_MS and pose["sent_mono_ms"] > 0:
            # Viewer clock is not our clock. Stale is judged on our receipt age only
            # when the viewer clock happens to match. Always keep the newest.
            pass
        self.latest_pose = pose
        return pose

    async def _capture(self):
        session = await self.page.context.new_cdp_session(self.page)
        self._screencast = session
        await session.send("Page.startScreencast", {
            "format": "jpeg", "quality": 80,
            "maxWidth": self.width, "maxHeight": self.height, "everyNthFrame": 1,
        })
        interval = 1 / self.fps

        def on_frame(params):
            asyncio.get_running_loop().create_task(self._on_frame(session, params))

        session.on("Page.screencastFrame", on_frame)
        while self.process and self.process.returncode is None:
            await asyncio.sleep(interval)

    async def _on_frame(self, session, params):
        started = time.monotonic()
        ack = session.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        meta = await self.page.evaluate(
            "() => ({ renderMs: (window.__mac && window.__mac.renderMs) ?? 0,"
            " draws: window.__mac && window.__mac.draws || 0,"
            " source: (window.__mac && window.__mac.source) || 'WAITING',"
            " pose: window.__mac && window.__mac.poseEcho || null })")
        jpeg = base64.b64decode(params["data"])
        self.frame_id += 1
        self.samples.append({
            "frame_id": self.frame_id,
            "capture_mono_ms": time.monotonic() * 1000,
            "capture_wall_ms": time.time() * 1000,
            "render_ms": meta.get("renderMs") or 0,
            "capture_ms": round((time.monotonic() - started) * 1000, 3),
            "source": meta.get("source") or "WAITING",
            "pose": meta.get("pose") or self._pose_echo(),
        })
        del self.samples[:-600]
        if self.process and self.process.stdin and self.process.returncode is None:
            self.process.stdin.write(jpeg)
            with suppress(Exception):
                await self.process.stdin.drain()
        with suppress(Exception):
            await ack

    def _pose_echo(self):
        pose = self.latest_pose
        if pose is None:
            return None
        return {
            "sent_mono_ms": pose["sent_mono_ms"],
            "sent_wall_ms": pose["sent_wall_ms"],
            "received_mono_ms": pose["received_mono_ms"],
        }

    async def _read(self):
        while True:
            chunk = await self.process.stdout.read(65536)
            if not chunk:
                raise RuntimeError("encoder closed")
            for kind, data in self.reader.feed(chunk):
                if kind == "init":
                    self.init = data
                    self.mime = self.reader.mime
                    continue
                encode_started = time.monotonic()
                first, count, start, end = fragment_timing(data, self.fps)
                # fragment_timing's index assumes frames were fed at `fps` from
                # t=0. CDP does not deliver that, so take the newest capture.
                newest = self.samples[-1] if self.samples else {
                    "frame_id": self.frame_id, "capture_mono_ms": time.monotonic() * 1000,
                    "capture_wall_ms": time.time() * 1000, "render_ms": 0, "capture_ms": 0,
                    "source": "WAITING", "pose": self._pose_echo(),
                }
                sidecar = segment_sidecar(
                    newest["frame_id"], newest.get("pose"), newest["capture_mono_ms"],
                    newest["capture_wall_ms"], start, end, request_id=None,
                    source=newest.get("source") or "MAC RENDER",
                    encode_ms=(time.monotonic() - encode_started) * 1000,
                    render_ms=newest.get("render_ms") or 0,
                    capture_ms=newest.get("capture_ms") or 0,
                )
                sidecar["encoder"] = self.encoder
                sidecar["bytes"] = len(data)
                sidecar["pose_to_frame_ms"] = pose_to_frame_ms(newest.get("pose"), newest["capture_mono_ms"])
                item = {"sidecar": sidecar, "payload": data}
                if self.fragments.full():
                    with suppress(asyncio.QueueEmpty):
                        self.fragments.get_nowait()
                self.fragments.put_nowait(item)

    async def _stderr(self):
        while line := await self.process.stderr.readline():
            self.error = line.decode(errors="replace").strip()[:300]

    async def close(self):
        for task in self._tasks:
            task.cancel()
        if self.process and self.process.returncode is None:
            self.process.kill()
            with suppress(Exception):
                await self.process.wait()


class App:
    def __init__(self, args, browser=None, page=None):
        self.args = args
        self.browser = browser
        self.page = page
        self.encoder = None

    async def start_encoder(self):
        if self.page is None:
            return
        self.encoder = RenderEncoder(
            self.page, self.args.width, self.args.height, self.args.fps,
            self.args.bitrate, None if self.args.hardware == "auto" else self.args.hardware == "videotoolbox")
        await self.encoder.start()

    async def health(self, _request):
        encoder = self.encoder
        return web.json_response({
            "service": "mac-render",
            "visual_only": True,
            "stereo": False,
            "label": "Mac desk, no headset",
            "encoder": None if encoder is None else encoder.encoder,
            "ready": bool(encoder and encoder.init),
            "error": None if encoder is None else encoder.error,
            "videotoolbox": videobox_available(),
            "width": self.args.width,
            "height": self.args.height,
            "fps": self.args.fps,
        })

    async def macview(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        encoder = self.encoder
        if encoder is None or not encoder.init:
            await socket.send_json({"type": "macview_error", "reason": "encoder not ready"})
            await socket.close()
            return socket
        await socket.send_json({
            "type": "video_init", "macview": "macview_init", "version": 1,
            "mime": encoder.mime, "source": "MAC RENDER",
            "width": self.args.width, "height": self.args.height,
            "fps": self.args.fps, "stereo": False,
        })
        await socket.send_bytes(encoder.init)
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                body = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            if body.get("type") != "next":
                continue
            try:
                item = await asyncio.wait_for(encoder.fragments.get(), timeout=2)
            except asyncio.TimeoutError:
                await socket.send_json({"type": "macview_error", "reason": "no fragment"})
                continue
            sidecar = dict(item["sidecar"], request_id=body.get("request_id"))
            await socket.send_json(sidecar)
            await socket.send_bytes(item["payload"])
        return socket

    async def pose(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            if message.type != WSMsgType.TEXT or self.encoder is None:
                continue
            try:
                body = json.loads(message.data)
                pose = self.encoder.apply_pose(body, time.monotonic() * 1000)
            except (json.JSONDecodeError, ValueError) as exc:
                await socket.send_json({"type": "pose_rejected", "reason": str(exc)})
                continue
            if self.page is not None:
                await self.page.evaluate(
                    """(pose) => { window.__macPose = {
                      position: pose.position, quaternion: pose.quaternion, ray: pose.ray,
                      sentMonoMs: pose.sent_mono_ms, sentWallMs: pose.sent_wall_ms,
                      receivedMonoMs: pose.received_mono_ms }; }""",
                    pose)
        return socket


def create_app(args, browser=None, page=None):
    app = App(args, browser, page)
    web_app = web.Application()
    web_app["mac"] = app
    web_app.router.add_get("/health", app.health)
    web_app.router.add_get("/ws/macview", app.macview)
    web_app.router.add_get("/ws/macview-pose", app.pose)

    def files(root):
        async def send(request):
            raw = root / request.match_info["tail"]
            if ".." in raw.parts or not raw.resolve().is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(raw.resolve())
        return send

    web_app.router.add_get("/render/{tail:.*}", files(HERE))
    web_app.router.add_get("/quest/{tail:.*}", files(ROOT / "quest"))
    if args.upstream:
        web_app.router.add_get("/map/{tail:.*}", proxy(args.upstream))
        web_app.router.add_get("/health/map", proxy_one(args.upstream, "/health"))
    return web_app


def proxy(upstream):
    async def send(request):
        return await proxy_one(upstream, "/map/" + request.match_info["tail"])(request)
    return send


def proxy_one(upstream, path):
    async def send(_request):
        import urllib.request
        def read():
            with urllib.request.urlopen(upstream + path, timeout=5) as response:
                return response.read(), response.headers.get_content_type()
        body, content_type = await asyncio.to_thread(read)
        return web.Response(body=body, content_type=content_type)
    return send


async def open_page(args):
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=not args.headed,
        executable_path=args.browser or None,
        args=["--use-angle=metal", "--enable-gpu", "--disable-background-timer-throttling",
              "--disable-renderer-backgrounding", f"--window-size={args.width},{args.height}"],
    )
    page = await browser.new_page(viewport={"width": args.width, "height": args.height})
    query = f"upstream=http://127.0.0.1:{args.port}&width={args.width}&height={args.height}"
    page.on("pageerror", lambda error: print(f"render page: {error}", flush=True))
    page.on("console", lambda message: print(f"render console: {message.text}", flush=True))
    await page.goto(f"http://127.0.0.1:{args.port}/render/render.html?{query}")
    await page.wait_for_function("() => window.__mac && (window.__mac.ready || window.__mac.error)")
    failed = await page.evaluate("() => window.__mac.error")
    if failed:
        raise RuntimeError(f"render page failed: {failed}")
    await page.wait_for_timeout(1500)
    state = await page.evaluate("() => window.__mac")
    print(f"render page: {state.get('points')} points, {state.get('draws')} draws, {state.get('source')}", flush=True)
    page._playwright = playwright
    return browser, page


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", help="Relay or Jetson origin serving /map/*.bin and /health")
    parser.add_argument("--sim-world", action="store_true", help="Launch the local simulated map on --sim-port")
    parser.add_argument("--sim-port", type=int, default=8443)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--hardware", choices=("auto", "videotoolbox", "libx264"), default="auto")
    parser.add_argument("--no-browser", action="store_true", help="Serve sockets only; tests use this")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--browser", help="Chromium binary, if Playwright has not downloaded its own")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.sim_world and not args.upstream:
        args.upstream = f"http://127.0.0.1:{args.sim_port}"
    web_app = create_app(args)
    loop = asyncio.new_event_loop()

    async def boot():
        if args.sim_world:
            from .sim import launch_sim
            web_app["sim"] = launch_sim(args.sim_port)
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        if not args.no_browser:
            browser, page = await open_page(args)
            web_app["mac"].browser = browser
            web_app["mac"].page = page
            await web_app["mac"].start_encoder()
        print(f"Mac view on ws://127.0.0.1:{args.port}/ws/macview (visual only, not stereo)", flush=True)

    loop.run_until_complete(boot())
    loop.run_forever()


if __name__ == "__main__":
    main()
