"""Production renderer and GPU buffers fed by actual DenseMap over loopback.

Synthetic samples only. Does not qualify physical LiDAR, Quest or GPU capacity.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from jetson.mapping.dense import DenseMap

ROOT = Path(__file__).resolve().parents[2]
FIRST = '123e4567-e89b-12d3-a456-426614174000'
SECOND = '123e4567-e89b-12d3-a456-426614174001'


def records(count):
    return [(-2000 + i % 32 * 125, i // 32 * 50, -1000 - i % 7 * 250,
             160 + i % 95, 128 + i % 127, 200, 1) for i in range(count)]


def mesh():
    points = [(-1., 0., -1.), (1., 0., -1.), (0., 1., -1.)]
    return struct.pack('<II', 3, 3) + b''.join(struct.pack('<3f4B', *point, 160, 160, 160, 255)
                                             for point in points) + struct.pack('<3I', 0, 1, 2)


@unittest.skipUnless(shutil.which('node') and (ROOT/'quest/node_modules/playwright').is_dir(), 'Node/Chromium required')
class MacRenderDenseBrowserTests(unittest.TestCase):
    def test_actual_dense_gpu_delete_reset_source_mismatch_and_idle_capture(self):
        cloud = DenseMap(capacity=20000, journal_size=20000)
        cloud.clear([FIRST, 1]); cloud.apply(records(1024))
        current = dict(source='SIMULATED', session_id=FIRST, map_epoch=1, generation=0,
                       revision=1, mesh_revision=1, points=3, triangles=1, fresh=True, connected=True, age_ms=0)
        flags = dict(paused=False)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path); query = parse_qs(path.query)
                get = lambda key, default: query.get(key, [default])[0]
                if path.path == '/dense/delta.bin':
                    if flags['paused']: self.reply(b'{}', status=503); return
                    body = cloud.delta(get('epoch', None), int(get('after', 0)), get('snapshot', 'false') == 'true',
                        min(8192, int(get('limit', 8192))), int(query['anchor'][0]) if 'anchor' in query else None)
                    self.reply(body, 'application/octet-stream'); return
                if path.path == '/map/status': self.reply(json.dumps(current).encode()); return
                if path.path == '/map/mesh.bin':
                    self.reply(mesh(), 'application/octet-stream', headers={
                        'X-Map-Session': current['session_id'], 'X-Map-Epoch': str(current['map_epoch']),
                        'X-Map-Generation': str(current['generation']), 'X-Map-Revision': str(current['mesh_revision'])})
                    return
                if path.path.startswith('/fixture/'):
                    operation = path.path.rsplit('/', 1)[-1]
                    if operation == 'delete': cloud.apply([(*records(1)[0][:3], 0, 0, 0, 0)])
                    elif operation == 'reset': cloud.clear([FIRST, 1])
                    elif operation == 'refill': cloud.apply(records(320))
                    elif operation == 'mismatch': current.update(session_id=SECOND, map_epoch=2)
                    elif operation == 'source-switch': cloud.clear([SECOND, 2]); cloud.apply(records(500))
                    elif operation == 'pause': flags['paused'] = True
                    else: self.send_error(404); return
                    self.reply(b'{"ok":true}'); return
                if path.path.startswith(('/quest/', '/tools/mac_render/')):
                    file = (ROOT/path.path.lstrip('/')).resolve()
                    if not file.is_relative_to(ROOT) or not file.is_file(): self.send_error(404); return
                    self.reply(file.read_bytes(), 'text/html' if file.suffix == '.html' else 'text/javascript'); return
                self.send_error(404)
            def reply(self, body, content_type='application/json', status=200, headers=None):
                self.send_response(status); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                for key, value in (headers or {}).items(): self.send_header(key, value)
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            result = subprocess.run(['node', str(ROOT/'tools/tests/mac_render_dense.browser.mjs'),
                f'http://127.0.0.1:{server.server_port}'], capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed']); self.assertEqual(report['initial_gpu_drawn_cells'], 1024)
            self.assertTrue(report['mismatched_mesh_hidden']); self.assertTrue(report['idle_pixels_consistent'])
            self.assertFalse(report['hardware_qualified'])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__': unittest.main()
