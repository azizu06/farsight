"""Real loopback control sockets; diagnostics never retain payloads or identities."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from tools.control_closures import ControlClosures
from tools.operator_relay import create_app


class ClosureMetadataTests(unittest.TestCase):
    def test_first_completion_wins_and_history_is_capped_without_private_exception_text(self):
        recorder = ControlClosures()
        first, second = recorder.begin(), recorder.begin()
        first.opened(); second.opened()
        self.assertEqual(recorder.status()["active"], 2)
        first.end("quest", "normal", 1001)
        first.end("upstream", "error", 1011, 1006, RuntimeError("private-account"))
        self.assertEqual(recorder.status()["history"][0]["first_ended_side"], "quest")
        self.assertEqual(recorder.status()["active"], 1)
        second.end("upstream", "send_timeout", exception=TimeoutError("private-session-id"), operation="send")
        self.assertEqual(recorder.status()["history"][-1]["exception_class"], "TimeoutError")
        self.assertEqual(recorder.status()["history"][-1]["operation"], "send")
        for _ in range(10):
            attempt = recorder.begin()
            attempt.end("upstream", "connect_failure", exception=ConnectionError("secret upstream URL"))
        report = recorder.status()
        self.assertEqual((report["attempts"], report["opened"], report["ended"], report["active"]), (12, 2, 2, 0))
        self.assertEqual(report["connect_failures"], 10)
        self.assertEqual(len(report["history"]), 8)
        self.assertNotIn("private", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))
        self.assertTrue(all(record["timestamp_utc"].endswith("Z") for record in report["history"]))


class ControlClosureSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="farsight-closure-diagnostics-")
        self.dist = Path(self.work.name)
        (self.dist / "index.html").write_text("offline fixture")
        self.remotes = []
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        upstream = web.Application()

        async def control(request):
            remote = web.WebSocketResponse()
            await remote.prepare(request)
            self.remotes.append(remote)
            await remote.send_json({"session_id": "private-session-id", "permit": "secret-permit"})
            async for message in remote:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await remote.send_str(message.data)
            return remote

        upstream.router.add_get("/ws/control", control)
        self.upstream_runner, self.origin = await self.start(upstream)
        self.relay_app = create_app(self.origin, self.dist)
        self.relay_runner, self.base = await self.start(self.relay_app)

    async def start(self, app):
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.client.close()
        await self.relay_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.work.cleanup()

    async def connect(self):
        socket = await self.client.ws_connect(self.base + "/ws/control")
        await asyncio.wait_for(socket.receive_json(), 1)
        self.addAsyncCleanup(socket.close)
        return socket

    async def health(self, ended=None, failures=None):
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            async with self.client.get(self.base + "/relay/health") as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                report = (await response.json())["control_closures"]
            if ((ended is None or report["ended"] == ended)
                    and (failures is None or report["connect_failures"] == failures)):
                text = json.dumps(report)
                for private in ("private-session-id", "secret-permit", self.origin, "private-close-message"):
                    self.assertNotIn(private, text)
                return report
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Control diagnostics did not reach expected counters")
            await asyncio.sleep(.01)

    async def test_remote_close_records_original_code_before_relay_cleanup(self):
        socket = await self.connect()
        self.assertEqual((await self.health())["active"], 1)
        close = asyncio.create_task(self.remotes[-1].close(code=1001, message=b"private-close-message"))
        await asyncio.wait_for(socket.receive(), 1)
        await asyncio.wait_for(close, 1)
        report = await self.health(ended=1)
        record = report["history"][-1]
        self.assertEqual((record["first_ended_side"], record["reason"], record["upstream_close_code"]),
                         ("upstream", "normal", 1001))
        self.assertEqual(record["operation"], "receive")
        self.assertIsNone(record["local_close_code"], "capture before relay-generated 1011")
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertEqual((report["opened"], report["active"], report["ended"]), (1, 0, 1))

    async def test_quest_close_and_simultaneous_reconnect_keep_counts_independent(self):
        first, second = await self.connect(), await self.connect()
        self.assertEqual((await self.health())["active"], 2)
        await first.close(code=1000, message=b"private-close-message")
        report = await self.health(ended=1)
        self.assertEqual(report["active"], 1)
        self.assertEqual(report["history"][-1]["first_ended_side"], "quest")
        self.assertEqual(report["history"][-1]["local_close_code"], 1000)
        self.assertIsNone(report["history"][-1]["upstream_close_code"])
        # A stopped JSON packet still forwards with the existing URL/JSON
        # rewriting behavior and no extra ACK.
        await second.send_str('{"deadman":false,"seq":5}')
        self.assertEqual(await second.receive_str(), '{"deadman": false, "seq": 5}')
        third = await self.connect()
        self.assertEqual((await self.health())["active"], 2)
        await asyncio.gather(second.close(), third.close())
        report = await self.health(ended=3)
        self.assertEqual((report["attempts"], report["opened"], report["active"]), (3, 3, 0))

    async def test_invalid_json_and_binary_stop_the_same_socket_without_storing_input(self):
        for ordinal, binary in enumerate((False, True), 1):
            socket = await self.connect()
            if binary:
                await socket.send_bytes(b"private-session-id secret-permit")
            else:
                await socket.send_str("private-session-id secret-permit not-json")
            message = await asyncio.wait_for(socket.receive(), 1)
            self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
            self.assertEqual(message.data, 1008)
            report = await self.health(ended=ordinal)
            record = report["history"][-1]
            self.assertEqual((record["first_ended_side"], record["reason"]), ("quest", "invalid_control"))
            self.assertIsNone(record["local_close_code"])
            self.assertEqual(record["exception_class"], None if binary else "JSONDecodeError")

    async def test_connect_failure_never_accepts_quest_and_keeps_only_exception_class(self):
        await self.upstream_runner.cleanup()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
            await self.client.ws_connect(self.base + "/ws/control")
        self.assertEqual(failure.exception.status, 502)
        report = await self.health(failures=1)
        self.assertEqual((report["attempts"], report["opened"], report["active"], report["ended"]), (1, 0, 0, 0))
        record = report["history"][-1]
        self.assertEqual((record["first_ended_side"], record["reason"], record["exception_class"]),
                         ("upstream", "connect_failure", "ClientConnectorError"))
        self.assertEqual(record["operation"], "connect")

    async def test_send_timeout_identifies_failed_destination_and_preserves_deadline(self):
        socket = await self.connect()
        original = aiohttp.ClientWebSocketResponse.send_str

        async def blocked_upstream(connection, *args, **kwargs):
            if str(connection._response.url.origin()) == self.origin:
                await asyncio.Future()  # deterministic write stall; real sockets remain open
            return await original(connection, *args, **kwargs)

        with patch.object(aiohttp.ClientWebSocketResponse, "send_str", blocked_upstream):
            await socket.send_json({"deadman": False, "seq": 1})
            message = await asyncio.wait_for(socket.receive(), 1)
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
        report = await self.health(ended=1)
        record = report["history"][-1]
        self.assertEqual((record["first_ended_side"], record["operation"], record["reason"]),
                         ("upstream", "send", "send_timeout"))
        self.assertEqual(record["exception_class"], "TimeoutError")
        self.assertIsNone(record["local_close_code"])
        self.assertIsNone(record["upstream_close_code"])
        # Default send_timeout is 500 ms, but control retains its 100 ms cap.
        self.assertLess(record["duration_ms"], 300)

    async def test_history_bound_is_observed_through_real_sockets(self):
        for ordinal in range(12):
            socket = await self.connect()
            await socket.close()
            await self.health(ended=ordinal + 1)
        report = await self.health(ended=12)
        self.assertEqual(len(report["history"]), 8)
        self.assertEqual((report["opened"], report["active"], report["ended"]), (12, 0, 12))

    async def test_prepare_failure_is_not_counted_as_an_opened_connection(self):
        original = web.WebSocketResponse.prepare

        async def prepare(socket, request):
            if request.app is self.relay_app:
                raise ConnectionResetError("private-session-id secret-permit")
            return await original(socket, request)

        with self.assertLogs("aiohttp.server", level="ERROR"), patch.object(web.WebSocketResponse, "prepare", prepare):
            with self.assertRaises(aiohttp.WSServerHandshakeError):
                await self.client.ws_connect(self.base + "/ws/control")
        report = await self.health()
        self.assertEqual((report["attempts"], report["opened"], report["ended"], report["prepare_failures"]), (1, 0, 0, 1))
        self.assertEqual(report["history"][-1]["reason"], "prepare_failure")
        self.assertEqual(report["history"][-1]["exception_class"], "ConnectionResetError")
        self.assertEqual(report["history"][-1]["operation"], "prepare")

    async def test_bonded_profile_explicitly_excludes_its_control_socket(self):
        app = create_app(self.origin, self.dist, bonded_upstream="http://127.0.0.1:1")
        runner, base = await self.start(app)
        try:
            async with self.client.get(base + "/relay/health") as response:
                packet = await response.json()
            self.assertIn("transport", packet)
            self.assertFalse(packet["control_closures"]["enabled"])
            self.assertEqual(packet["control_closures"]["excluded"], "bonded-control")
            self.assertEqual(packet["control_closures"]["attempts"], 0)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
