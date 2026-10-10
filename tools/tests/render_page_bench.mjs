/**
 * Perf bench for tools/mac_render/render.mjs. Same loopback harness as
 * mac_render_page.browser.mjs but with a large synthetic map and timed
 * capture-driven frames. Software-only (SwiftShader) numbers — compare
 * before/after on the same machine, never against hardware runs.
 *
 *   node tools/tests/render_page_bench.mjs [--points 600000] [--tris 160000]
 *        [--frames 90] [--width 1280] [--height 720] [--lod <index|adaptive>]
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';

const root = new URL('../../', import.meta.url);
const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : dflt;
};
const pointCount = Number(arg('points', 600000));
const triCount = Number(arg('tris', 0));
const frames = Number(arg('frames', 90));
const width = Number(arg('width', 1280));
const height = Number(arg('height', 720));
const lod = arg('lod', 'adaptive');

function percentile(values, f) {
  const ordered = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!ordered.length) return null;
  const i = (ordered.length - 1) * f;
  const lo = Math.floor(i), hi = Math.ceil(i);
  return ordered[lo] + (ordered[hi] - ordered[lo]) * (i - lo);
}

let status = {
  source: 'BENCH', session_id: '123e4567-e89b-12d3-a456-426614174000', map_epoch: 1,
  generation: 0, revision: 1, mesh_revision: 0, points: pointCount, triangles: 0,
  fresh: true, connected: true, age_ms: 0,
};

function pointsBuffer(count) {
  const bytes = Buffer.alloc(4 + count * 16);
  bytes.writeUInt32LE(count);
  for (let i = 0; i < count; i++) {
    const o = 4 + i * 16;
    // Deterministic spread across a ~40m floor so raster work is realistic.
    bytes.writeFloatLE((i % 977) / 24 - 20, o);
    bytes.writeFloatLE(((i * 7) % 433) / 24 - 9, o + 4);
    bytes.writeFloatLE(-1 - (i % 97) / 24, o + 8);
    bytes.fill(160 + (i % 96), o + 12, o + 16);
  }
  return bytes;
}

function meshBuffer(tris) {
  const verts = tris + 2;
  const bytes = Buffer.alloc(8 + verts * 16 + tris * 3 * 4);
  bytes.writeUInt32LE(verts);
  bytes.writeUInt32LE(tris * 3, 4);
  for (let i = 0; i < verts; i++) {
    const o = 8 + i * 16;
    bytes.writeFloatLE((i % 977) / 24 - 20, o);
    bytes.writeFloatLE(((i * 7) % 433) / 24 - 9, o + 4);
    bytes.writeFloatLE(-1 - (i % 97) / 24, o + 8);
    bytes.fill(200, o + 12, o + 16);
  }
  for (let t = 0; t < tris; t++) {
    const o = 8 + verts * 16 + t * 12;
    bytes.writeUInt32LE(t, o);
    bytes.writeUInt32LE(t + 1, o + 4);
    bytes.writeUInt32LE(t + 2, o + 8);
  }
  return bytes;
}

const pointsBody = pointCount ? pointsBuffer(pointCount) : null;
const meshBody = triCount ? meshBuffer(triCount) : null;
if (meshBody) { status.mesh_revision = 1; status.points = pointCount; status.triangles = triCount; }

const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  if (path === '/map/status') {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify(status));
    return;
  }
  if (path === '/map/points.bin' || path === '/map/mesh.bin') {
    const isMesh = path === '/map/mesh.bin';
    response.writeHead(200, { 'Content-Type': 'application/octet-stream',
      'X-Map-Session': status.session_id, 'X-Map-Epoch': '1',
      'X-Map-Generation': '0',
      'X-Map-Revision': String(isMesh ? status.mesh_revision : status.revision) });
    response.end(isMesh ? meshBody : pointsBody);
    return;
  }
  try {
    const allowed = path.startsWith('/quest/') || path.startsWith('/tools/mac_render/');
    if (!allowed || path.includes('..')) { response.writeHead(404); response.end(); return; }
    const content = await readFile(new URL(path.slice(1), root));
    response.writeHead(200, { 'Content-Type': path.endsWith('.html') ? 'text/html' : 'text/javascript' });
    response.end(content);
  } catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

let browser;
try {
  browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const page = await browser.newPage({ viewport: { width: 320, height: 240 } });
  const url = `${origin}/tools/mac_render/render.html?upstream=${encodeURIComponent(origin)}&width=${width}&height=${height}${lod === 'adaptive' ? '' : `&lod=${lod}`}`;
  await page.goto(url);
  await page.waitForFunction(() => window.__mac && window.__mac.ready === true, { timeout: 20000 });
  await page.waitForFunction(({ points, tris }) =>
    window.__mac.points === points && (window.__mac.triangles || 0) === tris && window.__mac.drawnPoints > 0,
    { points: pointCount, tris: triCount }, { timeout: 120000 });
  await page.evaluate(() => {
    window.dispatchEvent(new Event('pagehide'));
    window.__macCaptureDriven = true;
  });
  // Pose at ~30 Hz like the real driver.
  const pose = { tracked: true, receiptAgeMs: 0, receivedMonoMs: 987654321,
    sentMonoMs: 5, sentWallMs: 10, position: [1, 2, 3], quaternion: [0, 0, 0, 1],
    ray: { origin: [1, 2, 3], direction: [0, 0, -1] } };
  const renderMs = [], captureWall = [];
  for (let i = 0; i < frames; i++) {
    const t0 = performance.now();
    const result = await page.evaluate(p => { window.__macPose = p; return window.__macCapture(); }, pose);
    captureWall.push(performance.now() - t0);
    renderMs.push(result.meta.renderMs);
  }
  const state = await page.evaluate(() => window.__mac);
  console.log(JSON.stringify({
    scope: 'software-only Chromium/SwiftShader; relative before/after numbers only',
    width, height, points: state.points, triangles: state.triangles,
    lod: state.lod ?? null,
    frames,
    render_ms_p50: percentile(renderMs, 0.5),
    render_ms_p95: percentile(renderMs, 0.95),
    capture_wall_ms_p50: percentile(captureWall, 0.5),
    capture_wall_ms_p95: percentile(captureWall, 0.95),
    capture_ms_p50: state.captureMs ?? null,
    drawn_points: state.drawnPoints,
    draws: state.draws,
  }, null, 2));
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
