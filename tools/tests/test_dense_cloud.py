"""Actual Python DenseMap -> Node receiver. Synthetic/frozen TTL capacity, not a benchmark."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from jetson.mapping.dense import DenseMap, RECORD, packet, unpack
from jetson.mapping.state import MapState

ROOT = Path(__file__).resolve().parents[2]
FIRST = '123e4567-e89b-12d3-a456-426614174000'
SECOND = '123e4567-e89b-12d3-a456-426614174001'


class DenseSimulationIdentityTests(unittest.TestCase):
    def test_initial_demo_dense_source_matches_session_and_survives_reset(self):
        state = MapState(demo=True)
        self.addCleanup(state.dense_worker.close)
        self.assertEqual(state.dense.source, list(state.identity))
        previous_epoch = state.dense.epoch
        state.clear()
        self.assertNotEqual(state.dense.epoch, previous_epoch)
        self.assertEqual(state.dense.source, ['simulated-room', 1])
        phone = MapState()
        self.addCleanup(phone.dense_worker.close)
        self.assertIsNone(phone.dense.source)
        phone.start_session((FIRST, 2), 'Phone fixture')
        phone.clear()
        self.assertEqual(phone.dense.source, [FIRST, 2])

    def run_node(self, bodies, checks):
        module = (ROOT/'tools/mac_render/dense-cloud.mjs').as_uri()
        script = f'''import assert from 'node:assert/strict';
import {{ readFileSync }} from 'node:fs';
import {{ decodeDenseDelta, DenseCloudState, DenseCloudReceiver }} from {json.dumps(module)};
const bodies = JSON.parse(readFileSync(0, 'utf8')).map(value => {{
  const bytes = Buffer.from(value, 'base64');
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}});
{checks}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script],
            input=json.dumps([base64.b64encode(body).decode() for body in bodies]),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_python_demo_delta_decodes_and_resets_actual_js_receiver(self):
        state = MapState(demo=True)
        self.addCleanup(state.dense_worker.close)
        state.dense.apply([(0, 0, 0, 7, 8, 9, 1)])
        initial = state.dense.delta()
        first, _ = unpack(initial)
        state.clear()
        empty_reset = state.dense.delta(first['epoch'], first['cursor'])
        reset, _ = unpack(empty_reset)
        state.dense.apply([(400, 0, 0, 10, 11, 12, 1)])
        continuation = state.dense.delta(reset['epoch'], reset['cursor'])
        self.run_node([initial, empty_reset, continuation], '''
const state = new DenseCloudState({ capacity: 8, clock: () => 0 });
const receiver = new DenseCloudReceiver({ state, clock: () => 0 });
receiver.accept(decodeDenseDelta(bodies[0]));
assert.equal(receiver.sourceLabel, 'SIMULATED RGB-D');
assert.equal(receiver.identity, 'simulated_rgbd:simulated-room:1');
assert.equal(state.size, 1); assert.equal(state.cells.has('0_0_0'), true);
receiver.accept(decodeDenseDelta(bodies[1]));
assert.equal(receiver.sourceLabel, 'SIMULATED RGB-D');
assert.equal(state.size, 0); assert.equal(receiver.resets, 2);
receiver.accept(decodeDenseDelta(bodies[2]));
assert.equal(receiver.sourceLabel, 'SIMULATED RGB-D');
assert.equal(state.size, 1); assert.equal(state.cells.has('40_0_0'), true);
assert.equal(state.cells.has('0_0_0'), false);
receiver.dispose();
''')

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_unknown_simulation_or_real_identities_cannot_supply_dense_samples(self):
        sources = [None, ['not-a-uuid', 1], ['simulated-room-other', 1], ['SIMULATED-ROOM', 1],
                   ['simulated-room', -1], ['simulated-room', True], ['simulated-room', 1.5],
                   ['simulated-room', 2**53], [FIRST, True]]
        bodies = []
        for source in [[FIRST, 1], {'kind': 'arkit_mesh_neutral', 'session': FIRST, 'epoch': 1},
                       ['simulated-room', 1], {'kind': 'arkit_mesh_neutral', 'session': 'simulated-room', 'epoch': 1}, *sources]:
            cloud = DenseMap(clock=lambda: 0)
            cloud.source = source
            cloud.apply([(0, 0, 0, 7, 8, 9, 1)])
            bodies.append(cloud.delta())
        metadata, records = unpack(bodies[0])
        del metadata['source']
        bodies.append(packet(metadata, [RECORD.pack(*record) for record in records]))
        self.run_node(bodies, '''
assert.equal(decodeDenseDelta(bodies[0]).identity.label, 'SOURCE RGB-D SAMPLES');
assert.equal(decodeDenseDelta(bodies[1], 'geometry').identity.label, 'ARKIT MESH · NEUTRAL RGB');
assert.throws(() => decodeDenseDelta(bodies[2], 'geometry'));
assert.throws(() => decodeDenseDelta(bodies[3], 'geometry'));
for (const body of bodies.slice(4)) assert.throws(() => decodeDenseDelta(body));
''')


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
