"""Actual server/pose contract regressions; no devices or motor commands."""
import unittest
import asyncio
import time
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from tools.mac_render.server import create_app, parse_args, RenderEncoder
from tools.mac_render.protocol import parse_pose

class PoseContractTests(unittest.TestCase):
    def test_actual_auto_pose_alias_and_frustum_are_retained(self):
        message = dict(type='pose', version=1, session_id='mge1x-user', pose_id=17,
            sent_mono_ms=2, sent_wall_ms=3, space='ar_map', map_revision=9,
            fov_y_deg=85, aspect=1.7, eyes=[dict(position=[0,1.5,0],
                quaternion=[0,0,0,1], projection=[1.0]*16)],
            head=dict(position=[0,1.5,0],quaternion=[0,0,0,1]))
        pose = parse_pose(message, 100000)
        self.assertEqual((pose['frame_id'],pose['pose_id'],pose['space']), (17,17,'ar_map'))
        self.assertEqual(pose['fov_y_deg'],85)
        self.assertEqual(len(pose['eyes']),1)
        encoder = RenderEncoder(None,320,240,30,100000,False)
        encoder.apply_pose(message,100000)
        with self.assertRaisesRegex(ValueError,'obsolete'):
            encoder.apply_pose(message,100001)
        encoder.apply_pose(dict(type='pose_untracked',version=1,session_id='mge1x-user',frame_id=18),100002)
        self.assertIsNone(encoder.latest_pose)

class MapProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_mac_video_reports_measured_fragment_wait_without_removing_transit(self):
        app = create_app(parse_args(['--no-browser']))
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        encoder.init, encoder.mime = b'init', 'video/mp4'
        app['mac'].encoder = encoder
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            socket = await client.ws_connect('/ws/macview')
            await socket.receive_json(); await socket.receive_bytes()
            await socket.send_json(dict(type='next', request_id=17))
            started = time.monotonic()
            await asyncio.sleep(.08)
            captured = (time.monotonic() - .2) * 1000
            encoder._publish(dict(keyframe=True, payload=b'fragment',
                sidecar=dict(type='video_segment', capture_mono_ms=captured)))
            metadata = await socket.receive_json()
            elapsed = (time.monotonic() - started) * 1000
            self.assertEqual(metadata['request_id'], 17)
            self.assertGreater(metadata['server_wait_ms'], 40)
            self.assertLessEqual(metadata['server_wait_ms'], elapsed)
            self.assertGreaterEqual(metadata['age_ms'], 200, 'capture age still includes queue wait')
            self.assertEqual(await socket.receive_bytes(), b'fragment')
            await socket.close()
        finally:
            await client.close()

    async def test_merged_map_status_and_identity_headers_cross_real_proxy(self):
        seen=[]
        source=web.Application()
        async def status(request):
            seen.append(request.path)
            return web.json_response(dict(type='status',geometry_source='IPHONE ARKIT MESH',points=3))
        async def mesh(request):
            return web.Response(body=b'mesh',headers={'X-Map-Session':'capture','X-Map-Epoch':'2',
                'X-Map-Revision':'3','X-Map-Generation':'4'})
        source.router.add_get('/map/status',status)
        source.router.add_get('/map/mesh.bin',mesh)
        upstream=TestServer(source); await upstream.start_server()
        client=TestClient(TestServer(create_app(parse_args(['--upstream',str(upstream.make_url('/')),'--no-browser']))))
        await client.start_server()
        try:
            response=await client.get('/map/status')
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['geometry_source'],'IPHONE ARKIT MESH')
            response=await client.get('/map/mesh.bin')
            self.assertEqual(response.headers['X-Map-Epoch'],'2')
            self.assertEqual(await response.read(),b'mesh')
            self.assertEqual(seen,['/map/status'])
        finally:
            await client.close(); await upstream.close()

    async def test_real_python_rover_exposes_map_status(self):
        import httpx
        from jetson.mapping.server import create_app as rover_app
        app=rover_app(simulate_rover=True,detector='none')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.get('/map/status')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['type'],'status')
            self.assertIn('session_id',response.json())
            self.assertIn('map_epoch',response.json())
