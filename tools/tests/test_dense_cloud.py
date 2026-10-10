"""Actual Python DenseMap -> Node receiver. Synthetic/frozen TTL capacity, not a benchmark."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from jetson.mapping.dense import DenseMap, unpack

ROOT = Path(__file__).resolve().parents[2]
FIRST = '123e4567-e89b-12d3-a456-426614174000'
SECOND = '123e4567-e89b-12d3-a456-426614174001'


@unittest.skipUnless(shutil.which('node') and (ROOT/'quest/node_modules/three').is_dir(), 'Node/Three.js required')
class DenseCloudHTTPTests(unittest.TestCase):
    def test_paging_gpu_ranges_removal_resync_and_two_million_capacity(self):
        owner = self
        self.cloud = DenseMap(clock=lambda: 0)
        self.index = 0; self.limit = 8192
        self.active = self.peak = self.maximum = 0
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path); query = parse_qs(path.query)
                get = lambda key, default: query.get(key, [default])[0]
                if path.path == '/dense/delta.bin':
                    owner.active += 1; owner.peak = max(owner.peak, owner.active)
                    try:
                        body = owner.cloud.delta(get('epoch', None), int(get('after', 0)),
                            get('snapshot', 'false') == 'true', min(owner.limit, int(get('limit', 8192))),
                            int(query['anchor'][0]) if 'anchor' in query else None)
                        _, records = unpack(body); owner.maximum = max(owner.maximum, len(records))
                        self.reply(body, 'application/octet-stream')
                    finally: owner.active -= 1
                    return
                if path.path == '/fixture/reset':
                    owner.cloud = DenseMap(capacity=int(get('capacity', 20)), journal_size=int(get('journal', 100)),
                        cache_only=get('cache', '0') == '1', clock=lambda: 0)
                    owner.cloud.clear([SECOND if get('session', '') == 'second' else FIRST, 1])
                    owner.index = 0; owner.limit = int(get('limit', 8192))
                elif path.path == '/fixture/step':
                    count = int(get('count', 0)); assert 0 <= count <= 8192
                    records = [(i % 2000 * 10, i // 2000 % 1000 * 10, i // 2_000_000 * 10,
                                i % 256, i // 256 % 256, 90, 1)
                               for i in range(owner.index, owner.index + count)]
                    owner.cloud.apply(records); owner.index += count
                elif path.path == '/fixture/delete':
                    i = int(get('index', 0))
                    owner.cloud.apply([(i % 2000 * 10, i // 2000 % 1000 * 10, i // 2_000_000 * 10, 0, 0, 0, 0)])
                elif path.path == '/fixture/bad-source': owner.cloud.source = [FIRST, 99]
                elif path.path == '/fixture/metrics':
                    self.reply(json.dumps(dict(peak_dense_requests=owner.peak, max_records=owner.maximum)).encode()); return
                else:
                    self.send_error(404); return
                self.reply(b'{"ok":true}')
            def reply(self, body, content_type='application/json'):
                self.send_response(200); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            result = subprocess.run(['node', str(ROOT/'tools/tests/dense_cloud_fixture.mjs'),
                f'http://127.0.0.1:{server.server_port}'], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed']); self.assertEqual(report['accumulated_unique_cells'], 2_000_000)
            self.assertLessEqual(report['max_records_per_page'], 8192)
            self.assertFalse(report['physical_capture_qualified'])
            self.assertFalse(report['gpu_capacity_render_qualified'])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__': unittest.main()
