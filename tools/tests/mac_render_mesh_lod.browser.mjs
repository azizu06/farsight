/** Production Three/WebGL connected-mesh regression; SIMULATED geometry only.
 * CPU preparation (no Chromium/GPU): node tools/tests/mac_render_mesh_lod.browser.mjs --prepare
 * Run only with coordinated GPU GO: node tools/tests/mac_render_mesh_lod.browser.mjs --run --gpu-go
 * Sources are served verbatim from git, without a checkout or renderer edits.
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repository = fileURLToPath(new URL('../../', import.meta.url));
const WIDTH = 640, HEIGHT = 480, COLS = 40, ROWS = 32;
const TRIANGLES = COLS * ROWS * 2, VERTICES = (COLS + 1) * (ROWS + 1);
const SESSION = '123e4567-e89b-12d3-a456-426614174000';
const output = process.env.FARSIGHT_MESH_LOD_OUTPUT || '/tmp/farsight-mesh-lod-browser.json';
const hash = value => createHash('sha256').update(value).digest('hex');
const show = (revision, path) => execFileSync('git', ['show', `${revision}:${path}`], { cwd: repository, maxBuffer: 4 * 1024 * 1024 });
const assets = ['tools/mac_render/render.html', 'tools/mac_render/render.mjs', 'tools/mac_render/dense-cloud.mjs',
  'tools/mac_render/stereo-pass.mjs', 'quest/src/protocol.js', 'quest/src/map-response.js'];
const sources = ['2eab918', '7a98b13'].map((ref, index) => {
  const revision = execFileSync('git', ['rev-parse', ref], { cwd: repository, encoding: 'utf8' }).trim();
  const files = new Map(assets.map(path => [path, show(revision, path)]));
  const renderer = files.get('tools/mac_render/render.mjs').toString();
  const match = renderer.match(/const LADDER = \[([\s\S]*?)\n\];/);
  assert.ok(match, 'Production LADDER must be discoverable');
  const ladder = Function(`return [${match[1]}]`)();
  assert.equal(ladder.length, 5);
  return { name: index ? 'after' : 'before', revision, files, ladder,
    assetHashes: Object.fromEntries([...files].map(([path, content]) => [path, hash(content)])) };
});

function meshFixture() {
  const indices = [];
  const bytes = Buffer.alloc(8 + VERTICES * 16 + TRIANGLES * 12);
  bytes.writeUInt32LE(VERTICES); bytes.writeUInt32LE(TRIANGLES * 3, 4);
  for (let row = 0; row <= ROWS; row++) for (let col = 0; col <= COLS; col++) {
    const at = 8 + (row * (COLS + 1) + col) * 16;
    bytes.writeFloatLE(-1.8 + col * 3.6 / COLS, at);
    bytes.writeFloatLE(-1.2 + row * 2.4 / ROWS, at + 4);
    bytes.writeFloatLE(-3, at + 8);
    bytes.set([40, 220, 80, 255], at + 12);
  }
  for (let row = 0; row < ROWS; row++) for (let col = 0; col < COLS; col++) {
    const a = row * (COLS + 1) + col, b = a + 1, c = a + COLS + 1, d = c + 1;
    indices.push(a, b, d, a, d, c);
  }
  indices.forEach((value, index) => bytes.writeUInt32LE(value, 8 + VERTICES * 16 + index * 4));
  return { bytes, indices };
}
const fixture = meshFixture();
const status = { source: 'SIMULATED', session_id: SESSION, map_epoch: 1, generation: 0,
  revision: 1, mesh_revision: 1, points: VERTICES, triangles: TRIANGLES, connected: true, fresh: true, age_ms: 0 };
const metadata = { scope: 'Software-only production renderer, connected SIMULATED colored grid mesh, no sensors/devices/control',
  hardwareQualified: false, realRoomQualified: false, performanceQualified: false,
  fixture: { columns: COLS, rows: ROWS, vertices: VERTICES, triangles: TRIANGLES, meshSha256: hash(fixture.bytes),
    planeBounds: { x: [-1.8, 1.8], y: [-1.2, 1.2], z: -3 }, pose: { position: [0, 0, 0], quaternion: [0, 0, 0, 1], fovY: 60 },
    viewport: { width: WIDTH, height: HEIGHT }, interiorROI: 'Projected x ±1.3, y ±0.8; avoids silhouette/antialias boundary' },
  sources: sources.map(({ name, revision, assetHashes, ladder }) => ({ name, revision, assetHashes, ladder })) };
if (!process.argv.includes('--run')) {
  assert.ok(process.argv.includes('--prepare'), 'Specify --prepare or --run --gpu-go');
  console.log(JSON.stringify({ prepared: true, gpuInvoked: false, ...metadata }));
} else {
  assert.ok(process.argv.includes('--gpu-go'), 'Explicit coordinated GPU GO is required');
  const report = { passed: false, ...metadata, cases: [], errors: [] };
  let browser;
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://localhost'), path = url.pathname;
      if (path === '/map/status') { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(status)); return; }
      if (path === '/map/mesh.bin' || path === '/map/points.bin') {
        response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'X-Map-Session': SESSION,
          'X-Map-Epoch': '1', 'X-Map-Generation': '0', 'X-Map-Revision': '1' });
        response.end(path.endsWith('mesh.bin') ? fixture.bytes : Buffer.alloc(4)); return;
      }
      const name = path.split('/')[1], source = sources.find(value => value.name === name);
      if (source) {
        const asset = path.slice(name.length + 2), content = source.files.get(asset);
        assert.ok(content, `Unknown source asset ${asset}`);
        response.setHeader('Content-Type', asset.endsWith('.html') ? 'text/html' : 'text/javascript'); response.end(content); return;
      }
      // Absolute production module imports get rewritten only by browser routing;
      // the response body of every production module remains byte-for-byte git source.
      if (path.startsWith('/quest/node_modules/three/build/') && !path.includes('..')) {
        const content = await readFile(resolve(repository, path.slice(1)));
        report.threeAssets ||= {}; report.threeAssets[path] = hash(content);
        response.setHeader('Content-Type', 'text/javascript'); response.end(content); return;
      }
      response.writeHead(404); response.end();
    } catch (error) { response.writeHead(500); response.end(String(error)); }
  });
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const deadline = setTimeout(() => { report.errors.push('120-second fixture deadline exceeded'); void browser?.close(); server.closeAllConnections(); }, 120000);
  try {
    const { chromium } = await import(pathToFileURL(resolve(repository, 'quest/node_modules/playwright/index.mjs')).href);
    // Coordinated native ANGLE Metal run; actual backend is recorded below.
    browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=metal'] });
    report.browser = { version: browser.version(), launch: 'headless Chromium --use-gl=angle --use-angle=metal; no SwiftShader flags' };
    for (const source of sources) for (let tier = 0; tier < source.ladder.length; tier++) {
      const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT } });
      try {
        page.on('pageerror', error => report.errors.push(`${source.name}/${tier}: ${error.message}`));
        await page.route('**/quest/src/**', route => route.continue({ url: `${origin}/${source.name}${new URL(route.request().url()).pathname}` }));
        await page.routeWebSocket('**/*', socket => socket.close());
        await page.goto(`${origin}/${source.name}/tools/mac_render/render.html?upstream=${origin}&width=${WIDTH}&height=${HEIGHT}&quality=flat&style=unlit&lod=${tier}&jq=.98`);
        await page.waitForFunction(expected => window.__mac?.triangles === expected, TRIANGLES, { timeout: 15000 });
        await page.evaluate(() => {
          window.__macCaptureDriven = true;
          window.dispatchEvent(new Event('pagehide'));
          window.__macRegisterLayer({ init(ctx) {
            window.__meshFixture = { ctx, calls: [] };
            ctx.root.children.filter(value => value.isLineSegments || value.isPoints).forEach(value => { value.visible = false; });
            const gl = ctx.renderer.getContext(), original = gl.drawElements;
            gl.drawElements = function(mode, count, type, offset) {
              if (mode === gl.TRIANGLES) window.__meshFixture.calls.push({ count, type, offset });
              return original.call(this, mode, count, type, offset);
            };
          }, pre(ctx) {
            ctx.root.children.filter(value => value.isLineSegments || value.isPoints).forEach(value => { value.visible = false; });
          } });
        });
        const measured = await page.evaluate(expectedIndices => {
          const { ctx, calls } = window.__meshFixture, gl = ctx.renderer.getContext();
          const surface = ctx.root.children.find(value => value.isMesh), info = gl.getExtension('WEBGL_debug_renderer_info');
          calls.length = 0;
          window.__macPose = { tracked: true, receiptAgeMs: 0, receivedMonoMs: 1, sentMonoMs: 1, sentWallMs: 1,
            position: [0, 0, 0], quaternion: [0, 0, 0, 1], ray: null, fov_y_deg: 60, aspect: 4 / 3 };
          const captured = window.__macCapture(), width = gl.drawingBufferWidth, height = gl.drawingBufferHeight;
          const pixels = new Uint8Array(width * height * 4); gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
          const scale = height / (2 * 3 * Math.tan(Math.PI / 6));
          const x0 = Math.ceil(width / 2 - 1.3 * scale), x1 = Math.floor(width / 2 + 1.3 * scale);
          const y0 = Math.ceil(height / 2 - .8 * scale), y1 = Math.floor(height / 2 + .8 * scale);
          let covered = 0, total = 0;
          for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
            const offset = (y * width + x) * 4;
            if (pixels[offset + 1] > 150 && pixels[offset + 1] > pixels[offset] * 1.5 && pixels[offset + 1] > pixels[offset + 2] * 1.3) covered++;
            total++;
          }
          const index = surface.geometry.index.array;
          return { meta: captured.meta, drawRange: surface.geometry.drawRange, calls: [...calls],
            submittedTriangles: calls.reduce((sum, value) => sum + value.count / 3, 0),
            indexOrderVerbatim: expectedIndices.every((value, i) => index[i] === value),
            buffer: { width, height }, interior: { covered, total, coverage: covered / total, holes: 1 - covered / total, roi: [x0, y0, x1, y1] },
            gpu: { vendor: info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
              renderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER), version: gl.getParameter(gl.VERSION) },
            rawPixelChecksum: pixels.reduce((sum, value) => (sum + value) >>> 0, 0),
            jpegBytes: Math.floor(captured.data.length * 3 / 4) };
        }, fixture.indices);
        const fraction = source.ladder[tier].fraction;
        const expected = source.name === 'after' ? TRIANGLES : TRIANGLES * fraction;
        report.cases.push({ source: source.name, revision: source.revision, tier, expectedTriangles: expected, ...measured });
        assert.equal(measured.meta.source, 'SIMULATED'); assert.equal(measured.meta.lod.index, tier);
        assert.equal(measured.meta.triangles, TRIANGLES, 'Snapshot metadata alone is not evidence of actual full-mesh draw');
        assert.equal(measured.meta.drawnTriangles, expected); assert.equal(measured.submittedTriangles, expected);
        assert.equal(measured.drawRange.count, expected * 3);
        assert.ok(!/SwiftShader|llvmpipe|software/i.test(measured.gpu.renderer), `Native GPU required: ${measured.gpu.renderer}`);
        if (source.name === 'after') assert.equal(measured.indexOrderVerbatim, true);
        if (expected === TRIANGLES) assert.ok(measured.interior.coverage > .995, `Full mesh coverage ${measured.interior.coverage}`);
        else assert.ok(Math.abs(measured.interior.coverage - fraction) < .06, `Expected ${fraction} prefix creates actual interior holes; got ${measured.interior.coverage}`);
      } finally { await page.close(); }
    }
    assert.equal(report.errors.length, 0, report.errors.join('\n'));
    report.passed = true;
  } catch (error) { report.failure = String(error.stack || error); process.exitCode = 1; }
  finally {
    clearTimeout(deadline); await browser?.close(); server.closeAllConnections(); await new Promise(resolveClose => server.close(resolveClose));
    report.resourcesClosed = true; await writeFile(output, JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify({ passed: report.passed, output, resourcesClosed: report.resourcesClosed, failure: report.failure,
    cases: report.cases.map(value => ({ source: value.source, tier: value.tier, drawn: value.submittedTriangles,
      coverage: value.interior.coverage, gpu: value.gpu.renderer })) }));
}
