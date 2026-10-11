// Before/after default production renderer pixels, identical accepted native input.
import assert from 'node:assert/strict';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';

const origin = process.argv[2], fixture = JSON.parse(process.argv[3]);
const report = {baseline: '81d915b', scope: 'synthetic accepted native RGB-D -> actual DenseMap HTTP -> production Three.js/Chromium GPU',
  source_cells: fixture.source_cells, native_accepted: fixture.native_accepted,
  hardware_qualified: false, before: null, after: null};
let browser;
try {
  browser = await chromium.launch({args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
  for (const version of ['before', 'after']) {
    const page = await browser.newPage({viewport: {width: 512, height: 512}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    try {
      // Omit msurf: this regression must exercise the production default.
      await page.goto(`${origin}/${version}/tools/mac_render/render.html?upstream=${encodeURIComponent(origin)}&width=512&height=512&camera=snap&lod=0&quality=cine&style=unlit&raw=0`);
      await page.waitForFunction(count => window.__mac?.dense?.retained_cells === count && window.__macRegisterLayer,
        fixture.source_cells, {timeout: 7000});
      await page.evaluate(() => {
        window.__macCaptureDriven = true;
        window.__macRegisterLayer({init(ctx) {
          window.__liveDotDetailContext = ctx;
          ctx.camera.position.set(0, 0, 0);
          ctx.camera.quaternion.identity();
          ctx.camera.fov = 60;
          ctx.camera.updateProjectionMatrix();
          ctx.root.children.filter(object => object.type === 'GridHelper').forEach(object => {object.visible = false;});
        }});
      });
      if (version === 'before') {
        await page.waitForFunction(() => {
          window.__macCapture();
          return window.__mac.measuredSurface?.builds > 0 && window.__mac.measuredSurface?.triangles > 0 &&
            window.__mac.mapStatus?.triangles === 2;
        }, null, {timeout: 7000});
      } else {
        await page.waitForFunction(count => {
          window.__macCapture();
          return window.__mac.dense?.gpu_drawn_cells === count && window.__mac.dense?.gpu_uploaded_cells === count &&
            window.__mac.mapStatus?.triangles === 2;
        }, fixture.source_cells, {timeout: 7000});
      }
      report[version] = await page.evaluate(fixture => {
        const capture = window.__macCapture(), ctx = window.__liveDotDetailContext, gl = ctx.renderer.getContext();
        const width = gl.drawingBufferWidth, height = gl.drawingBufferHeight;
        const pixels = new Uint8Array(width * height * 4);
        gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        const focal = height / (2 * Math.tan(Math.PI / 6));
        const half = .14 * focal / fixture.checker_depth;
        let saturated = 0, total = 0, red = 0, green = 0, difference = 0;
        for (let y = Math.ceil(height / 2 - half); y < height / 2 + half; y++) {
          for (let x = Math.ceil(width / 2 - half); x < width / 2 + half; x++) {
            const at = (y * width + x) * 4, r = pixels[at], g = pixels[at + 1], b = pixels[at + 2];
            const redPixel = r > 150 && r > g + 80 && b < 80;
            const greenPixel = g > 150 && g > r + 80 && b < 80;
            saturated += Number(redPixel || greenPixel);
            red += Number(redPixel); green += Number(greenPixel);
            difference += Math.abs(r - g); total++;
          }
        }
        const [wx, wy, wz] = fixture.yellow_xyz;
        const yellowX = width / 2 + wx * focal / -wz, yellowY = height / 2 + wy * focal / -wz;
        let yellow = 0;
        for (let y = Math.floor(yellowY - 10); y <= yellowY + 10; y++) {
          for (let x = Math.floor(yellowX - 10); x <= yellowX + 10; x++) {
            const at = (y * width + x) * 4;
            yellow += Number(pixels[at] > 150 && pixels[at + 1] > 150 && pixels[at + 2] < 80);
          }
        }
        return {checker_contrast: saturated / total, mean_red_green_difference: difference / total,
          checker_red_pixels: red, checker_green_pixels: green, yellow_pixels: yellow,
          dense: {...capture.meta.dense}, measured_surface: capture.meta.measuredSurface,
          drawn_triangles: capture.meta.drawnTriangles, drawn_points: capture.meta.drawnPoints,
          source_triangles: capture.meta.mapStatus.triangles};
      }, fixture);
      assert.deepEqual(errors, []);
    } finally {
      await page.close();
    }
  }
  assert.equal(report.before.dense.retained_cells, fixture.source_cells);
  assert.equal(report.after.dense.retained_cells, fixture.source_cells);
  assert.equal(report.before.dense.gpu_drawn_cells, 0, 'baseline density layer hides the measured source dot group');
  assert.equal(report.before.yellow_pixels, 0, 'baseline neighborhood filter hides an accepted isolated native sample');
  assert.ok(report.before.checker_contrast < .2, 'baseline 4 cm/color-averaged faces blur the 2 cm native color features');
  assert.ok(report.after.checker_contrast > .5 && report.after.checker_contrast > report.before.checker_contrast + .3,
    'default measured dots preserve strong actual red/green pixel contrast');
  assert.ok(report.after.checker_red_pixels > 1000 && report.after.checker_green_pixels > 1000);
  assert.ok(report.after.yellow_pixels > 3, 'accepted isolated yellow sample remains visible in actual canvas pixels');
  assert.equal(report.after.dense.gpu_drawn_cells, fixture.source_cells, 'every retained source sample reaches the GPU draw');
  assert.equal(report.after.dense.gpu_uploaded_cells, fixture.source_cells);
  assert.equal(report.after.measured_surface, null, 'density/color averaging is absent from the default dot renderer');
  assert.equal(report.after.drawn_triangles, 0, 'coarse same-source faces do not obscure finer measured RGB dots');
  assert.equal(report.after.source_triangles, 2, 'the identical coarse measured mesh remains available from the fixture');
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.failure = String(error.stack || error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
console.log(JSON.stringify(report));
