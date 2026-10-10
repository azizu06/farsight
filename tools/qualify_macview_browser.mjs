#!/usr/bin/env node
/** Bounded desktop observation of the actual standalone Mac viewer.
 * node tools/qualify_macview_browser.mjs --url 'http://localhost:4173/macview.html?mac=http://localhost:8090' --out /tmp/macview-evidence
 * Use a production build/preview; unrelated Vite dev/HMR sockets are refused.
 * Does not enter XR or synthesize controller input. The page sends its usual
 * desktop visual pose. Video presentation cadence is NOT XR FPS/photon latency.
 */
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

export function standaloneURL(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/macview.html')
    throw new Error('Only a credential-free HTTP(S) /macview.html URL is allowed');
  const endpoint = url.searchParams.get('mac');
  if (endpoint) {
    const origin = new URL(endpoint);
    if (!['http:', 'https:'].includes(origin.protocol) || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash)
      throw new Error('mac must be a credential-free HTTP(S) origin');
  }
  return url;
}
export function options(argv) {
  const result = { url: 'http://127.0.0.1:4173/macview.html', out: '/tmp/farsight-macview-browser', seconds: 8, warmup: 15, minFps: 1, headed: false, graphics: 'swiftshader', nativeSockets: false, diagnosticNoReseek: false };
  for (let index = 0; index < argv.length; index++) {
    const name = argv[index];
    if (name === '--help') return { help: true };
    if (name === '--headed') { result.headed = true; continue; }
    if (name === '--native-sockets') { result.nativeSockets = true; continue; }
    if (name === '--diagnostic-no-reseek') { result.diagnosticNoReseek = true; continue; }
    const key = { '--url': 'url', '--out': 'out', '--seconds': 'seconds', '--warmup': 'warmup', '--min-fps': 'minFps', '--graphics': 'graphics' }[name];
    if (!key || !argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`Unknown option or missing value: ${name}`);
    result[key] = ['seconds', 'warmup', 'minFps'].includes(key) ? Number(argv[++index]) : argv[++index];
  }
  standaloneURL(result.url);
  if (!['swiftshader', 'metal'].includes(result.graphics)) throw new Error('graphics must be swiftshader or metal');
  for (const [key, low, high] of [['seconds', 1, 60], ['warmup', 1, 60], ['minFps', 0, 120]])
    if (!Number.isFinite(result[key]) || result[key] < low || result[key] > high) throw new Error(`${key} must be ${low}..${high}`);
  result.out = resolve(result.out);
  return result;
}
export function summary(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const at = quantile => sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(quantile * sorted.length))] : null;
  return { count: sorted.length, p50: at(.5), p95: at(.95), maximum: sorted.at(-1) ?? null };
}
export function failures(report, minFps) {
  const errors = [...report.errors];
  const video = report.video;
  if (!video || video.width <= 0 || video.height <= 0) errors.push('No decoded video dimensions');
  if (!video || video.callbacks < 3 || video.callbackHz < minFps) errors.push('Insufficient advancing video presentation callbacks');
  if (!video || !(video.mediaTimeAdvanceSeconds > 0)) errors.push('Video media time did not advance');
  if (!report.stream.init || report.stream.init.version !== 1 || report.stream.init.stereo !== false) errors.push('Missing explicit flat/non-stereo v1 initialization');
  if (!report.stream.segments) errors.push('No stream segment metadata observed');
  if (!report.stream.sources.length || report.stream.sources.some(source => !source || /unreported/i.test(source))) errors.push('Missing stream source provenance');
  if (report.stream.stereoClaims.some(claim => claim !== false)) errors.push('Unsupported or missing stereo declaration');
  if (video && report.stream.init && (video.width !== report.stream.init.width || video.height !== report.stream.init.height)) errors.push('Decoded dimensions differ from stream declaration');
  return [...new Set(errors)];
}

// Only observation callbacks are installed. No XR/session/input APIs are used.
function videoProbe() {
  const probe = window.__farsightMacviewQualification = { video: null, callbacks: [], supported: false, seeking: [], seeked: [], buffers: [] };
  const seen = new WeakSet();
  const observe = () => {
    for (const video of document.querySelectorAll('video')) {
      if (seen.has(video)) continue;
      seen.add(video); probe.video = video;
      for (const kind of ['seeking', 'seeked']) video.addEventListener(kind, () => {
        if (probe[kind].length < 10000) probe[kind].push({ now: performance.now(), currentTime: video.currentTime });
      });
      if (!video.requestVideoFrameCallback) continue;
      probe.supported = true;
      const frame = (now, metadata) => {
        if (probe.callbacks.length < 10000) probe.callbacks.push({ now, mediaTime: metadata.mediaTime, presentedFrames: metadata.presentedFrames, processingDuration: metadata.processingDuration ?? null });
        if (video.isConnected) video.requestVideoFrameCallback(frame);
      };
      video.requestVideoFrameCallback(frame);
    }
  };
  new MutationObserver(observe).observe(document, { childList: true, subtree: true });
  observe();
  setInterval(() => {
    const video = probe.video; if (!video || probe.buffers.length >= 10000) return;
    const ranges = video.buffered;
    probe.buffers.push({ now: performance.now(), currentTime: video.currentTime, seeking: video.seeking,
      bufferAhead: ranges.length ? Math.max(0, ranges.end(ranges.length - 1) - video.currentTime) : null });
  }, 50);
}
function videoSnapshot(reset = false) {
  const probe = window.__farsightMacviewQualification, video = probe?.video;
  const quality = video?.getVideoPlaybackQuality?.();
  const result = { width: video?.videoWidth ?? 0, height: video?.videoHeight ?? 0, currentTime: video?.currentTime ?? null,
    paused: video?.paused ?? null, readyState: video?.readyState ?? null, error: video?.error?.message ?? null,
    callbackSupported: probe?.supported ?? false, samples: probe?.callbacks ?? [], now: performance.now(),
    seeking: video?.seeking ?? null, seekingEvents: probe?.seeking ?? [], seekedEvents: probe?.seeked ?? [], bufferSamples: probe?.buffers ?? [],
    quality: quality ? { totalVideoFrames: quality.totalVideoFrames, droppedVideoFrames: quality.droppedVideoFrames } : null,
    status: document.querySelector('.mac-age')?.textContent ?? '', title: document.title };
  if (reset && probe) { probe.callbacks = []; probe.seeking = []; probe.seeked = []; probe.buffers = []; }
  return result;
}

export async function qualify(config) {
  const url = standaloneURL(config.url);
  const streamOrigin = new URL(url.searchParams.get('mac') || url.origin).origin.replace(/^http/, 'ws');
  const allowedSockets = new Set(['/ws/macview', '/ws/macview-pose'].map(path => streamOrigin + path));
  const report = { schema: 1, startedAt: new Date().toISOString(), evidenceClass: 'desktop-browser-video', url: url.href,
    limitations: ['Desktop video presentation callbacks are not XR FPS or pose-to-photon latency.', 'No headset, controller, stereo, room fidelity, or million-point hardware qualification.', config.nativeSockets ? 'WebSocket traffic stays browser-native; CDP message observations still add diagnostic overhead.' : 'WebSocket observation forwards traffic through Playwright; cadence includes this instrumentation.'],
    conditions: { seconds: config.seconds, warmupSeconds: config.warmup, headless: !config.headed, minCallbackHz: config.minFps, socketMode: config.nativeSockets ? 'native-constructor-guard' : 'playwright-forwarded', seekIntervention: config.diagnosticNoReseek ? 'Suppress currentTime writes after first real decoded frame; diagnostic experiment, not shipped behavior.' : 'none' },
    errors: [], consoleErrors: [], requestsFailed: [], pose: { sent: 0, untracked: 0, matchedEchoes: 0, lastSent: null, lastEcho: null },
    stream: { urls: [], init: null, segments: 0, sources: [], stereoClaims: [], binaryBytes: 0, lastSegment: null } };
  const sentPoses = new Map();
  const sidecarGaps = []; let lastSidecarAt = null;
  const timingSamples = Object.fromEntries(['age_ms', 'pose_to_frame_ms', 'render_ms', 'capture_ms', 'encode_ms'].map(key => [key, []]));
  // This is the checker checkout, never proof of the remotely served page revision.
  try { report.checkerCheckoutHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: fileURLToPath(new URL('..', import.meta.url)), encoding: 'utf8' }).trim(); } catch {}
  let browser, page, watchdog;
  await mkdir(config.out, { recursive: true });
  try {
    const { chromium } = await import('../quest/node_modules/playwright/index.mjs');
    browser = await chromium.launch({ headless: !config.headed, timeout: 10000,
      args: ['--autoplay-policy=no-user-gesture-required', ...(config.graphics === 'metal'
        ? ['--use-angle=metal', '--enable-gpu', '--ignore-gpu-blocklist']
        : ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])] });
    const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, serviceWorkers: 'block' });
    // Navigation response inspection via route.fulfill loses Chromium's private
    // address classification. Grant this disposable context's page origin the
    // local-network permission needed to observe its explicitly allowed stream.
    await context.grantPermissions(['local-network-access'], { origin: url.origin });
    report.conditions.browserVersion = browser.version(); report.conditions.graphics = config.graphics === 'metal' ? 'ANGLE Metal (GPU requested)' : 'ANGLE SwiftShader (software GPU)';
    report.conditions.localNetworkPermissionOrigin = url.origin;
    page = await context.newPage(); page.setDefaultTimeout(5000);
    // Abort navigation redirects to other pages and every HTTP control request.
    await context.route('**/*', async route => {
      const request = route.request(), target = new URL(request.url());
      if (target.pathname.startsWith('/ws/control') || (request.isNavigationRequest() && (target.pathname !== '/macview.html' || target.origin !== url.origin))) {
        report.errors.push(`Blocked out-of-scope request: ${target.origin}${target.pathname}`); return route.abort();
      }
      // Playwright route handlers only see a redirect chain's first request.
      // Fetch navigation without following redirects, then refuse every 3xx.
      if (request.isNavigationRequest()) {
        try {
          const response = await route.fetch({ maxRedirects: 0, timeout: 10000 });
          if (response.status() >= 300 && response.status() < 400) {
            report.errors.push('Blocked out-of-scope navigation redirect'); return route.abort();
          }
          return route.fulfill({ response });
        } catch (error) { report.errors.push(`Navigation fetch: ${error.message}`); return route.abort(); }
      }
      return route.continue();
    });
    const notePose = message => {
      if (typeof message !== 'string' || message.length > 65536) return;
      try {
        const pose = JSON.parse(message);
        if (pose.type === 'pose') {
          report.pose.sent++; report.pose.lastSent = pose;
          sentPoses.set(`${pose.session_id}:${pose.frame_id ?? pose.pose_id}:${pose.client_nonce}`, true);
          if (sentPoses.size > 256) sentPoses.delete(sentPoses.keys().next().value);
        } else if (pose.type === 'pose_untracked') report.pose.untracked++;
      } catch { report.errors.push('Malformed visual pose JSON'); }
    };
    const noteStream = message => {
      if (typeof message !== 'string') { report.stream.binaryBytes += message.length; return; }
      if (message.length > 65536) { report.errors.push('Stream metadata exceeds 64 KiB'); return; }
      try {
        const packet = JSON.parse(message);
        if (['video_init', 'video_segment'].includes(packet.type)) {
          const source = typeof packet.source === 'string' ? packet.source.slice(0, 200) : '';
          if (!report.stream.sources.includes(source) && report.stream.sources.length < 20) report.stream.sources.push(source);
          const stereo = packet.stereo ?? null;
          if (!report.stream.stereoClaims.includes(stereo)) report.stream.stereoClaims.push(stereo);
          const snapshot = Object.fromEntries(['type', 'version', 'mime', 'width', 'height', 'fps', 'stereo', 'source', 'frame_id', 'age_ms', 'media_start_s', 'media_end_s', 'map', 'pose', 'fov_y_deg', 'aspect', 'encoder', 'pose_to_frame_ms', 'render_ms', 'capture_ms', 'encode_ms', 'timing_scope'].map(key => [key, packet[key] ?? null]));
          if (packet.type === 'video_init') report.stream.init = snapshot;
          else {
            report.stream.segments++; report.stream.lastSegment = snapshot;
            const arrived = performance.now();
            if (lastSidecarAt !== null && sidecarGaps.length < 10000) sidecarGaps.push(arrived - lastSidecarAt);
            lastSidecarAt = arrived;
            for (const [key, samples] of Object.entries(timingSamples))
              if (samples.length < 10000 && Number.isFinite(packet[key]) && packet[key] >= 0) samples.push(packet[key]);
            if (packet.pose) {
              report.pose.lastEcho = packet.pose;
              if (sentPoses.has(`${packet.pose.session_id}:${packet.pose.frame_id ?? packet.pose.pose_id}:${packet.pose.client_nonce}`)) report.pose.matchedEchoes++;
            }
          }
        } else if (/_error$/.test(packet.type)) report.errors.push(`Stream error: ${JSON.stringify(packet).slice(0, 300)}`);
      } catch { report.errors.push('Malformed stream JSON'); }
    };
    const noteSocket = target => {
      if (!report.stream.urls.includes(target)) report.stream.urls.push(target);
    };
    if (config.nativeSockets) {
      // This constructor runs before page modules and rejects out-of-scope URLs
      // BEFORE invoking the native constructor. There is no traffic forwarding.
      await page.addInitScript(allowed => {
        window.__farsightNativeSocketBlocked = [];
        const Native = window.WebSocket;
        window.WebSocket = class extends Native {
          constructor(value, protocols) {
            const target = new URL(String(value), location.href).href;
            if (!allowed.includes(target)) {
              window.__farsightNativeSocketBlocked.push(`Blocked out-of-scope WebSocket: ${target}`);
              throw new DOMException('Out-of-scope qualification WebSocket', 'SecurityError');
            }
            super(value, protocols);
          }
        };
      }, [...allowedSockets]);
      page.on('websocket', socket => {
        const target = socket.url(); noteSocket(target);
        socket.on('framesent', frame => { if (target.endsWith('/ws/macview-pose')) notePose(frame.payload); });
        socket.on('framereceived', frame => { if (target.endsWith('/ws/macview')) noteStream(frame.payload); });
      });
    } else await page.routeWebSocket('**/*', socket => {
      const target = socket.url();
      if (!allowedSockets.has(target)) { report.errors.push(`Blocked out-of-scope WebSocket: ${target}`); socket.close(); return; }
      noteSocket(target); const server = socket.connectToServer();
      if (target.endsWith('/ws/macview-pose')) socket.onMessage(message => { notePose(message); server.send(message); });
      server.onMessage(message => { if (target.endsWith('/ws/macview')) noteStream(message); socket.send(message); });
    });
    page.on('pageerror', error => { if (report.errors.length < 50) report.errors.push(error.message.slice(0, 500)); });
    page.on('console', message => { if (message.type() === 'error' && report.consoleErrors.length < 50) report.consoleErrors.push(message.text().slice(0, 500)); });
    page.on('requestfailed', request => { if (report.requestsFailed.length < 50) report.requestsFailed.push({ url: request.url(), reason: request.failure()?.errorText }); });
    await page.addInitScript(videoProbe);
    const run = async () => {
      await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 10000 });
      await page.waitForFunction(() => window.__farsightMacviewQualification?.callbacks.length >= 1, undefined, { timeout: config.warmup * 1000 });
      if (config.diagnosticNoReseek) {
        report.limitations.push('Diagnostic intervention suppressed video currentTime writes; this result does not qualify unchanged product behavior.');
        await page.evaluate(() => {
          const probe = window.__farsightMacviewQualification, video = probe.video;
          const descriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime');
          probe.blockedSeekAttempts = 0;
          Object.defineProperty(video, 'currentTime', { configurable: true, get() { return descriptor.get.call(this); },
            set(_value) { probe.blockedSeekAttempts++; } });
        });
      }
      const before = await page.evaluate(videoSnapshot, true);
      await page.waitForTimeout(config.seconds * 1000);
      const after = await page.evaluate(videoSnapshot, false);
      report.conditions.blockedSeekAttempts = await page.evaluate(() => window.__farsightMacviewQualification?.blockedSeekAttempts ?? 0);
      report.conditions.webglRenderer = await page.evaluate(() => {
        const gl = document.querySelector('canvas')?.getContext('webgl2');
        const ext = gl?.getExtension('WEBGL_debug_renderer_info');
        return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'unavailable';
      });
      const elapsed = (after.now - before.now) / 1000;
      const intervals = after.samples.slice(1).map((sample, index) => sample.now - after.samples[index].now);
      report.video = { ...after, samples: undefined, bufferSamples: undefined, callbacks: after.samples.length, measurementSeconds: elapsed,
        callbackHz: after.samples.length / elapsed, intervalMs: summary(intervals),
        seekingCount: after.seekingEvents.length, seekedCount: after.seekedEvents.length, bufferAheadSeconds: summary(after.bufferSamples.filter(sample => sample.bufferAhead !== null).map(sample => sample.bufferAhead)),
        bufferSeekingSamples: after.bufferSamples.filter(sample => sample.seeking).length, bufferSampleCount: after.bufferSamples.length,
        processingDurationMs: summary(after.samples.filter(sample => sample.processingDuration !== null).map(sample => sample.processingDuration * 1000)),
        mediaTimeAdvanceSeconds: after.currentTime - before.currentTime,
        browserFrameDelta: before.quality && after.quality ? { total: after.quality.totalVideoFrames - before.quality.totalVideoFrames, dropped: after.quality.droppedVideoFrames - before.quality.droppedVideoFrames } : null };
    };
    await Promise.race([run(), new Promise((_, reject) => { watchdog = setTimeout(() => reject(new Error('Bounded qualification deadline exceeded')), (config.warmup + config.seconds + 15) * 1000); })]);
  } catch (error) { report.errors.push(error.message); }
  finally {
    clearTimeout(watchdog);
    if (page && !page.isClosed()) {
      try { report.finalPage = await page.evaluate(videoSnapshot, false); delete report.finalPage.samples; delete report.finalPage.bufferSamples;
        if (config.nativeSockets) report.errors.push(...await page.evaluate(() => window.__farsightNativeSocketBlocked ?? [])); } catch {}
      try { await page.screenshot({ path: resolve(config.out, 'screenshot.png'), timeout: 5000 }); report.screenshot = resolve(config.out, 'screenshot.png'); } catch (error) { report.errors.push(`Screenshot failed: ${error.message}`); }
    }
    if (browser) await browser.close();
  }
  report.errors.push(...report.consoleErrors.map(error => `Browser console: ${error}`));
  report.failures = failures(report, config.minFps); report.passed = report.failures.length === 0;
  report.decodeProof = { passed: report.passed, scope: 'Desktop decoded video sanity only; minimum callback Hz is not a headset performance target.' };
  report.performanceTarget = { evaluated: false, reason: 'No real-headset frame budget, stereo, or pose-to-photon target measured.' };
  report.stream.sidecarArrivalGapMs = summary(sidecarGaps);
  report.stream.sidecarTimingMs = Object.fromEntries(Object.entries(timingSamples).map(([key, values]) => [key, summary(values)]));
  report.stream.timingScope = 'Server-reported fragment sidecars across warmup, sampling and screenshot; not optical/headset latency.';
  report.finishedAt = new Date().toISOString();
  await writeFile(resolve(config.out, 'evidence.json'), JSON.stringify(report, null, 2) + '\n');
  return report;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const config = options(process.argv.slice(2));
    if (config.help) console.log('Usage: node tools/qualify_macview_browser.mjs [--url http://HOST/macview.html?mac=http://MAC:8090] [--out DIRECTORY] [--seconds 1..60] [--warmup 1..60] [--min-fps 0..120] [--graphics swiftshader|metal] [--native-sockets] [--diagnostic-no-reseek] [--headed]\nDesktop evidence only. Never enters XR or sends controller input; the normal page sends visual poses. Diagnostic-no-reseek changes only the disposable observation video and does not qualify shipped behavior.');
    else { const report = await qualify(config); console.log(JSON.stringify({ passed: report.passed, decodeProof: report.decodeProof, performanceTarget: report.performanceTarget, evidence: resolve(config.out, 'evidence.json'), screenshot: report.screenshot, video: report.video, failures: report.failures }, null, 2)); process.exitCode = report.passed ? 0 : 1; }
  } catch (error) { console.error(error.message); process.exitCode = 2; }
}
