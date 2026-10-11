import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, writeFile } from 'node:fs/promises';
import { chromium } from '../../quest/node_modules/playwright/index.mjs';

// GPU raster proof only: two synthetic colored planes, no device/control/feed.
const root = new URL('../../', import.meta.url);
const html = `<!doctype html><title>SIMULATED stereo raster fixture</title>
<body><strong id="source">SIMULATED · GPU RASTER FIXTURE · NO HEADSET</strong>
<script type="module">
import * as THREE from '/quest/node_modules/three/build/three.module.js';
import { StereoPass } from '/tools/mac_render/stereo-pass.mjs';
window.runStereoFixture = () => {
  const width = 512, height = 256, eyeWidth = width / 2;
  const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false });
  renderer.setPixelRatio(1); renderer.setSize(width, height, false);
  renderer.setClearColor(0x122433); renderer.outputColorSpace = THREE.SRGBColorSpace;
  document.body.append(renderer.domElement);
  const scene = new THREE.Scene();
  const near = new THREE.Mesh(new THREE.PlaneGeometry(.12, .16), new THREE.MeshBasicMaterial({ color: 0xff0000, toneMapped: false }));
  near.position.set(-.2, 0, -1);
  const far = new THREE.Mesh(new THREE.PlaneGeometry(.36, .42), new THREE.MeshBasicMaterial({ color: 0x00ff00, toneMapped: false }));
  far.position.set(.25, 0, -3); scene.add(near, far);
  const leftProjection = new THREE.Matrix4().makePerspective(-.09, .11, .1, -.1, .1, 20).toArray();
  const rightProjection = new THREE.Matrix4().makePerspective(-.091, .109, .1, -.1, .1, 20).toArray();
  const left = { eye: 'left', position: [-.05, 0, 0], quaternion: [0, 0, 0, 1], projection: leftProjection };
  const right = { eye: 'right', position: [.05, 0, 0], quaternion: [0, 0, 0, 1], projection: rightProjection };
  // Deliberately reverse input order; names determine packed eye assignment.
  const eyes = [right, left], stereo = new StereoPass({ THREE, renderer });
  const savedTarget = new THREE.WebGLRenderTarget(32, 32), hookTarget = new THREE.WebGLRenderTarget(16, 16);
  renderer.setRenderTarget(savedTarget); renderer.setViewport(3, 5, 21, 22);
  renderer.setScissor(7, 9, 15, 16); renderer.setScissorTest(false); renderer.autoClear = true;
  const state = () => ({ targetIsSaved: renderer.getRenderTarget() === savedTarget,
    viewport: renderer.getViewport(new THREE.Vector4()).toArray(), scissor: renderer.getScissor(new THREE.Vector4()).toArray(),
    scissorTest: renderer.getScissorTest(), autoClear: renderer.autoClear,
    activeCubeFace: renderer.getActiveCubeFace(), activeMipmapLevel: renderer.getActiveMipmapLevel() });
  const before = state();
  let pixels, captures = 0, publications = 0;
  const renderCapture = (pair, dimensions = { width, height }) => {
    const result = stereo.render(scene, pair, { ...dimensions,
      pre({ renderer }) {
        // A plug-in may change targets/state; StereoPass must rebind its eye.
        renderer.setRenderTarget(hookTarget); renderer.setViewport(1, 2, 4, 8);
        renderer.setScissorTest(false); renderer.autoClear = true;
      },
      post({ eye, renderer }) {
        if (eye !== 'right') return;
        // Only read after BOTH eye renders, in the same JS turn. Renderer is
        // still bound to the packed default framebuffer until helper returns.
        pixels = new Uint8Array(width * height * 4);
        renderer.getContext().readPixels(0, 0, width, height, renderer.getContext().RGBA,
          renderer.getContext().UNSIGNED_BYTE, pixels);
        captures++;
      } });
    publications++; return result;
  };
  const result = renderCapture(eyes), after = state();
  const pixelStats = {};
  for (const [eye, offset] of [['left', 0], ['right', eyeWidth]]) {
    const stats = { nonblack: 0, redCount: 0, greenCount: 0, redX: 0, greenX: 0 };
    for (let y = 0; y < height; y++) for (let x = 0; x < eyeWidth; x++) {
      const at = (y * width + x + offset) * 4, r = pixels[at], g = pixels[at + 1], b = pixels[at + 2];
      if (r + g + b > 12) stats.nonblack++;
      if (r > 180 && g < 80 && b < 80) { stats.redCount++; stats.redX += x + .5; }
      if (g > 180 && r < 80 && b < 80) { stats.greenCount++; stats.greenX += x + .5; }
    }
    stats.redX /= stats.redCount; stats.greenX /= stats.greenCount; pixelStats[eye] = stats;
  }
  const expected = {};
  for (const eye of ['left', 'right']) {
    const camera = stereo.cameras[eye];
    expected[eye] = { near: (near.position.clone().project(camera).x + 1) * eyeWidth / 2,
      far: (far.position.clone().project(camera).x + 1) * eyeWidth / 2 };
  }
  const successfulCaptures = captures, successfulPublications = publications;
  const invalid = [
    { pair: [left] }, { pair: [left, left] },
    { pair: [left, { ...right, projection: Array(16).fill(0) }] },
    { pair: eyes, dimensions: { width: 512, height: 256, codedWidth: 512, codedHeight: 128 } },
  ];
  let rejected = 0;
  for (const sample of invalid) {
    try { renderCapture(sample.pair, sample.dimensions); }
    catch { rejected++; }
  }
  const invalidAfter = state();
  const output = { source: 'SIMULATED', scope: 'Chromium/Three.js real GPU raster fixture; no encoded pair/headset',
    width, height, result, before, after, invalidAfter, pixelStats, expected,
    exactLeftProjection: stereo.cameras.left.projectionMatrix.toArray(),
    exactRightProjection: stereo.cameras.right.projectionMatrix.toArray(), leftProjection, rightProjection,
    invalidPairsRejected: rejected, successfulCaptures, successfulPublications, captures, publications };
  savedTarget.dispose(); hookTarget.dispose(); near.geometry.dispose(); far.geometry.dispose();
  near.material.dispose(); far.material.dispose(); renderer.dispose();
  return output;
};
</script></body>`;
const server = createServer(async (request, response) => {
  const path = new URL(request.url, 'http://localhost').pathname;
  try {
    if (path === '/') { response.setHeader('Content-Type', 'text/html'); response.end(html); return; }
    if (!path.startsWith('/quest/') && path !== '/tools/mac_render/stereo-pass.mjs') throw new Error('No route');
    const file = new URL(path.slice(1), root);
    response.setHeader('Content-Type', 'text/javascript'); response.end(await readFile(file));
  } catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let browser;
try {
  browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const page = await browser.newPage({ viewport: { width: 640, height: 360 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.waitForFunction(() => typeof window.runStereoFixture === 'function', { timeout: 5000 });
  const raster = await page.evaluate(() => window.runStereoFixture());
  assert.equal(errors.length, 0, errors.join('\n'));
  assert.ok((await page.locator('#source').textContent()).includes('SIMULATED'));
  assert.equal(raster.result.metadata.stereo, true); assert.equal(raster.result.metadata.layout, 'sbs-left-right');
  assert.deepEqual(raster.exactLeftProjection, raster.leftProjection);
  assert.deepEqual(raster.exactRightProjection, raster.rightProjection);
  for (const eye of ['left', 'right']) {
    const pixels = raster.pixelStats[eye];
    assert.ok(pixels.nonblack > 1000, `${eye}: black half`);
    assert.ok(pixels.redCount > 20 && pixels.greenCount > 20, `${eye}: both depth objects must rasterize`);
    assert.ok(Math.abs(pixels.redX - raster.expected[eye].near) < 1, `${eye}: near asymmetric projection centroid`);
    assert.ok(Math.abs(pixels.greenX - raster.expected[eye].far) < 1, `${eye}: far asymmetric projection centroid`);
    assert.equal(raster.result.eye_draws[eye].calls, 2);
    assert.equal(raster.result.eye_draws[eye].triangles, 4);
  }
  const nearDisparity = raster.pixelStats.left.redX - raster.pixelStats.right.redX;
  const farDisparity = raster.pixelStats.left.greenX - raster.pixelStats.right.greenX;
  assert.ok(nearDisparity > farDisparity + 5, 'near object requires greater depth disparity than far object');
  assert.ok(farDisparity > 1, 'far object is still a distinct stereo raster, not a mono copy');
  assert.deepEqual(raster.after, raster.before, 'renderer state restored after the packed pair');
  assert.deepEqual(raster.invalidAfter, raster.before, 'invalid inputs change no renderer state');
  assert.equal(raster.invalidPairsRejected, 4);
  assert.equal(raster.captures, raster.successfulCaptures, 'invalid pair cannot invoke capture hook');
  assert.equal(raster.publications, raster.successfulPublications, 'invalid pair yields no publishable result');
  const report = { passed: true, source: 'SIMULATED', scope: raster.scope,
    layout: raster.result.metadata.layout, per_eye: raster.pixelStats,
    near_pixel_disparity: nearDisparity, far_pixel_disparity: farDisparity,
    exact_asymmetric_projections: true, labeled_eye_assignment: true,
    renderer_state_restored: true, invalid_pairs_no_capture_publication: true,
    hardware_qualified: false, encoded_pair_qualified: false };
  const artifact = process.argv[2];
  if (artifact) await writeFile(artifact, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await browser?.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
