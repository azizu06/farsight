// Production page + actual WebGL eye assignment. Optional real local Mac encoder.
// Synthetic/desktop software evidence, not physical Quest qualification.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from '@playwright/test';
const output = '/tmp/farsight-exploration';
await mkdir(output, { recursive: true });
const mac = process.argv.includes('--mac') ? process.argv[process.argv.indexOf('--mac') + 1] : null;
const origin = 'http://127.0.0.1:5279';
const server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '5279', '--strictPort'],
  { cwd: new URL('..', import.meta.url), stdio: 'pipe' });
let logs = '', browser;
server.stdout.on('data', data => logs += data); server.stderr.on('data', data => logs += data);
const report = { source: 'SYNTHETIC_BROWSER_FIXTURE', physical_headset: false, errors: [], sockets: [], requests: [] };
try {
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw Error(logs);
    if (await fetch(origin).then(r => r.ok, () => false)) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ channel: 'chrome', args: ['--use-gl=angle', '--use-angle=metal', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', error => report.errors.push(error.message));
  page.on('websocket', socket => { const path = new URL(socket.url()).pathname; if (path !== '/') report.sockets.push(path); });
  page.on('request', request => report.requests.push(new URL(request.url()).pathname));
  await page.routeWebSocket('**/ws/macview**', socket => socket.close());
  await page.goto(`${origin}/explore.html`);
  await page.waitForSelector('#enter-vr:disabled');
  await page.screenshot({ path: `${output}/desktop-waiting.png` });
  await page.locator('#help').click(); await page.waitForSelector('#help-dialog[open]');
  await page.screenshot({ path: `${output}/desktop-help.png` });
  await page.locator('.close').click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: `${output}/mobile-waiting.png` });
  const gpu = await page.evaluate(async () => {
    const THREE = await import('/node_modules/three/build/three.module.js');
    const { ExploreStream } = await import('/src/explore-stream.ts');
    const { mapWalk } = await import('/src/map-walk.js');
    const renderer = new THREE.WebGLRenderer({ antialias: false }); renderer.setSize(320, 240);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(60, 4 / 3, .01, 100);
    camera.position.set(0, 1.6, 0);
    const stream = new ExploreStream(renderer, scene, camera, location.origin);
    const picture = document.createElement('canvas'); picture.width = 640; picture.height = 240;
    const context = picture.getContext('2d');
    context.fillStyle = '#ff0000'; context.fillRect(0, 0, 320, 240);
    context.fillStyle = '#0000ff'; context.fillRect(320, 0, 320, 240);
    const media = picture.captureStream(30); stream.viewer.video.srcObject = media;
    await stream.viewer.video.play();
    await new Promise(resolve => stream.viewer.video.requestVideoFrameCallback(resolve));
    let echo, id = 0;
    stream.socket = { readyState: WebSocket.OPEN, bufferedAmount: 0, send: value => { echo = JSON.parse(value); } };
    stream.viewer.update = () => {};
    Object.defineProperty(stream.viewer, 'displayedFrame', { get: () => ({ id: String(++id), age: 0, pose: echo,
      metadata: { stereo: true, layout: 'sbs-left-right', eye_width: 320, eye_height: 240 } }) });
    mapWalk.reset(); stream.resetSession(true);
    const transform = x => ({ position: { x, y: 1.6, z: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } });
    const pose = { transform: transform(0), views: ['left', 'right'].map((eye, i) => ({ eye,
      transform: transform(i ? .032 : -.032), projectionMatrix: camera.projectionMatrix.toArray() })) };
    stream.update(performance.now(), pose);
    const target = new THREE.WebGLRenderTarget(320, 240), pixel = new Uint8Array(4);
    renderer.setRenderTarget(target);
    const colors = [1, 2].map(layer => { camera.layers.set(layer); renderer.render(scene, camera);
      renderer.readRenderTargetPixels(target, 160, 120, 1, 1, pixel); return [...pixel]; });
    const oldSession = echo.session_id;
    stream.resetSession(false); camera.layers.set(0); stream.update(performance.now() + 100);
    renderer.render(scene, camera); renderer.readRenderTargetPixels(target, 160, 120, 1, 1, pixel);
    const desktop = [...pixel], newSession = echo.session_id;
    const drawCalls = renderer.info.render.calls, drawnPoints = renderer.info.render.points;
    stream.socket = null; stream.dispose(); media.getTracks().forEach(track => track.stop()); target.dispose(); renderer.dispose();
    return { colors, desktop, oldSession, newSession, drawCalls, drawnPoints };
  });
  assert.ok(gpu.colors[0][0] > 200 && gpu.colors[0][2] < 10, 'left XR eye sees red left half');
  assert.ok(gpu.colors[1][2] > 200 && gpu.colors[1][0] < 10, 'right XR eye sees blue right half');
  assert.ok(gpu.desktop[0] > 200 && gpu.desktop[2] < 10, 'desktop previews only the left eye');
  assert.notEqual(gpu.oldSession, gpu.newSession); assert.equal(gpu.drawnPoints, 0); assert.equal(gpu.drawCalls, 1);
  report.gpu = gpu;
  assert.ok(report.sockets.every(path => ['/ws/macview', '/ws/macview-pose'].includes(path)));
  assert.ok(!report.requests.some(path => /^\/(dense|map|phone|camera|health)/.test(path)), 'viewer downloads no geometry/camera/control payloads');
  await page.close();
  if (mac) {
    const actual = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const poses = [], sockets = [], requests = [];
    actual.on('pageerror', error => report.errors.push(error.message));
    actual.on('request', request => requests.push(new URL(request.url()).pathname));
    actual.on('websocket', socket => { const path = new URL(socket.url()).pathname; if (path !== '/') sockets.push(path);
      if (socket.url().includes('macview-pose')) socket.on('framesent', event => poses.push(JSON.parse(event.payload.toString()))); });
    await actual.goto(`${origin}/explore.html?mac=${encodeURIComponent(mac)}`);
    await actual.waitForSelector('canvas[data-state="rendering"]', { timeout: 45000 });
    await actual.waitForTimeout(1000);
    const before = poses.at(-1);
    await actual.keyboard.down('KeyW'); await actual.waitForTimeout(700); await actual.keyboard.up('KeyW');
    await actual.waitForTimeout(150);
    const after = poses.at(-1);
    const distance = Math.hypot(...after.head.position.map((value, i) => value - before.head.position[i]));
    assert.ok(distance > .4, 'keyboard moves the actual Mac renderer camera');
    await actual.screenshot({ path: `${output}/local-mac-scene.png` });
    const source = await actual.locator('#source').textContent();
    assert.ok(/simulat|replay/i.test(source), 'local generated source remains labeled');
    assert.ok(sockets.every(path => ['/ws/macview', '/ws/macview-pose'].includes(path)));
    assert.ok(!requests.some(path => /^\/(dense|map|phone|camera|health)/.test(path)));
    report.local_mac = { source, virtual_distance_m: distance, poses: poses.length, sockets, geometry_requests: 0 };
    await actual.close();
  }
  assert.deepEqual(report.errors, []); report.passed = true;
} catch (error) { report.failure = String(error.stack || error); process.exitCode = 1; }
finally {
  await browser?.close(); server.kill('SIGTERM');
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
}
console.log(JSON.stringify(report));
