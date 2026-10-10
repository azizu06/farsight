import test from 'node:test';
import assert from 'node:assert/strict';
import { standaloneURL, options, failures, summary, qualify } from '../qualify_macview_browser.mjs';
import { createServer } from 'node:http';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('refuses app/control paths and unsafe endpoint origins before launching', () => {
  for (const url of ['http://localhost/', 'http://localhost/ws/control', 'http://localhost/macview.html/', 'file:///macview.html', 'http://user:password@localhost/macview.html', 'http://localhost/macview.html?mac=http://localhost/ws/control', 'http://localhost/macview.html?mac=http://localhost/?key=value'])
    assert.throws(() => standaloneURL(url));
  assert.equal(standaloneURL('http://localhost:5173/macview.html?mac=http://localhost:8090').pathname, '/macview.html');
});
test('bounded durations and unknown/missing options fail before launching', () => {
  for (const args of [['--seconds', 'Infinity'], ['--warmup', '0'], ['--seconds', '61'], ['--url'], ['--cdp', 'http://localhost:9222']])
    assert.throws(() => options(args));
  assert.equal(options(['--seconds', '2']).seconds, 2);
});
const evidence = () => ({ errors: [], video: { width: 320, height: 240, callbacks: 10, callbackHz: 5, mediaTimeAdvanceSeconds: 2 },
  stream: { init: { width: 320, height: 240, version: 1, stereo: false }, segments: 4, sources: ['SIMULATED'], stereoClaims: [false] } });
test('flat simulated decode is accepted only as desktop evidence', () => assert.deepEqual(failures(evidence(), 1), []));
test('missing/stereo claims and missing source provenance cannot pass', () => {
  for (const field of ['init', 'sources', 'stereoClaims']) {
    const report = evidence();
    if (field === 'init') report.stream.init.stereo = true;
    if (field === 'sources') report.stream.sources = [''];
    if (field === 'stereoClaims') report.stream.stereoClaims = [null];
    assert.ok(failures(report, 1).length);
  }
});
test('stalled presentation, dimension mismatch and errors fail', () => {
  const stalled = evidence(); stalled.video.callbacks = 0; assert.ok(failures(stalled, 1).length);
  const mismatch = evidence(); mismatch.video.width = 640; assert.ok(failures(mismatch, 1).length);
  const error = evidence(); error.errors.push('Blocked out-of-scope WebSocket'); assert.ok(failures(error, 1).length);
});
test('empty timing series stay unknown', () => assert.deepEqual(summary([]), { count: 0, p50: null, p95: null, maximum: null }));

// Opt in with MACVIEW_BROWSER_TEST=1 to exercise safety on real Chromium.
for (const kind of ['forbidden-socket', 'navigation-redirect']) {
  test(`browser blocks ${kind} before the control server receives it`, { skip: process.env.MACVIEW_BROWSER_TEST !== '1' }, async () => {
    let controlRequests = 0;
    const server = createServer((request, response) => {
      if (request.url.startsWith('/ws/control')) controlRequests++;
      if (kind === 'navigation-redirect' && request.url === '/macview.html') {
        response.writeHead(302, { Location: '/ws/control' }); response.end();
      } else {
        response.writeHead(200, { 'Content-Type': 'text/html' });
        response.end('<!doctype html><script>new WebSocket(location.origin.replace(/^http/, "ws") + "/ws/control")</script>');
      }
    });
    server.on('upgrade', (request, socket) => { if (request.url.startsWith('/ws/control')) controlRequests++; socket.destroy(); });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      const report = await qualify({ ...options([]), url: `http://127.0.0.1:${server.address().port}/macview.html`, warmup: 1, seconds: 1,
        out: await mkdtemp(join(tmpdir(), 'macview-safety-')) });
      assert.equal(report.passed, false); assert.equal(controlRequests, 0);
      assert.ok(report.failures.some(error => /Blocked out-of-scope/.test(error)));
    } finally { await new Promise(resolve => server.close(resolve)); }
  });
}
