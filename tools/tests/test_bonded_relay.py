"""Local two-path relay tests; canonical simulated backend, never hardware."""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import aiohttp
from aiohttp import web

from tools.bonded_relay import BondedRelay, MAX_GET_BODY
from tools.fault_proxy import FaultProxy

ROOT = Path(__file__).resolve().parents[2]


class TCPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.runners = []
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.cleanup_runners)

    async def cleanup_runners(self):
        for runner in reversed(self.runners): await runner.cleanup()

    async def serve(self, app):
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup(); site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        self.runners.append(runner)
        return f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def local(self, broker):
        app = web.Application()
        app.router.add_get('/ws/control', broker.control)
        async def image(request):
            r = await broker.race_get('/camera.jpg')
            return web.Response(body=r.body, headers=r.headers)
        app.router.add_get('/camera.jpg', image)
        return await self.serve(app)

    async def until(self, call, predicate, seconds=3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            v = await call()
            if predicate(v): return v
            await asyncio.sleep(.01)
        self.fail('Condition deadline: ' + str(v))


class CanonicalTests(TCPTest):
    async def test_real_hub_identical_fanout_survives_one_tcp_loss_and_stops_on_last(self):
        work = tempfile.TemporaryDirectory(prefix='farsight-bonded-api-')
        self.addCleanup(work.cleanup)
        log = open(Path(work.name) / 'server.log', 'w+'); self.addCleanup(log.close)
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'jetson.server', '--demo', '--simulate-rover',
            '--detector', 'none', '--bonded-control', '--host', '127.0.0.1', '--port', str(port),
            '--mission-dir', work.name, '--tts-dir', work.name], cwd=ROOT,
            stdout=log, stderr=log, start_new_session=True)
        async def stop():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); await asyncio.to_thread(process.wait)
        self.addAsyncCleanup(stop)
        direct = f'http://127.0.0.1:{port}'
        async def get(path):
            async with self.client.get(direct + path) as r: return await r.json()
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if process.poll() is not None:
                log.seek(0); self.fail('Backend exited: ' + log.read()[-1500:])
            try:
                h = await get('/health')
                if h['camera']:
                    self.assertTrue(h['control']['simulated']); self.assertEqual(h['camera_source'], 'SIMULATED'); break
            except aiohttp.ClientError: pass
            await asyncio.sleep(.05)
        else: self.fail('Backend readiness deadline')
        proxies = [await FaultProxy(port, port=0).start(), await FaultProxy(port, port=0).start()]
        for proxy in proxies: self.addAsyncCleanup(proxy.close)
        broker = BondedRelay([f'http://127.0.0.1:{p.port}' for p in proxies], self.client)
        self.addAsyncCleanup(broker.close)
        base = await self.local(broker)
        ws = await self.client.ws_connect(base + '/ws/control'); self.addAsyncCleanup(ws.close)
        status = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(broker.status()['attached_paths'], 2)
        self.assertNotIn('secret', status); self.assertNotIn('path_id', status)
        sid = status['session_id']; sequences = [status['status_seq']]
        await ws.send_json(dict(session_id=sid, seq=0, deadman=False))
        while status.get('seq') != 0:
            status = await asyncio.wait_for(ws.receive_json(), 2); sequences.append(status['status_seq'])
        self.assertFalse(status['requires_release'])
        sent = time.monotonic(); image = await broker.race_get('/camera.jpg')
        self.assertTrue(image.body.startswith(b'\xff\xd8')); self.assertTrue(image.body.endswith(b'\xff\xd9'))
        self.assertEqual(image.headers['X-Frame-Source'], 'SIMULATED')
        age = float(image.headers['X-Frame-Age-Ms']) + (time.monotonic() - sent) * 1000
        self.assertLess(age, 500)
        status = await asyncio.wait_for(ws.receive_json(), 2); sequences.append(status['status_seq'])
        await ws.send_json(dict(session_id=sid, seq=1, deadman=True, permit=status['permit'],
            mode='teleop', v=.35, w=0, pan=0, video_fresh=True, video_age_ms=age))
        moving = await self.until(lambda: get('/control/status'),
            lambda p: p['seq'] == 1 and p['left_pwm'] > 0 and p['right_pwm'] > 0)
        self.assertEqual(moving['reason'], 'active', 'duplicate copies must not trigger stale-sequence stop')
        proxies[0].drop()
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: s['attached_paths'] == 1)
        self.assertFalse(ws.closed)
        status = await asyncio.wait_for(ws.receive_json(), 2); sequences.append(status['status_seq'])
        sent = time.monotonic(); camera = await broker.race_get('/camera.jpg')
        self.assertEqual(str(camera.origin), f'http://127.0.0.1:{proxies[1].port}')
        self.assertEqual(str(broker.preferred_origin()), str(camera.origin))
        # Explicitly fresh image receipt again; no synthetic age claim for motion.
        age = float(camera.headers['X-Frame-Age-Ms']) + (time.monotonic() - sent) * 1000
        self.assertLess(age, 500)
        status = await asyncio.wait_for(ws.receive_json(), 2); sequences.append(status['status_seq'])
        await ws.send_json(dict(session_id=sid, seq=2, deadman=True, permit=status['permit'],
            mode='teleop', v=.35, w=0, pan=0, video_fresh=True, video_age_ms=age))
        await self.until(lambda: get('/control/status'), lambda p: p['seq'] == 2 and p['left_pwm'] > 0)
        triggered = time.monotonic(); proxies[1].drop()
        stopped = await self.until(lambda: get('/control/status'), lambda p: p['left_pwm'] == p['right_pwm'] == 0)
        self.assertLess(time.monotonic() - triggered, .5)
        self.assertTrue(stopped['requires_release'])
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: s['attached_paths'] == 0)
        with self.assertRaises(web.HTTPServiceUnavailable): await broker.race_get('/camera.jpg')
        with self.assertRaises(web.HTTPServiceUnavailable): broker.preferred_origin()
        for _ in range(50):
            message = await asyncio.wait_for(ws.receive(), 1)
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR): break
        else: self.fail('Quest did not close after last path loss')
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertTrue(all(p.stats['connection_peak'] <= 3 for p in proxies))
        self.assertNotIn(sid, json.dumps(broker.status()))


class SocketFixtureTests(TCPTest):
    async def pair(self, *, old_copies=False, bad_second=False, binary_first=False, partial_first=False, malformed_first=False, oversized_first=False, blocked_health=False, blocked_camera=False, attach_control=True):
        self.commands = [[], []]; self.command_times = [[], []]; self.images = [0, 0]
        self.health_release = asyncio.Event()
        self.camera_release = asyncio.Event()
        started = asyncio.get_running_loop().time(); secret = 'a' * 64
        routes = []
        for index in range(2):
            async def control(request, i=index):
                ws = web.WebSocketResponse(); await ws.prepare(request)
                hello = await ws.receive_json()
                self.assertEqual(hello['type'], 'attach')
                self.assertEqual(hello['version'], 1)
                if i == 1: self.assertEqual(hello['secret'], secret)
                await ws.send_json(dict(type='attached', version=1, session_id='wrong' if i == 1 and bad_second else 'fixture-owner',
                    secret=secret, path_id=f'path-{i}'))
                async def publish():
                    count = 0
                    while not ws.closed:
                        seq = int((asyncio.get_running_loop().time() - started) / .025) + 1
                        if malformed_first and i == 0 and count >= 3:
                            await ws.send_json({'session_id': 'fixture-owner', 'status_seq': True}); return
                        if binary_first and i == 0 and count >= 3:
                            await ws.send_bytes(b'invalid-control'); return
                        packet = dict(type='control_status', session_id='fixture-owner', status_seq=seq,
                            permit='permit', permit_ms=250, rover=True, simulated=True,
                            requires_release=False, seq=-1, reason='stopped')
                        await ws.send_json(packet)
                        if old_copies:
                            await ws.send_json({**packet, 'status_seq': max(0, seq - 1), 'requires_release': True})
                        count += 1; await asyncio.sleep(.025)
                task = asyncio.create_task(publish())
                try:
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            self.commands[i].append(message.data); self.command_times[i].append(time.monotonic())
                finally:
                    task.cancel(); await asyncio.gather(task, return_exceptions=True)
                return ws
            async def camera(request, i=index):
                self.images[i] += 1
                if blocked_camera: await self.camera_release.wait()
                headers = {'Content-Type': 'image/jpeg', 'X-Frame-Id': '7', 'X-Frame-Age-Ms': '10', 'X-Frame-Source': 'SIMULATED'}
                if i == 0 and partial_first:
                    r = web.StreamResponse(headers=headers); await r.prepare(request)
                    with suppress(ConnectionError):
                        await r.write(b'\xff\xd8partial'); await asyncio.sleep(.3); await r.write(b'\xff\xd9')
                    return r
                if oversized_first:
                    if i == 0: return web.Response(body=b'\xff\xd8' + b'x' * MAX_GET_BODY + b'\xff\xd9', headers=headers)
                    await asyncio.sleep(.05)
                return web.Response(body=b'\xff\xd8complete\xff\xd9', headers=headers)
            app = web.Application(); app.router.add_get('/ws/control/bonded', control); app.router.add_get('/camera.jpg', camera)
            async def health(request):
                if blocked_health: await self.health_release.wait()
                return web.json_response({'camera': True})
            app.router.add_get('/health', health)
            routes.append(await self.serve(app))
        broker = BondedRelay(routes, self.client); self.addAsyncCleanup(broker.close)
        base = await self.local(broker)
        self.relay_url = base
        ws = await self.client.ws_connect(base + '/ws/control') if attach_control else None
        if ws is not None: self.addAsyncCleanup(ws.close)
        return broker, ws, routes

    async def test_dedup_ignores_old_stop_copy_and_fanout_is_identical_without_replay(self):
        broker, ws, _ = await self.pair(old_copies=True)
        sequences = []
        for _ in range(5):
            status = await asyncio.wait_for(ws.receive_json(), 1)
            self.assertFalse(status['requires_release']); sequences.append(status['status_seq'])
        self.assertEqual(sequences, sorted(set(sequences)))
        await ws.send_json(dict(session_id='fixture-owner', seq=0, deadman=False))
        await self.until(lambda: asyncio.sleep(0, result=self.commands), lambda c: len(c[0]) == len(c[1]) == 1)
        self.assertEqual(self.commands[0], self.commands[1])
        await ws.send_bytes(b'malformed')
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: not s['controller_active'])
        self.assertEqual([len(v) for v in self.commands], [1, 1])

    async def test_wrong_backend_excluded_from_images_and_secret_never_reaches_quest(self):
        broker, ws, routes = await self.pair(bad_second=True)
        status = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertNotIn('secret', status); self.assertNotIn('path_id', status)
        self.assertEqual(broker.status()['attached_paths'], 1)
        reply = await broker.race_get('/camera.jpg')
        self.assertEqual(str(reply.origin), routes[0]); self.assertEqual(self.images, [1, 0])

    async def test_complete_jpeg_wins_never_partial_spliced_and_headers_survive(self):
        broker, _, routes = await self.pair(partial_first=True)
        reply = await broker.race_get('/camera.jpg')
        self.assertEqual(reply.body, b'\xff\xd8complete\xff\xd9')
        self.assertEqual(str(reply.origin), routes[1]); self.assertEqual(reply.headers['X-Frame-Age-Ms'], '10')

    async def test_binary_status_fault_loses_only_one_path_and_stale_view_command_becomes_stop(self):
        broker, ws, _ = await self.pair(binary_first=True)
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: s['attached_paths'] == 1)
        status = await asyncio.wait_for(ws.receive_json(), 1); self.assertEqual(status['session_id'], 'fixture-owner')
        await ws.send_json(dict(session_id='fixture-owner', seq=0, deadman=True,
                               v=.5, w=0, video_fresh=True, video_age_ms=501))
        await self.until(lambda: asyncio.sleep(0, result=self.commands[1]), lambda c: len(c) == 1)
        command = json.loads(self.commands[1][0]); self.assertFalse(command['deadman']); self.assertEqual(command['v'], 0)

    async def test_malformed_status_is_a_path_fault_and_second_path_continues(self):
        broker, ws, _ = await self.pair(malformed_first=True)
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: s['attached_paths'] == 1)
        status = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertIs(type(status['status_seq']), int)
        self.assertFalse(ws.closed)

    async def test_oversized_complete_jpeg_is_rejected_before_winning(self):
        broker, _, routes = await self.pair(oversized_first=True)
        reply = await broker.race_get('/camera.jpg')
        self.assertEqual(reply.body, b'\xff\xd8complete\xff\xd9')
        self.assertEqual(str(reply.origin), routes[1])
        self.assertEqual(broker.get_tasks, set(), 'loser tasks are drained before returning')

    async def test_blocked_writer_is_retired_without_delaying_healthy_40hz_commands_or_replay(self):
        broker, ws, _ = await self.pair()
        slow = broker.paths[broker.origins[0]]
        blocked = asyncio.Event()
        original = slow.socket.send_str
        async def stall(data):
            await blocked.wait()
            await original(data)
        slow.socket.send_str = stall
        for seq in range(8):
            await ws.send_json(dict(session_id='fixture-owner', seq=seq, deadman=False))
            await asyncio.sleep(.025)
        await self.until(lambda: asyncio.sleep(0, result=self.commands[1]), lambda c: len(c) == 8)
        self.assertEqual(broker.status()['attached_paths'], 1)
        self.assertEqual([json.loads(x)['seq'] for x in self.commands[1]], list(range(8)))
        gaps = [b - a for a, b in zip(self.command_times[1], self.command_times[1][1:])]
        self.assertLess(max(gaps), .075, 'blocked path cannot impose its 100 ms send deadline on the survivor')
        blocked.set(); await asyncio.sleep(.03)
        self.assertEqual(self.commands[0], [], 'retired pending command is cancelled, never replayed')
        self.assertLessEqual(len(broker.writer_tasks), 1)

    async def test_quest_status_delivery_failure_ends_all_paths(self):
        broker, ws, _ = await self.pair()
        async def fail(data): raise ConnectionError('test downstream fault')
        broker.local.send_str = fail
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: s['attached_paths'] == 0)
        self.assertTrue(broker.status()['disconnected'])
        for _ in range(20):
            message = await asyncio.wait_for(ws.receive(), 1)
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR): break
        else: self.fail('Quest remained open after downstream delivery failure')

    async def test_concurrent_blocked_gets_are_capped_without_queue_and_cleanup_drains_all(self):
        broker, _, _ = await self.pair(blocked_health=True)
        races = [asyncio.create_task(broker.race_get('/health')) for _ in range(4)]
        await self.until(lambda: asyncio.sleep(0, result=len(broker.get_tasks)), lambda n: n == 8)
        with self.assertRaises(web.HTTPServiceUnavailable): await broker.race_get('/health')
        self.assertEqual(broker.status()['active_http_races'], 4)
        self.assertEqual(len(broker.get_tasks), 8)
        await broker.close()
        await asyncio.gather(*races, return_exceptions=True)
        self.assertEqual(broker.get_tasks, set())
        self.assertEqual(broker.status()['active_http_races'], 0)
        self.assertEqual(broker.writer_tasks, set())
        self.health_release.set()

    async def test_precontroller_camera_is_503_without_fetching_unvalidated_backend(self):
        broker, _, _ = await self.pair(attach_control=False)
        async with self.client.get(self.relay_url + '/camera.jpg') as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.images, [0, 0], 'no image is fetched before ownership validates a route')
        health = await broker.race_get('/health')
        self.assertTrue(json.loads(health.body)['camera'], 'precontroller health stays read-only')
        self.assertEqual(broker.get_tasks, set())

    async def test_inflight_camera_cannot_cross_to_a_new_controller_even_if_session_string_repeats(self):
        broker, ws, _ = await self.pair(blocked_camera=True)
        old_owner = broker.owner
        old_image = asyncio.create_task(broker.race_get('/camera.jpg'))
        await self.until(lambda: asyncio.sleep(0, result=self.images), lambda n: n == [1, 1])
        await ws.close()
        await self.until(lambda: asyncio.sleep(0, result=broker.status()), lambda s: not s['controller_active'])
        replacement = await self.client.ws_connect(self.relay_url + '/ws/control')
        self.addAsyncCleanup(replacement.close)
        await asyncio.wait_for(replacement.receive_json(), 1)
        self.assertIsNot(broker.owner, old_owner)
        self.assertEqual(broker.session_id, 'fixture-owner', 'fixture deliberately repeats SID to verify owner identity too')
        self.camera_release.set()
        with self.assertRaises(web.HTTPServiceUnavailable): await old_image
        self.assertEqual(broker.get_tasks, set(), 'old image candidates are cancelled and drained')
        current = await broker.race_get('/camera.jpg')
        self.assertEqual(current.body, b'\xff\xd8complete\xff\xd9')

    async def test_three_stops_in_one_client_burst_do_not_false_fault_healthy_writers(self):
        broker, ws, _ = await self.pair()
        for seq in range(3):
            # aiohttp writes these small frames without a scheduler sleep;
            # the relay's buffered receive can consume the entire burst.
            await ws.send_json(dict(session_id='fixture-owner', seq=seq, deadman=False))
        await self.until(lambda: asyncio.sleep(0, result=self.commands),
                         lambda c: len(c[0]) == len(c[1]) == 3)
        self.assertEqual(broker.status()['attached_paths'], 2)
        self.assertFalse(ws.closed)
        self.assertEqual(self.commands[0], self.commands[1])
        self.assertEqual([json.loads(c)['seq'] for c in self.commands[0]], [0, 1, 2])


if __name__ == '__main__': unittest.main()
