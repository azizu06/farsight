// Actual production WebGL pixels: coarse coverage and retained native RGB-D.
// Synthetic observations only; no rover, control, pose socket or link benchmark.
// Build the raw adapter first. --packet optionally supplies a synthetic packet.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';

const option = (name, fallback) => {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
};
const output = resolve(option('--out', '/tmp/farsight-render-detail'));
const fixtureScript = `
import json,struct,sys
import cv2,numpy as np
y,x=np.indices((480,640))
image=np.where(((x//10+y//10)%2)[...,None]==0,np.array([185,210,238]),np.array([55,75,95])).astype(np.uint8)
image[(x%80<2)|(y%80<2)]=[20,30,40]
_,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,95]);jpeg=jpeg.tobytes()
header=dict(version=1,type='frame',session_id='123e4567-e89b-12d3-a456-426614174000',map_epoch=1,map_generation=1,frame_id=0,tracking='normal',capture_source='SIMULATED',transform=np.eye(4).flatten(order='F').tolist(),image=dict(width=640,height=480,jpeg_len=len(jpeg),intrinsics=[480,0,0,0,480,0,320,240,1]),depth=dict(width=64,height=48))
raw=json.dumps(header).encode()
sys.stdout.buffer.write(struct.pack('<I',len(raw))+raw+jpeg+np.full((48,64),3,dtype='<f4').tobytes()+np.full((48,64),2,dtype=np.uint8).tobytes())
`;
const packetFile = option('--packet', null);
const packet = packetFile ? await readFile(packetFile)
  : execFileSync(process.env.FARSIGHT_PYTHON || resolve('.venv/bin/python'), ['-c', fixtureScript]);
const baseline = 'cb8646f';
const show = path => execFileSync('git', ['show', `${baseline}:${path}`]);
await mkdir(`${output}/baseline-source`, { recursive: true });
for (const file of ['mac-layer.js', 'raw-view.js', 'raw-fusion.worker.js']) {
  await writeFile(`${output}/baseline-source/${file}`, show(`tools/dense_viewer/${file}`));
}
const config = `import {defineConfig} from ${JSON.stringify(resolve('quest/node_modules/vite/dist/node/index.js'))};
export default defineConfig({root:${JSON.stringify(`${output}/baseline-source`)},base:'./',build:{
outDir:${JSON.stringify(`${output}/baseline-layer`)},emptyOutDir:true,
lib:{entry:${JSON.stringify(`${output}/baseline-source/mac-layer.js`)},formats:['es'],fileName:()=> 'raw-texture-layer.mjs'},
rollupOptions:{external:['three'],output:{paths:{three:'/quest/node_modules/three/build/three.module.js'}}}}});`;
await writeFile(`${output}/baseline.config.mjs`, config);
execFileSync(process.execPath, ['quest/node_modules/vite/bin/vite.js', 'build', '--config', `${output}/baseline.config.mjs`]);
const beforeRenderer = show('tools/mac_render/render.mjs');
const session = '123e4567-e89b-12d3-a456-426614174000';
const columns = 49, rows = 33, count = columns * rows;
const points = Buffer.alloc(4 + count * 16); points.writeUInt32LE(count);
for (let y = 0; y < rows; y++) for (let x = 0; x < columns; x++) {
  const at = 4 + (y * columns + x) * 16;
  points.writeFloatLE((x - 24) * .05, at); points.writeFloatLE((y - 16) * .05, at + 4); points.writeFloatLE(-3, at + 8);
  points.set([50, 200, 150, 255], at + 12);
}
function rawFrame(index) {
  const n = packet.readUInt32LE(0), header = JSON.parse(packet.subarray(4, 4 + n));
  assert.equal(header.capture_source, 'SIMULATED', 'Use a synthetic fixture for this regression');
  header.frame_id = index; header.transform[12] += index * 8;
  const text = Buffer.from(JSON.stringify(header)), prefix = Buffer.alloc(4); prefix.writeUInt32LE(text.length);
  return Buffer.concat([prefix, text, packet.subarray(4 + n)]);
}
let version = 'before', reads = 0, allowed = 0, rawCalls = 0;
const server = createServer(async (request, response) => {
  try {
    const path = new URL(request.url, 'http://localhost').pathname;
    if (path === '/map/status') {
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ source: 'SIMULATED', session_id: session, map_epoch: 1, generation: 1,
        revision: 1, mesh_revision: 0, points: count, triangles: 0, voxel_m: .05, connected: true, fresh: true, age_ms: 0 })); return;
    }
    if (path === '/map/points.bin') {
      response.writeHead(200, { 'X-Map-Session': session, 'X-Map-Epoch': '1', 'X-Map-Generation': '1', 'X-Map-Revision': '1' });
      response.end(points); return;
    }
    if (path === '/phone/frame.bin') {
      rawCalls++;
      if (reads >= allowed) { response.writeHead(204); response.end(); return; }
      const index = reads++;
      response.setHeader('X-Capture-Key', `synthetic:${index}`); response.setHeader('X-Capture-Age-Ms', '0');
      response.end(rawFrame(index)); return;
    }
    let file = null;
    if (path.startsWith('/dense-layer/')) file = `${version === 'before' ? output + '/baseline-layer' : '.cache/dense-layer-dist'}/${path.slice(13)}`;
    else if (path.startsWith('/tools/mac_render/') || path.startsWith('/quest/')) file = `.${path}`;
    if (!file || path.includes('..')) { response.writeHead(404); response.end(); return; }
    const content = version === 'before' && path === '/tools/mac_render/render.mjs' ? beforeRenderer : await readFile(file);
    response.setHeader('Content-Type', path.endsWith('.html') ? 'text/html' : 'text/javascript'); response.end(content);
  } catch (error) { response.writeHead(500); response.end(String(error)); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const report = { scope: 'Production Three.js/WebGL with two calibrated SIMULATED RGB-D observations', baseline,
  hardware_qualified: false, live_room_qualified: false, cases: {}, errors: [] };
let browser;
try {
  browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=metal'] });
  for (version of ['before', 'after']) {
    reads = allowed = rawCalls = 0;
    const page = await browser.newPage({ viewport: { width: 640, height: 480 } });
    page.on('pageerror', error => report.errors.push(`${version}: ${error.message}`));
    page.on('websocket', socket => report.errors.push(`Unexpected socket: ${socket.url()}`));
    try {
      await page.goto(`${origin}/tools/mac_render/render.html?upstream=${origin}&width=640&height=480&camera=snap&lod=1`);
      await page.waitForFunction(expected => window.__mac?.points === expected && window.__macRawLayer, count);
      await page.evaluate(() => {
        window.__macCaptureDriven = true;
        window.__macRegisterLayer({ init(ctx) {
          window.__detailContext = ctx; ctx.camera.position.set(0, 0, 0); ctx.camera.quaternion.identity();
          ctx.camera.fov = 50; ctx.camera.updateProjectionMatrix();
          ctx.root.children.filter(value => value.type === 'GridHelper').forEach(value => { value.visible = false; });
        } });
      });
      const capture = () => page.evaluate(() => {
        const captured = window.__macCapture(), ctx = window.__detailContext, gl = ctx.renderer.getContext();
        const width = gl.drawingBufferWidth, height = gl.drawingBufferHeight;
        const pixels = new Uint8Array(width * height * 4); gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        const scale = height / (6 * Math.tan(25 * Math.PI / 180)), roi = [];
        let covered = 0, total = 0;
        for (let y = Math.ceil(height / 2 - .55 * scale); y < height / 2 + .55 * scale; y++) {
          for (let x = Math.ceil(width / 2 - .90 * scale); x < width / 2 + .90 * scale; x++) {
            const at = (y * width + x) * 4;
            covered += Number(pixels[at + 1] > 130); total++;
            roi.push(pixels[at], pixels[at + 1], pixels[at + 2]);
          }
        }
        const extension = gl.getExtension('WEBGL_debug_renderer_info');
        return { coverage: covered / total, roi, source: captured.meta.source,
          drawn_points: captured.meta.drawnPoints, raw: window.__macRawLayer.stats(),
          gpu: extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) };
      });
      const coarse = await capture();
      assert.equal(rawCalls, 0, 'Optional raw networking stays off until explicitly enabled');
      await page.screenshot({ path: `${output}/${version}-coarse.png` });
      allowed = 1; await page.evaluate(() => { window.__macCaptureDriven = false; window.__macRawLayer.setEnabled(true); });
      await page.waitForFunction(() => window.__macRawLayer.stats().raw_frame === 0);
      const textured = await capture();
      allowed = 2; await page.waitForFunction(() => window.__macRawLayer.stats().raw_frame === 1);
      const retained = await capture();
      await page.evaluate(() => { window.__macCaptureDriven = true; });
      const difference = retained.roi.reduce((sum, value, i) => sum + Math.abs(value - textured.roi[i]), 0) / retained.roi.length;
      await page.screenshot({ path: `${output}/${version}-retained.png` });
      for (const sample of [coarse, textured, retained]) delete sample.roi;
      report.cases[version] = { coarse, textured, retained, retained_rgb_mean_difference: difference, raw_pulls: reads };
      assert.equal(reads, 2); assert.equal(coarse.drawn_points, count);
      assert.equal(retained.raw.raw_point_capacity, textured.raw.raw_point_capacity, 'Texture history keeps one native point buffer');
    } finally { await page.close(); }
  }
  assert.equal(report.errors.length, 0, report.errors.join('\n'));
  assert.ok(report.cases.before.coarse.coverage < .4, 'Reproduce separated coarse dots in the original production renderer');
  assert.ok(report.cases.after.coarse.coverage > .995, 'Neighboring coarse samples cover the measured plane');
  assert.ok(report.cases.before.retained_rgb_mean_difference > 20, 'Reproduce loss of RGB detail when the camera turns away');
  assert.ok(report.cases.after.retained_rgb_mean_difference < 2, 'Preserve native texture pixels after a different view arrives');
  assert.equal(report.cases.after.retained.raw.raw_patches, 2);
  report.passed = true;
} catch (error) { report.failure = String(error.stack || error); process.exitCode = 1; }
finally {
  await browser?.close(); server.closeAllConnections(); await new Promise(done => server.close(done));
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
}
console.log(JSON.stringify({ passed: report.passed, output, failure: report.failure,
  before: report.cases.before, after: report.cases.after }));
