"""Production relay integration; protocol fixtures, not motor/video qualification."""

import asyncio
import json

from aiohttp import web

from tools.operator_relay import create_app
from tools.tests.test_bonded_relay import TCPTest


class BondedOperatorIntegrationTests(TCPTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.remote_sockets = [set(), set()]
        self.posts = [0, 0]
        self.routes = []
        start = asyncio.get_running_loop().time()
        for index in range(2):
            app = web.Application()

            async def control(request, i=index):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                hello = await ws.receive_json()
                self.assertEqual(hello['type'], 'attach')
                await ws.send_json(dict(type='attached', version=1, session_id='fixture-owner',
                                        secret='b' * 64, path_id=f'fixture-{i}'))
                self.remote_sockets[i].add(ws)

                async def statuses():
                    while True:
                        ordinal = int((asyncio.get_running_loop().time() - start) / .025) + 1
                        await ws.send_json(dict(session_id='fixture-owner', status_seq=ordinal,
                            permit='fixture-permit', permit_ms=250, rover=True, simulated=True,
                            requires_release=True, seq=-1, reason='release_required'))
                        await asyncio.sleep(.025)

                task = asyncio.create_task(statuses())
                try:
                    async for _ in ws:
                        pass
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self.remote_sockets[i].discard(ws)
                return ws

            async def health(request, i=index):
                return web.json_response(dict(camera=True, rover=True, video=dict(ready=True),
                    camera_source='SIMULATED', detector='none',
                    audio_url=self.routes[i] + '/tts/test.wav'))

            async def camera(request):
                return web.Response(body=b'\xff\xd8fixture\xff\xd9', content_type='image/jpeg',
                    headers={'X-Frame-Age-Ms': '12', 'X-Frame-Id': '7', 'X-Frame-Source': 'SIMULATED'})

            async def scan(request, i=index):
                self.posts[i] += 1
                return web.json_response({'path': i})

            app.router.add_get('/ws/control/bonded', control)
            app.router.add_get('/health', health)
            app.router.add_get('/camera.jpg', camera)
            app.router.add_post('/scan', scan)
            self.routes.append(await self.serve(app))
        self.app = create_app(self.routes[0], bonded_upstream=self.routes[1])
        self.base = await self.serve(self.app)

    async def test_private_endpoint_is_blocked_and_health_exposes_profile_without_secret(self):
        async with self.client.get(self.base + '/health') as response:
            self.assertEqual(response.status, 200)
            health = await response.json()
        self.assertEqual(health['relay_transport']['profile'], 'bonded-jpeg')
        self.assertTrue(health['video']['ready'], 'real backend encoder state is not fabricated')
        self.assertEqual(health['audio_url'], '/tts/test.wav')
        async with self.client.get(self.base + '/ws/control/bonded') as response:
            self.assertEqual(response.status, 403)
        ws = await self.client.ws_connect(self.base + '/ws/control')
        self.addAsyncCleanup(ws.close)
        status = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertNotIn('secret', status)
        self.assertNotIn('path_id', status)
        async with self.client.get(self.base + '/relay/health') as response:
            report = await response.json()
        self.assertEqual(report['transport']['attached_paths'], 2)
        self.assertNotIn('b' * 64, json.dumps(report))
        self.assertNotIn('fixture-owner', json.dumps(report))

    async def test_survivor_camera_and_post_routing_never_retry_mutation(self):
        ws = await self.client.ws_connect(self.base + '/ws/control')
        self.addAsyncCleanup(ws.close)
        await asyncio.wait_for(ws.receive_json(), 2)
        await asyncio.gather(*(remote.close() for remote in tuple(self.remote_sockets[0])))
        await self.until(lambda: asyncio.sleep(0, result=self.app['bonded'].status()),
                         lambda value: value['attached_paths'] == 1)
        async with self.client.get(self.base + '/camera.jpg') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['X-Frame-Age-Ms'], '12')
            self.assertEqual(await response.read(), b'\xff\xd8fixture\xff\xd9')
        async with self.client.post(self.base + '/scan', json={'fixture': True}) as response:
            self.assertEqual(await response.json(), {'path': 1})
        self.assertEqual(self.posts, [0, 1])
        async with self.client.get(self.base + '/stream.mjpg') as response:
            self.assertEqual(response.status, 501)
        self.assertFalse(ws.closed)
