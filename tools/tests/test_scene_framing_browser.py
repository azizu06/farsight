"""Actual default camera framing, native pixels and camera ownership over HTTP."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from jetson.mapping.dense import DenseMap, RECORD

ROOT = Path(__file__).resolve().parents[2]
BASELINE = '7fbd4d2'
SESSION = '123e4567-e89b-12d3-a456-426614174000'
FIRST_CENTER = [-4, 1, -6]


def records(center):
    cx, cy, cz = (round(value * 1000) for value in center)
    return [(cx + x, cy + y, cz, 0, 255, 100, 1)
            for y in range(-1200, 1201, 25) for x in range(-2000, 2001, 25)]


@unittest.skipUnless(shutil.which('node') and (ROOT / 'quest/node_modules/playwright').is_dir(),
                     'Node/Chromium required')
class SceneFramingBrowserTests(unittest.TestCase):
    def test_default_overview_and_tracked_manual_live_canvas_camera_ownership(self):
        baseline = subprocess.run(['git', 'show', f'{BASELINE}:tools/mac_render/render.mjs'],
                                  cwd=ROOT, capture_output=True, check=True).stdout
        cloud = DenseMap(capacity=20000, journal_size=20000)
        current = dict(center=FIRST_CENTER, epoch_number=1)
        fixture_lock = threading.Lock()

        def description():
            rows = records(current['center'])
            return dict(center=current['center'], low=[current['center'][0] - 2, current['center'][1] - 1.2, current['center'][2]],
                        high=[current['center'][0] + 2, current['center'][1] + 1.2, current['center'][2]],
                        source_cells=len(rows), epoch_number=current['epoch_number'],
                        source_epoch=cloud.epoch,
                        source_sha256=hashlib.sha256(b''.join(RECORD.pack(*row) for row in rows)).hexdigest())

        def replace(center):
            with fixture_lock:
                current['center'] = list(center)
                current['epoch_number'] += 1
                cloud.clear([SESSION, current['epoch_number']])
                cloud.apply(records(center))
                return description()

        cloud.clear([SESSION, 1])
        cloud.apply(records(FIRST_CENTER))
        first = description()

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
                    self.reply(json.dumps(dict(source='SIMULATED OFF-ORIGIN ROOM', session_id=SESSION,
                        map_epoch=current['epoch_number'], generation=0, revision=0, mesh_revision=0,
                        points=0, triangles=0, fresh=True, connected=True, age_ms=0)).encode())
                elif path == '/fixture/reset':
                    self.reply(json.dumps(replace([float(get(axis, 0)) for axis in ('x', 'y', 'z')])).encode())
                elif path.startswith(('/quest/', '/tools/mac_render/')):
                    file = (ROOT / path.lstrip('/')).resolve()
                    if not file.is_relative_to(ROOT) or not file.is_file():
                        self.send_error(404)
                        return
                    body = baseline if version == 'before' and path == '/tools/mac_render/render.mjs' else file.read_bytes()
                    self.reply(body, 'text/html' if file.suffix == '.html' else 'text/javascript')
                else:
                    self.send_error(404)

            def reply(self, body, content_type='application/json'):
                self.send_response(200)
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
            result = subprocess.run(['node', str(ROOT / 'tools/tests/scene_framing.browser.mjs'),
                                     f'http://127.0.0.1:{server.server_port}', json.dumps(first)],
                                    capture_output=True, text=True, timeout=70)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed'])
            self.assertEqual(report['before']['source_sha256'], first['source_sha256'])
            self.assertEqual(report['after']['source_sha256'], first['source_sha256'])
            self.assertTrue(report['tracked_and_expired_pose_preserved'])
            self.assertTrue(report['manual_layer_camera_preserved'])
            self.assertTrue(report['live_orbit_camera_preserved'])
            self.assertTrue(report['owned_overview_reframes_new_epoch'])
            self.assertFalse(report['hardware_qualified'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
