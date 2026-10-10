"""GET-only route qualification against controlled HTTP services; no rig access."""
import asyncio
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from tools.check_demo_routes import qualify


class RouteChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.statuses = {}
        self.delays = {}
        self.raw = {}
        self.redirects = {}
        self.bodies = {
            '/relay/health': dict(relay=True, upstream='http://100.103.169.40:8443',
                quest_built=True, render=dict(configured=True)),
            '/health': dict(type='status', rover=True, camera=True, camera_drive_ready=True,
                camera_age_ms=30, camera_source='PHONE RGB', camera_error=None),
            '/map/status': dict(type='status', source='IPHONE LIDAR', connected=True,
                fresh=True, tracking='normal', session_id='12345678-1234-4321-9876-123456789abc',
                map_epoch=1, points=3, triangles=1),
            '/dense/status': dict(jetson_points=3, mac_points=3, error=None, age_s=.02),
        }
        self.render = dict(service='mac-render', ready=True, drawn_points=3, drawn_triangles=1,
                           dense=dict(gpu_drawn_cells=3, gpu_uploaded_cells=3, error=None))
        async def render(request):
            self.requests.append(('render', request.method, request.path))
            return web.json_response(self.render)
        app = web.Application(); app.router.add_get('/health', render)
        self.renderer = TestServer(app); await self.renderer.start_server()
        self.renderer_origin = str(self.renderer.make_url('/')).rstrip('/')
        self.bodies['/relay/health']['render']['upstream'] = self.renderer_origin

        async def handle(request):
            self.requests.append(('operator', request.method, request.path))
            await asyncio.sleep(self.delays.get(request.path, 0))
            if request.path in self.redirects:
                raise web.HTTPFound(self.redirects[request.path])
            if request.path in self.statuses:
                return web.Response(status=self.statuses[request.path])
            if request.path in self.raw:
                return web.Response(body=self.raw[request.path])
            if request.path == '/map/mesh.bin':
                return web.Response(body=struct.pack('<II', 3, 3) + bytes(60), content_type='application/octet-stream')
            if request.path == '/map/points.bin':
                return web.Response(body=struct.pack('<I', 3) + bytes(48), content_type='application/octet-stream')
            return web.json_response(self.bodies[request.path])
        app = web.Application()
        for route in [*self.bodies, '/map/mesh.bin', '/map/points.bin']:
            app.router.add_get(route, handle)
        self.operator = TestServer(app); await self.operator.start_server()
        self.origin = str(self.operator.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.operator.close(); await self.renderer.close()

    async def check(self, **kwargs):
        return await qualify(self.origin, **kwargs)

    def check_named(self, report, name):
        return next(check for check in report['checks'] if check['name'] == name)

    async def test_real_contracts_pass_and_only_small_get_requests_are_made(self):
        report = await self.check(require_dense=True, require_render=True)
        self.assertTrue(report['qualified'], report)
        self.assertEqual(report['routes']['backend'], 'http://100.103.169.40:8443')
        self.assertEqual(report['routes']['render'], self.renderer_origin)
        self.assertTrue(all(method == 'GET' for _, method, _ in self.requests))
        self.assertEqual(self.check_named(report, 'map_payload')['facts']['prefix_bytes'], 24)
        self.assertFalse(any(path.startswith('/ws/') or path in ('/scan', '/control/status') for _, _, path in self.requests))

    async def test_backend_502_fails_even_when_local_relay_health_is_200(self):
        self.statuses['/health'] = 502
        report = await self.check(camera_only=True)
        self.assertFalse(report['qualified'])
        check = self.check_named(report, 'backend')
        self.assertEqual(check['http_status'], 502)
        self.assertIn('/health', check['url'])
        self.assertIn('100.103.169.40', check['action'])

    async def test_http_200_without_fresh_camera_or_rover_semantics_is_not_ready(self):
        for mutation in (dict(camera=False), dict(camera_age_ms=500), dict(rover=False),
                         dict(type='mac-render'), dict(camera_drive_ready=False)):
            before = self.bodies['/health'].copy()
            self.bodies['/health'].update(mutation)
            report = await self.check(camera_only=True)
            self.assertFalse(report['qualified'], mutation)
            self.bodies['/health'] = before

    async def test_configured_unmatched_geometry_cannot_hide_behind_healthy_backend(self):
        self.bodies['/relay/health']['geometry'] = dict(source='IPHONE ARKIT MESH', matched=False)
        self.bodies['/map/status'].update(geometry_source='IPHONE ARKIT MESH', points=0,
            geometry=dict(matched=False, connected=False, snapshot_complete=None))
        report = await self.check()
        self.assertFalse(report['qualified'])
        check = self.check_named(report, 'map')
        self.assertEqual(check['state'], 'geometry_unavailable')
        self.assertIn('--geometry-upstream', check['action'])

    async def test_valid_geometry_must_be_matched_complete_and_fresh(self):
        self.bodies['/relay/health']['geometry'] = dict(source='IPHONE ARKIT MESH')
        self.bodies['/map/status'].update(geometry_source='IPHONE ARKIT MESH',
            geometry=dict(matched=True, connected=True, snapshot_complete=True))
        self.assertTrue((await self.check())['qualified'])
        for mutation in (dict(matched=False), dict(connected=False), dict(snapshot_complete=False)):
            old = self.bodies['/map/status']['geometry'].copy()
            self.bodies['/map/status']['geometry'].update(mutation)
            self.assertFalse((await self.check())['qualified'], mutation)
            self.bodies['/map/status']['geometry'] = old
        self.bodies['/map/status']['fresh'] = False
        self.assertFalse((await self.check())['qualified'])

    async def test_camera_only_does_not_require_a_scan_and_empty_map_is_named_awaiting_scan(self):
        self.bodies['/map/status'].update(points=0, triangles=0, fresh=False, connected=False)
        self.assertTrue((await self.check(camera_only=True))['qualified'])
        report = await self.check()
        self.assertFalse(report['qualified'])
        self.assertEqual(self.check_named(report, 'map')['state'], 'awaiting_scan')

    async def test_snapshot_header_must_be_nonempty_even_when_status_claims_points(self):
        self.raw['/map/mesh.bin'] = struct.pack('<II', 0, 0)
        report = await self.check()
        self.assertFalse(report['qualified'])
        self.assertEqual(self.check_named(report, 'map_payload')['state'], 'empty_map_payload')

    async def test_truncated_snapshot_content_length_and_nonfinite_vertex_fail(self):
        for body in (struct.pack('<II', 3, 3) + bytes(16),
                     struct.pack('<II', 3, 3) + struct.pack('<fff', float('nan'), 0, 0) + bytes(48)):
            self.raw['/map/mesh.bin'] = body
            report = await self.check()
            self.assertFalse(report['qualified'])
            self.assertFalse(self.check_named(report, 'map_payload')['passed'])

    async def test_missing_dense_mount_and_empty_or_failed_dense_lane_are_explicit(self):
        self.statuses['/dense/status'] = 404
        report = await self.check(require_dense=True)
        self.assertFalse(report['qualified'])
        self.assertIn('--dense-map', self.check_named(report, 'dense')['action'])
        self.statuses.clear()
        for mutation in (dict(mac_points=0), dict(error='upstream timeout'), dict(age_s=None)):
            old = self.bodies['/dense/status'].copy()
            self.bodies['/dense/status'].update(mutation)
            self.assertFalse((await self.check(require_dense=True))['qualified'], mutation)
            self.bodies['/dense/status'] = old

    async def test_renderer_must_be_selected_ready_and_draw_geometry(self):
        self.render['drawn_points'] = self.render['drawn_triangles'] = 0
        self.render['dense']['gpu_drawn_cells'] = self.render['dense']['gpu_uploaded_cells'] = 0
        report = await self.check(require_render=True)
        self.assertFalse(report['qualified'])
        self.assertEqual(self.check_named(report, 'renderer')['state'], 'renderer_empty')
        self.render.update(drawn_points=3, ready=False)
        self.assertFalse((await self.check(require_render=True))['qualified'])
        self.bodies['/relay/health']['render']['configured'] = False
        self.assertFalse((await self.check(require_render=True))['qualified'])

    async def test_dense_only_drawing_passes_but_coarse_fallback_does_not_qualify_dense_rendering(self):
        self.render.update(drawn_points=0, drawn_triangles=0)
        self.assertTrue((await self.check(require_dense=True, require_render=True))['qualified'])
        self.render.update(drawn_points=3, drawn_triangles=1)
        for mutation in (dict(gpu_drawn_cells=0), dict(gpu_uploaded_cells=0), dict(error='dense download failed')):
            before = self.render['dense'].copy()
            self.render['dense'].update(mutation)
            report = await self.check(require_dense=True, require_render=True)
            self.assertFalse(report['qualified'], mutation)
            self.assertEqual(self.check_named(report, 'renderer')['state'], 'renderer_dense_not_ready')
            self.render['dense'] = before

    async def test_selected_route_mismatches_and_external_renderer_metadata_cannot_trigger_a_probe(self):
        report = await self.check(camera_only=True, expected_upstream='http://100.103.169.41:8443')
        self.assertFalse(report['qualified'])
        report = await self.check(require_render=True, renderer='http://127.0.0.1:8091')
        self.assertFalse(report['qualified'])
        self.assertFalse(any(name == 'render' for name, _, _ in self.requests))
        for unsafe in ('http://198.51.100.10:8091', 'http://user:SECRET@127.0.0.1:8091'):
            self.bodies['/relay/health']['render']['upstream'] = unsafe
            report = await self.check(require_render=True)
            self.assertFalse(report['qualified'])
            self.assertIsNone(report['routes']['render'])
            self.assertNotIn('SECRET', json.dumps(report))
        self.assertFalse(any(name == 'render' for name, _, _ in self.requests))

    async def test_actual_operator_relay_selections_and_missing_dense_mount(self):
        from tools.operator_relay import create_app
        relay = TestServer(create_app(self.origin, render_upstream=self.renderer_origin))
        await relay.start_server()
        try:
            report = await qualify(str(relay.make_url('/')), require_render=True)
            self.assertTrue(report['qualified'], report)
            self.assertEqual(report['routes']['backend'], self.origin)
            self.assertEqual(report['routes']['render'], self.renderer_origin)
            report = await qualify(str(relay.make_url('/')), require_dense=True)
            self.assertFalse(report['qualified'])
            self.assertEqual(self.check_named(report, 'dense')['http_status'], 404)
        finally:
            await relay.close()

    async def test_cli_returns_failed_exit_and_persists_the_same_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target = directory + '/routes.json'
            for expected in (0, 1):
                if expected:
                    self.statuses['/health'] = 502
                process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'tools.check_demo_routes',
                    '--operator', self.origin, '--camera-only', '--output', target,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
                self.assertEqual(process.returncode, expected, stderr.decode())
                self.assertEqual(Path(target).read_text(), stdout.decode())
                self.assertEqual(json.loads(stdout)['qualified'], expected == 0)

    async def test_no_404_fallback_redirect_or_sensitive_health_dump(self):
        self.bodies['/health'].update(permit='SECRET-PERMIT', credentials='SECRET-AUTH')
        self.statuses['/map/status'] = 404
        report = await self.check()
        self.assertFalse(report['qualified'])
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(sum(path == '/health' for _, _, path in self.requests), 1)
        self.statuses.clear()
        self.redirects['/health'] = '/ws/control'
        report = await self.check(camera_only=True)
        self.assertFalse(report['qualified'])
        self.assertEqual(self.check_named(report, 'backend')['http_status'], 302)
        self.assertFalse(any(path.startswith('/ws/') for _, _, path in self.requests))
        self.redirects.clear()
        self.raw['/relay/health'] = b'x' * 500
        report = await self.check(camera_only=True, max_bytes=256)
        self.assertFalse(report['qualified'])
        self.assertIn('byte limit', self.check_named(report, 'relay')['detail'])

    async def test_overall_deadline_and_bad_origins_fail_without_hardware_or_credentials(self):
        self.delays['/health'] = .4
        started = time.monotonic()
        report = await self.check(camera_only=True, timeout=.08)
        self.assertLess(time.monotonic() - started, .3)
        self.assertFalse(report['qualified'])
        with self.assertRaises(ValueError):
            await qualify('http://user:secret@localhost:8080')
        with self.assertRaises(ValueError):
            await self.check(camera_only=True, require_dense=True)


if __name__ == '__main__':
    unittest.main()
