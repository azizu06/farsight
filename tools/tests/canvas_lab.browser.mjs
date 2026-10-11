/** Real desktop canvas verification and requestAnimationFrame timing.
 *
 *   node tools/tests/canvas_lab.browser.mjs [--points 20000000] [--seconds 4]
 *        [--url http://localhost:8080/tools/mac_render/canvas-lab.html]
 *        [--out /tmp/farsight-canvas-lab] [--software] [--headed]
 *
 * Native GPU is the default; --software explicitly selects SwiftShader.
 * Timing describes this browser/machine and SIMULATED geometry only.
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';

const option = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at < 0 ? fallback : process.argv[at + 1];
};
const software = process.argv.includes('--software');
const headed = process.argv.includes('--headed');
const output = resolve(option('out', '/tmp/farsight-canvas-lab'));
const durationMs = Number(option('seconds', 4)) * 1000;
const viewport = { width: 1280, height: 800 };
const launchArgs = software
  ? ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']
  : process.platform === 'darwin' ? ['--use-gl=angle', '--use-angle=metal'] : [];
const report = {
  passed: false,
  source: 'SIMULATED',
  scope: 'Real desktop browser canvas with simulated room geometry; no headset, sensors, or motor qualification',
  started_at: new Date().toISOString(),
  viewport,
  duration_requested_ms: durationMs,
  concurrent_load: option('load-note', 'Other concurrent system/GPU load was not measured'),
  browser: { platform: process.platform, headless: !headed, software_requested: software, launch_args: launchArgs },
  artifacts: {},
  errors: [],
};

function percentile(values, fraction) {
  const ordered = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!ordered.length) return null;
  const position = (ordered.length - 1) * fraction;
  const low = Math.floor(position), high = Math.ceil(position);
  return ordered[low] + (ordered[high] - ordered[low]) * (position - low);
}

let browser, server;
await mkdir(output, { recursive: true });
try {
  let url;
  if (option('url')) {
    url = new URL(option('url'));
  } else {
    server = createServer(async (request, response) => {
      try {
        const path = new URL(request.url, 'http://localhost').pathname;
        const content = await readFile(new URL(path.slice(1), new URL('../../', import.meta.url)));
        response.setHeader('Content-Type', path.endsWith('.html') ? 'text/html' : 'text/javascript');
        response.end(content);
      } catch {
        response.writeHead(404);
        response.end();
      }
    });
    await new Promise(listen => server.listen(0, '127.0.0.1', listen));
    url = new URL(`http://127.0.0.1:${server.address().port}/tools/mac_render/canvas-lab.html`);
  }
  const requestedPoints = Number(option('points', url.searchParams.get('points') || 20000000));
  url.searchParams.set('points', requestedPoints);
  report.url = url.href;
  report.requested_points = requestedPoints;

  browser = await chromium.launch({ headless: !headed, args: launchArgs });
  report.browser.version = browser.version();
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
  page.on('pageerror', error => report.errors.push(error.message));
  await page.addInitScript(() => {
    // Observe browser export APIs without substituting render or capture results.
    window.__canvasLabBrowserExports = { data_url: 0, canvas_blob: 0, offscreen_blob: 0 };
    for (const [prototype, method, counter] of [
      [HTMLCanvasElement.prototype, 'toDataURL', 'data_url'],
      [HTMLCanvasElement.prototype, 'toBlob', 'canvas_blob'],
      [globalThis.OffscreenCanvas?.prototype, 'convertToBlob', 'offscreen_blob'],
    ]) {
      if (!prototype?.[method]) continue;
      const original = prototype[method];
      prototype[method] = function (...args) {
        window.__canvasLabBrowserExports[counter]++;
        return original.apply(this, args);
      };
    }
  });
  await page.goto(url.href);
  await page.waitForFunction(() => window.__canvasLab?.ready === true, null, { timeout: 120000 });
  const stats = () => page.evaluate(() => window.__canvasLab.stats());
  const waitPoints = count => page.waitForFunction(expected => {
    const state = window.__canvasLab.stats();
    return state.points === expected && state.uploaded_points === expected;
  }, count, { timeout: 120000 });
  await waitPoints(requestedPoints);
  report.initial = await stats();
  assert.match(report.initial.source, /^SIMULATED/, 'Canvas source must identify simulated geometry');
  assert.equal(report.initial.points, requestedPoints, 'Initial points must equal requested points');
  assert.equal(report.initial.uploaded_points, requestedPoints, 'Requested geometry must reach GPU buffers');
  assert.equal(report.initial.max_points, 20000000, 'Canvas must expose the requested 20-million-point maximum');
  assert.equal(report.initial.grid_visible, true, 'The requested canvas grid must remain visible');
  assert.equal(report.initial.preserve_drawing_buffer, false, 'Direct canvas presentation must use an unpreserved backbuffer');
  if (!url.searchParams.has('aa') || url.searchParams.get('aa') === '0') {
    assert.equal(report.initial.antialias, false, 'Default direct canvas must avoid multisample antialiasing');
  }
  report.visible_text = await page.locator('body').innerText();
  assert.equal(report.visible_text.trim(), '', 'Bare custom canvas must not show text');
  report.ui_controls = await page.locator('button, input, select, textarea').count();
  assert.equal(report.ui_controls, 0, 'Bare custom canvas must not show UI controls');

  // Warm up the production frame loop before measuring. No screenshot or image
  // encoding happens inside the measured requestAnimationFrame interval.
  await page.waitForTimeout(750);
  const timed = await page.evaluate(async duration => {
    const before = window.__canvasLab.stats();
    const exportsBefore = { ...window.__canvasLabBrowserExports };
    const intervals = [];
    let first, previous, last;
    await new Promise(done => {
      const tick = timestamp => {
        if (first === undefined) first = timestamp;
        if (previous !== undefined) intervals.push(timestamp - previous);
        previous = last = timestamp;
        if (timestamp - first >= duration) done();
        else requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    return {
      before,
      after: window.__canvasLab.stats(),
      elapsed_ms: last - first,
      intervals,
      exports_before: exportsBefore,
      exports_after: { ...window.__canvasLabBrowserExports },
    };
  }, durationMs);
  assert.ok(timed.intervals.length > 0, 'Production canvas must continue producing frames');
  assert.ok(timed.after.frame_count > timed.before.frame_count, 'Renderer frame count must advance');
  assert.equal(timed.after.capture_calls, 0, 'Canvas frame loop must avoid the capture/encoding path');
  const exportDelta = Object.fromEntries(Object.keys(timed.exports_after).map(key =>
    [key, timed.exports_after[key] - timed.exports_before[key]]));
  assert.equal(Object.values(exportDelta).reduce((sum, count) => sum + count, 0), 0,
    'Measured frame interval must not export canvas images');
  const frameP50 = percentile(timed.intervals, .5), frameP95 = percentile(timed.intervals, .95);
  const gpuRenderer = timed.after.gpu_renderer || null;
  report.gpu = {
    renderer: gpuRenderer,
    software_observed: gpuRenderer ? /swiftshader|llvmpipe|softpipe|software/i.test(gpuRenderer) : null,
  };
  report.timing = {
    elapsed_ms: timed.elapsed_ms,
    observed_frames: timed.intervals.length,
    renderer_frames: timed.after.frame_count - timed.before.frame_count,
    observed_fps: timed.intervals.length * 1000 / timed.elapsed_ms,
    raf_frame_ms_p50: frameP50,
    raf_frame_ms_p95: frameP95,
    renderer_frame_ms_p50: timed.after.frame_ms_p50,
    renderer_frame_ms_p95: timed.after.frame_ms_p95,
    cpu_render_ms_p50: timed.after.render_ms_p50,
    cpu_render_ms_p95: timed.after.render_ms_p95,
    frame_budget_60hz_fraction_p95: frameP95 / (1000 / 60),
    frame_budget_90hz_fraction_p95: frameP95 / (1000 / 90),
    cpu_render_budget_90hz_fraction_p95: Number.isFinite(timed.after.render_ms_p95)
      ? timed.after.render_ms_p95 / (1000 / 90) : null,
    canvas_export_calls: exportDelta,
    geometry_uploads_during_timing: timed.after.geometry_uploads - timed.before.geometry_uploads,
    note: 'rAF cadence and CPU render submission time; these are not GPU completion timings or headset frame timings',
  };
  report.render = timed.after;

  const canvas = page.locator('canvas').first();
  report.canvas = await canvas.evaluate(element => ({
    width: element.width, height: element.height,
    css_width: element.getBoundingClientRect().width,
    css_height: element.getBoundingClientRect().height,
    device_pixel_ratio: devicePixelRatio,
  }));
  assert.equal(report.canvas.width, viewport.width, 'Canvas backingstore must use the full native viewport width');
  assert.equal(report.canvas.height, viewport.height, 'Canvas backingstore must use the full native viewport height');
  assert.equal(report.canvas.css_width, viewport.width, 'Canvas must fill the viewport width');
  assert.equal(report.canvas.css_height, viewport.height, 'Canvas must fill the viewport height');
  const screenshot = async name => {
    const path = resolve(output, `${name}.png`);
    const png = await canvas.screenshot({ path, animations: 'disabled' });
    report.artifacts[name] = path;
    return png.toString('base64');
  };
  // Decode external browser screenshots, rather than trusting renderer metadata
  // or asking the page to manufacture a capture result.
  const examinePixels = (first, second) => page.evaluate(async ({ first, second }) => {
    const decode = async base64 => {
      const response = await fetch(`data:image/png;base64,${base64}`);
      const bitmap = await createImageBitmap(await response.blob());
      const surface = new OffscreenCanvas(bitmap.width, bitmap.height);
      const context = surface.getContext('2d');
      context.drawImage(bitmap, 0, 0);
      const image = context.getImageData(0, 0, surface.width, surface.height);
      bitmap.close();
      return image;
    };
    const a = await decode(first), b = second ? await decode(second) : null;
    let visible = 0, colorful = 0, changed = 0, difference = 0;
    for (let i = 0; i < a.data.length; i += 4) {
      const rgb = [a.data[i], a.data[i + 1], a.data[i + 2]];
      if (Math.max(...rgb) > 24) visible++;
      if (Math.max(...rgb) > 48 && Math.max(...rgb) - Math.min(...rgb) > 24) colorful++;
      if (b) {
        const delta = rgb.reduce((sum, value, channel) => sum + Math.abs(value - b.data[i + channel]), 0);
        if (delta > 24) changed++;
        difference += delta;
      }
    }
    const total = a.width * a.height;
    return {
      width: a.width, height: a.height, pixels: total,
      visible_fraction: visible / total, colorful_fraction: colorful / total,
      changed_fraction: b ? changed / total : null,
      mean_channel_difference: b ? difference / (total * 3) : null,
      same_dimensions: b ? a.width === b.width && a.height === b.height : null,
    };
  }, { first, second });

  await page.evaluate(() => window.__canvasLab.setAutoOrbit(false));
  await page.waitForTimeout(100);
  const viewA = await screenshot('canvas-view-a');
  report.artifacts.fullscreen = resolve(output, 'canvas-lab-fullscreen.png');
  await page.screenshot({ path: report.artifacts.fullscreen, fullPage: true, animations: 'disabled' });
  report.pixels = await examinePixels(viewA);
  assert.ok(report.pixels.visible_fraction > .05, 'Actual canvas pixels must contain visible geometry');
  assert.ok(report.pixels.colorful_fraction > .01, 'Actual canvas pixels must contain room colors');
  await page.evaluate(() => window.__canvasLab.setAutoOrbit(true));
  await page.waitForTimeout(900);
  await page.evaluate(() => window.__canvasLab.setAutoOrbit(false));
  await page.waitForTimeout(100);
  const viewB = await screenshot('canvas-view-b');
  report.camera_motion = await examinePixels(viewA, viewB);
  assert.equal(report.camera_motion.same_dimensions, true);
  assert.ok(report.camera_motion.changed_fraction > .005, 'Camera orbit must change the actual canvas view');

  assert.equal(timed.after.drawn_points, requestedPoints, 'The full requested point count must be submitted for drawing');
  assert.ok(timed.after.draw_calls > 0, 'The canvas must issue actual draw calls');
  report.max_points_load = requestedPoints === 20000000;
  const changedPoints = requestedPoints === 1 ? 2 : Math.max(1, Math.floor(requestedPoints / 2));
  await page.evaluate(count => window.__canvasLab.setPointCount(count), changedPoints);
  await waitPoints(changedPoints);
  report.point_count_change = { requested: changedPoints, stats: await stats() };
  await page.evaluate(count => window.__canvasLab.setPointCount(count), requestedPoints);
  await waitPoints(requestedPoints);
  report.point_count_restore = await stats();
  assert.equal(report.errors.length, 0, report.errors.join('\n'));
  report.passed = true;
} catch (error) {
  report.failure = String(error.stack || error);
  process.exitCode = 1;
} finally {
  await browser?.close();
  if (server) {
    server.closeAllConnections();
    await new Promise(close => server.close(close));
  }
  report.finished_at = new Date().toISOString();
  report.artifacts.report = resolve(output, 'report.json');
  await writeFile(report.artifacts.report, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({
    passed: report.passed,
    source: report.source,
    gpu: report.gpu,
    timing: report.timing,
    pixels: report.pixels,
    camera_motion: report.camera_motion,
    artifacts: report.artifacts,
    failure: report.failure,
  }, null, 2));
}
