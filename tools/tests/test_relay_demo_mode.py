"""Explicit relay-only demo bypass through loopback sockets; no rover hardware."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web

from tools import demo
from tools.operator_relay import create_app


class DemoModeConfigurationTests(unittest.TestCase):
    def test_bonded_combination_is_rejected_before_sessions_or_processes(self):
        with self.assertRaisesRegex(ValueError, "single-route"):
            create_app("http://127.0.0.1:18443", bonded_upstream="http://127.0.0.1:18444", demo_mode=True)
        with patch.object(sys, "argv", ["demo", "--upstream", "http://127.0.0.1:18443", "--demo-mode",
                                       "--bonded-upstream", "http://127.0.0.1:18444", "--check"]), \
                patch.object(demo.subprocess, "Popen") as spawn, patch.object(demo, "free_port") as ports, \
                redirect_stderr(StringIO()), self.assertRaises(SystemExit) as failure:
            demo.main()
        self.assertEqual(failure.exception.code, 1)
        spawn.assert_not_called(); ports.assert_not_called()
        with self.assertRaisesRegex(ValueError, "boolean"):
            create_app("http://127.0.0.1:18443", demo_mode="true")

    def test_launcher_forwards_only_explicit_selection_to_relay(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "quest/dist").mkdir(parents=True)
            (root / "quest/dist/index.html").write_text("offline")
            for enabled in (False, True):
                argv = ["demo", "--upstream", "http://127.0.0.1:18443"] + (["--demo-mode"] if enabled else [])
                child = Mock(); child.poll.return_value = 0
                with patch.object(demo, "ROOT", root), patch.object(demo, "free_port"), \
                        patch.object(demo, "wait_http"), patch.object(sys, "argv", argv), \
                        patch.object(demo.subprocess, "Popen", return_value=child) as spawn, \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    demo.main()
                command = spawn.call_args.args[0]
                self.assertEqual(command[1:3], ["-m", "tools.operator_relay"])
                self.assertEqual("--demo-mode" in command, enabled)
                self.assertEqual(spawn.call_count, 1)


class DemoModeSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="farsight-relay-demo-mode-")
        self.dist = Path(self.work.name); (self.dist / "index.html").write_text("offline")
        self.runners, self.sockets = [], []
        self.commands = []
        self.remote_ended = asyncio.Event()
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        upstream = web.Application()

        async def control(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"fixture": "simulated-transport", "simulated": True, "rover": False})
            try:
                async for message in socket:
                    self.commands.append(message.data)
                    await socket.send_str(message.data)
            finally:
                self.remote_ended.set()
            return socket

        upstream.router.add_get("/ws/control", control)
        self.origin = await self.start(upstream)

    async def start(self, app):
        runner = web.AppRunner(app, shutdown_timeout=.1); await runner.setup()
        self.runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await asyncio.gather(*(socket.close() for socket in self.sockets), return_exceptions=True)
        await self.client.close()
        for runner in reversed(self.runners): await runner.cleanup()
        self.work.cleanup()

    async def relay(self, **kwargs):
        return await self.start(create_app(self.origin, self.dist, **kwargs))

    async def connect(self, base, path="/ws/control"):
        socket = await self.client.ws_connect(base + path)
        self.sockets.append(socket); await socket.receive()
        return socket

    async def health(self, base):
        async with self.client.get(base + "/relay/health") as response:
            return await response.json()

    async def test_default_stale_view_zeroing_and_enabled_exact_unmodified_fields(self):
        with patch.dict(os.environ, {"FARSIGHT_DEMO_MODE": "1"}):
            default = await self.relay()
        socket = await self.connect(default)
        packets = [json.dumps(dict(seq=ordinal, deadman=True, v=.4, w=-.2,
                                   video_fresh=fresh, video_age_ms=501))
                   for ordinal, fresh in enumerate((True, False), 1)]
        for packet in packets:
            await socket.send_str(packet)
            stopped = await socket.receive_json()
            self.assertEqual((stopped["deadman"], stopped["v"], stopped["w"]), (False, 0, 0))
        self.assertFalse((await self.health(default))["demo_mode"], "no environment fallback")
        enabled = await self.relay(demo_mode=True)
        socket = await self.connect(enabled)
        for packet in packets:
            await socket.send_str(packet)
            self.assertEqual(await socket.receive_json(), json.loads(packet))
            self.assertEqual(self.commands[-1], packet, "actual age/freshness and command text are forwarded")
        self.assertTrue((await self.health(enabled))["demo_mode"])

    async def test_enabled_invalid_json_nonobject_and_binary_still_close_1008(self):
        base = await self.relay(demo_mode=True)
        for invalid in ("not-json", "[]", b"binary"):
            socket = await self.connect(base)
            if isinstance(invalid, bytes): await socket.send_bytes(invalid)
            else: await socket.send_str(invalid)
            message = await asyncio.wait_for(socket.receive(), 1)
            self.assertEqual((message.type, message.data), (aiohttp.WSMsgType.CLOSE, 1008))
        self.assertEqual(self.commands, [])

    async def test_enabled_disconnect_closes_upstream_without_replay(self):
        base = await self.relay(demo_mode=True)
        socket = await self.connect(base)
        await socket.send_json({"deadman": False, "seq": 1}); await socket.receive()
        await socket.close(); await asyncio.wait_for(self.remote_ended.wait(), 1)
        self.remote_ended.clear()
        fresh = await self.connect(base)
        await asyncio.sleep(.03)
        self.assertEqual(len(self.commands), 1)
        await fresh.close(); await asyncio.wait_for(self.remote_ended.wait(), 1)

    async def test_enabled_control_send_deadline_is_still_100_ms(self):
        base = await self.relay(demo_mode=True)
        socket = await self.connect(base)
        original = aiohttp.ClientWebSocketResponse.send_str

        async def blocked(connection, *args, **kwargs):
            if str(connection._response.url.origin()) == self.origin: await asyncio.Future()
            return await original(connection, *args, **kwargs)

        with patch.object(aiohttp.ClientWebSocketResponse, "send_str", blocked):
            await socket.send_json({"deadman": False, "seq": 1})
            message = await asyncio.wait_for(socket.receive(), .4)
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
        record = (await self.health(base))["control_closures"]["history"][-1]
        self.assertEqual((record["reason"], record["operation"]), ("send_timeout", "send"))
        self.assertLess(record["duration_ms"], 300)

    async def test_enabled_render_frame_and_pose_are_not_control_routes(self):
        renderer = web.Application()
        render_packets = []

        async def opaque(request):
            socket = web.WebSocketResponse(); await socket.prepare(request)
            await socket.send_str("opaque-fixture")
            async for message in socket:
                render_packets.append((request.path, message.data))
                if message.type == aiohttp.WSMsgType.TEXT: await socket.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY: await socket.send_bytes(message.data)
            return socket

        renderer.router.add_get("/ws/macview", opaque)
        renderer.router.add_get("/ws/macview-pose", opaque)
        render = await self.start(renderer)
        base = await self.relay(demo_mode=True, render_upstream=render)
        packet = '{"deadman":true,"v":1,"video_fresh":false,"video_age_ms":501}'
        for path in ("/ws/macview", "/ws/macview-pose"):
            socket = await self.connect(base, path); await socket.send_str(packet)
            self.assertEqual(await socket.receive_str(), packet)
            await socket.send_bytes(b"opaque"); self.assertEqual(await socket.receive_bytes(), b"opaque")
        self.assertEqual(self.commands, [])
        self.assertEqual(len(render_packets), 4)

    async def test_default_bonded_configuration_remains_available(self):
        base = await self.relay(bonded_upstream="http://127.0.0.1:1")
        packet = await self.health(base)
        self.assertFalse(packet["demo_mode"])
        self.assertIn("transport", packet)
        self.assertFalse(packet["control_closures"]["enabled"])


if __name__ == "__main__":
    unittest.main()
