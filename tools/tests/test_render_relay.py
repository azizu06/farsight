"""Opaque render transport over real loopback sockets; no renderer or headset."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout, suppress
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web

from tools import demo
from tools.operator_relay import create_app, render_origin


class RenderConfigurationTests(unittest.TestCase):
    def test_loopback_only_without_credentials_paths_queries_or_fragments(self):
        for value in ("http://127.0.0.1:8485", "https://localhost:8485", "http://[::1]:8485"):
            self.assertEqual(str(render_origin(value)), value)
        for value in ("https://remote.example:8485", "http://192.168.1.2:8485", "file:///tmp/socket",
                      "http://user:secret@localhost:8485", "http://localhost:8485/path",
                      "http://localhost:8485?x=1", "http://localhost:8485#fragment"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Render upstream"):
                render_origin(value)
        with self.assertRaisesRegex(ValueError, "different port"):
            create_app("http://127.0.0.1:8485", render_upstream="http://127.0.0.1:8485")
        for alias in ("http://localhost:8485", "https://localhost:8485", "http://[::1]:8485"):
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "different port"):
                create_app("http://127.0.0.1:8485", render_upstream=alias)
        with self.assertRaisesRegex(ValueError, "different port"):
            create_app("http://127.0.0.1:8485", bonded_upstream="http://localhost:8486",
                       render_upstream="http://127.0.0.1:8486")

    def test_launcher_validates_and_forwards_flag_without_starting_renderer(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "quest/dist").mkdir(parents=True)
            (root / "quest/dist/index.html").write_text("offline")
            argv = ["demo", "--upstream", "http://127.0.0.1:18443", "--render-upstream", "http://127.0.0.1:8485"]
            with patch.object(demo, "ROOT", root), patch.object(demo, "free_port"), \
                    patch.object(sys, "argv", argv + ["--check"]), patch.object(demo.subprocess, "Popen") as spawn, \
                    redirect_stdout(StringIO()):
                demo.main()
                spawn.assert_not_called()
            child = Mock()
            child.poll.return_value = 0
            with patch.object(demo, "ROOT", root), patch.object(demo, "free_port"), \
                    patch.object(sys, "argv", argv), patch.object(demo, "wait_http"), \
                    patch.object(demo.subprocess, "Popen", return_value=child) as spawn, \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                demo.main()
            command = spawn.call_args.args[0]
            self.assertEqual(command[1:3], ["-m", "tools.operator_relay"])
            self.assertEqual(command[command.index("--render-upstream") + 1], "http://127.0.0.1:8485")
            self.assertEqual(spawn.call_count, 1, "renderer is never launched by this transport flag")
            for invalid in ("http://remote.example:8485", "http://127.0.0.1:18443", "https://localhost:18443",
                            "http://127.0.0.1:8080", "https://localhost:8080"):
                with patch.object(demo, "ROOT", root), patch.object(demo, "free_port"), \
                        patch.object(sys, "argv", argv[:-1] + [invalid, "--check"]), \
                        patch.object(demo.subprocess, "Popen") as spawn, \
                        redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                    demo.main()
                self.assertEqual(error.exception.code, 1)
                spawn.assert_not_called()


class RenderSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="farsight-render-transport-")
        self.dist = Path(self.work.name)
        (self.dist / "index.html").write_text("offline")
        self.runners = []
        self.sockets = []
        self.jetson_paths = []
        self.render_paths = []
        self.render_sockets = []
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))

        async def jetson(request):
            self.jetson_paths.append(request.path)
            socket = web.WebSocketResponse(max_msg_size=8*1024*1024)
            await socket.prepare(request)
            await socket.send_str("jetson-fixture")
            async for message in socket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await socket.send_str(message.data)
            return socket

        async def camera(request):
            self.jetson_paths.append(request.path)
            return web.Response(body=b"jetson-jpeg", content_type="image/jpeg")

        async def render(request):
            self.render_paths.append((request.path, request.query_string))
            socket = web.WebSocketResponse(max_msg_size=9*1024*1024)
            await socket.prepare(request)
            self.render_sockets.append(socket)
            await socket.send_str(' {"origin":"' + self.render_origin + '","opaque":true} ')
            async for message in socket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await socket.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await socket.send_bytes(message.data)
            return socket

        jetson_app = web.Application()
        jetson_app.router.add_get("/camera.jpg", camera)
        jetson_app.router.add_get("/ws/{name}", jetson)
        self.jetson_origin = await self.start(jetson_app)
        render_app = web.Application()
        render_app.router.add_get("/ws/macview", render)
        render_app.router.add_get("/ws/macview-pose", render)
        self.render_origin = await self.start(render_app)
        self.relay_app = create_app(self.jetson_origin, self.dist, render_upstream=self.render_origin)
        self.base = await self.start(self.relay_app)

    async def start(self, app):
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        self.runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await asyncio.gather(*(socket.close() for socket in self.sockets), return_exceptions=True)
        await self.client.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.work.cleanup()

    async def connect(self, path, base=None):
        socket = await asyncio.wait_for(self.client.ws_connect((base or self.base) + path), 1)
        self.sockets.append(socket)
        return socket

    async def test_render_origin_cannot_recurse_to_the_relay_authority(self):
        # Both authorities are loopback; reject the same port before dialing.
        port = self.render_origin.rsplit(":", 1)[1]
        async with self.client.get(self.base + "/ws/macview", headers={"Host": f"localhost:{port}"}) as response:
            self.assertEqual(response.status, 503)
            self.assertIn("points back", await response.text())
        self.assertEqual(self.render_paths, [])
        self.assertEqual(self.jetson_paths, [])

    async def test_two_reserved_endpoints_preserve_opaque_text_binary_and_query(self):
        text = ' { "deadman": true, "video_age_ms": 900, "url": "' + self.render_origin + '/asset" } '
        for path in ("/ws/macview", "/ws/macview-pose"):
            socket = await self.connect(path + "?format=test")
            welcome = await socket.receive_str()
            self.assertEqual(welcome, ' {"origin":"' + self.render_origin + '","opaque":true} ')
            await socket.send_str(text)
            self.assertEqual(await socket.receive_str(), text)
            packet = bytes(range(256)) * (8000 if path == "/ws/macview" else 40)
            await socket.send_bytes(packet)
            self.assertEqual(await socket.receive_bytes(), packet)
        self.assertEqual(self.render_paths, [("/ws/macview", "format=test"), ("/ws/macview-pose", "format=test")])
        self.assertEqual(self.jetson_paths, [])
        async with self.client.get(self.base + "/camera.jpg") as response:
            self.assertEqual(await response.read(), b"jetson-jpeg")
        self.assertEqual(self.jetson_paths, ["/camera.jpg"])

    async def test_disabled_endpoints_are_explicitly_unavailable_and_never_jetson(self):
        base = await self.start(create_app(self.jetson_origin, self.dist))
        for path in ("/ws/macview", "/ws/macview-pose"):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
                await self.client.ws_connect(base + path)
            self.assertEqual(failure.exception.status, 503)
        async with self.client.get(base + "/relay/health") as response:
            report = (await response.json())["render"]
        self.assertEqual(report, dict(configured=False, endpoints=["/ws/macview", "/ws/macview-pose"], transport_only=True))
        self.assertEqual(self.jetson_paths, [])
        self.assertEqual(self.render_paths, [])

    async def test_video_init_next_segment_binary_and_version_one_pose_are_opaque(self):
        frame = await self.connect("/ws/macview"); await frame.receive()
        native = self.render_sockets[-1]
        init = ' {"type":"video_init","codec":"avc1.fixture","source":"SIMULATED","url":"' + self.render_origin + '/reserved"} '
        await native.send_str(init); await native.send_bytes(b"synthetic-fmp4-init\x00\xff")
        self.assertEqual(await frame.receive_str(), init)
        self.assertEqual(await frame.receive_bytes(), b"synthetic-fmp4-init\x00\xff")
        request = ' { "type": "next", "request_id": 17 } '
        await frame.send_str(request); self.assertEqual(await frame.receive_str(), request)
        metadata = ' {"type":"video_segment","request_id":17,"frame":9,"age_ms":23,"source":"SIMULATED"} '
        await native.send_str(metadata); await native.send_bytes(b"synthetic-fmp4-segment\xff\x00")
        self.assertEqual(await frame.receive_str(), metadata)
        self.assertEqual(await frame.receive_bytes(), b"synthetic-fmp4-segment\xff\x00")
        pose = await self.connect("/ws/macview-pose"); await pose.receive()
        record = ' {"version":1,"head":{"position":[0,1.6,0]},"ray":{"direction":[0,0,-1]},"clock":{"client_ms":123}} '
        await pose.send_str(record)
        self.assertEqual(await pose.receive_str(), record)
        self.assertEqual(self.jetson_paths, [])

    async def test_health_reports_configuration_without_renderer_ready_claims(self):
        async with self.client.get(self.base + "/relay/health") as response:
            report = (await response.json())["render"]
        self.assertEqual(report, dict(configured=True, endpoints=["/ws/macview", "/ws/macview-pose"], transport_only=True))
        self.assertFalse(any(key in report for key in ("ready", "live", "stereo", "render_ready")))

    async def test_render_traffic_does_not_bypass_stale_view_control_guard(self):
        frame = await self.connect("/ws/macview"); await frame.receive()
        await frame.send_bytes(b"opaque-frame"); self.assertEqual(await frame.receive_bytes(), b"opaque-frame")
        control = await self.connect("/ws/control"); await control.receive()
        await control.send_json({"deadman": True, "video_fresh": True, "video_age_ms": 501,
                                 "v": .6, "w": .2, "seq": 1})
        stopped = await control.receive_json()
        self.assertEqual((stopped["deadman"], stopped["v"], stopped["w"]), (False, 0, 0))
        self.assertEqual(stopped["video_age_ms"], 501)
        self.assertEqual(self.jetson_paths, ["/ws/control"])

    async def test_origin_host_and_non_websocket_requests_do_not_reach_renderer(self):
        for headers in ({"Origin": "https://foreign.example"}, {"Host": "foreign.example"}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
                await self.client.ws_connect(self.base + "/ws/macview", headers=headers)
            self.assertEqual(failure.exception.status, 403)
        async with self.client.get(self.base + "/ws/macview") as response:
            self.assertEqual(response.status, 400)
        async with self.client.post(self.base + "/ws/macview-pose", data=b"pose") as response:
            self.assertEqual(response.status, 405)
        self.assertEqual(self.render_paths, [])
        self.assertEqual(self.jetson_paths, [])

    async def test_pose_and_frame_message_limits_are_independent(self):
        pose = await self.connect("/ws/macview-pose"); await pose.receive()
        await pose.send_bytes(b"x" * (16384 + 1))
        message = await asyncio.wait_for(pose.receive(), 1)
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
        frame = await self.connect("/ws/macview"); await frame.receive()
        # Upstream oversize is also enforced, without collecting multiple frames.
        with suppress(ConnectionError, aiohttp.ClientError):
            await self.render_sockets[-1].send_bytes(b"x" * (8*1024*1024 + 1))
        message = await asyncio.wait_for(frame.receive(), 1)
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)

    async def test_dedicated_pool_remains_available_when_jetson_pools_are_full(self):
        for path, count in (("/ws/video", 16), ("/ws/control", 4)):
            for _ in range(count):
                socket = await self.connect(path)
                self.assertEqual(await socket.receive_str(), "jetson-fixture")
        pose = await self.connect("/ws/macview-pose")
        self.assertIn("opaque", await pose.receive_str())
        await pose.send_bytes(b"pose"); self.assertEqual(await pose.receive_bytes(), b"pose")
        self.assertIsNot(self.relay_app["render"].connector, self.relay_app["http"].connector)
        self.assertIsNot(self.relay_app["render"].connector, self.relay_app["control"].connector)

    async def test_admission_is_bounded_and_close_does_not_reconnect_or_replay(self):
        for _ in range(4):
            socket = await self.connect("/ws/macview"); await socket.receive()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
            await asyncio.wait_for(self.client.ws_connect(self.base + "/ws/macview-pose"), .5)
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(len(self.render_paths), 4)
        await self.sockets[0].close()
        await asyncio.sleep(.05)
        socket = await self.connect("/ws/macview-pose"); await socket.receive()
        self.assertEqual(len(self.render_paths), 5)
        await self.render_sockets[-1].close(code=1001)
        self.assertEqual((await socket.receive()).type, aiohttp.WSMsgType.CLOSE)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.render_paths), 5, "no relay-managed reconnect")

    async def test_short_pose_send_deadline_does_not_change_frame_deadline(self):
        original = aiohttp.ClientWebSocketResponse.send_bytes

        async def blocked(connection, *args, **kwargs):
            if str(connection._response.url.origin()) == self.render_origin:
                await asyncio.Future()
            return await original(connection, *args, **kwargs)

        for path, early_timeout in (("/ws/macview-pose", None), ("/ws/macview", .25)):
            socket = await self.connect(path); await socket.receive()
            with patch.object(aiohttp.ClientWebSocketResponse, "send_bytes", blocked):
                await socket.send_bytes(b"opaque")
                if early_timeout is not None:
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(socket.receive(), early_timeout)
                message = await asyncio.wait_for(socket.receive(), .4)
            self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)


if __name__ == "__main__":
    unittest.main()
