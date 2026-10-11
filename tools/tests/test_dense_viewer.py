import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from tools.dense_viewer import create_app


class ViewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowlist_never_forwards_control_or_mutation(self):
        received = []
        async def upstream(request):
            received.append((request.method, request.path_qs))
            return web.Response(body=b'captured-measured-data')
        backend = web.Application(); backend.router.add_route('*', '/{tail:.*}', upstream)
        async with TestServer(backend) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'assets').mkdir(); (root/'index.html').write_text('viewer')
                async with TestClient(TestServer(create_app(str(server.make_url('/')).rstrip('/'), dist=root))) as client:
                    for method, path in [('GET','/ws/control'),('POST','/dense/metrics'),
                                         ('POST','/phone'),('GET','/map/reset'),('GET','/dense/delta.bin')]:
                        r = await client.request(method,path)
                        self.assertIn(r.status,(404,405))
                    self.assertEqual(received,[])
                    r = await client.get('/dense/chunk.bin?id=0_0_0&lod=0')
                    self.assertEqual(await r.read(),b'captured-measured-data')
                    self.assertEqual(received,[('GET','/dense/chunk.bin?id=0_0_0&lod=0')])
