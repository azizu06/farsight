import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { spawn, execFileSync } from 'node:child_process';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';

// Actual production renderer + Three.js, synthetic loopback map, no devices.
const root = new URL('../../', import.meta.url);
const first = '123e4567-e89b-12d3-a456-426614174000';
const second = '123e4567-e89b-12d3-a456-426614174001';
let status = { source: 'SIMULATED', session_id: first, map_epoch: 1, generation: 0,
  revision: 1, mesh_revision: 0, points: 300, triangles: 0, fresh: true, connected: true, age_ms: 0 };
let pointCount = 300, responseRevision = null, badSession = false, switchDuringBody = false;
let active = 0, peakActive = 0, pointRequests = 0, meshRequests = 0;
const paths = [];
function points(count) {
  const bytes = Buffer.alloc(4 + count * 16); bytes.writeUInt32LE(count);
  for (let i = 0; i < count; i++) {
    const offset = 4 + i * 16;
    bytes.writeFloatLE((i % 20 - 10) / 5, offset);
    bytes.writeFloatLE(Math.floor(i / 20) / 8, offset + 4);
    bytes.writeFloatLE(-1 - i % 7 / 4, offset + 8);
    bytes.fill(255, offset + 12, offset + 16);
  }
  return bytes;
}
function mesh() {
  const packed = points(3), result = Buffer.alloc(8 + 3 * 16 + 3 * 4);
  result.writeUInt32LE(3); result.writeUInt32LE(3, 4); packed.copy(result, 8, 4);
  result.writeUInt32LE(0, 56); result.writeUInt32LE(1, 60); result.writeUInt32LE(2, 64);
  return result;
}
const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  paths.push(path);
  if (path.startsWith('/map/')) {
    active++; peakActive = Math.max(peakActive, active);
    response.on('finish', () => active--);
    if (path === '/map/status') {
      response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(status)); return;
    }
    const snapshot = { ...status };
    const isMesh = path === '/map/mesh.bin';
    if (isMesh) meshRequests++; else pointRequests++;
    response.writeHead(200, { 'Content-Type': 'application/octet-stream',
      'X-Map-Session': badSession ? second : snapshot.session_id,
      'X-Map-Epoch': String(snapshot.map_epoch), 'X-Map-Generation': String(snapshot.generation),
      'X-Map-Revision': String(responseRevision ?? (isMesh ? snapshot.mesh_revision : snapshot.revision)) });
    response.flushHeaders();
    if (switchDuringBody) {
      switchDuringBody = false;
      status = { ...status, session_id: second, map_epoch: 2, generation: 0, revision: 1 };
      setTimeout(() => response.end(isMesh ? mesh() : points(pointCount)), 80);
    } else response.end(isMesh ? mesh() : points(pointCount));
    return;
  }
  try {
    const allowed = path.startsWith('/quest/') || path.startsWith('/tools/mac_render/');
    if (!allowed || path.includes('..')) { response.writeHead(404); response.end(); return; }
    const content = await readFile(new URL(path.slice(1), root));
    response.writeHead(200, { 'Content-Type': path.endsWith('.html') ? 'text/html' : 'text/javascript' }); response.end(content);
  } catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const page = await browser.newPage({ viewport: { width: 320, height: 240 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${origin}/tools/mac_render/render.html?upstream=${encodeURIComponent(origin)}&width=320&height=240`);
  await page.waitForFunction(() => window.__mac.points === 300 && window.__mac.drawnPoints > 0, { timeout: 10000 });
  await page.evaluate(() => {
    window.dispatchEvent(new Event('pagehide')); // disable periodic timer for deterministic manual pulls
    window.__macCaptureDriven = true;
  });
  const positive = await page.evaluate(() => window.__macCapture());
  assert.equal(positive.meta.points, 300); assert.ok(positive.meta.drawnPoints > 0);
  assert.equal(positive.meta.source, 'SIMULATED');
  assert.ok(Buffer.from(positive.data, 'base64').subarray(0, 2).equals(Buffer.from([255, 216])));
  const unchangedCount = pointRequests;
  await page.evaluate(() => Promise.all([window.__macRefreshMap(), window.__macRefreshMap(), window.__macRefreshMap()]));
  assert.equal(pointRequests, unchangedCount, 'unchanged map must not redownload');
  assert.equal(peakActive, 1, 'one serial bounded map refresh, never overlapping pulls');
  assert.ok(paths.includes('/map/status')); assert.ok(!paths.includes('/health/map'));

  // Newer response revision wins and is retained as the applied revision.
  status = { ...status, revision: 2 }; responseRevision = 3; pointCount = 320;
  await page.evaluate(() => window.__macRefreshMap());
  assert.equal((await page.evaluate(() => window.__mac)).mapStatus.applied_points_revision, 3);
  const cachedCount = pointRequests;
  await page.evaluate(() => window.__macRefreshMap()); assert.equal(pointRequests, cachedCount);
  responseRevision = null;

  // Strict mismatch rejects a body while retaining the last accepted scene.
  status = { ...status, revision: 4 }; badSession = true;
  await page.evaluate(() => window.__macRefreshMap());
  let state = await page.evaluate(() => window.__mac);
  assert.equal(state.points, 320); assert.ok(state.mapError); assert.equal(state.retained, true);
  badSession = false;
  // Capture changes while an old body is in flight: post-download status must
  // clear the old world rather than publishing that old snapshot.
  switchDuringBody = true;
  await page.evaluate(() => window.__macRefreshMap());
  state = await page.evaluate(() => window.__mac);
  assert.equal(state.points, 0); assert.equal(state.mapStatus.generation, 0);
  await page.evaluate(() => window.__macRefreshMap());
  assert.equal((await page.evaluate(() => window.__mac)).points, 320);
  status = { ...status, generation: 1, revision: 1, mesh_revision: 0, points: 0, triangles: 0 };
  await page.evaluate(() => window.__macRefreshMap());
  const empty = await page.evaluate(() => window.__macCapture());
  assert.equal(empty.meta.points, 0); assert.equal(empty.meta.drawnPoints, 0);
  assert.notEqual(positive.data, empty.data, 'positive geometry changes actual encoded canvas pixels');
  status = { ...status, generation: 2, revision: 1, mesh_revision: 1, points: 3, triangles: 1,
    geometry_source: 'IPHONE ARKIT MESH', geometry: { retained: true, partial: false } };
  await page.evaluate(() => window.__macRefreshMap());
  state = (await page.evaluate(() => window.__macCapture())).meta;
  assert.equal(state.points, 3); assert.equal(state.triangles, 1); assert.equal(state.retained, true);
  assert.equal(state.source, 'IPHONE ARKIT MESH'); assert.equal(meshRequests, 1);

  const pose = { tracked: true, receiptAgeMs: 0, receivedMonoMs: 987654321,
    sentMonoMs: 5, sentWallMs: 10, position: [1, 2, 3], quaternion: [0, 0, 0, 1],
    ray: { origin: [1, 2, 3], direction: [0, 0, -1] } };
  const producerScript = execFileSync(process.env.FARSIGHT_PYTHON || new URL('../../.venv/bin/python', import.meta.url).pathname,
    ['-c', 'from tools.mac_render.server import _CAPTURE_POSE_JS; print(_CAPTURE_POSE_JS)'],
    { cwd: root, encoding: 'utf8' });
  const producerPose = { tracked: true, receipt_age_ms: 0, received_mono_ms: pose.receivedMonoMs,
    sent_mono_ms: pose.sentMonoMs, sent_wall_ms: pose.sentWallMs, position: pose.position,
    quaternion: pose.quaternion, ray: pose.ray, session_id: first, frame_id: 1, pose_id: 1,
    fov_y_deg: 85, aspect: 1.7, space: 'ar_map' };
  const captureFromProducer = value => page.evaluate(({ script, pose }) => (0, eval)(script)(pose),
    { script: producerScript, pose: value });
  let captured = await captureFromProducer(producerPose);
  assert.equal(captured.meta.poseValid, true); assert.equal(captured.meta.rayVisible, true);
  assert.equal(captured.meta.poseEcho.received_mono_ms, pose.receivedMonoMs, 'Python epoch is echo only');
  assert.deepEqual(captured.meta.viewPosition, [1, 2, 3]);
  assert.equal(captured.meta.poseEcho.session_id, first);
  assert.equal(captured.meta.fovY, 85); assert.equal(captured.meta.aspect, 1.7);
  await page.waitForTimeout(270);
  captured = await page.evaluate(() => window.__macCapture());
  assert.equal(captured.meta.poseEcho, null); assert.equal(captured.meta.rayVisible, false);
  assert.deepEqual(captured.meta.viewPosition, [1, 2, 3], 'stale pose retains camera position');
  for (const invalid of [{ ...pose, receiptAgeMs: 251 }, { ...pose, tracked: false, position: [9, 9, 9] },
      { ...pose, quaternion: [0, 0, 0, 0] }, null]) {
    captured = await page.evaluate(value => { window.__macPose = value; return window.__macCapture(); }, invalid);
    assert.equal(captured.meta.poseEcho, null); assert.equal(captured.meta.rayVisible, false);
    assert.deepEqual(captured.meta.viewPosition, [1, 2, 3]);
  }
  captured = await captureFromProducer(null);
  assert.equal(captured.meta.poseEcho, null, 'atomic producer preserves disconnect clear');
  assert.equal(errors.length, 0, errors.join('\n'));
  // One actual positive-map JPEG through FFmpeg -> H.264 fMP4; a functional
  // format check, not an encoder throughput or headset qualification.
  const ffmpegProcess = spawn('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-vcodec', 'mjpeg',
    '-framerate', '10', '-i', 'pipe:0', '-frames:v', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
    '-movflags', 'frag_keyframe+empty_moov+default_base_moof', '-f', 'mp4', 'pipe:1']);
  const encoded = [], logs = [];
  const deadline = setTimeout(() => ffmpegProcess.kill('SIGKILL'), 5000);
  ffmpegProcess.stdout.on('data', chunk => encoded.push(chunk)); ffmpegProcess.stderr.on('data', chunk => logs.push(chunk));
  ffmpegProcess.stdin.end(Buffer.from(positive.data, 'base64'));
  const code = await new Promise((resolve, reject) => { ffmpegProcess.once('error', reject); ffmpegProcess.once('exit', resolve); });
  clearTimeout(deadline);
  assert.equal(code, 0, Buffer.concat(logs).toString());
  const mp4 = Buffer.concat(encoded);
  for (const box of ['ftyp', 'moov', 'moof']) assert.ok(mp4.includes(Buffer.from(box)), box);
  console.log(JSON.stringify({ passed: true, scope: 'software-only Chromium/SwiftShader + loopback synthetic geometry',
    positive_drawn_points: positive.meta.drawnPoints, ffmpeg_fmp4_bytes: mp4.length,
    map_peak_active_requests: peakActive, pose_expiry_ms: 250, stale_camera_position_retained: true }));
} finally {
  await browser?.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
