"""Producer -> retained Mac -> production dot renderer scene replacement.

Synthetic calibrated observations only; actual HTTP, Three.js and Chromium GPU.
This does not qualify physical LiDAR or target GPU capacity.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import numpy as np

from jetson.mapping.dense import DenseMap, unpack
from jetson.mapping.geometry import Calibration
from jetson.mapping.phone import RGBDFrame
from jetson.mapping.revisit import DepthView
from tools.dense_map import DenseAccumulator

ROOT = Path(__file__).resolve().parents[2]
SESSION = '123e4567-e89b-12d3-a456-426614174000'
OCCLUDED = (0, 0, -3000, 0, 0, 255, 1)
OUTSIDE = (10000, 0, -1000, 255, 255, 0, 1)


def dots(depth, rgb):
    # Identical viewing rays: green background becomes visible through the
    # exact pixels covered by red foreground, without synthesized geometry.
    return [(x * depth, y * depth, -depth * 1000, *rgb, 1)
            for y in range(-240, 241, 40) for x in range(-240, 241, 40)]


def view(frame_id):
    frame = RGBDFrame(
        dict(_mapping_sensor_key=(SESSION, 1, 0, frame_id, float(frame_id))),
        np.zeros((8, 8, 3), np.uint8), np.full((8, 8), 2, np.float32),
        np.full((8, 8), 2, np.uint8), Calibration(8, 8, 4, 4, 4, 4), np.eye(4), b'')
    return DepthView.from_frame(frame)


@unittest.skipUnless(shutil.which('node') and (ROOT / 'quest/node_modules/playwright').is_dir(),
                     'Node/Chromium required')
class DenseRevisitBrowserTests(unittest.TestCase):
    def test_native_revisit_resync_returning_object_and_gpu_dot_pixels(self):
        now = [0.]
        source = DenseMap(cache_only=True, ttl=30, clock=lambda: now[0])
        source.source = [SESSION, 1]
        mac = DenseAccumulator('http://127.0.0.1:8443')
        foreground, background = dots(1, (255, 0, 0)), dots(2, (0, 255, 0))
        source.apply([*foreground, *background, OCCLUDED, OUTSIDE])
        fixture_lock = threading.Lock()

        def pull(**changes):
            cursor = dict(mac.cursor, **changes)
            if isinstance(cursor.get('snapshot'), str):
                cursor['snapshot'] = cursor['snapshot'] == 'true'
            metadata, rows = unpack(source.delta(**cursor))
            mac.accept(metadata, rows)
            return metadata, rows

        pull()
        source_epoch, mac_epoch = source.epoch, mac.cloud.epoch
        status = dict(source='SIMULATED DOT REVISIT', session_id=SESSION, map_epoch=1,
                      generation=0, revision=0, mesh_revision=0, points=0, triangles=0,
                      fresh=True, connected=True, age_ms=0)

        def fixture(operation):
            with fixture_lock:
                if operation == 'clear':
                    now[0] = .1
                    source.publish_view(view(2))
                    metadata, rows = pull()
                elif operation == 'resync':
                    now[0] = 2.5  # View is stale, while original source cells still fit TTL30.
                    metadata, rows = pull(epoch=None, after=0)
                    self.assertTrue(metadata['reset'])
                    self.assertGreater(metadata['view']['age_ms'], 2000)
                    self.assertFalse(any(row in foreground for row in rows))
                elif operation == 'return':
                    now[0] = 2.6  # Earlier than the five-second identical-record coalescing window.
                    source.apply(foreground)
                    metadata, rows = pull()
                    self.assertEqual(sum(row in foreground for row in rows), len(foreground))
                    self.assertIn('newer_than_view', metadata)
                elif operation == 'clear-again':
                    now[0] = 2.7
                    source.publish_view(view(3))
                    metadata, rows = pull()
                else:
                    raise KeyError(operation)
                self.assertEqual(source.epoch, source_epoch)
                self.assertEqual(mac.cloud.epoch, mac_epoch)
                keys = set(mac.cloud.cells)
                key = lambda row: tuple(value // 10 for value in row[:3])
                self.assertIn(key(OCCLUDED), keys)
                self.assertIn(key(OUTSIDE), keys)
                self.assertTrue(all(key(row) in keys for row in background))
                expected_red = len(foreground) if operation == 'return' else 0
                self.assertEqual(sum(key(row) in keys for row in foreground), expected_red)
                return dict(operation=operation, source_epoch=source.epoch, mac_epoch=mac.cloud.epoch,
                            foreground_cells=expected_red, mac_cells=len(keys),
                            source_upserts=sum(row[-1] == 1 for row in rows),
                            view_age_ms=metadata.get('view', {}).get('age_ms'),
                            source_snapshot_reset=metadata['reset'],
                            occluded_preserved=True, outside_preserved=True)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path)
                query = parse_qs(path.query)
                get = lambda key, default: query.get(key, [default])[0]
                try:
                    if path.path == '/dense/delta.bin':
                        body = mac.cloud.delta(
                            get('epoch', None), int(get('after', 0)), get('snapshot', 'false') == 'true',
                            min(8192, int(get('limit', 8192))),
                            int(query['anchor'][0]) if 'anchor' in query else None)
                        self.reply(body, 'application/octet-stream')
                    elif path.path == '/map/status':
                        self.reply(json.dumps(status).encode())
                    elif path.path.startswith('/fixture/'):
                        self.reply(json.dumps(fixture(path.path.rsplit('/', 1)[-1])).encode())
                    elif path.path.startswith(('/quest/', '/tools/mac_render/')):
                        file = (ROOT / path.path.lstrip('/')).resolve()
                        if not file.is_relative_to(ROOT) or not file.is_file():
                            self.send_error(404)
                            return
                        self.reply(file.read_bytes(), 'text/html' if file.suffix == '.html' else 'text/javascript')
                    else:
                        self.send_error(404)
                except Exception as error:
                    self.reply(json.dumps(dict(error=str(error))).encode(), status=500)

            def reply(self, body, content_type='application/json', status=200):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                ['node', str(ROOT / 'tools/tests/dense_revisit.browser.mjs'),
                 f'http://127.0.0.1:{server.server_port}', str(len(foreground))],
                capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed'])
            self.assertEqual(report['initial_drawn_cells'], 2 * len(foreground) + 2)
            self.assertEqual(report['cleared_drawn_cells'], len(background) + 2)
            self.assertEqual(report['returned_upserts'], len(foreground))
            self.assertTrue(report['expired_view_resync_preserved_scene'])
            self.assertTrue(report['occluded_and_outside_gpu_preserved'])
            self.assertFalse(report['hardware_qualified'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
