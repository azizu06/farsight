// Synthetic native observations; production dense transport, buffers and GPU pixels.
import assert from 'node:assert/strict';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';

const origin = process.argv[2], foregroundCount = Number(process.argv[3]);
const fullCount = 2 * foregroundCount + 2, clearedCount = foregroundCount + 2;
const command = async operation => {
  const response = await fetch(`${origin}/fixture/${operation}`);
  const result = await response.json();
  assert.equal(response.status, 200, JSON.stringify(result));
  return result;
};

let browser;
try {
  browser = await chromium.launch({args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
  const page = await browser.newPage({viewport: {width: 256, height: 256}}), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${origin}/tools/mac_render/render.html?upstream=${encodeURIComponent(origin)}&width=256&height=256&style=unlit&quality=flat&camera=snap&lod=0&msurf=0&raw=0`);
  await page.waitForFunction(() => window.__macRegisterLayer && window.__macCapture);
  await page.evaluate(() => {
    window.__macCaptureDriven = true;
    window.__macRegisterLayer({init(ctx) {
      window.__dotRevisitContext = ctx;
      ctx.camera.position.set(0, 0, 0);
      ctx.camera.quaternion.identity();
      ctx.camera.fov = 60;
      ctx.camera.updateProjectionMatrix();
      ctx.root.children.filter(object => object.type === 'GridHelper').forEach(object => {object.visible = false;});
    }});
  });
  const waitCount = async count => page.waitForFunction(count => {
    window.__macCapture();
    const metrics = window.__mac.dense;
    return metrics?.retained_cells === count && metrics.gpu_uploaded_cells === count && metrics.gpu_drawn_cells === count;
  }, count, {timeout: 7000});
  const capture = async () => page.evaluate(() => {
    const frame = window.__macCapture(), ctx = window.__dotRevisitContext, gl = ctx.renderer.getContext();
    const width = gl.drawingBufferWidth, height = gl.drawingBufferHeight;
    const pixels = new Uint8Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    const center = ((height >> 1) * width + (width >> 1)) * 4;
    let redPixels = 0, greenPixels = 0, redGpu = 0, greenGpu = 0, occluded = false, outside = false;
    for (let i = 0; i < pixels.length; i += 4) {
      redPixels += Number(pixels[i] > 100 && pixels[i] > pixels[i + 1] + 60 && pixels[i] > pixels[i + 2] + 60);
      greenPixels += Number(pixels[i + 1] > 100 && pixels[i + 1] > pixels[i] + 60 && pixels[i + 1] > pixels[i + 2] + 60);
    }
    ctx.root.traverse(object => {
      if (!object.isPoints || !object.visible || !object.parent.visible) return;
      const geometry = object.geometry, position = geometry.getAttribute('position'), color = geometry.getAttribute('color');
      if (!position || !color) return;
      const count = Math.min(position.count, geometry.drawRange.count);
      for (let i = 0; i < count; i++) {
        const x = position.getX(i), z = position.getZ(i), r = color.getX(i), g = color.getY(i), b = color.getZ(i);
        redGpu += Number(r > .9 && g < .05 && b < .05);
        greenGpu += Number(g > .9 && r < .05 && b < .05);
        occluded ||= Math.abs(z + 3) < .0001 && b > .9 && r < .05 && g < .05;
        outside ||= Math.abs(x - 10) < .0001 && Math.abs(z + 1) < .0001 && r > .9 && g > .9;
      }
    });
    return {pixel: [...pixels.subarray(center, center + 4)], redPixels, greenPixels, redGpu, greenGpu,
      occluded, outside, metrics: {...frame.meta.dense}, triangles: frame.meta.drawnTriangles};
  });
  const assertRetained = state => {
    assert.equal(state.greenGpu, foregroundCount, 'all measured background dots remain in the live GPU draw range');
    assert.ok(state.occluded, 'occluded blue point remains in the GPU draw range');
    assert.ok(state.outside, 'out-of-view yellow point remains in the GPU draw range');
    assert.equal(state.triangles, 0, 'this regression renders only dense dots');
  };
  const assertGreen = state => {
    assertRetained(state);
    assert.equal(state.redGpu, 0, 'removed red points are absent from the live GPU draw range');
    assert.equal(state.redPixels, 0, 'removed foreground leaves no red canvas pixels');
    assert.ok(state.greenPixels > 100, 'measured background produces actual green canvas pixels');
    assert.ok(state.pixel[1] > state.pixel[0] + 100 && state.pixel[1] > state.pixel[2] + 100,
      'center pixel exposes the measured green background');
  };
  await waitCount(fullCount);
  const initial = await capture();
  assertRetained(initial);
  assert.equal(initial.redGpu, foregroundCount);
  assert.ok(initial.redPixels > 100);
  assert.ok(initial.pixel[0] > initial.pixel[1] + 100, 'original foreground is visibly red');

  const clearedSource = await command('clear');
  await waitCount(clearedCount);
  const cleared = await capture();
  assertGreen(cleared);
  assert.ok(cleared.metrics.received_deletes >= foregroundCount);

  const resyncSource = await command('resync');
  assert.equal(resyncSource.source_epoch, clearedSource.source_epoch);
  assert.equal(resyncSource.mac_epoch, clearedSource.mac_epoch);
  assert.equal(resyncSource.source_snapshot_reset, true);
  assert.ok(resyncSource.view_age_ms > 2000);
  await waitCount(clearedCount);
  const resynced = await capture();
  assertGreen(resynced);

  const returningSource = await command('return');
  assert.equal(returningSource.source_upserts, foregroundCount, 'identical returning dots produce immediate upserts');
  await waitCount(fullCount);
  const returned = await capture();
  assertRetained(returned);
  assert.equal(returned.redGpu, foregroundCount);
  assert.ok(returned.redPixels > 100);
  assert.ok(returned.pixel[0] > returned.pixel[1] + 100, 'returning object reaches the actual red GPU pixels');
  assert.ok(returned.metrics.received_upserts >= cleared.metrics.received_upserts + foregroundCount);

  await command('clear-again');
  await waitCount(clearedCount);
  const final = await capture();
  assertGreen(final);
  assert.ok(final.metrics.received_deletes >= 2 * foregroundCount);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({passed: true,
    scope: 'synthetic native DepthView -> DenseMap -> DenseAccumulator -> HTTP -> production Three.js/Chromium GPU',
    initial_drawn_cells: initial.metrics.gpu_drawn_cells, cleared_drawn_cells: cleared.metrics.gpu_drawn_cells,
    returned_drawn_cells: returned.metrics.gpu_drawn_cells, final_drawn_cells: final.metrics.gpu_drawn_cells,
    returned_upserts: returningSource.source_upserts, expired_view_resync_preserved_scene: true,
    occluded_and_outside_gpu_preserved: true,
    initial_pixel: initial.pixel, cleared_pixel: cleared.pixel, resynced_pixel: resynced.pixel,
    returned_pixel: returned.pixel, final_pixel: final.pixel, hardware_qualified: false}));
} finally {
  await browser?.close();
}
