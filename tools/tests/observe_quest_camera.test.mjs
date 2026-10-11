import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { CdpObserver, SNAPSHOT_EXPRESSION, debuggerSocket, discoverTarget,
  loopbackOrigin, observe, parseArgs, sanitizeSidecar } from '../observe_quest_camera.mjs';

const APP = 'http://localhost:8080';
const snapshot = { appOrigin: APP, path: '/', diagnostics: { text: 'FPS 72 · frame p95 13.9 ms\nvideo age 640 ms', hidden: false },
  diagnosticsCount: 1, source: { text: 'SIMULATED', hidden: false }, status: { text: 'VIDEO STALE', hidden: false },
  userAgent: 'Mock browser, not headset evidence', visibility: 'visible', documentHidden: false, permit: 'NEVER_KEEP' };

function wsFrame(value, opcode = 1) {
  const payload = Buffer.from(typeof value === 'string' ? value : JSON.stringify(value));
  let header;
  if (payload.length < 126) header = Buffer.from([0x80 | opcode, payload.length]);
  else if (payload.length <= 65535) { header = Buffer.alloc(4); header[0] = 0x80 | opcode; header[1] = 126; header.writeUInt16BE(payload.length, 2); }
  else { header = Buffer.alloc(10); header[0] = 0x80 | opcode; header[1] = 127; header.writeBigUInt64BE(BigInt(payload.length), 2); }
  return Buffer.concat([header, payload]);
}

// Tiny local CDP protocol server. No Chromium, GPU, Quest, ADB or app streams.
async function mockCdp({ value = snapshot, rejectRuntime = false, ignoreRuntime = false, events = true, burst = 0 } = {}) {
  const commands = [], requests = [], sockets = new Set(); let closed = 0;
  let origin;
  const server = createServer((request, response) => {
    requests.push({ method: request.method, path: request.url });
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify([{ type: 'page', url: `${APP}/?macview=1&secret=URL_SECRET`,
      webSocketDebuggerUrl: `${origin.replace('http:', 'ws:')}/devtools/page/mock` }]));
  });
  server.on('upgrade', (request, socket) => {
    sockets.add(socket); socket.on('close', () => { sockets.delete(socket); closed++; });
    requests.push({ method: request.method, path: request.url });
    const accept = createHash('sha1').update(request.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    let pending = Buffer.alloc(0);
    socket.on('data', chunk => {
      pending = Buffer.concat([pending, chunk]);
      while (pending.length >= 2) {
        const opcode = pending[0] & 15, masked = !!(pending[1] & 128);
        let length = pending[1] & 127, offset = 2;
        if (length === 126) { if (pending.length < 4) return; length = pending.readUInt16BE(2); offset = 4; }
        else if (length === 127) { if (pending.length < 10) return; length = Number(pending.readBigUInt64BE(2)); offset = 10; }
        if (pending.length < offset + (masked ? 4 : 0) + length) return;
        const mask = masked ? pending.subarray(offset, offset + 4) : null; offset += masked ? 4 : 0;
        const payload = Buffer.from(pending.subarray(offset, offset + length)); pending = pending.subarray(offset + length);
        if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
        if (opcode === 8) { socket.end(wsFrame('', 8)); return; }
        if (opcode !== 1) continue;
        const command = JSON.parse(payload.toString()); commands.push(command);
        if (command.method === 'Runtime.evaluate' && ignoreRuntime) continue;
        if (command.method === 'Runtime.evaluate' && rejectRuntime) socket.write(wsFrame({ id: command.id, error: { message: 'SECRET_BACKEND_ERROR' } }));
        else socket.write(wsFrame({ id: command.id, result: command.method === 'Runtime.evaluate' ? { result: { value } } : {} }));
        if (command.method === 'Network.enable' && events) {
          const event = (requestId, packet, opcode = 1) => ({ method: 'Network.webSocketFrameReceived', params: {
            requestId, timestamp: 42.5, response: { opcode, payloadData: JSON.stringify(packet) } } });
          socket.write(wsFrame(event('late', { type: 'video_segment', macview: 'macview_segment', frame_id: 5,
            source: 'SIMULATED', age_ms: 700, permit: 'SECRET_PERMIT', pose: { client_nonce: 'SECRET_NONCE' }, url: 'SECRET_URL' })));
          socket.write(wsFrame({ method: 'Network.webSocketCreated', params: { requestId: 'camera', url: 'ws://localhost:8080/ws/video?token=SECRET_QUERY' } }));
          socket.write(wsFrame(event('camera', { type: 'video_segment', frame: 8, source: 'IPHONE LIDAR', age_ms: 70,
            jetson_encode_ms: 9, camera_ingest_ms: 3, relay_request_ms: 90, relay_network_bound_ms: 80,
            relay_residence_ms: 1, samples: [{ frame: 8, media_start_s: 1, permit: 'SECRET_SAMPLE' }] })));
          socket.write(wsFrame({ method: 'Network.webSocketCreated', params: { requestId: 'mac', url: 'ws://localhost:8080/ws/macview' } }));
          socket.write(wsFrame(event('mac', { type: 'video_segment', macview: 'macview_segment', frame_id: 10, source: 'SIMULATED', age_ms: 100 })));
          for (let i = 0; i < burst; i++) socket.write(wsFrame(event('camera', { type: 'video_segment', frame: 10 + i, source: 'SIMULATED' })));
          socket.write(wsFrame(event('control', { type: 'control_status', permit: 'SECRET_CONTROL' })));
          socket.write(wsFrame(event('camera', { type: 'video_segment', secret: 'SECRET_BINARY' }, 2)));
          socket.write(wsFrame('x'.repeat(65537)));
        }
      }
    });
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  origin = `http://127.0.0.1:${server.address().port}`;
  return { origin, commands, requests, get closed() { return closed; },
    async close() { for (const socket of sockets) socket.destroy(); await new Promise(resolve => server.close(resolve)); } };
}

test('only credential-free loopback origins and same-port page debug sockets', () => {
  assert.equal(loopbackOrigin('http://[::1]:9222/'), 'http://[::1]:9222');
  for (const bad of ['https://example.com', 'http://127.0.0.2:9222', 'http://user:pass@localhost:9222',
    'http://localhost:9222/?token=private', 'http://localhost:9222/path', 'file:///tmp/private']) assert.throws(() => loopbackOrigin(bad));
  assert.equal(debuggerSocket('ws://127.0.0.1:9222/devtools/page/id', 'http://localhost:9222'), 'ws://127.0.0.1:9222/devtools/page/id');
  for (const bad of ['ws://example.com:9222/devtools/page/id', 'ws://localhost:9223/devtools/page/id',
    'ws://localhost:9222/devtools/browser/id', 'ws://localhost:9222/devtools/page/id?token=private',
    'wss://localhost:9222/devtools/page/id']) assert.throws(() => debuggerSocket(bad, 'http://localhost:9222'));
});

test('discovery rejects wrong path/origin, multiple matches, redirects and oversized JSON', async () => {
  const origin = 'http://localhost:9222';
  const page = url => ({ type: 'page', url, webSocketDebuggerUrl: 'ws://localhost:9222/devtools/page/id' });
  const fetchImpl = body => async (_url, options) => { assert.equal(options.redirect, 'manual'); return new Response(JSON.stringify(body)); };
  await assert.rejects(discoverTarget(origin, APP, { fetchImpl: fetchImpl([page(APP + '/macview.html'), page('http://localhost:8081/')]) }), /matching_page_missing/);
  await assert.rejects(discoverTarget(origin, APP, { fetchImpl: fetchImpl([page(APP + '/'), page(APP + '/?demo=0')]) }), /multiple_matching_pages/);
  await assert.rejects(discoverTarget(origin, APP, { fetchImpl: async () => new Response(null, { status: 302, headers: { Location: 'https://example.com/secret' } }) }), /discovery_http_error/);
  await assert.rejects(discoverTarget(origin, APP, { fetchImpl: async () => new Response(' '.repeat(65537)) }), /discovery_oversized/);
  await assert.rejects(discoverTarget(origin, APP, { fetchImpl: async () => new Response('{}') }), /invalid_targets/);
});

test('sidecars retain only reported timing/frame/source; credentials and pose never copied', () => {
  const value = sanitizeSidecar({ type: 'video_segment', frame: 0, source: 'IPHONE LIDAR', age_ms: NaN,
    encode_pipeline_ms: 20, feed_wait_ms: 1,
    samples: [{ frame: 0, secret: 'SECRET_SAMPLE' }], cookie: 'SECRET_COOKIE', permit: 'SECRET_PERMIT',
    pose: { client_nonce: 'SECRET_NONCE' }, token: 'SECRET_TOKEN', request_id: 'SECRET_REQUEST' });
  assert.equal(value.frame, 0); assert.equal(value.frame_id, null); assert.equal(value.age_ms, null);
  assert.equal(value.samples[0].media_start_s, null); assert.equal(value.macview, null);
  assert.equal(value.encode_pipeline_ms, 20); assert.equal(value.feed_wait_ms, 1); assert.equal(value.relay_request_ms, null);
  assert.equal(JSON.stringify(value).includes('SECRET'), false);
  assert.equal(sanitizeSidecar({ type: 'control_status', permit: 'private' }), null);
  assert.equal(sanitizeSidecar({ type: 'video_segment', source: 'http://host/?token=private' }).source, null);
});

test('command guard rejects all navigation/input/control/close and nonconstant evaluation', async () => {
  const observer = new CdpObserver('ws://localhost:9222/devtools/page/id');
  for (const method of ['Browser.close', 'Page.navigate', 'Target.createTarget', 'Input.dispatchKeyEvent',
    'Runtime.callFunctionOn', 'Network.getResponseBody']) await assert.rejects(observer.command(method), /forbidden/);
  await assert.rejects(observer.command('Runtime.evaluate', { expression: 'new WebSocket("/ws/control")' }), /forbidden/);
});

test('local actual native-WebSocket CDP attach reads existing page only and preserves unknown metrics', async () => {
  const server = await mockCdp();
  try {
    const report = await observe({ debugOrigin: server.origin, appOrigin: APP, seconds: 1 });
    assert.equal(report.completed, true); assert.equal(report.snapshots.length, 1);
    assert.equal(report.snapshots[0].snapshot.diagnostics.text, snapshot.diagnostics.text);
    assert.equal(report.snapshots[0].snapshot.status.text, 'VIDEO STALE');
    assert.ok(Object.values(report.metrics).every(value => value === null));
    assert.equal(report.sidecars.length, 3);
    assert.equal(report.sidecars[0].endpoint_channel, null); assert.equal(report.sidecars[0].packet_channel, 'macview');
    assert.equal(report.sidecars[1].endpoint_channel, 'camera'); assert.equal(report.sidecars[1].packet_channel, null);
    assert.equal(report.sidecars[1].metadata.relay_request_ms, 90);
    assert.equal(report.sidecars[1].metadata.relay_network_bound_ms, 80);
    assert.equal(report.sidecars[1].metadata.relay_residence_ms, 1);
    assert.equal(report.sidecars[2].endpoint_channel, 'macview');
    assert.equal(report.discarded_events, 1);
    assert.deepEqual(server.commands.map(command => command.method), ['Network.enable', 'Runtime.evaluate', 'Network.disable']);
    assert.equal(server.commands[1].params.expression, SNAPSHOT_EXPRESSION);
    assert.equal(server.commands[1].params.userGesture, false);
    assert.deepEqual(server.requests, [{ method: 'GET', path: '/json/list' }, { method: 'GET', path: '/devtools/page/mock' }]);
    const output = JSON.stringify(report);
    assert.equal(output.includes('SECRET'), false); assert.equal(output.includes('devtools'), false);
    assert.equal(output.includes('/ws/control'), false);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(server.closed, 1, 'only observer CDP socket closed; no target/browser command');
  } finally { await server.close(); }
});

test('event storage is bounded and reports observation loss separately from missing video metrics', async () => {
  const server = await mockCdp({ burst: 3700 });
  try {
    const report = await observe({ debugOrigin: server.origin, appOrigin: APP, seconds: 1 });
    assert.equal(report.completed, true);
    assert.ok(report.sidecars.length <= 3600); assert.ok(report.discarded_sidecars >= 103);
    assert.ok(report.sidecars.reduce((total, item) => total + Buffer.byteLength(JSON.stringify(item)), 0) <= 4 * 1024 * 1024);
    assert.equal(report.metrics.decoded_fps, null);
    assert.equal(report.errors.length, 0);
  } finally { await server.close(); }
});

test('missing/hidden diagnostics stay missing/hidden; page change aborts without navigating', async () => {
  const server = await mockCdp({ value: { ...snapshot, diagnostics: null, appOrigin: 'http://localhost:8081' }, events: false });
  try {
    const report = await observe({ debugOrigin: server.origin, appOrigin: APP, seconds: 1 });
    assert.equal(report.completed, false); assert.equal(report.snapshots.length, 0);
    assert.equal(report.errors[0].code, 'observed_page_changed');
    assert.deepEqual(server.commands.map(command => command.method), ['Network.enable', 'Runtime.evaluate', 'Network.disable']);
  } finally { await server.close(); }
  const hidden = await mockCdp({ value: { ...snapshot, diagnostics: { text: '', hidden: true } }, events: false });
  try {
    const report = await observe({ debugOrigin: hidden.origin, appOrigin: APP, seconds: 1 });
    assert.equal(report.snapshots[0].snapshot.diagnostics.hidden, true);
    assert.equal(report.snapshots[0].snapshot.diagnostics.text, '');
    assert.equal(report.metrics.xr_fps, null);
  } finally { await hidden.close(); }
});

test('command errors and timeouts are bounded, scrub backend strings, disable and close own socket', async () => {
  for (const config of [{ rejectRuntime: true }, { ignoreRuntime: true }]) {
    const server = await mockCdp({ ...config, events: false });
    try {
      const start = performance.now();
      const report = await observe({ debugOrigin: server.origin, appOrigin: APP, seconds: 1 });
      assert.equal(report.completed, false); assert.ok(performance.now() - start < 3500);
      assert.equal(JSON.stringify(report).includes('SECRET'), false);
      assert.equal(server.commands.at(-1).method, 'Network.disable');
    } finally { await server.close(); }
  }
});

test('CLI duration bounded and required endpoint explicitly supplied; no guessed device socket', () => {
  assert.deepEqual(parseArgs(['--help']), { help: true });
  assert.throws(() => parseArgs([]), /required_arguments_missing/);
  for (const seconds of ['0', '61', 'NaN', '1.5']) assert.throws(() => parseArgs(['--debug-origin', 'http://localhost:9222',
    '--app-origin', APP, '--seconds', seconds, '--out', '/tmp/window.json']));
  assert.equal(parseArgs(['--debug-origin', 'http://localhost:9222', '--app-origin', APP, '--out', '/tmp/window.json']).seconds, 30);
});
