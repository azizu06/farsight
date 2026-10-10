// Actual Chromium production-front-end smoke, software only. No route interception,
// hardware, target access, fake capture freshness, control retries or rearming retries.
// node quest/tests/bonded-control.e2e.mjs --out /tmp/bonded-browser.json
import { spawn } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';

const root = fileURLToPath(new URL('../..', import.meta.url));
const python = process.env.FARSIGHT_PYTHON ?? (existsSync(`${root}.venv/bin/python`) ? `${root}.venv/bin/python` : 'python3');
const outputIndex = process.argv.indexOf('--out');
const output = outputIndex >= 0 ? process.argv[outputIndex + 1] : null;
if (outputIndex >= 0 && !output) throw Error('--out requires a file');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const children = [], phases = [], samples = [];
let browser, page, fatal = null, backendPort, primaryPort, secondaryPort, relayPort;
let primary, secondary, pageErrors = 0;
const began = Date.now();
let samplePhase = 'setup', sampling = false, samplingTask;

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer(); server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { const { port } = server.address(); server.close(() => resolve(port)); });
  });
}
function start(name, args) {
  const process = spawn(python, args, { cwd: root, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = ''; const entry = { name, process, log: () => log };
  for (const stream of [process.stdout, process.stderr]) stream.on('data', d => { log = (log + d).slice(-3000); });
  process.on('error', () => { entry.spawnFailed = true; });
  children.push(entry); return entry;
}
async function terminate(entry) {
  if (entry.process.exitCode !== null || entry.process.signalCode !== null) return;
  try { process.kill(-entry.process.pid, 'SIGTERM'); } catch {}
  const end = Date.now() + 3000;
  while (entry.process.exitCode === null && entry.process.signalCode === null && Date.now() < end) await sleep(30);
  if (entry.process.exitCode === null && entry.process.signalCode === null) {
    try { process.kill(-entry.process.pid, 'SIGKILL'); } catch {}
    await sleep(50);
  }
}
async function json(url) {
  const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(1000) });
  if (!response.ok) throw Error('HTTP request failed');
  return response.json();
}
async function waitFor(name, operation, ms = 8000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await operation()) return;
    await sleep(30);
  }
  throw Error(`${name}: deadline`);
}
async function readyProcess(url, entry, ms = 15000) {
  await waitFor(`${entry.name} readiness`, async () => {
    if (entry.spawnFailed || entry.process.exitCode !== null || entry.process.signalCode !== null) throw Error(`${entry.name} exited`);
    try { return (await fetch(url, { signal: AbortSignal.timeout(500) })).ok; } catch { return false; }
  }, ms);
}
const direct = () => `http://127.0.0.1:${backendPort}`;
const relay = () => `http://127.0.0.1:${relayPort}`;
const status = () => json(`${direct()}/control/status`);
const observations = () => page.evaluate(() => globalThis.__bondedSmoke.snapshot());
const reason = value => ['active', 'deadman_released', 'controller_disconnected', 'lease_expired', 'release_required', 'expired_permit', 'camera_stale', 'video_stale', 'scan_finished', 'wrong_session'].includes(value) ? value : 'other';
function proxy(name, port) {
  return start(name, ['-m', 'tools.fault_proxy', '--port', String(port), '--upstream-port', String(backendPort), '--delay-ms', '0', '--bandwidth-mbps', '0']);
}
async function proxyReady(entry) { await readyProcess(`http://127.0.0.1:${entry === primary ? primaryPort : secondaryPort}/health`, entry); }
async function positiveWindow(ms, phase) {
  const end = Date.now() + ms; let count = 0, maximumDrawAge = 0;
  samplePhase = phase;
  while (Date.now() < end) {
    const s = await status(), seen = await observations();
    assert(s.simulated === true && s.left_pwm > 0 && s.right_pwm > 0 && s.reason === 'active', `${phase}: motion stopped`);
    assert(seen.lastDraw && seen.lastDraw.source === 'SIMULATED' && seen.currentDrawAgeMs < 500, `${phase}: drawn capture stale`);
    assert(seen.drawLoopAgeMs < 250, `${phase}: display stale`);
    maximumDrawAge = Math.max(maximumDrawAge, seen.currentDrawAgeMs); count++;
    await sleep(40);
  }
  return { samples: count, maximumDrawAgeMs: Math.round(maximumDrawAge) };
}

try {
  const ports = new Set(); while (ports.size < 4) ports.add(await freePort());
  [backendPort, primaryPort, secondaryPort, relayPort] = [...ports];
  const backend = start('simulated backend', ['-m', 'jetson.server', '--demo', '--simulate-rover', '--detector', 'none', '--bonded-control', '--host', '127.0.0.1', '--port', String(backendPort)]);
  await readyProcess(`${direct()}/health`, backend);
  const h = await json(`${direct()}/health`);
  assert(h.source === 'SIMULATED' && h.control.simulated === true, 'Only explicitly simulated motors/source are permitted');
  primary = proxy('primary proxy', primaryPort); secondary = proxy('secondary proxy', secondaryPort);
  await Promise.all([proxyReady(primary), proxyReady(secondary)]);
  const operator = start('production operator relay', ['-m', 'tools.operator_relay', '--upstream', `http://127.0.0.1:${primaryPort}`, '--bonded-upstream', `http://127.0.0.1:${secondaryPort}`, '--port', String(relayPort)]);
  await readyProcess(`${relay()}/relay/health`, operator);
  browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  page = await browser.newPage({ viewport: { width: 960, height: 600 } });
  page.on('pageerror', () => { pageErrors++; });
  await page.addInitScript(() => {
    localStorage.setItem('farsight.viewer', JSON.stringify({ h264: true }));
    const state = { connections: 0, opens: 0, closes: 0, statuses: [], commands: [], draws: [], keys: [] };
    const sessions = new Map(), responses = new WeakMap(), blobs = new WeakMap(), urls = new Map();
    const ordinal = id => { if (typeof id !== 'string') return null; if (!sessions.has(id)) sessions.set(id, sessions.size + 1); return sessions.get(id); };
    const bounded = (list, value) => { list.push(value); if (list.length > 2000) list.shift(); };
    const category = value => ['active', 'deadman_released', 'controller_disconnected', 'lease_expired', 'release_required', 'expired_permit', 'camera_stale', 'video_stale', 'scan_finished', 'wrong_session'].includes(value) ? value : 'other';
    const Native = window.WebSocket;
    window.WebSocket = class extends Native {
      constructor(url, ...args) {
        super(url, ...args);
        if (new URL(String(url), location.href).pathname !== '/ws/control') return;
        state.connections++;
        this.addEventListener('open', () => { state.opens++; });
        this.addEventListener('close', () => { state.closes++; });
        this.addEventListener('message', event => {
          const p = JSON.parse(event.data);
          bounded(state.statuses, { at: performance.now(), owner: ordinal(p.session_id), status_seq: p.status_seq,
            seq: p.seq, reason: category(p.reason), requires_release: p.requires_release, left_pwm: p.left_pwm, right_pwm: p.right_pwm });
        });
      }
      send(data) {
        if (new URL(this.url).pathname === '/ws/control') {
          const p = JSON.parse(data);
          bounded(state.commands, { at: performance.now(), owner: ordinal(p.session_id), seq: p.seq,
            deadman: p.deadman, v: p.v, w: p.w, video_age_ms: p.video_age_ms });
        }
        return super.send(data);
      }
    };
    const nativeFetch = window.fetch.bind(window), nativeBlob = Response.prototype.blob;
    window.fetch = async (...args) => {
      const at = performance.now(), response = await nativeFetch(...args);
      if (String(args[0]).includes('/camera.jpg') && response.status === 200) {
        responses.set(response, { at, frame: Number(response.headers.get('X-Frame-Id')),
          source: response.headers.get('X-Frame-Source') === 'SIMULATED' ? 'SIMULATED' : 'OTHER',
          age: Number(response.headers.get('X-Frame-Age-Ms')) });
      }
      return response;
    };
    Response.prototype.blob = async function() {
      const body = await nativeBlob.call(this), metadata = responses.get(this);
      if (metadata) blobs.set(body, metadata); return body;
    };
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = blob => {
      const url = create(blob), metadata = blobs.get(blob); if (metadata) urls.set(url, metadata);
      if (urls.size > 64) urls.delete(urls.keys().next().value); return url;
    };
    const draw = CanvasRenderingContext2D.prototype.drawImage;
    CanvasRenderingContext2D.prototype.drawImage = function(...args) {
      const result = draw.apply(this, args);
      if (this.canvas.id === 'rover-camera') {
        const metadata = urls.get(args[0]?.src);
        if (metadata) bounded(state.draws, { at: performance.now(), frame: metadata.frame,
          source: metadata.source, receiptBoundMs: metadata.age + performance.now() - metadata.at });
      }
      return result;
    };
    for (const type of ['keydown', 'keyup']) window.addEventListener(type, e => {
      if (['Space', 'KeyW'].includes(e.code)) bounded(state.keys, { at: performance.now(), type, code: e.code });
    });
    globalThis.__bondedSmoke = {
      snapshot() {
        const now = performance.now(), lastDraw = state.draws.at(-1);
        return { ...state, now, ownerCount: sessions.size, lastDraw,
          currentDrawAgeMs: lastDraw ? lastDraw.receiptBoundMs + now - lastDraw.at : null,
          drawLoopAgeMs: lastDraw ? now - lastDraw.at : null };
      }
    };
  });
  await page.goto(`${relay()}/?h264=1`);
  await page.locator('#rover-message').waitFor({ timeout: 10000 });
  sampling = true;
  samplingTask = (async () => {
    while (sampling) {
      try {
        const s = await status(); samples.push({ elapsed_ms: Date.now() - began, phase: samplePhase,
          reason: reason(s.reason), seq: s.seq, requires_release: s.requires_release,
          left_pwm: s.left_pwm, right_pwm: s.right_pwm });
        if (samples.length > 1000) samples.shift();
      } catch { samples.push({ elapsed_ms: Date.now() - began, phase: samplePhase, error: 'status_request_failed' }); }
      await sleep(40);
    }
  })();
  await waitFor('ready with decoded/drawn JPEG', async () => {
    const seen = await observations();
    return /Ready: hold deadman/.test(await page.locator('#rover-message').textContent()) &&
      !(await status()).requires_release && seen.draws.length >= 3;
  });
  const settings = await page.evaluate(() => ({ saved: JSON.parse(localStorage.getItem('farsight.viewer')).h264,
    toggles: [...document.querySelectorAll('input[type=checkbox]')].map(e => ({ label: e.closest('label')?.textContent ?? '', disabled: e.disabled, checked: e.checked })) }));
  assert.equal(settings.saved, true, 'JPEG profile must not overwrite the saved H264 preference');
  const h264 = settings.toggles.find(e => e.label.includes('H.264 video'));
  assert(h264?.disabled && !h264.checked, 'Bonded profile did not force/lock JPEG');
  assert.equal(await page.locator('.source-banner').getAttribute('data-kind'), 'SIMULATED');
  phases.push({ name: 'profile', savedH264: settings.saved, effectiveJPEG: true });
  await page.keyboard.down('Space'); await page.keyboard.down('KeyW');
  await waitFor('initial simulated forward motion', async () => { const s = await status(); return s.left_pwm > 0 && s.right_pwm > 0 && s.reason === 'active'; }, 3000);
  const before = await observations();
  assert.equal(before.connections, 1, 'Initial Quest control connection unexpectedly restarted');
  assert.equal(before.ownerCount, 1);
  const warm = await positiveWindow(400, 'both-paths');
  phases.push({ name: 'both-paths', ...warm, controlConnections: before.connections, ownerOrdinal: 1 });
  const cutAt = (await observations()).now;
  await terminate(primary);
  await waitFor('one validated path', async () => (await json(`${relay()}/relay/health`)).transport.attached_paths === 1, 3000);
  const continued = await positiveWindow(1000, 'one-path');
  const after = await observations();
  assert.equal(after.connections, 1, 'First path loss restarted Quest WS');
  assert.equal(after.closes, 0, 'First path loss closed Quest WS');
  assert.equal(after.ownerCount, 1, 'First path loss changed logical owner');
  const heldStatuses = after.statuses.filter(s => s.at >= cutAt);
  assert(heldStatuses.length >= 10 && heldStatuses.every(s => s.owner === 1 && !s.requires_release && s.left_pwm > 0 && s.right_pwm > 0), 'First loss interrupted positive controller statuses or latched a stop');
  assert(after.draws.some(d => d.at > cutAt && d.source === 'SIMULATED' && d.receiptBoundMs < 500), 'No actual fresh JPEG draw after first loss');
  phases.push({ name: 'one-path', ...continued, positiveStatuses: heldStatuses.length, sameOwner: true, sameQuestWS: true });
  samplePhase = 'last-loss'; const lastLoss = Date.now();
  await terminate(secondary);
  await waitFor('zero output after last loss', async () => { const s = await status(); return s.left_pwm === 0 && s.right_pwm === 0; }, 2000);
  const stoppedMs = Date.now() - lastLoss;
  await waitFor('Quest WS closed after last loss', async () => (await observations()).closes >= 1, 3000);
  phases.push({ name: 'last-loss', observedZeroMs: stoppedMs, questClosed: true });
  primary = proxy('restored primary proxy', primaryPort); secondary = proxy('restored secondary proxy', secondaryPort);
  await Promise.all([proxyReady(primary), proxyReady(secondary)]);
  samplePhase = 'restored-held';
  await waitFor('replacement owner while keys remain held', async () => (await observations()).ownerCount >= 2, 8000);
  const restoredAt = (await observations()).now;
  await waitFor('fresh actual JPEG under replacement owner', async () => {
    const seen = await observations(); return seen.lastDraw?.at > restoredAt && seen.currentDrawAgeMs < 400;
  });
  const heldEnd = Date.now() + 600; let zeroSamples = 0;
  while (Date.now() < heldEnd) {
    const s = await status(); assert.equal(s.left_pwm, 0, 'Held key resumed left output after reconnect'); assert.equal(s.right_pwm, 0, 'Held key resumed right output after reconnect');
    const seen = await observations(); assert(!seen.commands.some(c => c.at >= restoredAt && c.deadman), 'New owner received held-motion command without physical release');
    zeroSamples++; await sleep(40);
  }
  const stillHeld = await observations();
  assert.equal(stillHeld.keys.filter(k => k.type === 'keyup').length, 0, 'Test accidentally released the physical keys before safety check');
  phases.push({ name: 'restored-held', zeroSamples, newOwner: true, activeCommands: 0, physicalKeyReleaseEvents: 0 });
  await page.keyboard.up('KeyW'); await page.keyboard.up('Space');
  samplePhase = 'released';
  await waitFor('release permits rearm', async () => /Ready: hold deadman/.test(await page.locator('#rover-message').textContent()) && !(await status()).requires_release);
  await page.keyboard.down('Space'); await page.keyboard.down('KeyW');
  await waitFor('fresh press restores simulated forward motion', async () => { const s = await status(); return s.left_pwm > 0 && s.right_pwm > 0; }, 3000);
  phases.push({ name: 'fresh-press', positivePWM: true });
  await page.keyboard.up('KeyW'); await page.keyboard.up('Space');
  await waitFor('final release stops', async () => { const s = await status(); return s.left_pwm === 0 && s.right_pwm === 0; }, 2000);
  assert.equal(pageErrors, 0, 'Unexpected uncaught page errors');
  assert.equal(await page.locator('.source-banner').getAttribute('data-kind'), 'SIMULATED');
} catch (error) {
  fatal = error.message;
  process.exitCode = 1;
} finally {
  sampling = false; if (samplingTask) await samplingTask;
  let browserEvidence = null;
  if (page) { browserEvidence = await observations().catch(() => null); await page.keyboard.up('KeyW').catch(() => {}); await page.keyboard.up('Space').catch(() => {}); }
  if (browser) await browser.close();
  await Promise.all(children.map(terminate));
  const report = { scope: 'Software-only Chromium/SwiftShader; canonical simulated backend and production bonded relay. Local TCP proxies add zero latency and no bandwidth cap. No phone, Quest, cellular, physical camera, target access or real motors.',
    passed: !fatal, elapsed_ms: Date.now() - began, fatal, pageErrorCount: pageErrors, phases, samples, browser: browserEvidence };
  if (output) writeFileSync(output, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: report.passed, fatal, phases, elapsed_ms: report.elapsed_ms }, null, 2));
}
