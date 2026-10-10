"""Video has reserved relay connections even when every bulk connection is busy."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from tools.operator_relay import create_app


class RelayCameraLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_bypasses_saturated_bulk_pool_and_reports_hops(self):
        release = asyncio.Event()
        admitted = 0

        async def bulk(request):
            nonlocal admitted
            admitted += 1
            await release.wait()
            return web.Response(body=b"map")

        async def video(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json(dict(type="video_init", mime="fixture"))
            await ws.send_bytes(b"init")
            async for message in ws:
                item = json.loads(message.data)
                await asyncio.sleep(.02)
                await ws.send_json(dict(type="video_segment", request_id=item["request_id"],
                                        server_wait_ms=10, age_ms=30, jetson_encode_ms=8,
                                        camera_ingest_ms=2))
                await ws.send_bytes(b"fragment")
            return ws

        upstream = web.Application()
        upstream.router.add_get("/map/points.bin", bulk)
        upstream.router.add_get("/ws/video", video)
        async with TestServer(upstream) as rover:
            with tempfile.TemporaryDirectory() as root:
                async with TestServer(create_app(str(rover.make_url('/')).rstrip('/'), Path(root))) as relay:
                    async with aiohttp.ClientSession() as client:
                        jobs = [asyncio.create_task(client.get(relay.make_url('/map/points.bin'))) for _ in range(16)]
                        try:
                            async with asyncio.timeout(1):
                                while admitted < 16:
                                    await asyncio.sleep(.005)
                            async with asyncio.timeout(.3):
                                ws = await client.ws_connect(relay.make_url('/ws/video'))
                                self.assertEqual((await ws.receive_json())["type"], "video_init")
                                self.assertEqual(await ws.receive_bytes(), b"init")
                                await ws.send_json(dict(type="next", request_id=1))
                                packet = await ws.receive_json()
                                self.assertEqual(await ws.receive_bytes(), b"fragment")
                                self.assertEqual(packet["age_ms"], 30)
                                self.assertGreaterEqual(packet["relay_request_ms"], 20)
                                self.assertGreaterEqual(packet["relay_network_bound_ms"], 10)
                                self.assertGreaterEqual(packet["relay_residence_ms"], 0)
                                await ws.close()
                            async with client.get(relay.make_url('/relay/health')) as reply:
                                stats = (await reply.json())["video"]
                            self.assertEqual(stats["fragments"], 1)
                            self.assertIsNotNone(stats["send_ms"])
                        finally:
                            release.set()
                            for result in await asyncio.gather(*jobs, return_exceptions=True):
                                if isinstance(result, aiohttp.ClientResponse):
                                    result.release()
