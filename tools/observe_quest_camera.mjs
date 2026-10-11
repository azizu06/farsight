#!/usr/bin/env node
/** Read existing page diagnostics and received sidecars. Never creates a viewer. */
import { writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const MAX_MESSAGE = 64 * 1024;
const MAX_SIDECARS = 3600;
const MAX_EVENT_BYTES = 4 * 1024 * 1024;
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);
const ALLOWED_METHODS = new Set(['Network.enable', 'Network.disable', 'Runtime.evaluate']);
const NUMBERS = ['version', 'frame', 'frame_id', 'age_ms', 'server_wait_ms',
  'relay_request_ms', 'relay_network_bound_ms', 'relay_residence_ms',
  'media_start_s', 'media_end_s', 'capture_wall_ms', 'capture_mono_ms',
  'encode_ms', 'encode_pipeline_ms', 'feed_wait_ms', 'render_ms', 'capture_ms', 'capture_eval_ms',
  'jetson_encode_ms', 'camera_ingest_ms'];

// Constant, read-only DOM expression: no globals from the application, inputs,
// XR calls, video playback commands, storage, navigation, fetch or WebSockets.
export const SNAPSHOT_EXPRESSION = `(() => {
  const read = (selector, limit) => {
    const element = document.querySelector(selector);
    return element ? { text: (element.textContent || '').slice(0, limit), hidden: !!element.hidden } : null;
  };
  return {
    appOrigin: location.origin, path: location.pathname,
    diagnostics: read('pre.diagnostics', 4096),
    diagnosticsCount: document.querySelectorAll('pre.diagnostics').length,
    status: read('#rover-message', 1024),
    detail: read('#rover-details', 1024),
    source: read('.source-banner', 1024),
    userAgent: navigator.userAgent.slice(0, 512),
    visibility: document.visibilityState,
    documentHidden: !!document.hidden
  };
})()`;

function fault(code) { const error = new Error(code); error.code = code; return error; }
function safeError(error) { return typeof error?.code === 'string' ? error.code : 'observation_failed'; }
function effectivePort(url) { return url.port || (['https:', 'wss:'].includes(url.protocol) ? '443' : '80'); }
function boundedString(value, limit) { return typeof value === 'string' ? value.slice(0, limit) : null; }
function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

export function loopbackOrigin(value) {
  let url;
  try { url = new URL(value); } catch { throw fault('invalid_origin'); }
  if (!['http:', 'https:'].includes(url.protocol) || !LOOPBACK.has(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw fault('invalid_loopback_origin');
  return url.origin;
}

export function debuggerSocket(value, debugOrigin) {
  let url;
  try { url = new URL(value); } catch { throw fault('invalid_debugger_socket'); }
  const origin = new URL(debugOrigin);
  if (url.protocol !== (origin.protocol === 'https:' ? 'wss:' : 'ws:') ||
      !LOOPBACK.has(url.hostname) || effectivePort(url) !== effectivePort(origin) ||
      url.username || url.password || url.search || url.hash ||
      !/^\/devtools\/page\/[^/]+$/.test(url.pathname)) throw fault('invalid_debugger_socket');
  return url.href;
}

export async function discoverTarget(debugOrigin, appOrigin, { fetchImpl = fetch, signal } = {}) {
  debugOrigin = loopbackOrigin(debugOrigin); appOrigin = loopbackOrigin(appOrigin);
  const deadline = AbortSignal.timeout(3000);
  const response = await fetchImpl(new URL('/json/list', debugOrigin), {
    redirect: 'manual', signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
  });
  if (response.status !== 200) { await response.body?.cancel(); throw fault('debugger_discovery_http_error'); }
  const reader = response.body?.getReader();
  if (!reader) throw fault('debugger_discovery_empty');
  const chunks = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_MESSAGE) throw fault('debugger_discovery_oversized');
      chunks.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  let targets;
  try { targets = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw fault('debugger_discovery_invalid_json'); }
  if (!Array.isArray(targets)) throw fault('debugger_discovery_invalid_targets');
  const matches = targets.filter(target => {
    if (target?.type !== 'page' || typeof target.url !== 'string') return false;
    try {
      const url = new URL(target.url);
      return !url.username && !url.password && url.origin === appOrigin && url.pathname === '/';
    } catch { return false; }
  });
  if (matches.length !== 1) throw fault(matches.length ? 'multiple_matching_pages' : 'matching_page_missing');
  return { socket: debuggerSocket(matches[0].webSocketDebuggerUrl, debugOrigin), appOrigin };
}

export function sanitizeSidecar(packet) {
  if (!object(packet) || packet.type !== 'video_segment') return null;
  const result = { type: 'video_segment' };
  for (const key of NUMBERS) result[key] = typeof packet[key] === 'number' && Number.isFinite(packet[key]) ? packet[key] : null;
  result.macview = packet.macview === 'macview_segment' ? 'macview_segment' : null;
  result.source = typeof packet.source === 'string' && /^[\w .-]{1,120}$/.test(packet.source) ? packet.source : null;
  result.stereo = typeof packet.stereo === 'boolean' ? packet.stereo : null;
  // Never copy pose/session/nonce/request-id/permit or arbitrary extra fields.
  result.samples = Array.isArray(packet.samples) ? packet.samples.slice(0, 64).map(sample => {
    const item = {};
    for (const key of ['frame', 'frame_id', 'media_start_s', 'media_end_s', 'age_ms', 'capture_mono_ms', 'capture_wall_ms']) {
      item[key] = typeof sample?.[key] === 'number' && Number.isFinite(sample[key]) ? sample[key] : null;
    }
    return item;
  }) : null;
  return result;
}

function sanitizeSnapshot(value) {
  if (!object(value)) throw fault('snapshot_invalid');
  const dom = key => object(value[key]) ? { text: boundedString(value[key].text, key === 'diagnostics' ? 4096 : 1024),
    hidden: typeof value[key].hidden === 'boolean' ? value[key].hidden : null } : null;
  return { appOrigin: boundedString(value.appOrigin, 200), path: boundedString(value.path, 200),
    diagnostics: dom('diagnostics'), diagnosticsCount: Number.isSafeInteger(value.diagnosticsCount) ? value.diagnosticsCount : null,
    status: dom('status'), detail: dom('detail'), source: dom('source'),
    userAgent: boundedString(value.userAgent, 512), visibility: boundedString(value.visibility, 20),
    documentHidden: typeof value.documentHidden === 'boolean' ? value.documentHidden : null };
}

export class CdpObserver {
  constructor(url, { WebSocketImpl = WebSocket, signal, onEvent, onDrop } = {}) {
    this.url = url; this.WebSocketImpl = WebSocketImpl; this.signal = signal;
    this.onEvent = onEvent; this.onDrop = onDrop; this.pending = new Map(); this.id = 0;
    this.socket = null; this.connected = false; this.closed = false;
  }
  async open() {
    if (this.signal?.aborted) throw fault('observation_deadline');
    const socket = this.socket = new this.WebSocketImpl(this.url);
    socket.addEventListener('message', event => this.receive(event.data));
    socket.addEventListener('close', () => { this.closed = true; this.failPending('debugger_closed'); });
    socket.addEventListener('error', () => this.failPending('debugger_socket_error'));
    this.abort = () => { this.failPending('observation_deadline'); this.close(); };
    this.signal?.addEventListener('abort', this.abort, { once: true });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(fault('debugger_open_timeout')), 3000);
      const finish = error => {
        clearTimeout(timer); socket.removeEventListener('open', opened); socket.removeEventListener('error', failed);
        socket.removeEventListener('close', ended); this.signal?.removeEventListener('abort', aborted);
        if (error) reject(error); else { this.connected = true; resolve(); }
      };
      const opened = () => finish(); const failed = () => finish(fault('debugger_socket_error'));
      const ended = () => finish(fault('debugger_closed')); const aborted = () => finish(fault('observation_deadline'));
      socket.addEventListener('open', opened, { once: true }); socket.addEventListener('error', failed, { once: true });
      socket.addEventListener('close', ended, { once: true }); this.signal?.addEventListener('abort', aborted, { once: true });
    });
  }
  receive(data) {
    if (typeof data !== 'string' || Buffer.byteLength(data) > MAX_MESSAGE) { this.onDrop?.(); return; }
    let packet; try { packet = JSON.parse(data); } catch { this.onDrop?.(); return; }
    if (!object(packet)) return;
    if (Number.isInteger(packet.id)) {
      const request = this.pending.get(packet.id); if (!request) return;
      clearTimeout(request.timer); this.pending.delete(packet.id);
      if (packet.error) request.reject(fault('debugger_command_rejected'));
      else request.resolve(packet.result);
    } else if (typeof packet.method === 'string') this.onEvent?.(packet);
  }
  command(method, params = {}) {
    if (!ALLOWED_METHODS.has(method) || (method === 'Runtime.evaluate' && params.expression !== SNAPSHOT_EXPRESSION)) {
      return Promise.reject(fault('forbidden_debugger_command'));
    }
    if (!this.connected || this.closed || this.signal?.aborted) return Promise.reject(fault('debugger_not_open'));
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(fault('debugger_command_timeout')); }, 1200);
      this.pending.set(id, { resolve, reject, timer });
      try { this.socket.send(JSON.stringify({ id, method, params })); }
      catch { clearTimeout(timer); this.pending.delete(id); reject(fault('debugger_send_failed')); }
    });
  }
  failPending(code) {
    for (const { reject, timer } of this.pending.values()) { clearTimeout(timer); reject(fault(code)); }
    this.pending.clear();
  }
  close() {
    this.signal?.removeEventListener('abort', this.abort);
    this.closed = true; this.failPending('debugger_closed');
    // Close this observation connection only. Never close the page/browser.
    try { this.socket?.close(); } catch {}
  }
}

function wait(ms, signal) {
  if (signal?.aborted) return Promise.reject(fault('observation_deadline'));
  return new Promise((resolve, reject) => {
    const aborted = () => { clearTimeout(timer); reject(fault('observation_deadline')); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', aborted); resolve(); }, ms);
    signal?.addEventListener('abort', aborted, { once: true });
  });
}

export async function observe({ debugOrigin, appOrigin, seconds = 30 }, dependencies = {}) {
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > 60) throw fault('seconds_must_be_1_to_60');
  debugOrigin = loopbackOrigin(debugOrigin); appOrigin = loopbackOrigin(appOrigin);
  const report = { version: 1, completed: false, app_origin: appOrigin, path: '/', seconds_requested: seconds,
    started_wall_ms: Date.now(), observed_seconds: null, snapshots: [], sidecars: [], errors: [],
    discarded_events: 0, discarded_sidecars: 0, stream_creation_events: 0,
    metrics: { optical_fps: null, decoded_fps: null, xr_fps: null, camera_age_ms: null, mac_render_age_ms: null },
    limitations: [
      'Reads the existing page and received JSON sidecars; no new viewer, control socket or pose writer.',
      'DOM diagnostics are raw reported text, may be hidden or stale, and are not independently measured optical FPS.',
      'Existing sockets attached late may lack creation events: endpoint channel remains unknown.',
      'Sidecar receipt/timestamps do not prove decoded presentation, hardware identity or pose-to-photon latency.',
      'Debugger, observer and producer clocks differ; raw timestamps are not compared across clocks. Relay network timing is a reported round-trip bound.',
      'Event storage is bounded; discarded counts describe observation loss, not video-frame loss.',
      'CDP Network observation can mirror binary WebSocket traffic before it is discarded; debugging adds CPU/USB overhead that must be recorded with the run.',
    ] };
  const controller = new AbortController(); const stop = setTimeout(() => controller.abort(), (seconds + 8) * 1000);
  let observer, enabled = false, started;
  const streams = new Map(); let eventBytes = 0;
  try {
    const target = await discoverTarget(debugOrigin, appOrigin, { fetchImpl: dependencies.fetchImpl, signal: controller.signal });
    observer = new CdpObserver(target.socket, { WebSocketImpl: dependencies.WebSocketImpl, signal: controller.signal,
      onDrop: () => report.discarded_events++, onEvent: packet => {
        const params = packet.params;
        if (!object(params)) return;
        if (packet.method === 'Network.webSocketCreated') {
          if (typeof params.requestId !== 'string' || params.requestId.length > 128 || streams.size >= 64) return;
          try {
            const url = new URL(params.url), expected = new URL(appOrigin);
            const protocol = expected.protocol === 'https:' ? 'wss:' : 'ws:';
            if (url.protocol === protocol && url.host === expected.host && !url.username && !url.password &&
                ['/ws/video', '/ws/macview'].includes(url.pathname)) {
              streams.set(params.requestId, url.pathname === '/ws/video' ? 'camera' : 'macview');
              report.stream_creation_events++;
            }
          } catch {}
        } else if (packet.method === 'Network.webSocketClosed') {
          streams.delete(params.requestId);
        } else if (packet.method === 'Network.webSocketFrameReceived' && params.response?.opcode === 1 &&
                   typeof params.response.payloadData === 'string') {
          const payload = params.response.payloadData;
          if (Buffer.byteLength(payload) > MAX_MESSAGE) { report.discarded_events++; return; }
          let body; try { body = JSON.parse(payload); } catch { return; }
          const metadata = sanitizeSidecar(body); if (!metadata) return;
          const sidecar = { received_wall_ms: Date.now(), elapsed_ms: started === undefined ? null : performance.now() - started,
            debugger_timestamp_s: typeof params.timestamp === 'number' && Number.isFinite(params.timestamp) ? params.timestamp : null,
            endpoint_channel: streams.get(params.requestId) ?? null,
            packet_channel: metadata.macview ? 'macview' : null, metadata };
          const bytes = Buffer.byteLength(JSON.stringify(sidecar));
          if (report.sidecars.length >= MAX_SIDECARS || eventBytes + bytes > MAX_EVENT_BYTES) report.discarded_sidecars++;
          else { report.sidecars.push(sidecar); eventBytes += bytes; }
        }
      } });
    await observer.open();
    await observer.command('Network.enable', { maxTotalBufferSize: MAX_MESSAGE, maxResourceBufferSize: MAX_MESSAGE, maxPostDataSize: 0 });
    enabled = true; started = performance.now();
    for (let sample = 0; sample < seconds; sample++) {
      const delay = started + sample * 1000 - performance.now();
      if (delay > 0) await wait(delay, controller.signal);
      if (performance.now() - started >= seconds * 1000) break;
      try {
        const reply = await observer.command('Runtime.evaluate', { expression: SNAPSHOT_EXPRESSION,
          returnByValue: true, awaitPromise: false, userGesture: false, silent: true,
          allowUnsafeEvalBlockedByCSP: false, timeout: 800 });
        if (reply?.exceptionDetails) throw fault('snapshot_evaluation_failed');
        const snapshot = sanitizeSnapshot(reply?.result?.value);
        if (snapshot.appOrigin !== appOrigin || snapshot.path !== '/') throw fault('observed_page_changed');
        report.snapshots.push({ received_wall_ms: Date.now(), elapsed_ms: performance.now() - started, snapshot });
      } catch (error) {
        report.errors.push({ elapsed_ms: performance.now() - started, code: safeError(error) });
        if (error?.code !== 'debugger_command_timeout' && error?.code !== 'snapshot_evaluation_failed') throw error;
      }
    }
    const delay = started + seconds * 1000 - performance.now();
    if (delay > 0) await wait(delay, controller.signal);
    report.observed_seconds = (performance.now() - started) / 1000;
    report.completed = report.snapshots.length > 0 && report.errors.length === 0;
  } catch (error) {
    if (!report.errors.some(item => item.code === safeError(error))) report.errors.push({ code: safeError(error) });
  } finally {
    if (enabled && observer && !observer.closed && !controller.signal.aborted) {
      try { await observer.command('Network.disable'); } catch (error) { report.errors.push({ code: safeError(error) }); report.completed = false; }
    }
    observer?.close(); clearTimeout(stop); controller.abort(); report.finished_wall_ms = Date.now();
  }
  return report;
}

const HELP = `Observe an OWNER-PREPARED existing Quest/app page via loopback CDP (Node >=22).
Usage: node tools/observe_quest_camera.mjs --debug-origin http://127.0.0.1:9222
  --app-origin http://localhost:8080 --seconds 30 --out /absolute/path/camera-window.json
The owner prepares the debugger endpoint and existing '/' page/diagnostics first.
This tool performs no ADB setup, navigation, XR/input, playback or control commands.
Only Network.enable/disable and a constant read-only Runtime.evaluate are sent.
No new video stream or browser tab; only this tool's CDP socket is closed.
Optional operator HTTP polling is not implemented. Unknown metrics stay null.
Exit 0 means observation completed, NOT hardware/performance qualification.\n`;

export function parseArgs(args) {
  if (args.includes('--help') || args.includes('-h')) return { help: true };
  const options = { seconds: 30 };
  const seen = new Set();
  const keys = { '--debug-origin': 'debugOrigin', '--app-origin': 'appOrigin', '--seconds': 'seconds', '--out': 'out' };
  for (let i = 0; i < args.length; i++) {
    const key = keys[args[i]];
    if (!key || !args[i + 1] || args[i + 1].startsWith('--') || seen.has(key)) throw fault('invalid_arguments');
    seen.add(key);
    options[key] = key === 'seconds' ? Number(args[++i]) : args[++i];
  }
  if (!options.debugOrigin || !options.appOrigin || !options.out) throw fault('required_arguments_missing');
  if (!Number.isInteger(options.seconds) || options.seconds < 1 || options.seconds > 60) throw fault('seconds_must_be_1_to_60');
  loopbackOrigin(options.debugOrigin); loopbackOrigin(options.appOrigin);
  return options;
}

async function main() {
  let options;
  try { options = parseArgs(process.argv.slice(2)); }
  catch (error) { process.stderr.write(`${safeError(error)}; use --help\n`); return 1; }
  if (options.help) { process.stdout.write(HELP); return 0; }
  // Bound CLI exit even if a debugger fails to complete its WS close handshake.
  const hardStop = setTimeout(() => process.exit(1), (options.seconds + 11) * 1000);
  try {
    const report = await observe(options);
    await writeFile(options.out, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
    process.stdout.write(JSON.stringify({ completed: report.completed, snapshots: report.snapshots.length,
      sidecars: report.sidecars.length, errors: report.errors.map(item => item.code) }) + '\n');
    return report.completed ? 0 : 1;
  } catch { process.stderr.write('observation_or_output_failed\n'); return 1; }
  finally { clearTimeout(hardStop); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const code = await main();
  // Only this observer process exits; the existing browser target stays open.
  process.exit(code);
}
