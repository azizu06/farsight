"""Mac visual offload server. Renders the map, encodes H.264, never sends /ws/control.

python -m tools.mac_render --upstream http://127.0.0.1:8080 --port 8090
python -m tools.mac_render --sim-world --port 8090
"""

import argparse
import asyncio
import base64
import json
import shutil
import struct
import time
from contextlib import suppress
from pathlib import Path

from aiohttp import WSMsgType, web

from .encode import screencast_to_ffmpeg, videobox_available
from .mp4 import Mp4Reader, fragment_timing
from .protocol import (
    oldest_of,
    parse_pose,
    pose_is_stale,
    pose_to_frame_ms,
    segment_sidecar,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


class RenderEncoder:
    def __init__(self, page, width, height, fps, bitrate, hardware, fragment_ms=None):
        self.page = page
        self.fragment_ms = fragment_ms
        self.width, self.height, self.fps, self.bitrate = width, height, fps, bitrate
        self.hardware = hardware
        self.process = None
        self.reader = Mp4Reader()
        self.init = None
        self.mime = None
        self.encoder = None
        self.error = None
        self.frame_id = 0
        self.fragments = asyncio.Queue(maxsize=8)
        self.viewers = set()
        self.keyframe = None
        self.latest_pose = None
        self.samples = []
        self._in_flight = []
        self._tasks = []
        self._screencast = None

    async def start(self):
        if not shutil.which("ffmpeg"):
            self.error = "ffmpeg unavailable"
            return
        args, self.encoder = screencast_to_ffmpeg(
            self.width, self.height, self.fps, self.bitrate, self.hardware, self.fragment_ms)
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
        # Drop the pose the Mac has already been holding past the limit, then
        # take this one. Both ends of that check are the Mac monotonic clock.
        # A message that just arrived has not been held at all.
        if self.latest_pose is not None and pose_is_stale(self.latest_pose, now_mono_ms):
            self.latest_pose = None
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
        drawn = meta.get("pose") or None
        arrived = time.monotonic()
        sample = {
            "frame_id": self.frame_id,
            "capture_mono_ms": arrived * 1000,
            "capture_wall_ms": time.time() * 1000,
            "render_ms": meta.get("renderMs") or 0,
            "capture_ms": round((time.monotonic() - started) * 1000, 3),
            "source": meta.get("source") or "WAITING",
            "pose": drawn,
        }
        self.samples.append(sample)
        del self.samples[:-600]
        if self.process and self.process.stdin and self.process.returncode is None:
            self.process.stdin.write(jpeg)
            sample["arrived_mono"] = arrived
            sample["fed_mono"] = time.monotonic()
            self._in_flight.append(sample)
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
                _first, count, start, end = fragment_timing(data, self.fps)
                covered = self._in_flight[:count]
                del self._in_flight[:count]
                # The fragment holds every frame from the oldest fed JPEG to
                # this mdat. Age is the oldest, or the picture looks newer
                # than it is. Encode time is that JPEG hitting stdin until
                # the fragment comes back out.
                oldest = oldest_of(covered, count) or {
                    "frame_id": self.frame_id, "capture_mono_ms": time.monotonic() * 1000,
                    "capture_wall_ms": time.time() * 1000, "render_ms": 0, "capture_ms": 0,
                    "source": "WAITING", "pose": None, "fed_mono": time.monotonic(),
                }
                fed = oldest.get("fed_mono") or time.monotonic()
                arrived = oldest.get("arrived_mono") or fed
                encode_ms = (time.monotonic() - fed) * 1000
                # Screenshot to fragment, including the wait to hand the JPEG
                # to the encoder. encode_ms is the encoder's own time.
                sidecar_queue_ms = (fed - arrived) * 1000
                sidecar = segment_sidecar(
                    oldest["frame_id"], oldest.get("pose"), oldest["capture_mono_ms"],
                    oldest["capture_wall_ms"], start, end, request_id=None,
                    source=oldest.get("source") or "MAC RENDER",
                    encode_ms=encode_ms,
                    render_ms=oldest.get("render_ms") or 0,
                    capture_ms=oldest.get("capture_ms") or 0,
                )
                sidecar["queue_ms"] = round(sidecar_queue_ms, 3)
                sidecar["encoder"] = self.encoder
                sidecar["bytes"] = len(data)
                sidecar["frames"] = count
                sidecar["pose_to_frame_ms"] = pose_to_frame_ms(
                    oldest.get("pose"), oldest["capture_mono_ms"])
                item = {"sidecar": sidecar, "payload": data, "keyframe": _is_keyframe(data)}
                if item["keyframe"]:
                    self.keyframe = item
                self._publish(item)

    def _publish(self, item):
        """One copy per viewer. Each viewer starts, and restarts, on a keyframe."""
        for viewer in list(self.viewers):
            viewer.offer(item)
        if self.fragments.full():
            with suppress(asyncio.QueueEmpty):
                self.fragments.get_nowait()
        with suppress(asyncio.QueueFull):
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
            self.args.bitrate, None if self.args.hardware == "auto" else self.args.hardware == "videotoolbox",
            getattr(self.args, "fragment_ms", None))
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
            # What the render page actually holds; dense points come from the relay's /dense lane.
            "page": await self._page_state(),
        })

    async def _page_state(self):
        if self.page is None:
            return None
        try:
            state = await asyncio.wait_for(self.page.evaluate(
                "() => window.__mac && { source: window.__mac.source, points: window.__mac.points,"
                " draws: window.__mac.draws, dense: window.__mac.dense || null }"), timeout=1)
        except Exception as exc:  # a busy or crashed page is reported, not hidden
            return {"error": str(exc)[:200]}
        return state

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
            "encoder": encoder.encoder,
        })
        await socket.send_bytes(encoder.init)
        queue = ViewerQueue()
        encoder.viewers.add(queue)
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
                item = await asyncio.wait_for(queue.latest(), timeout=2)
            except asyncio.TimeoutError:
                await socket.send_json({"type": "macview_error", "reason": "no fragment"})
                continue
            sidecar = dict(item["sidecar"], request_id=body.get("request_id"))
            await socket.send_json(sidecar)
            await socket.send_bytes(item["payload"])
        encoder.viewers.discard(queue)
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
                      receivedMonoMs: pose.received_mono_ms,
                      poseId: pose.pose_id, sessionId: pose.session_id,
                      fovY: pose.fov_y_deg, aspect: pose.aspect,
                      pageMonoMs: performance.now() }; }""",
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
    dist = ROOT / "quest" / "dist"
    if dist.is_dir():
        web_app.router.add_get("/", viewer_index(dist))
        web_app.router.add_get("/macview.html", viewer_index(dist))
        web_app.router.add_get("/assets/{tail:.*}", files(dist / "assets"))
    if args.upstream:
        web_app.router.add_get("/map/{tail:.*}", proxy(args.upstream))
        web_app.router.add_get("/health/map", proxy_one(args.upstream, "/health"))
        web_app.router.add_get("/dense/{tail:.*}", dense_proxy(args.upstream))
    return web_app


def viewer_index(dist):
    async def send(_request):
        page = dist / "macview.html"
        if not page.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(page)
    return send


class ViewerQueue:
    """Latest-only fragments for one viewer.

    A viewer is handed nothing until a keyframe, so it never decodes a fragment
    whose reference frames it missed. When the viewer asks for the next fragment
    and a backlog has built up (a slow start, a stall), everything before the
    newest queued keyframe is dropped: the picture jumps forward instead of the
    backlog becoming standing latency. A full queue waits for the next keyframe.
    """

    def __init__(self, limit=12):
        self.items = []
        self.limit = limit
        self.synced = False
        self.skipped = 0
        self._ready = asyncio.Event()

    def offer(self, item):
        if item["keyframe"]:
            self.synced = True
        if not self.synced:
            return
        if len(self.items) >= self.limit:
            self.skipped += len(self.items)
            self.items.clear()
            self.synced = bool(item["keyframe"])
            if not self.synced:
                return
        self.items.append(item)
        self._ready.set()

    def take(self):
        """The next decodable fragment, skipping any backlog to its newest keyframe."""
        keys = [index for index, item in enumerate(self.items) if item["keyframe"]]
        if keys and keys[-1] > 0:
            self.skipped += keys[-1]
            del self.items[:keys[-1]]
        item = self.items.pop(0)
        if not self.items:
            self._ready.clear()
        return item

    async def latest(self):
        while not self.items:
            self._ready.clear()
            await self._ready.wait()
        return self.take()


def _is_keyframe(data):
    """A fragment whose mdat carries an IDR picture, so a viewer can start decoding on it.

    ffmpeg writes MP4 samples as AVCC: 4-byte big-endian NAL lengths, not Annex B
    start codes, and default-base-is-moof says nothing about sync samples.
    """
    at = data.find(b"mdat")
    if at < 4:
        return False
    end = min(len(data), at - 4 + struct.unpack_from(">I", data, at - 4)[0])
    pos = at + 4
    while pos + 4 <= end:
        length = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if length == 0 or pos + length > end:
            return False
        if data[pos] & 0x1F == 5:
            return True
        pos += length
    return False


def dense_proxy(upstream):
    """Relay /dense/* for the render page, keeping the query and the upstream status."""
    async def send(request):
        import urllib.error
        import urllib.request
        url = f"{upstream}/dense/{request.match_info['tail']}"
        if request.query_string:
            url += "?" + request.query_string

        def read():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return response.status, response.read(), response.headers.get_content_type()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read(), "text/plain"
            except OSError as exc:
                return 503, str(exc).encode(), "text/plain"
        status, body, content_type = await asyncio.to_thread(read)
        return web.Response(status=status, body=body, content_type=content_type)
    return send


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
    query = (
        f"upstream=http://127.0.0.1:{args.port}&width={args.width}&height={args.height}"
        f"&style={args.style}&dense={0 if getattr(args, 'no_dense', False) else 1}"
    )
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
    parser.add_argument("--fragment-ms", type=int, default=None,
                        help="fMP4 fragment length; default one frame, the lowest added delay")
    parser.add_argument("--hardware", choices=("auto", "videotoolbox", "libx264"), default="auto")
    parser.add_argument("--no-browser", action="store_true", help="Serve sockets only; tests use this")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-dense", action="store_true",
                        help="Ignore the relay /dense lane and draw only the coarse preview (the before shot)")
    parser.add_argument("--browser", help="Chromium binary, if Playwright has not downloaded its own")
    parser.add_argument("--style", choices=("lit", "unlit"), default="lit",
                        help="lit is the Mac render look. unlit is the old flat look, for the before shot")
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
        print(f"Mac view on http://127.0.0.1:{args.port}/ (visual only, not stereo)", flush=True)
        print(f"stream ws://127.0.0.1:{args.port}/ws/macview", flush=True)

    try:
        loop.run_until_complete(boot())
        loop.run_forever()
    finally:
        sim = web_app.get("sim")
        if sim is not None:
            from .sim import stop_sim
            stop_sim(sim)


if __name__ == "__main__":
    main()
