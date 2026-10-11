// Production pixels with a textured measured plane, the same samples through
// the dense wire, and an independently measured foreground point. SIMULATED.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFileSync } from 'node:child_process';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';

const out = resolve(process.argv[2] || '/tmp/farsight-texture-composition');
await mkdir(out, { recursive: true });
const baseline = 'ac20bdc';
const before = execFileSync('git', ['show', `${baseline}:tools/mac_render/render.mjs`]);
const packet = execFileSync(resolve('.venv/bin/python'), ['-c', `
import json,struct,sys
import cv2,numpy as np
y,x=np.indices((480,640))
rgb=np.where(((x//10+y//10)%2)[...,None]==0,[185,210,238],[55,75,95]).astype(np.uint8)
_,jpg=cv2.imencode('.jpg',rgb,[cv2.IMWRITE_JPEG_QUALITY,95]);jpg=jpg.tobytes()
header=dict(version=1,type='frame',session_id='123e4567-e89b-12d3-a456-426614174000',map_epoch=1,map_generation=1,frame_id=1,tracking='normal',capture_source='SIMULATED',transform=np.eye(4).flatten(order='F').tolist(),image=dict(width=640,height=480,jpeg_len=len(jpg),intrinsics=[480,0,0,0,480,0,320,240,1]),depth=dict(width=256,height=192))
y,x=np.indices((192,256));depth=(1/(1+.35*(x-127.5)/192)).astype('<f4')
h=json.dumps(header).encode()
sys.stdout.buffer.write(struct.pack('<I',len(h))+h+jpg+depth.tobytes()+np.full((192,256),2,np.uint8).tobytes())
`]);
let version, denseEnabled, denseRows, denseCount, rawReads;
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost'), path = url.pathname;
    if (path === '/map/status') {
      res.setHeader('Content-Type', 'application/json');
      return res.end(JSON.stringify({ source: 'SIMULATED', session_id: '123e4567-e89b-12d3-a456-426614174000',
        map_epoch: 1, generation: 1, revision: 0, mesh_revision: 0, points: 0, triangles: 0,
        voxel_m: .04, connected: false, fresh: false, age_ms: 0 }));
    }
    if (path === '/phone/frame.bin') {
      rawReads++; res.setHeader('X-Capture-Key', 'simulated:1'); res.setHeader('X-Capture-Age-Ms', '0');
      return res.end(packet);
    }
    if (path === '/dense/delta.bin') {
      if (!denseEnabled) { res.writeHead(404); return res.end(); }
      const after = Number(url.searchParams.get('after') || 0), end = Math.min(denseCount, after + 8192);
      const more = end < denseCount;
      const header = Buffer.from(JSON.stringify({ format: 'xyz_mm_rgb8_op', epoch: 'b'.repeat(32),
        source: ['123e4567-e89b-12d3-a456-426614174000', 1], voxel_m: .01, ttl_s: 30,
        points: denseCount, cursor: end, reset: after === 0, anchor: denseCount, snapshot: more, more }));
      const prefix = Buffer.alloc(4); prefix.writeUInt32LE(header.length);
      return res.end(Buffer.concat([prefix, header, denseRows.subarray(after * 16, end * 16)]));
    }
    let file;
    if (path.startsWith('/dense-layer/')) file = resolve('.cache/dense-layer-dist', path.slice(13));
    else if (path.startsWith('/tools/mac_render/') || path.startsWith('/quest/')) file = `.${path}`;
    if (!file || path.includes('..')) { res.writeHead(404); return res.end(); }
    res.setHeader('Content-Type', path.endsWith('.html') ? 'text/html' : 'text/javascript');
    res.end(version === 'before' && path === '/tools/mac_render/render.mjs' ? before : await readFile(file));
  } catch (error) { res.writeHead(500); res.end(String(error)); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const report = { scope: 'SIMULATED production WebGL composition; no rover, control or live acceptance',
  baseline, hardware_qualified: false, cases: {}, errors: [] };
let browser;
try {
  browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=metal'] });
  for (version of ['before', 'after']) {
    denseEnabled = false; denseRows = Buffer.alloc(0); denseCount = rawReads = 0;
    const page = await browser.newPage({ viewport: { width: 640, height: 480 } });
    page.on('pageerror', error => report.errors.push(`${version}: ${error.message}`));
    page.on('websocket', socket => report.errors.push(`Unexpected socket: ${socket.url()}`));
    try {
      await page.goto(`${origin}/tools/mac_render/render.html?upstream=${origin}&width=640&height=480&camera=snap&lod=1&presentation=canvas`);
      await page.waitForFunction(() => window.__macRawLayer && window.__macRegisterLayer);
      await page.evaluate(() => {
        window.__macCaptureDriven = true;
        window.__macRegisterLayer({ init(ctx) {
          window.__compositionContext = ctx;
          ctx.camera.position.set(0, 0, 0); ctx.camera.quaternion.identity();
          ctx.camera.fov = 2 * Math.atan(240 / 480) * 180 / Math.PI; ctx.camera.updateProjectionMatrix();
          ctx.root.children.filter(x => x.type === 'GridHelper').forEach(x => { x.visible = false; });
        } });
      });
      await page.evaluate(() => window.__macRawLayer.captureOnce());
      await page.waitForFunction(() => window.__macRawLayer.stats().raw_frame === 1);
      const samples = await page.evaluate(() => {
        // Freeze just this immutable test observation; production expiry remains intact.
        window.__macRawLayer.poll = () => false;
        const ctx = window.__compositionContext, raw = window.__macRawLayer.stats();
        const group = ctx.root.children.find(g => g.children.some(x => x.type === 'Mesh' && x.material.map));
        const geometry = group.children.find(x => x.type === 'Points').geometry;
        window.__macCapture(); const gl = ctx.renderer.getContext();
        const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
        gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        window.__nativeCompositionPixels = pixels;
        return { positions: Array.from(geometry.getAttribute('position').array.subarray(0, raw.raw_points * 3)),
          colors: Array.from(geometry.getAttribute('color').array.subarray(0, raw.raw_points * 3)), raw };
      });
      const cells = new Map();
      for (let i = 0; i < samples.positions.length; i += 3) {
        const xyz = samples.positions.slice(i, i + 3).map(v => Math.round(v * 1000));
        const rgb = samples.colors.slice(i, i + 3).map(v => Math.round(255 * (v <= .0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - .055)));
        cells.set(xyz.map(v => Math.floor(v / 10)).join('_'), [...xyz, ...rgb]);
      }
      // This independent synthetic measured return must occlude the texture behind it.
      cells.set('foreground', [220, 140, -600, 255, 0, 0]);
      denseCount = cells.size; denseRows = Buffer.alloc(denseCount * 16); let row = 0;
      for (const values of cells.values()) {
        const at = row++ * 16;
        for (let axis = 0; axis < 3; axis++) denseRows.writeInt32LE(values[axis], at + axis * 4);
        denseRows.set([...values.slice(3), 1], at + 12);
      }
      denseEnabled = true;
      await page.waitForFunction(n => { window.__macCapture(); return window.__mac.dense?.retained_cells === n; }, denseCount);
      const composed = await page.evaluate(() => {
        const capture = window.__macCapture(), ctx = window.__compositionContext, gl = ctx.renderer.getContext();
        const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight, pixels = new Uint8Array(w * h * 4);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        let error = 0, changed = 0, total = 0;
        for (let y = 40; y < h - 40; y++) for (let x = 40; x < w - 40; x++) {
          if (Math.abs(x - 496) < 50 && Math.abs(y - (h - 128)) < 50) continue;
          const at = (y * w + x) * 4; let difference = 0;
          for (let c = 0; c < 3; c++) difference += Math.abs(pixels[at + c] - window.__nativeCompositionPixels[at + c]);
          error += difference; changed += Number(difference > 15); total++;
        }
        const at = ((h - 128) * w + 496) * 4;
        return { native: window.__macRawLayer.stats(), dense: capture.meta.dense,
          drawn_points: capture.meta.drawnPoints, drawn_triangles: capture.meta.drawnTriangles,
          surface_display: capture.meta.surfaceDisplay, foreground_rgb: Array.from(pixels.subarray(at, at + 3)),
          texture_changed_fraction: changed / total, texture_mean_rgb_difference: error / (3 * total) };
      });
      await page.screenshot({ path: `${out}/${version}-composed.png` });
      await page.evaluate(() => window.__macRawLayer.setEnabled(false));
      const hidden = await page.evaluate(() => window.__macCapture().meta);
      report.cases[version] = { ...composed, raw_reads: rawReads, native_hidden: {
        drawn_points: hidden.drawnPoints, drawn_triangles: hidden.drawnTriangles,
        surface_display: hidden.surfaceDisplay } };
      assert.equal(rawReads, 1);
      assert.equal(composed.drawn_points, denseCount + composed.native.raw_fallback_points);
      assert.equal(composed.drawn_triangles, samples.raw.raw_triangles);
      assert.equal(hidden.drawnPoints, denseCount); assert.equal(hidden.drawnTriangles, 0);
      assert.ok(composed.foreground_rgb[0] > 240 && composed.foreground_rgb[1] < 20 && composed.foreground_rgb[2] < 20);
      if (version === 'after') {
        assert.equal(composed.surface_display.dense_depth_bias_m, .0175);
        assert.equal(hidden.surfaceDisplay.dense_depth_bias_m, 0);
      }
    } finally { await page.close(); }
  }
  assert.equal(report.errors.length, 0, report.errors.join('\n'));
  assert.ok(report.cases.before.texture_mean_rgb_difference > 10, 'Reproduce dense discs covering native detail');
  assert.ok(report.cases.after.texture_mean_rgb_difference < 1, 'Keep native detail with the same dense samples');
  assert.ok(report.cases.after.texture_changed_fraction < .01);
  report.passed = true;
} catch (error) { report.failure = String(error.stack || error); process.exitCode = 1; }
finally {
  await browser?.close(); server.closeAllConnections(); await new Promise(done => server.close(done));
  await writeFile(`${out}/report.json`, JSON.stringify(report, null, 2));
}
console.log(JSON.stringify({ out, passed: report.passed, failure: report.failure,
  before: report.cases.before?.texture_mean_rgb_difference, after: report.cases.after?.texture_mean_rgb_difference }));
