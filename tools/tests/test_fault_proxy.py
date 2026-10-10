"""Real localhost TCP tests; no privileged traffic shaping or network changes."""

import asyncio
from contextlib import suppress
import socket
import time
import unittest

from aiohttp import web, WSMsgType

from tools.fault_proxy import CHUNK, QUEUE_CHUNKS, MAX_CONNECTIONS, FaultProxy
from tools.qualify_link import capture_receipt_bound, measure
from tools.operator_relay import upstream_origin


class FaultProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handlers, self.writers = set(), set()
        self.proxies = []

        async def echo(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            self.writers.add(writer)
            try:
                while payload := await reader.read(16384):
                    writer.write(payload)
                    await writer.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                self.writers.discard(writer)
                self.handlers.discard(task)

        self.server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.upstream_port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for proxy in self.proxies:
            await proxy.close()
            self.assertFalse(proxy.tasks)
            self.assertFalse(proxy.writers)
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.transport.abort()
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*tuple(self.handlers), return_exceptions=True)

    async def proxy(self, **options):
        proxy = FaultProxy(options.pop("upstream_port", self.upstream_port), port=0, **options)
        await proxy.start()
        self.proxies.append(proxy)
        return proxy

    async def test_delay_is_roundtrip_and_does_not_corrupt_data(self):
        proxy = await self.proxy(delay_ms=40)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        started = time.monotonic()
        writer.write(b"exact bytes")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 1), b"exact bytes")
        duration = time.monotonic() - started
        self.assertGreaterEqual(duration, .075)
        self.assertLess(duration, .4)
        writer.close()
        await writer.wait_closed()

    async def test_aggregate_bandwidth_bounds_two_connections_and_queues(self):
        proxy = await self.proxy(bandwidth_mbps=.5)
        connections = [await asyncio.open_connection("127.0.0.1", proxy.port) for _ in range(2)]
        payload = b"a" * (CHUNK * 2)
        started = time.monotonic()
        # Echo means 2 * 32 KiB * 2 directions = 128 KiB in the shared budget.
        for reader, writer in connections:
            writer.write(payload)
            await writer.drain()
        data = await asyncio.gather(*(asyncio.wait_for(reader.readexactly(len(payload)), 5)
                                      for reader, writer in connections))
        duration = time.monotonic() - started
        self.assertEqual(data, [payload, payload])
        self.assertGreaterEqual(duration, 1.8)
        self.assertLess(duration, 3.5)
        self.assertLessEqual(proxy.stats["queue_peak_chunks"], QUEUE_CHUNKS)
        self.assertEqual(proxy.stats["bytes"], len(payload) * 4)
        for reader, writer in connections:
            writer.close()
            await writer.wait_closed()

    async def test_half_close_delivers_pending_bytes_and_eof(self):
        proxy = await self.proxy(delay_ms=10)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        payload = b"complete before eof" * 1000
        writer.write(payload)
        writer.write_eof()
        received = await asyncio.wait_for(reader.read(), 2)
        self.assertEqual(received, payload)
        writer.close()
        await writer.wait_closed()

    async def test_unavailable_upstream_closes_instead_of_hanging(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            missing_port = sock.getsockname()[1]
        proxy = await self.proxy(upstream_port=missing_port)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        with suppress(ConnectionResetError):
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        with suppress(ConnectionResetError):
            await writer.wait_closed()

    async def test_outage_drops_every_connection_and_rejects_new_connections(self):
        proxy = await self.proxy(drop_after=.2)
        connections = [await asyncio.open_connection("127.0.0.1", proxy.port) for _ in range(3)]
        for reader, writer in connections:
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await reader.readexactly(4), b"ping")
        for reader, writer in connections:
            with suppress(ConnectionResetError):
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
        self.assertTrue(proxy.stats["dropped"])
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        with suppress(ConnectionResetError):
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        with suppress(ConnectionResetError):
            await writer.wait_closed()
        await asyncio.sleep(.05)
        self.assertFalse(proxy.tasks)
        self.assertFalse(proxy.writers)
        self.assertGreaterEqual(proxy.stats["rejected"], 1)

    async def test_connection_count_is_bounded(self):
        proxy = await self.proxy()
        connections = [await asyncio.open_connection("127.0.0.1", proxy.port)
                       for _ in range(MAX_CONNECTIONS + 3)]
        await asyncio.sleep(.05)
        self.assertLessEqual(len(proxy.tasks), MAX_CONNECTIONS)
        self.assertLessEqual(proxy.stats["connection_peak"], MAX_CONNECTIONS)
        self.assertGreaterEqual(proxy.stats["rejected"], 3)
        for reader, writer in connections:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()

    def test_invalid_or_nonlocal_settings_rejected(self):
        for settings in ({"upstream_host": "rover.example"}, {"delay_ms": float("nan")},
                         {"bandwidth_mbps": -1}, {"drop_after": float("inf")}, {"delay_ms": 6000}):
            with self.assertRaises(ValueError):
                FaultProxy(self.upstream_port, port=0, **settings)
        with self.assertRaises(ValueError):
            FaultProxy(8443, port=8443)

    def test_receipt_bound_subtracts_only_valid_server_wait(self):
        self.assertEqual(capture_receipt_bound(100, 200, 50), 250)
        for wait in (None, -1, 201, float("nan"), float("inf"), True, "50"):
            self.assertEqual(capture_receipt_bound(100, 200, wait), 300)
        for age, elapsed in ((-1, 200), (100, float("inf")), (True, 200)):
            with self.assertRaises(ValueError):
                capture_receipt_bound(age, elapsed)


class QualifierPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.age = 60
        self.commands = []
        self.two_initial_requests = False
        app = web.Application()
        async def health(request):
            return web.json_response({"camera": True, "video": {"ready": True}})
        async def control(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"session_id": "stopped-probe", "seq": -1})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                command = message.json()
                self.commands.append(command)
                await ws.send_json({"session_id": "stopped-probe", "seq": command["seq"]})
            return ws
        async def video(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "video_init", "mime": "video/mp4"})
            await ws.send_bytes(b"MOCK initialization, no decoder verification")
            try:
                first = await asyncio.wait_for(ws.receive_json(), 1)
                second = await asyncio.wait_for(ws.receive_json(), .1)
                self.two_initial_requests = first["request_id"] == 1 and second["request_id"] == 2
                pending = [first, second]
                while True:
                    command = pending.pop(0)
                    before = time.monotonic()
                    await asyncio.sleep(.03)
                    wait = (time.monotonic() - before) * 1000
                    await ws.send_json({"type": "video_segment", "request_id": command["request_id"],
                                        "age_ms": self.age, "server_wait_ms": wait})
                    await ws.send_bytes(b"MOCK fragment, no decoder verification")
                    pending.append(await ws.receive_json())
            except (OSError, TypeError, asyncio.TimeoutError):
                pass
            finally:
                await ws.close()
            return ws
        app.router.add_get("/health", health)
        app.router.add_get("/ws/control", control)
        app.router.add_get("/ws/video", video)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.origin = upstream_origin(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_qualifier_matches_two_request_window_and_sends_only_stops(self):
        report = await measure(self.origin, seconds=.3, video_mode="h264")
        self.assertTrue(self.two_initial_requests)
        self.assertEqual(report["h264_request_window"], 2)
        self.assertTrue(report["transport_checks_passed"], report)
        self.assertGreaterEqual(report["capture_receipt_bound_p95_ms"], self.age)
        self.assertLess(report["capture_receipt_bound_p95_ms"], 500)
        self.assertTrue(all(command["deadman"] is False for command in self.commands))

    async def test_stale_fragments_fail_transport_checks(self):
        self.age = 600
        report = await measure(self.origin, seconds=.3, video_mode="h264")
        self.assertFalse(report["capture_receipt_check_passed"])
        self.assertFalse(report["transport_checks_passed"])
        self.assertGreater(report["capture_receipt_bound_p95_ms"], 500)


if __name__ == "__main__":
    unittest.main()
