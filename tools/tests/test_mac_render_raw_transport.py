"""Actual renderer HTTP paths; loopback fixtures, no rover or raw stream."""
import asyncio
import gzip
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tools.mac_render.server import create_app, parse_args


class RawTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_keeps_negotiated_compression_and_capture_receipt(self):
        native = b'captured-native-depth-and-jpeg' * 10000
        encoded = gzip.compress(native)
        seen = []

        async def frame(request):
            encoding = request.headers.get('Accept-Encoding')
            seen.append((encoding, request.query.get('after')))
            headers = {'Content-Type': 'application/octet-stream',
                       'X-Capture-Key': 'capture:epoch:frame',
                       'X-Capture-Age-Ms': '31', 'Vary': 'Accept-Encoding'}
            if encoding == 'gzip':
                headers['Content-Encoding'] = 'gzip'
                return web.Response(body=encoded, headers=headers)
            return web.Response(body=native, headers=headers)

        source = web.Application()
        source.router.add_get('/phone/frame.bin', frame)
        upstream = TestServer(source)
        await upstream.start_server()
        app = create_app(parse_args(['--no-browser', '--upstream',
                                     str(upstream.make_url('/'))]))
        client = TestClient(TestServer(app), auto_decompress=False)
        await client.start_server()
        try:
            response = await client.get('/phone/frame.bin?after=old%3A3',
                                        headers={'Accept-Encoding': 'gzip'})
            body = await response.read()
            self.assertEqual(seen, [('gzip', 'old:3')])
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertEqual(response.headers['Vary'], 'Accept-Encoding')
            self.assertEqual(response.headers['X-Capture-Key'], 'capture:epoch:frame')
            self.assertEqual(response.headers['X-Capture-Age-Ms'], '31')
            self.assertEqual(body, encoded)
            self.assertEqual(gzip.decompress(body), native)
        finally:
            await client.close()
            await upstream.close()

    async def test_capture_can_start_while_both_map_connections_are_occupied(self):
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def busy_map(request):
            requests.append(request.path)
            if len(requests) == 2:
                entered.set()
            await release.wait()
            return web.Response(body=b'{}', content_type='application/json')

        async def frame(request):
            return web.Response(body=b'one-capture', headers={
                'X-Capture-Key': 'one', 'X-Capture-Age-Ms': '0'})

        source = web.Application()
        source.router.add_get('/map/status', busy_map)
        source.router.add_get('/dense/delta.bin', busy_map)
        source.router.add_get('/phone/frame.bin', frame)
        upstream = TestServer(source)
        await upstream.start_server()
        app = create_app(parse_args(['--no-browser', '--upstream',
                                     str(upstream.make_url('/'))]))
        client = TestClient(TestServer(app))
        await client.start_server()
        occupied = [asyncio.create_task(client.get(path))
                    for path in ['/map/status', '/dense/delta.bin']]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(client.get('/phone/frame.bin'), 1)
            self.assertFalse(release.is_set(), 'Map transfers are still in flight')
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'one-capture')
        finally:
            release.set()
            await asyncio.gather(*occupied, return_exceptions=True)
            await client.close()
            await upstream.close()

    async def test_capture_status_and_identity_body_stay_verbatim(self):
        async def frame(request):
            if request.query.get('after') == 'same':
                return web.Response(status=204, headers={'X-Capture-Key': 'same'})
            return web.Response(status=503, body=b'{"detail":"camera not ready"}',
                                content_type='application/json')

        source = web.Application()
        source.router.add_get('/phone/frame.bin', frame)
        upstream = TestServer(source)
        await upstream.start_server()
        client = TestClient(TestServer(create_app(parse_args([
            '--no-browser', '--upstream', str(upstream.make_url('/'))]))))
        await client.start_server()
        try:
            response = await client.get('/phone/frame.bin?after=same',
                                        headers={'Accept-Encoding': 'identity'})
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers['X-Capture-Key'], 'same')
            self.assertEqual(await response.read(), b'')
            response = await client.get('/phone/frame.bin',
                                        headers={'Accept-Encoding': 'identity'})
            self.assertEqual(response.status, 503)
            self.assertEqual(await response.read(), b'{"detail":"camera not ready"}')
        finally:
            await client.close()
            await upstream.close()
