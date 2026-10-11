/** Actual-renderer stereo pixels, synthetic markers only; no devices/control.
 * Private preparation: FARSIGHT_ROOT=/path/to/farsight node this.mjs --prepare
 * Run only after owner SHA + GPU GO: same environment, node this.mjs --run
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repository = resolve(process.env.FARSIGHT_ROOT || fileURLToPath(new URL('../../../', import.meta.url)));
const output = resolve(process.env.FARSIGHT_STEREO_OUTPUT || '/tmp/farsight-stereo-browser-evidence');
const python = process.env.FARSIGHT_PYTHON || resolve(repository, '.venv/bin/python');
const THREE = await import(pathToFileURL(resolve(repository, 'quest/node_modules/three/build/three.module.js')).href);
export const WIDTH = 1280, HEIGHT = 720, EYE_WIDTH = WIDTH / 2;
export const SESSION = '123e4567-e89b-12d3-a456-426614174000';
const MAP_SESSION = '123e4567-e89b-12d3-a456-426614174001';
export const MARKERS = [
  { name: 'near', color: 'red', position: [0, 1.8, -1], size: .08 },
  { name: 'far', color: 'blue', position: [0, .8, -4], size: .32 },
];
export function fixtureEyes() {
  return ['left', 'right'].map((eye, index) => {
    const camera = new THREE.PerspectiveCamera(90, EYE_WIDTH / HEIGHT, .05, 20);
    camera.projectionMatrix.elements[8] = index ? .04 : .025;
    camera.projectionMatrix.elements[9] = index ? .025 : -.015;
    return { eye, position: [index ? .032 : -.032, 1.6, 0], quaternion: [0, 0, 0, 1],
      projection: camera.projectionMatrix.toArray() };
  });
}
export function expectedCentroids(eyes = fixtureEyes()) {
  return Object.fromEntries(eyes.map(eye => {
    const camera = new THREE.PerspectiveCamera(); camera.position.fromArray(eye.position);
    camera.quaternion.fromArray(eye.quaternion); camera.projectionMatrix.fromArray(eye.projection); camera.updateMatrixWorld(true);
    return [eye.eye, Object.fromEntries(MARKERS.map(marker => {
      const ndc = new THREE.Vector3().fromArray(marker.position).project(camera);
      return [marker.name, { x: (ndc.x + 1) * EYE_WIDTH / 2, y: (1 - ndc.y) * HEIGHT / 2 }];
    }))];
  }));
}
export function centroid(pixels, width, height, half, color) {
  let sumX = 0, sumY = 0, count = 0;
  const eyeWidth = width / 2, offset = half * eyeWidth;
  for (let y = 0; y < height; y++) for (let x = 0; x < eyeWidth; x++) {
    const at = (y * width + offset + x) * 4, red = pixels[at], green = pixels[at + 1], blue = pixels[at + 2];
    const match = color === 'red' ? red > 150 && red > green * 2 && red > blue * 2
      : blue > 150 && blue > red * 2 && blue > green * 2;
    if (match) { sumX += x + .5; sumY += y + .5; count++; }
  }
  assert.ok(count > 150, `${color} marker missing in half${half}: ${count} color pixels`);
  return { x: sumX / count, y: sumY / count, pixels: count };
}
function pythonJSON(program, value) {
  return JSON.parse(execFileSync(python, ['-c', program], { cwd: repository,
    input: JSON.stringify(value), encoding: 'utf8', timeout: 10000 }));
}
function publicationGate(meta) {
  return pythonJSON('import json,sys; from tools.mac_render.stereo import capture_format; print(json.dumps(capture_format(json.load(sys.stdin), "sbs-left-right",1280,720)))', meta);
}
function producerPose(eyes, frame = 7) {
  return { tracked: true, receipt_age_ms: 0, received_mono_ms: 987654321,
    sent_mono_ms: 123, sent_wall_ms: 1791670000000, position: [0, 1.6, 0], quaternion: [0, 0, 0, 1], ray: null,
    session_id: SESSION, frame_id: frame, pose_id: frame, client_nonce: `SIM-fixture-${frame}`,
    space: 'ar_map', map_revision: 1, stereo: true, fov_y_deg: 90, aspect: WIDTH / HEIGHT, eyes };
}
function assertMetadata(meta, pose) {
  assert.equal(meta.stereo, true, 'Actual renderer must explicitly produce stereo, never silently qualify mono');
  assert.equal(meta.layout, 'sbs-left-right'); assert.equal(meta.eye_width, EYE_WIDTH); assert.equal(meta.eye_height, HEIGHT);
  assert.equal(meta.source, 'SIMULATED'); assert.equal(meta.poseEcho?.session_id, pose.session_id);
  assert.equal(meta.poseEcho?.frame_id, pose.frame_id); assert.equal(meta.poseEcho?.pose_id, pose.pose_id);
  assert.deepEqual(meta.poseEcho?.eyes.map(eye => eye.eye), ['left', 'right'], 'Packed metadata must declare canonical left/right labels');
  for (const eye of fixtureEyes()) {
    const echo = meta.poseEcho.eyes.find(value => value.eye === eye.eye);
    assert.deepEqual(echo.position, eye.position); assert.deepEqual(echo.quaternion, eye.quaternion);
    assert.deepEqual(echo.projection, eye.projection, 'Exact asymmetric eye projection preserved');
  }
  assert.notEqual(publicationGate(meta), null, 'Real server gate must accept this actual captured pair');
}
function assertPixels(result, report) {
  assert.equal(result.width, WIDTH); assert.equal(result.height, HEIGHT);
  const expected = expectedCentroids(), measured = {};
  for (const [index, eye] of ['left', 'right'].entries()) {
    measured[eye] = {};
    for (const marker of MARKERS) {
      const point = centroid(result.pixels, WIDTH, HEIGHT, index, marker.color);
      measured[eye][marker.name] = point;
      assert.ok(Math.abs(point.x - expected[eye][marker.name].x) <= 2.5, `${eye}/${marker.name} x centroid does not match provided projection`);
      assert.ok(Math.abs(point.y - expected[eye][marker.name].y) <= 2.5, `${eye}/${marker.name} y centroid does not match provided projection`);
    }
  }
  const near = measured.left.near.x - measured.right.near.x, far = measured.left.far.x - measured.right.far.x;
  assert.ok(near > far + 10, `Near disparity must exceed far: near=${near},far=${far}`);
  assert.ok(Math.abs(near) > 10 && Math.abs(far) > 3, 'Duplicated mono halves are not stereo');
  Object.assign(report, { expectedCentroids: expected, measuredCentroids: measured, nearDisparityPixels: near, farDisparityPixels: far });
}
function encodePair(data) {
  const command = pythonJSON('import json; from tools.mac_render.server import RenderEncoder; print(json.dumps(RenderEncoder(None,1280,720,30,8000000,True,layout="sbs-left-right").ffmpeg_command()))', null);
  command.splice(command.lastIndexOf('-f'), 0, '-frames:v', '1');
  const bytes = execFileSync(command[0], command.slice(1), { input: Buffer.from(data, 'base64'), maxBuffer: 12 * 1024 * 1024, timeout: 10000 });
  const dimensions = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-count_frames',
    '-show_entries', 'stream=width,height,nb_read_frames', '-of', 'json', 'pipe:0'], { input: bytes, encoding: 'utf8', timeout: 10000 })).streams[0];
  assert.equal(dimensions.width, WIDTH); assert.equal(dimensions.height, HEIGHT); assert.equal(Number(dimensions.nb_read_frames), 1);
  const pixels = execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-i', 'pipe:0', '-frames:v', '1',
    '-pix_fmt', 'rgba', '-f', 'rawvideo', 'pipe:1'], { input: bytes, maxBuffer: 12 * 1024 * 1024, timeout: 10000 });
  const result = { width: WIDTH, height: HEIGHT, pixels }, measurements = {};
  assertPixels(result, measurements);
  return { bytes, report: { source: 'Actual RenderEncoder.ffmpeg_command, explicit hardware=True; one actual captured pair',
    actualEncoder: command[command.indexOf('-c:v') + 1],
    codedWidth: dimensions.width, codedHeight: dimensions.height, decodedFrames: 1, encodedBytes: bytes.length, command, ...measurements } };
}
export async function run() {
  const report = { passed: false, class: 'Software-only actual-renderer stereo pixel fixture; SIMULATED markers, no hardware',
    contract: { layout: 'sbs-left-right', width: WIDTH, height: HEIGHT, eyeWidth: EYE_WIDTH, eyeHeight: HEIGHT },
    hardwareQualified: false, realSensorGeometry: false, errors: [] };
  await mkdir(output, { recursive: true });
  try { report.checkoutHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repository, encoding: 'utf8' }).trim(); } catch {}
  let browser;
  const paths = [], emptyPoints = Buffer.alloc(4), emptyMesh = Buffer.alloc(8);
  const status = { source: 'SIMULATED', session_id: MAP_SESSION, map_epoch: 1, generation: 0,
    revision: 1, mesh_revision: 0, points: 0, triangles: 0, connected: true, fresh: true, age_ms: 0 };
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname; paths.push(path);
    if (path === '/map/status') { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(status)); return; }
    if (path === '/map/points.bin' || path === '/map/mesh.bin') {
      response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'X-Map-Session': MAP_SESSION,
        'X-Map-Epoch': '1', 'X-Map-Generation': '0', 'X-Map-Revision': path.endsWith('mesh.bin') ? '0' : '1' });
      response.end(path.endsWith('mesh.bin') ? emptyMesh : emptyPoints); return;
    }
    try {
      if (!(path.startsWith('/quest/') || path.startsWith('/tools/mac_render/'))) throw new Error('not fixture asset');
      const asset = resolve(repository, path.slice(1)), fromRoot = relative(repository, asset);
      if (fromRoot.startsWith('..') || fromRoot.startsWith('/')) throw new Error('outside root');
      const data = await readFile(asset); response.writeHead(200, { 'Content-Type': path.endsWith('.html') ? 'text/html' : 'text/javascript' }); response.end(data);
    } catch { response.writeHead(404); response.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const { chromium } = await import(pathToFileURL(resolve(repository, 'quest/node_modules/playwright/index.mjs')).href);
    browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT } });
    page.on('pageerror', error => report.errors.push(error.message));
    await page.routeWebSocket('**/*', socket => { report.errors.push('Unexpected fixture WebSocket: ' + socket.url()); socket.close(); });
    const query = execFileSync(python, ['-c', 'from tools.mac_render.server import render_page_query,parse_args; import sys; print(render_page_query(parse_args(["--port",sys.argv[1],"--width","1280","--height","720","--stereo","sbs-left-right"])))', String(server.address().port)],
      { cwd: repository, encoding: 'utf8', timeout: 10000 }).trim();
    await page.goto(`${origin}/tools/mac_render/render.html?${query}&quality=flat&style=unlit&lod=1&jq=.98`);
    await page.waitForFunction(() => typeof window.__macRegisterLayer === 'function' &&
      typeof window.__macCapture === 'function' && typeof window.__macRefreshMap === 'function', undefined, { timeout: 10000 });
    await page.evaluate(() => window.__macRefreshMap());
    await page.evaluate(markers => {
      window.__macCaptureDriven = true;
      window.__stereoFixture = { draws: [], viewportCalls: [] };
      window.__macRegisterLayer({ init(ctx) {
        const { THREE, renderer, scene, root } = ctx; root.visible = false;
        // Establish the ordinary full-canvas state before testing eye passes.
        renderer.setRenderTarget(null); renderer.setViewport(0, 0, ctx.width, ctx.height);
        renderer.setScissor(0, 0, ctx.width, ctx.height); renderer.setScissorTest(false);
        window.__stereoFixture.context = ctx;
        const gl = renderer.getContext(), viewport = gl.viewport;
        gl.viewport = function(...args) { window.__stereoFixture.viewportCalls.push([...args]); return viewport.apply(this, args); };
        for (const marker of markers) {
          const object = new THREE.Mesh(new THREE.PlaneGeometry(marker.size, marker.size),
            new THREE.MeshBasicMaterial({ color: marker.color, fog: false, toneMapped: false, side: THREE.DoubleSide }));
          object.position.fromArray(marker.position); scene.add(object);
        }
        const draw = renderer.render;
        renderer.render = function(actualScene, camera) {
          if (actualScene === scene) {
            const viewport = renderer.getViewport(new THREE.Vector4()), scissor = renderer.getScissor(new THREE.Vector4());
            window.__stereoFixture.draws.push({ viewport: viewport.toArray(), scissor: scissor.toArray(),
              scissorTest: renderer.getScissorTest(), projection: camera.projectionMatrix.toArray(), position: camera.position.toArray() });
          }
          return draw.call(this, actualScene, camera);
        };
      } });
    }, MARKERS);
    const script = execFileSync(python, ['-c', 'from tools.mac_render.server import _CAPTURE_POSE_JS; print(_CAPTURE_POSE_JS)'], { cwd: repository, encoding: 'utf8', timeout: 10000 });
    const capture = pose => page.evaluate(async ({ script, pose }) => {
      const ctx = window.__stereoFixture.context, THREE = ctx.THREE;
      const before = { viewport: ctx.renderer.getViewport(new THREE.Vector4()).toArray(),
        actualViewport: Array.from(ctx.renderer.getContext().getParameter(ctx.renderer.getContext().VIEWPORT)),
        scissor: ctx.renderer.getScissor(new THREE.Vector4()).toArray(), scissorTest: ctx.renderer.getScissorTest(), renderTarget: ctx.renderer.getRenderTarget() === null };
      window.__stereoFixture.draws = []; window.__stereoFixture.viewportCalls = [];
      const result = await (0, eval)(script)(pose);
      const after = { viewport: ctx.renderer.getViewport(new THREE.Vector4()).toArray(),
        actualViewport: Array.from(ctx.renderer.getContext().getParameter(ctx.renderer.getContext().VIEWPORT)),
        scissor: ctx.renderer.getScissor(new THREE.Vector4()).toArray(), scissorTest: ctx.renderer.getScissorTest(), renderTarget: ctx.renderer.getRenderTarget() === null };
      return { ...result, before, after, draws: window.__stereoFixture.draws, viewportCalls: window.__stereoFixture.viewportCalls };
    }, { script, pose });
    const readPixels = data => page.evaluate(async data => {
      const image = new Image(); image.src = 'data:image/jpeg;base64,' + data; await image.decode();
      const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
      const ctx = canvas.getContext('2d', { willReadFrequently: true }); ctx.drawImage(image, 0, 0);
      return { width: canvas.width, height: canvas.height, pixels: Array.from(ctx.getImageData(0, 0, canvas.width, canvas.height).data) };
    }, data);
    const pose = producerPose(fixtureEyes()), positive = await capture(pose);
    assertMetadata(positive.meta, pose); assertPixels(await readPixels(positive.data), report);
    await writeFile(resolve(output, 'packed-pair.jpg'), Buffer.from(positive.data, 'base64'));
    const encoded = encodePair(positive.data); report.encodedPair = encoded.report;
    await writeFile(resolve(output, 'packed-pair.mp4'), encoded.bytes);
    assert.deepEqual(positive.after, positive.before, 'Eye rendering must restore viewport/scissor/target state');
    assert.equal(positive.draws.length, 2, 'Exactly two main scene draws, without a duplicate head-camera draw');
    assert.ok(positive.viewportCalls.some(viewport => viewport[0] === 0 && viewport[2] === EYE_WIDTH && viewport[3] === HEIGHT), 'Actual left-eye viewport missing');
    assert.ok(positive.viewportCalls.some(viewport => viewport[0] === EYE_WIDTH && viewport[2] === EYE_WIDTH && viewport[3] === HEIGHT), 'Actual right-eye viewport missing');
    report.positiveMetadata = positive.meta; report.viewportState = { before: positive.before, after: positive.after, draws: positive.draws, viewportCalls: positive.viewportCalls };
    const reversed = await capture(producerPose([...fixtureEyes()].reverse(), 8));
    if (publicationGate(reversed.meta) === null) report.reversedPairPolicy = 'rejected/unavailable';
    else {
      const reversedPixels = {}; assertPixels(await readPixels(reversed.data), reversedPixels);
      report.reversedPairPixels = reversedPixels;
      try {
        assertMetadata(reversed.meta, producerPose(fixtureEyes(), 8));
        report.reversedPairPolicy = 'canonical labels, correctly sorted and rendered';
      } catch (error) {
        report.reversedPairPolicy = 'pixels correctly sorted; noncanonical accepted metadata';
        report.errors.push(error.message);
      }
    }
    const invalid = [
      ['missing', producerPose([], 9)], ['one-eye', producerPose([fixtureEyes()[0]], 10)],
      ['duplicate-label', producerPose(fixtureEyes().map(eye => ({ ...eye, eye: 'left' })), 11)],
      ['unlabeled', producerPose(fixtureEyes().map(({ eye, ...view }) => view), 12)],
      ['singular-projection', producerPose(fixtureEyes().map(eye => ({ ...eye, projection: Array(16).fill(0) })), 13)],
      ['invalid-position', producerPose(fixtureEyes().map(eye => ({ ...eye, position: [null, 1.6, 0] })), 14)],
      ['disconnect', null],
    ];
    report.rejectedPairs = [];
    for (const [name, value] of invalid) {
      const result = await capture(value);
      assert.equal(publicationGate(result.meta), null, `${name} pair must never pass the actual stereo publication gate`);
      assert.deepEqual(result.after, result.before, `${name} pair rejection must not leave an eye viewport active`);
      report.rejectedPairs.push(name);
    }
    const recovered = await capture(producerPose(fixtureEyes(), 15));
    assertMetadata(recovered.meta, producerPose(fixtureEyes(), 15)); assertPixels(await readPixels(recovered.data), {});
    assert.deepEqual(recovered.after, recovered.before);
    report.recoveredPair = { passed: true, sessionId: recovered.meta.poseEcho.session_id, frameId: recovered.meta.poseEcho.frame_id };
    assert.equal(report.errors.length, 0, report.errors.join('\n'));
    assert.ok(!paths.some(path => path.startsWith('/ws/control')));
    report.passed = report.errors.length === 0;
  } catch (error) { report.errors.push(error.stack || error.message); }
  finally { await browser?.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
  await writeFile(resolve(output, 'evidence.json'), JSON.stringify(report, null, 2) + '\n');
  return report;
}
if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--run')) { const report = await run(); console.log(JSON.stringify(report, null, 2)); process.exitCode = report.passed ? 0 : 1; }
  else console.log(JSON.stringify({ preparedOnly: true, gpuRun: false, source: 'SIMULATED test markers only',
    expectations: expectedCentroids(), nearDisparity: expectedCentroids().left.near.x - expectedCentroids().right.near.x,
    farDisparity: expectedCentroids().left.far.x - expectedCentroids().right.far.x, acceptance: 'Pending renderer owner implementation SHA and Root GPU GO; not passed.' }, null, 2));
}
