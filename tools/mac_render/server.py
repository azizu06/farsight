"""Mac visual offload server. Renders the map, encodes H.264, never sends /ws/control.

python -m tools.mac_render --upstream http://127.0.0.1:8080 --port 8090
python -m tools.mac_render --sim-world --port 8090
"""

import argparse
import asyncio
import base64
import json
import shutil
import signal
import struct
import subprocess
import time
from contextlib import suppress, asynccontextmanager
from collections import deque
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, WSMsgType, web
from tools.operator_relay import upstream_origin

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

_UNSET = object()

_POSE_PAGE_JS = """(pose) => { window.__macPose = {
  position: pose.position, quaternion: pose.quaternion, ray: pose.ray,
  sentMonoMs: pose.sent_mono_ms, sentWallMs: pose.sent_wall_ms,
  receivedMonoMs: pose.received_mono_ms, receiptAgeMs: pose.receipt_age_ms,
  tracked: true, session_id: pose.session_id, frame_id: pose.frame_id,
  pose_id: pose.pose_id, client_nonce: pose.client_nonce,
  fov_y_deg: pose.fov_y_deg, aspect: pose.aspect, space: pose.space,
  map_revision: pose.map_revision, eyes: pose.eyes }; }"""


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
        self.viewers = set()
        self.latest_pose = None
        self.samples = []
        self._tasks = []
        self._screencast = None
        self._pose_session = None
        self._pose_seq = -1
        self._pending_pose = _UNSET
        self._pose_pump = None
        self.dropped_captures = 0
        self.last_snapshot = {}
        self.capture_times = deque(maxlen=120)
        self.fragment_times = deque(maxlen=120)

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
        def failed(task):
            if not task.cancelled() and task.exception() is not None:
                self.error = f"{type(task.exception()).__name__}: {task.exception()}"[:300]
                if self.process and self.process.returncode is None:
                    with suppress(ProcessLookupError):
                        self.process.kill()
        for task in self._tasks:
            task.add_done_callback(failed)

    def apply_pose(self, message, now_mono_ms):
        pose = parse_pose(message, now_mono_ms)
        if "session_id" in pose:
            if pose["session_id"] == self._pose_session and pose["frame_id"] <= self._pose_seq:
                raise ValueError("obsolete pose")
            self._pose_session, self._pose_seq = pose["session_id"], pose["frame_id"]
        self.latest_pose = pose if pose["tracked"] else None
        return pose

    async def _capture(self):
        await self.page.evaluate("() => { window.__macCaptureDriven = true; }")
        interval = 1 / self.fps
        while self.process and self.process.returncode is None:
            started, wall = time.monotonic(), time.time() * 1000
            # Render + pixels + provenance are returned atomically by the same
            # browser JS turn. There is one writer and no capture task backlog.
            params = await asyncio.wait_for(self.page.evaluate("() => window.__macCapture()"), 3)
            await asyncio.wait_for(self._on_frame(None, params, started * 1000, wall), 3)
            await asyncio.sleep(max(0, interval - (time.monotonic() - started)))

    async def _on_frame(self, session, params, captured=None, wall=None):
        started = time.monotonic()
        meta = params.get("meta") or await self.page.evaluate(
            "() => ({ renderMs: (window.__mac && window.__mac.renderMs) ?? 0,"
            " draws: window.__mac && window.__mac.draws || 0,"
            " source: (window.__mac && window.__mac.source) || 'WAITING',"
            " pose: window.__mac && window.__mac.poseEcho || null,"
            " map: window.__mac && window.__mac.map || null })")
        if len(params["data"]) > 6 * 1024 * 1024:
            raise ValueError("screencast exceeds capture limit")
        # Decoding several hundred KB of base64 per frame is pure CPU; keep it
        # off the event loop so websocket sends and pose handling stay prompt.
        jpeg = await asyncio.to_thread(base64.b64decode, params["data"], validate=True)
        self.last_snapshot = meta
        self.frame_id += 1
        self.samples.append({
            "frame_id": self.frame_id,
            "input_index": self.frame_id - 1,
            "capture_mono_ms": captured if captured is not None else started * 1000,
            "capture_wall_ms": wall if wall is not None else time.time() * 1000,
            "render_ms": meta.get("renderMs") or 0,
            "capture_ms": round(time.monotonic() * 1000 - (captured if captured is not None else started * 1000), 3),
            "source": meta.get("source") or "WAITING",
            "pose": meta.get("poseEcho", meta.get("pose")), "map": meta.get("mapStatus", meta.get("map")),
            "fov_y_deg": meta.get("fovY", 50), "aspect": meta.get("aspect", self.width / self.height),
        })
        del self.samples[:-600]
        self.capture_times.append(time.monotonic())
        if self.process and self.process.stdin and self.process.returncode is None:
            self.process.stdin.write(jpeg)
            await self.process.stdin.drain()

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
                # image2pipe stamps each fed image at fps regardless of CDP wall
                # cadence. Match MP4 input ordinals, never a later captured image.
                base = self.frame_id - len(self.samples)  # input_index of samples[0]
                represented = self.samples[first - base:first - base + count]
                if not count or len(represented) != count:
                    raise ValueError("fragment capture provenance unavailable")
                newest = represented[0]  # oldest represented image bounds the whole fragment
                sidecar = segment_sidecar(
                    newest["frame_id"], newest.get("pose"), newest["capture_mono_ms"],
                    newest["capture_wall_ms"], start, end, request_id=None,
                    source=newest.get("source") or "MAC RENDER",
                    encode_ms=(time.monotonic() - encode_started) * 1000,
                    render_ms=newest.get("render_ms") or 0,
                    capture_ms=newest.get("capture_ms") or 0,
                )
                sidecar["encoder"] = self.encoder
                sidecar["map"] = newest.get("map")
                sidecar["fov_y_deg"] = newest.get("fov_y_deg")
                sidecar["aspect"] = newest.get("aspect")
                sidecar["timing_scope"] = "Mac atomic render request through encoded fragment; not camera or headset age"
                sidecar["bytes"] = len(data)
                sidecar["encode_pipeline_ms"] = max(0, time.monotonic() * 1000 - newest["capture_mono_ms"])
                self.fragment_times.append((time.monotonic(), count))
                sidecar["pose_to_frame_ms"] = pose_to_frame_ms(newest.get("pose"), newest["capture_mono_ms"])
                self._publish({"sidecar": sidecar, "payload": data,
                               "keyframe": _is_keyframe(data)})

    def subscribe(self):
        """Per-viewer fragment queue; the viewer starts and restarts on a
        keyframe so it never decodes a fragment whose references it missed."""
        viewer = ViewerQueue()
        self.viewers.add(viewer)
        return viewer

    def unsubscribe(self, viewer):
        self.viewers.discard(viewer)

    def _publish(self, item):
        """Shared-encode fan-out: one copy per viewer, so a slow or paused
        client can never backpressure the encoder or other viewers."""
        for viewer in list(self.viewers):
            viewer.offer(item)

    def echo_pose(self, pose):
        """Latest-wins handoff of the newest validated pose to the render page.
        One in-flight page.evaluate at most; intermediate poses are dropped so
        72 Hz input can never queue a CDP backlog behind the render loop."""
        self._pending_pose = pose
        if self.page is None:
            return
        if self._pose_pump is None or self._pose_pump.done():
            self._pose_pump = asyncio.create_task(self._pose_loop())

    async def _pose_loop(self):
        while self._pending_pose is not _UNSET:
            pose, self._pending_pose = self._pending_pose, _UNSET
            try:
                if pose is None:
                    await self.page.evaluate("() => { window.__macPose = null; }")
                else:
                    pose["receipt_age_ms"] = max(0, time.monotonic() * 1000 - pose["received_mono_ms"])
                    await self.page.evaluate(_POSE_PAGE_JS, pose)
            except Exception:
                self._pending_pose = _UNSET
                return

    async def _stderr(self):
        while line := await self.process.stderr.readline():
            self.error = line.decode(errors="replace").strip()[:300]

    async def close(self):
        self.viewers.clear()
        if self._pose_pump is not None:
            self._pose_pump.cancel()
            with suppress(asyncio.CancelledError):
                await self._pose_pump
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._screencast is not None:
            with suppress(Exception):
                await self._screencast.send("Page.stopScreencast")
        if self.process and self.process.returncode is None:
            self.process.kill()
            with suppress(Exception):
                await asyncio.wait_for(self.process.wait(), 3)


class App:
    def __init__(self, args, browser=None, page=None):
        self.args = args
        self.browser = browser
        self.page = page
        self.encoder = None
        self.pose_socket = None
        self._videobox = None

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
            "ready": bool(encoder and encoder.init and encoder.process and encoder.process.returncode is None),
            "error": None if encoder is None else encoder.error,
            "videotoolbox": await self.videotoolbox(),
            "width": self.args.width,
            "height": self.args.height,
            "fps": self.args.fps,
            "drawn_points": encoder.last_snapshot.get("drawnPoints", 0) if encoder else 0,
            "drawn_triangles": encoder.last_snapshot.get("drawnTriangles", 0) if encoder else 0,
            "map": encoder.last_snapshot.get("mapStatus") if encoder else None,
            "dense": encoder.last_snapshot.get("dense") if encoder else None,
            "capture_fps": ((len(encoder.capture_times) - 1) / (encoder.capture_times[-1] - encoder.capture_times[0]))
                if encoder and len(encoder.capture_times) > 1 else None,
            "encoded_frame_fps": (sum(row[1] for row in list(encoder.fragment_times)[1:]) /
                (encoder.fragment_times[-1][0] - encoder.fragment_times[0][0]))
                if encoder and len(encoder.fragment_times) > 1 else None,
            "queued_fragments": sum(len(viewer.items) for viewer in encoder.viewers) if encoder else 0,
        })

    async def videotoolbox(self):
        # ffmpeg -encoders spawns a subprocess; cache it instead of paying that
        # on every health poll.
        if self._videobox is None:
            self._videobox = await asyncio.to_thread(videobox_available)
        return self._videobox

    @asynccontextmanager
    async def owned_socket(self, attribute, request):
        existing = getattr(self, attribute)
        if existing is not None:
            raise web.HTTPServiceUnavailable(text="Visual stream already has an owner")
        socket = web.WebSocketResponse(max_msg_size=16384, heartbeat=30)
        setattr(self, attribute, socket)
        try:
            await socket.prepare(request)
            yield socket
        finally:
            with suppress(Exception):
                await asyncio.wait_for(socket.close(), .25)
            if attribute == "pose_socket" and self.encoder:
                self.encoder.latest_pose = None
                self.encoder.echo_pose(None)
                if self.encoder._pose_pump:
                    with suppress(Exception):
                        await asyncio.wait_for(asyncio.shield(self.encoder._pose_pump), .5)
            if getattr(self, attribute) is socket:
                setattr(self, attribute, None)

    async def macview(self, request):
        # Multiple viewers share the one encoder; each gets its own freshness
        # queue so a slow client's socket can never stall the stream for others.
        socket = web.WebSocketResponse(max_msg_size=16384, heartbeat=30)
        await socket.prepare(request)
        encoder = self.encoder
        if encoder is None or not encoder.init:
            await socket.send_json({"type": "macview_error", "reason": "encoder not ready"})
            await socket.close()
            return socket
        queue = encoder.subscribe()
        try:
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
                if not isinstance(body, dict) or body.get("type") != "next" or type(body.get("request_id")) is not int:
                    continue
                waiting_at = time.monotonic()
                try:
                    item = await asyncio.wait_for(queue.latest(), timeout=2)
                except asyncio.TimeoutError:
                    await socket.send_json({"type": "macview_error", "reason": "no fragment"})
                    continue
                sidecar = dict(item["sidecar"], request_id=body.get("request_id"))
                sidecar["server_wait_ms"] = (time.monotonic() - waiting_at) * 1000
                sidecar["age_ms"] = max(0, time.monotonic() * 1000 - sidecar["capture_mono_ms"])
                await asyncio.wait_for(socket.send_json(sidecar), .5)
                await asyncio.wait_for(socket.send_bytes(item["payload"]), .5)
        finally:
            encoder.unsubscribe(queue)
            with suppress(Exception):
                await asyncio.wait_for(socket.close(), .25)
        return socket

    async def pose(self, request):
        async with self.owned_socket("pose_socket", request) as socket:
            async for message in socket:
                if message.type != WSMsgType.TEXT or self.encoder is None:
                    continue
                try:
                    body = json.loads(message.data)
                    pose = self.encoder.apply_pose(body, time.monotonic() * 1000)
                except (json.JSONDecodeError, ValueError) as exc:
                    await socket.send_json({"type": "pose_rejected", "reason": str(exc)})
                    continue
                self.encoder.echo_pose(pose if pose["tracked"] else None)
            return socket


def create_app(args, browser=None, page=None):
    app = App(args, browser, page)
    web_app = web.Application()
    web_app["mac"] = app
    async def resources(application):
        async with ClientSession(auto_decompress=False,
                timeout=ClientTimeout(total=60, connect=5, sock_read=15),
                connector=TCPConnector(limit=2)) as client:
            application["map_client"] = client
            try:
                yield
            finally:
                if app.encoder:
                    await app.encoder.close()
                if app.browser:
                    await app.browser.close()
                if app.page and getattr(app.page, "_playwright", None):
                    await app.page._playwright.stop()
                simulator = application.get("sim")
                if simulator and simulator.poll() is None:
                    simulator.terminate()
                    try:
                        await asyncio.to_thread(simulator.wait, 3)
                    except Exception:
                        simulator.kill()
                        await asyncio.to_thread(simulator.wait, 3)
    web_app.cleanup_ctx.append(resources)
    web_app.router.add_get("/health", app.health)
    web_app.router.add_get("/ws/macview", app.macview)
    web_app.router.add_get("/ws/macview-pose", app.pose)

    def files(root):
        async def send(request):
            raw = root / request.match_info["tail"]
            if not raw.resolve().is_relative_to(root.resolve()) or not raw.resolve().is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(raw.resolve())
        return send

    web_app.router.add_get("/render/{tail:.*}", files(HERE))
    web_app.router.add_get("/quest/{tail:.*}", files(ROOT / "quest"))
    if args.upstream:
        web_app.router.add_get("/map/{tail:.*}", proxy(args.upstream))
        web_app.router.add_get("/health/map", proxy_one(args.upstream, "/map/status"))
        web_app.router.add_get("/dense/{tail:.*}", dense_proxy(args.upstream))
    return web_app


class ViewerQueue:
    """Latest-only fragments for one viewer.

    A viewer is handed nothing until a keyframe, so it never decodes a fragment
    whose reference frames it missed. When the viewer asks for the next fragment
    and a backlog has built up (a slow start, a stall), everything before the
    newest queued keyframe is dropped: the picture jumps forward instead of the
    backlog becoming standing latency. A full queue waits for the next keyframe.

    Adopted from aziz/mac-dense-render (quest-hud handoff).
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
    Adopted from aziz/mac-dense-render.
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
    """Relay /dense/* for the render page, keeping the query and the upstream status.

    Adopted from aziz/mac-dense-render; uses the shared map_client session
    instead of a per-request blocking fetch.
    """
    async def send(request):
        url = str(upstream_origin(upstream)) + "/dense/" + request.match_info["tail"]
        if request.query_string:
            url += "?" + request.query_string
        async with request.app["map_client"].get(url,
                headers={"Accept-Encoding": "identity"}, allow_redirects=False) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 64 * 1024 * 1024:
                    raise web.HTTPBadGateway(text="Dense response exceeds bounded size")
            headers = {key: value for key, value in response.headers.items()
                       if key.lower().startswith(("x-map-", "x-geometry-", "x-dense-"))
                       or key.lower() == "content-type"}
            headers["Cache-Control"] = "no-store"
            return web.Response(status=response.status, body=bytes(body), headers=headers)
    return send


def proxy(upstream):
    async def send(request):
        return await proxy_one(upstream, "/map/" + request.match_info["tail"])(request)
    return send


def proxy_one(upstream, path):
    async def send(request):
        maximum = 65536 if path.endswith("status") else 12 * 1024 * 1024
        async with request.app["map_client"].get(str(upstream_origin(upstream)) + path,
                headers={"Accept-Encoding": "identity"}, allow_redirects=False) as response:
            if response.status != 200:
                raise web.HTTPBadGateway(text=f"Map source returned HTTP {response.status}")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise web.HTTPBadGateway(text="Map source returned unexpected encoding")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > maximum:
                    raise web.HTTPBadGateway(text="Map response exceeds bounded snapshot size")
            headers = {key: value for key, value in response.headers.items()
                       if key.lower().startswith(("x-map-", "x-geometry-")) or key.lower() == "content-type"}
            headers["Cache-Control"] = "no-store"
            return web.Response(body=bytes(body), headers=headers)
    return send


async def open_page(args):
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    executable = args.browser
    if not executable and not Path(playwright.chromium.executable_path).exists() and shutil.which("node"):
        # Reuse the frontend's already installed browser; no download at demo startup.
        result = await asyncio.to_thread(subprocess.run, ["node", "-e",
            "process.stdout.write(require('playwright').chromium.executablePath())"],
            cwd=ROOT / "quest", capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and Path(result.stdout).is_file():
            executable = result.stdout
    browser = await playwright.chromium.launch(
        headless=not args.headed,
        executable_path=executable,
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
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.sim_port <= 65535:
        parser.error("ports must be 1..65535")
    if args.upstream:
        try:
            args.upstream = str(upstream_origin(args.upstream))
        except ValueError as error:
            parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.sim_world and not args.upstream:
        args.upstream = f"http://127.0.0.1:{args.sim_port}"
    web_app = create_app(args)
    loop = asyncio.new_event_loop()

    async def boot():
        if args.sim_world:
            from .sim import launch_sim
            web_app["sim"] = await asyncio.to_thread(launch_sim, args.sim_port)
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        try:
            if not args.no_browser:
                browser, page = await open_page(args)
                web_app["mac"].browser = browser
                web_app["mac"].page = page
                await web_app["mac"].start_encoder()
            print(f"Mac view on ws://127.0.0.1:{args.port}/ws/macview (visual only, not stereo)", flush=True)
            stopped = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stopped.set)
            await stopped.wait()
        finally:
            await runner.cleanup()

    try:
        loop.run_until_complete(boot())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
