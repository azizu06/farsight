"""Real local WebSocket contract/failure tests; no renderer or physical rover."""
import asyncio
import json
import struct
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from tools.qualify_macview import boxes, qualify


def eyes():
    projection = [1.1, 0, 0, 0, 0, 1.7, 0, 0, 0, 0, -1.001, -1, 0, 0, -.02, 0]
    return [dict(eye=label, position=[x, 1.6, 0], quaternion=[0, 0, 0, 1],
                 projection=list(projection)) for label, x in (('left', -.032), ('right', .032))]


def box(kind, payload=b''):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


class MacViewQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode = 'normal'
        self.pose_packet = None
        self.requests = []
        self.sent_requests = []
        self.queries = []

        async def pose(request):
            self.requests.append(request.path)
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                self.pose_packet = json.loads(message.data)
            return socket

        async def video(request):
            self.requests.append(request.path)
            self.queries.append(dict(request.query))
            if self.mode == 'redirect':
                raise web.HTTPFound('/ws/control')
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            init = dict(type='video_init', version=1, mime='video/mp4; codecs="avc1.4d001f"',
                        width=1280, height=720, fps=30, stereo=False, source='MAC RENDER')
            if self.mode == 'no-stereo':
                del init['stereo']
            if self.mode.startswith('sbs'):
                init.update(stereo=True, layout='sbs-left-right', eye_width=640, eye_height=720)
                if self.mode == 'sbs-init-width':
                    init['width'] = 1282
                elif self.mode == 'sbs-init-eye-width':
                    init['eye_width'] = 320
                elif self.mode == 'sbs-init-height':
                    init['height'] = 721
                elif self.mode == 'sbs-init-bool-dimension':
                    init['eye_width'] = True
                elif self.mode == 'sbs-init-layout':
                    init['layout'] = 'right-left'
                elif self.mode == 'sbs-init-flat':
                    init['stereo'] = False
            await socket.send_json(init)
            await socket.send_bytes(box(b'ftyp', b'isom') + box(b'moov'))
            frame = 0
            async for message in socket:
                packet = json.loads(message.data)
                self.sent_requests.append(packet)
                if self.mode == 'stall':
                    continue
                await asyncio.sleep(.002)
                frame += 1
                sidecar = dict(type='video_segment', version=1, request_id=packet['request_id'],
                               media_start_s=(frame - 1) / 10, media_end_s=frame / 10,
                               frame_id=frame * 3, age_ms=4, stereo=False, source='SIMULATED')
                if self.mode.startswith('sbs'):
                    sidecar.update(stereo=True, layout='sbs-left-right', eye_width=640, eye_height=720,
                                   pose=dict(eyes=eyes(), session_id='owner-viewer', frame_id=frame, pose_id=frame))
                    if self.mode == 'sbs-reversed-pair':
                        sidecar['pose']['eyes'].reverse()
                    elif self.mode == 'sbs-duplicate-eye':
                        sidecar['pose']['eyes'][1]['eye'] = 'left'
                    elif self.mode == 'sbs-missing-eye':
                        sidecar['pose']['eyes'].pop()
                    elif self.mode == 'sbs-singular-projection':
                        sidecar['pose']['eyes'][0]['projection'] = [0] * 16
                    elif self.mode == 'sbs-nonfinite-projection':
                        sidecar['pose']['eyes'][0]['projection'][0] = float('nan')
                    elif self.mode == 'sbs-bad-quaternion':
                        sidecar['pose']['eyes'][0]['quaternion'] = [0, 0, 0, 0]
                    elif self.mode == 'sbs-no-pose':
                        del sidecar['pose']
                    elif self.mode == 'sbs-segment-dimensions':
                        sidecar['eye_width'] = 320
                    elif self.mode == 'sbs-segment-width':
                        sidecar['width'] = 640
                    elif self.mode == 'sbs-layout-changes' and frame == 2:
                        sidecar['layout'] = 'right-left'
                    elif self.mode == 'sbs-stereo-changes' and frame == 2:
                        sidecar['stereo'] = False
                    elif self.mode == 'sbs-bad-session':
                        sidecar['pose']['session_id'] = 'http://private/?token=secret'
                    elif self.mode == 'sbs-bad-frame':
                        sidecar['pose']['frame_id'] = True
                    elif self.mode == 'sbs-alias-disagreement':
                        sidecar['pose']['pose_id'] += 1
                    elif self.mode == 'sbs-no-identity':
                        sidecar['pose'] = dict(eyes=eyes())
                if self.pose_packet:
                    sidecar['pose'] = dict(sent_mono_ms=self.pose_packet['sent_mono_ms'])
                if self.mode == 'wrong-request':
                    sidecar['request_id'] += 100
                elif self.mode == 'duplicate-frame':
                    sidecar['frame_id'] = 3
                elif self.mode == 'overlap':
                    sidecar['media_start_s'] = 0
                elif self.mode == 'nan-age':
                    sidecar['age_ms'] = float('nan')
                elif self.mode == 'no-pose-echo':
                    sidecar.pop('pose', None)
                await socket.send_json(sidecar)
                fragment = box(b'moof') + box(b'mdat', b'fixture-not-a-codec')
                if self.mode == 'bad-box':
                    fragment = struct.pack('>I4s', 99999, b'moof')
                await socket.send_bytes(fragment)
                if self.mode == 'early-close':
                    await socket.close()
                    break
            return socket

        async def control(request):
            self.requests.append(request.path)
            return web.Response(status=500)

        app = web.Application()
        app.router.add_get('/ws/macview', video)
        app.router.add_get('/ws/macview-pose', pose)
        app.router.add_get('/ws/control', control)
        self.server = TestServer(app)
        await self.server.start_server()
        self.origin = str(self.server.make_url('/'))

    async def asyncTearDown(self):
        await self.server.close()

    async def test_default_observer_checks_fragment_contract_without_pose_or_control(self):
        report = await qualify(self.origin, segments=4)
        self.assertTrue(report['qualified'], report)
        self.assertEqual(self.requests, ['/ws/macview'])
        self.assertEqual(self.queries, [{}])
        self.assertEqual(len(report['evidence']['samples']), 4)
        self.assertEqual(report['evidence']['sources_claimed'], ['SIMULATED'])
        self.assertFalse(report['evidence']['init']['stereo'])
        self.assertTrue(all(p['type'] == 'next' for p in self.sent_requests))
        self.assertIn('no codec decode', report['limitations'][0])
        for sample in report['evidence']['samples']:
            self.assertGreaterEqual(sample['receipt_age_bound_ms'], sample['reported_render_age_ms'])

    async def test_stationary_pose_is_opt_in_and_requires_echo(self):
        report = await qualify(self.origin, segments=3, probe_pose=True)
        self.assertTrue(report['qualified'], report)
        self.assertEqual(self.requests, ['/ws/macview-pose', '/ws/macview'])
        self.assertEqual(self.pose_packet['head']['position'], [0, 1.6, 0])
        self.assertNotIn('v', self.pose_packet)
        self.mode = 'no-pose-echo'
        report = await qualify(self.origin, segments=3, probe_pose=True)
        self.assertFalse(report['qualified'])
        self.assertIn('not echoed', report['errors'][0])

    async def test_sbs_requests_explicit_layout_and_checks_declared_pair_receive_only(self):
        self.mode = 'sbs'
        report = await qualify(self.origin, segments=3, layout='sbs-left-right')
        self.assertTrue(report['qualified'], report)
        self.assertEqual(self.requests, ['/ws/macview'])
        self.assertEqual(self.queries, [{'layout': 'sbs-left-right'}])
        self.assertIsNone(self.pose_packet)
        self.assertTrue(all(packet['type'] == 'next' for packet in self.sent_requests))
        init = report['evidence']['init']
        self.assertTrue(init['stereo'])
        self.assertEqual((init['width'], init['height'], init['eye_width'], init['eye_height']), (1280, 720, 640, 720))
        for sample in report['evidence']['samples']:
            self.assertEqual(sample['layout'], 'sbs-left-right')
            self.assertEqual([view['eye'] for view in sample['declared_eye_views']], ['left', 'right'])
            self.assertEqual(sample['declared_pose_identity']['session_id'], 'owner-viewer')
        self.assertIn('declared packing', report['limitations'][-1])

    async def test_sbs_bad_eye_pairs_and_inconsistent_metadata_fail(self):
        for mode in ('sbs-reversed-pair', 'sbs-duplicate-eye', 'sbs-missing-eye', 'sbs-singular-projection',
                     'sbs-nonfinite-projection', 'sbs-bad-quaternion', 'sbs-no-pose', 'sbs-segment-dimensions',
                     'sbs-segment-width', 'sbs-layout-changes', 'sbs-stereo-changes', 'sbs-bad-session',
                     'sbs-bad-frame', 'sbs-alias-disagreement'):
            with self.subTest(mode=mode):
                self.mode = mode
                report = await qualify(self.origin, segments=3, layout='sbs-left-right')
                self.assertFalse(report['qualified'], report)
                self.assertTrue(report['errors'])
        self.assertNotIn('/ws/macview-pose', self.requests)
        self.assertNotIn('/ws/control', self.requests)

    async def test_sbs_requires_valid_packed_init_dimensions_and_layout(self):
        for mode in ('sbs-init-width', 'sbs-init-eye-width', 'sbs-init-height', 'sbs-init-bool-dimension',
                     'sbs-init-layout', 'sbs-init-flat'):
            with self.subTest(mode=mode):
                self.mode = mode
                report = await qualify(self.origin, segments=3, layout='sbs-left-right')
                self.assertFalse(report['qualified'], report)
                self.assertTrue(report['errors'])

    async def test_sbs_optional_pose_identity_may_be_absent_but_pair_is_required(self):
        self.mode = 'sbs-no-identity'
        report = await qualify(self.origin, segments=3, layout='sbs-left-right')
        self.assertTrue(report['qualified'], report)
        self.assertEqual(report['evidence']['samples'][0]['declared_pose_identity'], {})

    async def test_sbs_rejects_pose_probe_or_unsupported_layout_without_connecting(self):
        report = await qualify(self.origin, layout='sbs-left-right', probe_pose=True)
        self.assertFalse(report['qualified'])
        self.assertIn('cannot use --probe-pose', report['errors'][0])
        self.assertFalse((await qualify(self.origin, layout='unknown'))['qualified'])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.queries, [])

    async def test_invalid_flow_and_payloads_fail(self):
        for mode in ('wrong-request', 'duplicate-frame', 'overlap', 'nan-age', 'bad-box', 'no-stereo', 'early-close'):
            with self.subTest(mode=mode):
                self.mode = mode
                report = await qualify(self.origin, segments=3)
                self.assertFalse(report['qualified'], report)
                self.assertTrue(report['errors'])

    async def test_redirect_never_reaches_control(self):
        self.mode = 'redirect'
        report = await qualify(self.origin, segments=3)
        self.assertFalse(report['qualified'])
        self.assertEqual(self.requests, ['/ws/macview'])
        self.assertIn('redirects', report['errors'][0])

    async def test_silent_stream_has_one_overall_deadline(self):
        self.mode = 'stall'
        report = await qualify(self.origin, segments=3, timeout=.04)
        self.assertFalse(report['qualified'])
        self.assertIn('deadline', report['errors'][0])
        self.assertLess(report['elapsed_seconds'], .5)

    async def test_unsafe_origins_and_bad_parameters_open_no_connection(self):
        for origin in ('http://example.com', 'http://user:password@127.0.0.1', self.origin + 'ws/control'):
            self.assertFalse((await qualify(origin, segments=3))['qualified'])
        for count in (True, 0, 201):
            self.assertFalse((await qualify(self.origin, segments=count))['qualified'])
        self.assertFalse((await qualify(self.origin, timeout=float('nan')))['qualified'])
        self.assertEqual(self.requests, [])


class MP4BoundaryTests(unittest.TestCase):
    def test_extended_and_eof_sizes_and_truncation(self):
        self.assertEqual(boxes(struct.pack('>I4sQ', 1, b'mdat', 17) + b'x'), [b'mdat'])
        self.assertEqual(boxes(struct.pack('>I4s', 0, b'mdat') + b'x'), [b'mdat'])
        for body in (b'x', struct.pack('>I4s', 1, b'mdat'), struct.pack('>I4s', 7, b'mdat')):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    boxes(body)


if __name__ == '__main__':
    unittest.main()
