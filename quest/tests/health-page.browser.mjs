import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chromium } from '@playwright/test';
// The real driving page (/) and the splat page under a live-looking /health: every health poll's
// handlers must run without throwing, and the rover's H.264 offer must be auto-selected.
const origin = 'http://127.0.0.1:5259';
const health = {
  rover: true, camera: true, camera_source: 'IPHONE LIDAR', camera_age_ms: 40, camera_drive_ready: true,
  ready: true, enabled: true, source: 'IPHONE LIDAR', connected: true, fresh: true,
  video: { enabled: true, ready: true, error: null, fps: 10, bitrate: 500000 },
  assistant: { whisper: true, vlm: true }, control: { simulated: false, mode: 'teleop', reason: 'deadman_released' },
};
const server = spawn(process.execPath, ['--input-type=module', '-e',
  "import {createServer} from 'vite';const s=await createServer({server:{host:'127.0.0.1',port:5259,strictPort:true,watch:null}});await s.listen();"],
  { cwd: new URL('..', import.meta.url), stdio: 'pipe' });
let log = '', browser;
server.stdout.on('data', v => log += v); server.stderr.on('data', v => log += v);
try {
  let ready = false;
  for (let i = 0; i < 100; i++) { if (server.exitCode !== null) throw Error(log); try { ready = (await fetch(origin)).ok; } catch {} if (ready) break; await new Promise(r => setTimeout(r, 100)); }
  assert.ok(ready, log);
  browser = await chromium.launch({ args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  for (const path of ['/', '/?demo=0&voice=1', '/splat.html']) {
    const page = await browser.newPage({ viewport: { width: 960, height: 640 } });
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.addInitScript(() => {
      localStorage.setItem('farsight.viewer', JSON.stringify({ voice: true }));
      window.voicePermissionRequests = 0;
      const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = constraints => {
        window.voicePermissionRequests++;
        return capture(constraints);
      };
    });
    await page.routeWebSocket('**/ws/**', socket => socket.close());
    await page.route('**/camera.jpg', r => r.fulfill({ status: 503, body: '' }));
    await page.route('**/health', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify(health) }));
    await page.goto(`${origin}${path}`);
    if (path !== '/splat.html') {
      // Auto-selection must happen after the voice check. Without frames it later falls back to
      // JPEG (6 s), so wait for the selection itself rather than sampling at a fixed time.
      const selected = await page.waitForFunction(() => [...document.querySelectorAll('#settings label.check')]
        .find(label => label.textContent.includes('H.264'))?.querySelector('input')?.checked === true,
        null, { timeout: 15000 }).then(() => true, () => false);
      assert.equal(selected, true, `${path}: the rover's H.264 offer is auto-selected after the voice check`);
    }
    // Health is polled every 2 s; cover a repeat poll after the first answer.
    await page.waitForTimeout(2500);
    assert.deepEqual(errors, [], `${path}: no page errors with a healthy /health`);
    if (path !== '/splat.html') {
      assert.equal(await page.locator('#voice-vr-enable').evaluate(button => button.hidden), false,
        `${path}: the left-trigger assistant control is available with rover Whisper`);
      assert.equal(await page.locator('#voice-record').evaluate(button => button.hidden), false,
        `${path}: desktop voice questions remain available`);
      assert.equal(await page.evaluate(() => window.voicePermissionRequests), 0,
        `${path}: arming saved voice or voice=1 waits for a deliberate held trigger to request the microphone`);
    }
    await page.close();
  }
  console.log('health-page browser test passed');
} finally {
  await browser?.close();
  server.kill();
}
