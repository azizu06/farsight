"""Default rendering preserves measured dot detail and isolated native returns.

Both versions use the same accepted RGB-D samples, HTTP source, camera and
supporting modules. Only render.mjs changes from the recorded baseline.
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

import numpy as np

from jetson.mapping.dense import DenseMap, RECORD
from jetson.mapping.geometry import Calibration
from jetson.mapping.phone import RGBDFrame, measured_points

ROOT = Path(__file__).resolve().parents[2]
BASELINE = '81d915b'
SESSION = '123e4567-e89b-12d3-a456-426614174000'


def native_fixture():
    height = width = 100
    yy, xx = np.indices((height, width))
    checker = (xx // 2 + yy // 2) % 2  # Approximate 2 cm color features, sampled every 1 cm.
    rgb = np.where(checker[..., None] == 0, np.array([255, 0, 0]),
                   np.array([0, 255, 0])).astype(np.uint8)
    confidence = np.zeros((height, width), np.uint8)
    confidence[30:71, 30:71] = 2
    confidence[50, 95] = 2  # A genuine isolated accepted return beyond the checker patch.
    rgb[50, 95] = [255, 255, 0]
    # Slightly over 1 m avoids placing every native sample exactly on a cell
    # boundary after floating-point millimetre quantization.
    frame = RGBDFrame({}, rgb, np.full((height, width), 1.001, np.float32), confidence,
                      Calibration(width, height, 100, 100, 50, 50), np.eye(4), b'')
    points, colors = measured_points(frame)
    cloud = DenseMap(capacity=10000, journal_size=10000)
    cloud.clear([SESSION, 1])
    cloud.integrate(points, colors)
    rows = [RECORD.unpack(cell[1]) for cell in cloud.cells.values()]
    yellow = [row for row in rows if row[3:6] == (255, 255, 0)]
    assert len(points) == 41 * 41 + 1 and len(rows) == len(points)
    assert len(yellow) == 1
    return cloud, dict(source_cells=len(rows), native_accepted=len(points),
                       yellow_xyz=[value / 1000 for value in yellow[0][:3]],
                       checker_depth=-rows[0][2] / 1000)


def coarse_mesh(depth):
    points = [(-.22, -.22, -depth), (.22, -.22, -depth),
              (-.22, .22, -depth), (.22, .22, -depth)]
    vertices = b''.join(struct.pack('<3f4B', *point, 128, 128, 0, 255) for point in points)
    return struct.pack('<II', 4, 6) + vertices + struct.pack('<6I', 0, 1, 2, 1, 3, 2)


@unittest.skipUnless(shutil.which('node') and (ROOT / 'quest/node_modules/playwright').is_dir(),
                     'Node/Chromium required')
class LiveDotDetailBrowserTests(unittest.TestCase):
    def test_default_dot_pixels_keep_native_color_features_and_isolated_returns(self):
        baseline = subprocess.run(
            ['git', 'show', f'{BASELINE}:tools/mac_render/render.mjs'], cwd=ROOT,
            capture_output=True, check=True).stdout
        cloud, details = native_fixture()
        mesh = coarse_mesh(details['checker_depth'])
        status = dict(source='SIMULATED NATIVE DOT DETAIL', session_id=SESSION, map_epoch=1,
                      generation=0, revision=1, mesh_revision=1, points=0, triangles=2,
                      fresh=True, connected=True, age_ms=0)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                get = lambda key, default: query.get(key, [default])[0]
                path, version = url.path, None
                if path.startswith(('/before/', '/after/')):
                    _, version, relative = path.split('/', 2)
                    path = '/' + relative
                if path == '/dense/delta.bin':
                    body = cloud.delta(get('epoch', None), int(get('after', 0)),
                                       get('snapshot', 'false') == 'true',
                                       min(8192, int(get('limit', 8192))),
                                       int(query['anchor'][0]) if 'anchor' in query else None)
                    self.reply(body, 'application/octet-stream')
                elif path == '/map/status':
                    self.reply(json.dumps(status).encode())
                elif path == '/map/mesh.bin':
                    self.reply(mesh, 'application/octet-stream', headers={
                        'X-Map-Session': SESSION, 'X-Map-Epoch': '1',
                        'X-Map-Generation': '0', 'X-Map-Revision': '1'})
                elif path.startswith(('/quest/', '/tools/mac_render/')):
                    file = (ROOT / path.lstrip('/')).resolve()
                    if not file.is_relative_to(ROOT) or not file.is_file():
                        self.send_error(404)
                        return
                    body = baseline if version == 'before' and path == '/tools/mac_render/render.mjs' else file.read_bytes()
                    self.reply(body, 'text/html' if file.suffix == '.html' else 'text/javascript')
                else:
                    self.send_error(404)

            def reply(self, body, content_type='application/json', headers=None):
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
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
                ['node', str(ROOT / 'tools/tests/live_dot_detail.browser.mjs'),
                 f'http://127.0.0.1:{server.server_port}', json.dumps(details)],
                capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed'])
            self.assertEqual(report['after']['dense']['gpu_drawn_cells'], details['source_cells'])
            self.assertGreater(report['after']['checker_contrast'], report['before']['checker_contrast'] + .3)
            self.assertGreater(report['after']['yellow_pixels'], 3)
            self.assertFalse(report['hardware_qualified'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
