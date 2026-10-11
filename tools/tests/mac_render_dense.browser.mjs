import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';
const origin = process.argv[2];
const command = async operation => { const reply = await fetch(`${origin}/fixture/${operation}`); assert.equal(reply.status, 200); };
let browser;
try {
  browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const page = await browser.newPage({ viewport: { width: 320, height: 240 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${origin}/tools/mac_render/render.html?upstream=${encodeURIComponent(origin)}&width=320&height=240&style=unlit&lod=0&msurf=0`);
  const waitDense = async count => page.waitForFunction(count => window.__mac.dense?.retained_cells === count &&
      window.__mac.dense?.gpu_uploaded_cells === count && window.__mac.dense?.gpu_drawn_cells === count,
    count, { timeout: 5000 });
  try { await waitDense(1024); } catch (error) {
    console.log(JSON.stringify(await page.evaluate(() => ({ stage: 'initial-dense', dense: window.__mac.dense,
      source: window.__mac.source, points: window.__mac.points, drawnPoints: window.__mac.drawnPoints,
      mapError: window.__mac.mapError, error: window.__mac.error }))));
    throw error;
  }
  await page.waitForFunction(() => window.__mac.triangles === 1 && window.__mac.drawnTriangles > 0, { timeout: 5000 });
  const initial = await page.evaluate(() => window.__macCapture());
  assert.ok(initial.meta.source.startsWith('SIMULATED'));
  assert.ok((await page.locator('#banner').textContent()).includes('SIMULATED'));
  assert.equal(initial.meta.dense.gpu_drawn_cells, 1024); assert.equal(initial.meta.dense.gpu_uploaded_cells, 1024);
  await command('delete'); await waitDense(1023);
  const deleted = await page.evaluate(() => window.__macCapture());
  assert.ok(deleted.meta.dense.received_deletes >= 1);
  await command('reset'); await waitDense(0);
  assert.equal((await page.evaluate(() => window.__mac)).dense.gpu_chunks, 0);
  await page.waitForFunction(() => window.__mac.drawnPoints === 0 && window.__mac.points === 0,
    null, { timeout: 3000 });
  assert.equal((await page.evaluate(() => window.__macCapture())).meta.drawnPoints, 0,
    'an empty authoritative dense scene must not reveal older coarse dots');
  await command('refill'); await waitDense(320);
  await command('mismatch'); await page.evaluate(() => window.__macRefreshMap());
  const mismatch = await page.evaluate(() => window.__macCapture());
  assert.equal(mismatch.meta.mapStatus.triangles, 1, 'fixture still offers a real measured mesh');
  assert.equal(mismatch.meta.triangles, 0, 'mesh from other capture session is hidden');
  assert.equal(mismatch.meta.drawnTriangles, 0);
  assert.equal(mismatch.meta.dense.gpu_drawn_cells, 320);
  await command('source-switch'); await waitDense(500);
  const matched = await page.evaluate(() => window.__macCapture());
  assert.equal(matched.meta.triangles, 1); assert.ok(matched.meta.drawnTriangles > 0);
  assert.ok(matched.meta.source.startsWith('SIMULATED'));
  // Stop page-driven dense changes and pin a clean scene between captures.
  // Observe actual canvas pixels, not retained GPU-count metadata alone.
  await command('pause');
  await page.waitForFunction(() => window.__mac.dense?.error?.includes('503'), { timeout: 5000 });
  await page.evaluate(() => {
    window.__macCaptureDriven = true;
    window.__macRegisterLayer({ init(context) { window.__denseFixtureContext = context; } });
  });
  const capture = async () => page.evaluate(async () => {
    const frame = window.__macCapture();
    const response = await fetch(`data:image/jpeg;base64,${frame.data}`);
    const bitmap = await createImageBitmap(await response.blob());
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height), context = canvas.getContext('2d');
    context.drawImage(bitmap, 0, 0); bitmap.close();
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let nonblack = 0, total = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      nonblack += Number(pixels[i] + pixels[i + 1] + pixels[i + 2] > 12); total++;
    }
    return { jpeg: frame.data, meta: frame.meta, nonblack, total };
  });
  const fresh = await capture();
  assert.ok(fresh.nonblack > fresh.total / 4, 'first rendered frame is nonblack');
  await page.waitForTimeout(60);
  const idle = await capture();
  await page.waitForTimeout(60);
  const repeated = await capture();
  const idleConsistent = fresh.jpeg === idle.jpeg && idle.jpeg === repeated.jpeg;
  const report = { passed: idleConsistent, scope: 'synthetic DenseMap/Three.js/Chromium SwiftShader loopback',
    initial_gpu_drawn_cells: initial.meta.dense.gpu_drawn_cells,
    deleted_gpu_drawn_cells: deleted.meta.dense.gpu_drawn_cells,
    switched_gpu_drawn_cells: matched.meta.dense.gpu_drawn_cells,
    mismatched_mesh_hidden: mismatch.meta.triangles === 0 && mismatch.meta.drawnTriangles === 0,
    idle_pixels_consistent: idleConsistent, fresh_nonblack_pixels: fresh.nonblack,
    idle_nonblack_pixels: idle.nonblack, repeat_nonblack_pixels: repeated.nonblack,
    retained_idle_gpu_drawn_cells: idle.meta.dense.gpu_drawn_cells, hardware_qualified: false };
  // Preserve a sanitized failure record so the original outcome is reviewable.
  if (!idleConsistent) await writeFile('/tmp/farsight-dense-render-idle-failure.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  assert.equal(errors.length, 0, errors.join('\n'));
  assert.equal(idleConsistent, true, 'idle capture must retain actual positive canvas pixels, not black cleared backbuffer');
} finally { await browser?.close(); }
