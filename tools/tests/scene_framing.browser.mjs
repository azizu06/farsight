// Camera changes only: identical measured records, colors and production GPU path.
import assert from 'node:assert/strict';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';

const origin = process.argv[2], first = JSON.parse(process.argv[3]);
const SESSION = '123e4567-e89b-12d3-a456-426614174000';
const report = {baseline: '7fbd4d2', scope: 'synthetic off-origin DenseMap HTTP + actual production Chromium/Three.js GPU',
  hardware_qualified: false};
const distance = (a, b) => Math.hypot(...a.map((value, i) => value - b[i]));
const reset = async center => {
  const response = await fetch(`${origin}/fixture/reset?x=${center[0]}&y=${center[1]}&z=${center[2]}`);
  assert.equal(response.status, 200);
  return response.json();
};
let browser;
try {
  browser = await chromium.launch({args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
  const errors = [];
  const open = async (version, live = false) => {
    const page = await browser.newPage({viewport: {width: 512, height: 320}});
    page.on('pageerror', error => errors.push(error.message));
    const name = live ? 'canvas-lab' : 'render';
    await page.goto(`${origin}/${version}/tools/mac_render/${name}.html?upstream=${encodeURIComponent(origin)}&width=512&height=320&style=unlit&camera=snap&lod=0&source=live`);
    await page.waitForFunction(live => window.__macRegisterLayer && (!live || window.__canvasLab?.ready), live);
    await page.evaluate(live => {
      // This layer observes the real camera without changing its ownership.
      window.__macRegisterLayer({init(ctx) {window.__framingContext = ctx;}});
      if (live) window.__canvasLab.setAutoOrbit(false);
    }, live);
    return page;
  };
  const waitCloud = async (page, description, previousResets = -1, requireDrawn = false) => {
    await page.waitForFunction(({count, previousResets, requireDrawn}) => {
      window.__macCapture();
      const metrics = window.__mac.dense;
      return metrics?.retained_cells === count && metrics.resets > previousResets && !metrics.bootstrap_incomplete &&
        (!requireDrawn || (metrics.gpu_uploaded_cells === count && metrics.gpu_drawn_cells === count));
    }, {count: description.source_cells, previousResets, requireDrawn}, {timeout: 10000});
  };
  const snapshot = async (page, description, hash = false) => page.evaluate(async ({description, hash}) => {
    const frame = window.__macCapture(), ctx = window.__framingContext;
    const {THREE, camera, renderer, root} = ctx, gl = renderer.getContext();
    const width = gl.drawingBufferWidth, height = gl.drawingBufferHeight;
    const pixels = new Uint8Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    let colored = 0;
    for (let i = 0; i < pixels.length; i += 4)
      colored += Number(pixels[i + 1] > 150 && pixels[i + 1] > pixels[i] + 70 && pixels[i + 1] > pixels[i + 2] + 40);
    const projected = [];
    for (const x of [description.low[0], description.high[0]])
      for (const y of [description.low[1], description.high[1]])
        for (const z of [description.low[2], description.high[2]])
          projected.push(new THREE.Vector3(x, y, z).project(camera).toArray());
    const low = [0, 1].map(axis => Math.min(...projected.map(point => point[axis])));
    const high = [0, 1].map(axis => Math.max(...projected.map(point => point[axis])));
    const center = new THREE.Vector3(...description.center).project(camera).toArray();
    let sha256 = null, pointCount = 0;
    if (hash) {
      const bytes = new Uint8Array(description.source_cells * 16), view = new DataView(bytes.buffer);
      const srgb = value => Math.round(255 * (value <= .0031308 ? 12.92 * value : 1.055 * value ** (1 / 2.4) - .055));
      root.traverse(object => {
        if (!object.isPoints || !object.visible || !object.parent.visible) return;
        const position = object.geometry.getAttribute('position'), color = object.geometry.getAttribute('color');
        if (!position || !color) return;
        const count = Math.min(position.count, object.geometry.drawRange.count);
        for (let i = 0; i < count; i++) {
          const at = pointCount++ * 16;
          view.setInt32(at, Math.round(position.getX(i) * 1000), true);
          view.setInt32(at + 4, Math.round(position.getY(i) * 1000), true);
          view.setInt32(at + 8, Math.round(position.getZ(i) * 1000), true);
          bytes.set([srgb(color.getX(i)), srgb(color.getY(i)), srgb(color.getZ(i)), 1], at + 12);
        }
      });
      sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('');
    }
    return {position: camera.position.toArray(), quaternion: camera.quaternion.toArray(), center_ndc: center,
      low_ndc: low, high_ndc: high, projected_area: (high[0] - low[0]) * (high[1] - low[1]) / 4,
      colored_pixels: colored, colored_fraction: colored / (width * height), source_sha256: sha256, point_count: pointCount,
      dense: {...frame.meta.dense}, grid_visible: root.children.some(object => object.type === 'GridHelper' && object.visible),
      pose_valid: frame.meta.poseValid, pose_echo: frame.meta.poseEcho,
      scene_overview: frame.meta.sceneOverview ?? null};
  }, {description, hash});
  const assertFramed = sample => {
    assert.ok(Math.abs(sample.center_ndc[0]) < .04 && Math.abs(sample.center_ndc[1]) < .04,
      'actual measured scene center is centered by the production camera');
    assert.ok(sample.low_ndc.every(value => value > -.98) && sample.high_ndc.every(value => value < .98),
      'actual measured rectangle corners fit inside the visible camera');
    assert.ok(sample.projected_area > .10, 'known cloud fills a substantial part of the actual canvas projection');
    assert.ok(sample.colored_fraction > .01, 'native point colors produce visible positive pixels');
    assert.equal(sample.grid_visible, true, 'requested display grid remains visible');
  };

  const beforePage = await open('before');
  await waitCloud(beforePage, first, -1, true);
  report.before = await snapshot(beforePage, first, true);
  assert.deepEqual(report.before.position, [9, 6, 9], 'recorded baseline keeps the old fixed camera');
  assert.equal(report.before.source_sha256, first.source_sha256);
  await beforePage.close();

  const page = await open('after');
  await waitCloud(page, first, -1, true);
  report.after = await snapshot(page, first, true);
  assertFramed(report.after);
  assert.equal(report.after.source_sha256, first.source_sha256, 'camera framing does not move, recolor or replace native samples');
  assert.equal(report.after.point_count, first.source_cells);
  assert.ok(report.after.projected_area > report.before.projected_area * 3);
  assert.ok(report.after.colored_pixels > report.before.colored_pixels * 1.5, 'actual colored canvas coverage improves');
  let resets = report.after.dense.resets;
  const next = await reset([6, 2, 4]);
  await waitCloud(page, next, resets, true);
  const reframed = await snapshot(page, next, true);
  assertFramed(reframed);
  assert.equal(reframed.source_sha256, next.source_sha256);
  assert.ok(distance(reframed.position, report.after.position) > 5, 'overview-owned camera follows the new captured world');
  report.owned_overview_reframes_new_epoch = true;

  const tracked = {tracked: true, receiptAgeMs: 0, receivedMonoMs: 987654321, sentMonoMs: 5, sentWallMs: 10,
    position: [6, 4, 10], quaternion: [0, 0, 0, 1], session_id: SESSION, frame_id: 7, pose_id: 7,
    fov_y_deg: 70, aspect: 1.6, space: 'ar_map'};
  const trackedCapture = await page.evaluate(pose => {window.__macPose = pose; return window.__macCapture();}, tracked);
  assert.equal(trackedCapture.meta.poseValid, true);
  assert.deepEqual(trackedCapture.meta.viewPosition, tracked.position);
  assert.equal(trackedCapture.meta.poseEcho.frame_id, tracked.frame_id);
  await page.waitForTimeout(280);
  let expired = await snapshot(page, next);
  assert.equal(expired.pose_valid, false);
  assert.equal(expired.pose_echo, null);
  assert.deepEqual(expired.position, tracked.position, 'pose expiry keeps its exact last tracked camera');
  resets = expired.dense.resets;
  const third = await reset([12, 3, -10]);
  await waitCloud(page, third, resets);
  expired = await snapshot(page, third);
  assert.deepEqual(expired.position, tracked.position, 'new scene epoch cannot reclaim a previously tracked camera');
  report.tracked_and_expired_pose_preserved = true;
  await page.close();

  const manualPage = await open('after');
  await waitCloud(manualPage, third, -1, true);
  const manual = await manualPage.evaluate(center => {
    window.__macRegisterLayer({init(ctx) {
      ctx.camera.position.set(17, 6, 9);
      ctx.camera.lookAt(...center);
      ctx.requestFrame();
    }});
    window.__macCapture();
    return {position: window.__framingContext.camera.position.toArray(), quaternion: window.__framingContext.camera.quaternion.toArray(),
      resets: window.__mac.dense.resets};
  }, third.center);
  const fourth = await reset([-8, 2, -8]);
  await waitCloud(manualPage, fourth, manual.resets);
  const held = await snapshot(manualPage, fourth);
  assert.deepEqual(held.position, manual.position);
  assert.deepEqual(held.quaternion, manual.quaternion, 'explicit plug-in camera framing survives later scene epochs');
  report.manual_layer_camera_preserved = true;
  await manualPage.close();

  const livePage = await open('after', true);
  await waitCloud(livePage, fourth, -1, true);
  await livePage.waitForFunction(description => {
    // Dense bootstrap and the live OrbitControls fit finish on separate draws.
    // Wait for the real rendered camera to frame the known input rectangle.
    window.__macCapture();
    const {THREE, camera} = window.__framingContext;
    const center = new THREE.Vector3(...description.center).project(camera);
    if (Math.abs(center.x) >= .04 || Math.abs(center.y) >= .04) return false;
    const corners = [];
    for (const x of [description.low[0], description.high[0]])
      for (const y of [description.low[1], description.high[1]])
        for (const z of [description.low[2], description.high[2]])
          corners.push(new THREE.Vector3(x, y, z).project(camera));
    const lowX = Math.min(...corners.map(point => point.x)), highX = Math.max(...corners.map(point => point.x));
    const lowY = Math.min(...corners.map(point => point.y)), highY = Math.max(...corners.map(point => point.y));
    return lowX > -.98 && lowY > -.98 && highX < .98 && highY < .98 &&
      (highX - lowX) * (highY - lowY) / 4 > .10;
  }, fourth, {timeout: 10000});
  const live = await snapshot(livePage, fourth, true);
  assertFramed(live);
  assert.equal(live.source_sha256, fourth.source_sha256);
  assert.equal((await livePage.evaluate(() => window.__canvasLab.stats())).source_mode, 'live');
  await livePage.mouse.move(250, 150);
  await livePage.mouse.down();
  await livePage.mouse.move(320, 190, {steps: 8});
  await livePage.mouse.up();
  await livePage.waitForTimeout(600);
  const orbited = await snapshot(livePage, fourth);
  assert.ok(distance(orbited.position, live.position) > .5, 'real pointer interaction changes the live OrbitControls camera');
  const orbitRadius = distance(orbited.position, fourth.center);
  const fifth = await reset([10, 2, 6]);
  await waitCloud(livePage, fifth, orbited.dense.resets);
  // Project the original target after the reset: accepted manual orbit must
  // remain around it, while the new source data updates independently.
  const liveHeld = await snapshot(livePage, fourth);
  assert.ok(Math.abs(distance(liveHeld.position, fourth.center) - orbitRadius) < .01);
  assert.ok(Math.abs(liveHeld.center_ndc[0]) < .04 && Math.abs(liveHeld.center_ndc[1]) < .04,
    'scene changes do not replace the manually chosen live orbit target');
  report.live_orbit_camera_preserved = true;
  await livePage.close();

  assert.deepEqual(errors, []);
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = String(error.stack || error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
console.log(JSON.stringify(report));
