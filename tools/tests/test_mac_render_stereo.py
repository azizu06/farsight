"""Stereo wire/admission/provenance checks; not stereo raster or Quest evidence."""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock
from urllib.parse import parse_qs
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from tools.mac_render.protocol import eye_views, parse_pose
from tools.mac_render.server import RenderEncoder, create_app, parse_args, render_page_query
from tools.mac_render.stereo import SBS, capture_format
from tools.operator_relay import create_app as relay_app


def pair():
    return [dict(eye=eye, position=[x, 1.6, 0], quaternion=[0, 0, 0, 1],
                 projection=[1.2, 0, 0, 0, 0, 1.4, 0, 0, x, .07, -1.0002, -1, 0, 0, -.020002, 0])
            for eye, x in [('right', .032), ('left', -.032)]]


def metadata():
    return dict(stereo=True, layout=SBS, eye_width=960, eye_height=1080,
                poseEcho=dict(session_id='xr-session', frame_id=17, pose_id=17, eyes=pair()))


class StereoContractTests(unittest.TestCase):
    def test_labels_and_asymmetric_projections_survive_parser_in_any_order(self):
        parsed = parse_pose(dict(type='pose', version=1, stereo=True, eyes=pair(),
            sent_mono_ms=1, sent_wall_ms=2, head=dict(position=[0, 1.6, 0], quaternion=[0, 0, 0, 1])), 3)
        self.assertEqual(parsed['eyes'], pair())
        for invalid in [pair()[:1], [pair()[0], pair()[0]],
                        [{**eye, 'projection': [1] * 16} for eye in pair()]]:
            with self.assertRaises(ValueError): eye_views(invalid, require_pair=True)

    def test_no_stereo_claim_for_missing_mono_or_malformed_pair(self):
        valid = metadata()
        self.assertEqual(capture_format(valid, SBS, 1920, 1080),
            dict(stereo=True, layout=SBS, eye_width=960, eye_height=1080))
        for invalid in [{}, {**valid, 'stereo': False}, {**valid, 'layout': 'unknown'},
                        {**valid, 'eye_width': 640}, {**valid, 'poseEcho': None},
                        {**valid, 'poseEcho': {**valid['poseEcho'], 'eyes': pair()[:1]}}]:
            self.assertIsNone(capture_format(invalid, SBS, 1920, 1080))
        self.assertEqual(capture_format({}, None, 1280, 720), {'stereo': False})
        self.assertIsNone(capture_format(valid, None, 1280, 720))

    def test_mode_defaults_and_actual_render_query(self):
        mono, stereo = parse_args([]), parse_args(['--stereo', SBS])
        self.assertEqual((mono.width, mono.height, mono.stereo), (1280, 720, None))
        self.assertEqual((stereo.width, stereo.height), (1920, 1080))
        query = parse_qs(render_page_query(stereo))
        self.assertEqual(query['layout'], [SBS]); self.assertEqual(query['camera'], ['snap'])
        fallback = parse_args(['--stereo', SBS, '--width', '1280', '--height', '720'])
        self.assertEqual((fallback.width, fallback.height), (1280, 720))


class StereoWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_vt_launch_enables_output_low_delay_and_fixed_packed_dimensions(self):
        encoder = RenderEncoder(None, 1920, 1080, 30, 8_000_000, True, layout=SBS)
        process = type('Process', (), {'returncode': None, 'kill': lambda self: None, 'wait': AsyncMock()})()
        async def blocked(): await asyncio.Event().wait()
        arguments = ['ffmpeg', '-flags', 'low_delay', '-i', 'pipe:0', '-c:v', 'h264_videotoolbox', 'pipe:1']
        with patch('tools.mac_render.server.shutil.which', return_value='/ffmpeg'), \
             patch('tools.mac_render.server.screencast_to_ffmpeg', return_value=(arguments, 'h264_videotoolbox')) as factory, \
             patch('tools.mac_render.server.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as launch, \
             patch.object(encoder, '_capture', side_effect=blocked), \
             patch.object(encoder, '_read', side_effect=blocked), \
             patch.object(encoder, '_stderr', side_effect=blocked):
            await encoder.start()
            command = list(launch.call_args.args)
            self.assertLess(command.index('-i'), command.index('+low_delay'))
            self.assertEqual(command[command.index('-s:v') + 1], '1920x1080')
            self.assertEqual(factory.call_args.kwargs, {'fragment_ms': 0})
            await encoder.close()

    async def test_legacy_viewer_rejected_and_stereo_init_waits_for_valid_pair(self):
        args = parse_args(['--stereo', SBS, '--no-browser'])
        app = create_app(args)
        encoder = RenderEncoder(None, args.width, args.height, 30, 8_000_000, False, layout=SBS)
        encoder.init, encoder.mime = b'init', 'video/mp4'
        app['mac'].encoder = encoder
        client = TestClient(TestServer(app)); await client.start_server()
        try:
            with self.assertRaises(WSServerHandshakeError): await client.ws_connect('/ws/macview')
            socket = await client.ws_connect('/ws/macview?layout=' + SBS)
            self.assertEqual((await socket.receive_json())['type'], 'macview_error')
            await socket.close()
            encoder.last_snapshot = metadata()
            socket = await client.ws_connect('/ws/macview?layout=' + SBS)
            init = await socket.receive_json()
            self.assertTrue(init['stereo']); self.assertEqual(init['layout'], SBS)
            self.assertEqual((init['eye_width'], init['eye_height']), (960, 1080))
            self.assertEqual(await socket.receive_bytes(), b'init')
            await socket.close()
        finally: await client.close()

    async def test_layout_opt_in_survives_actual_operator_relay(self):
        args = parse_args(['--stereo', SBS, '--no-browser'])
        app = create_app(args)
        encoder = RenderEncoder(None, args.width, args.height, 30, 8_000_000, False, layout=SBS)
        encoder.init, encoder.mime, encoder.last_snapshot = b'init', 'video/mp4', metadata()
        app['mac'].encoder = encoder
        producer = TestServer(app); await producer.start_server()
        relay = TestClient(TestServer(relay_app('http://127.0.0.1:1', render_upstream=str(producer.make_url('/')))))
        await relay.start_server()
        try:
            socket = await relay.ws_connect('/ws/macview?layout=' + SBS)
            init = await socket.receive_json()
            self.assertEqual(init['layout'], SBS); self.assertTrue(init['stereo'])
            self.assertEqual(await socket.receive_bytes(), b'init')
            await socket.close()
        finally:
            await relay.close(); await producer.close()

    async def test_only_one_complete_pair_is_published_with_exact_pose_identity(self):
        for count, valid, expected in [(1, True, 1), (1, False, 0), (2, True, 0)]:
            encoder = RenderEncoder(None, 1920, 1080, 30, 8_000_000, False, layout=SBS)
            pose = {**metadata()['poseEcho'], 'received_mono_ms': 10}
            encoder.frame_id = count
            encoder.samples = [dict(frame_id=i + 1, capture_mono_ms=20, capture_wall_ms=30,
                pose=pose, format=capture_format(metadata(), SBS, 1920, 1080) if valid else None)
                for i in range(count)]
            class Output:
                chunks = [b'fragment', b'']
                async def read(self, _): return self.chunks.pop(0)
            encoder.process = type('Process', (), {'stdout': Output()})()
            queue = encoder.subscribe()
            with patch.object(encoder.reader, 'feed', return_value=[('fragment', b'payload')]), \
                 patch('tools.mac_render.server.fragment_timing', return_value=(0, count, 0, count / 30)), \
                 patch('tools.mac_render.server._is_keyframe', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'encoder closed'): await encoder._read()
            self.assertEqual(len(queue.items), expected)
            if expected:
                sidecar = queue.items[0]['sidecar']
                self.assertTrue(sidecar['stereo']); self.assertEqual(sidecar['pose']['frame_id'], 17)
                self.assertEqual(sidecar['pose']['eyes'], pair())
