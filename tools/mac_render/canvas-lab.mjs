import { OrbitControls } from '/quest/node_modules/three/examples/jsm/controls/OrbitControls.js';
import { createCanvasFixture, MAX_CANVAS_POINTS } from './canvas-lab-scene.mjs';

const params = new URLSearchParams(location.search);
const stage = document.querySelector('#stage');
const live = params.get('source') === 'live';
const points = Math.min(MAX_CANVAS_POINTS, Number(params.get('points') || 2000000));
params.set('width', String(innerWidth));
params.set('height', String(innerHeight));
params.set('lod', '1');
params.set('presentation', 'canvas');
if (!params.has('aa')) params.set('aa', '0');
if (live) params.delete('fixture'); else params.set('fixture', '1');
history.replaceState(null, '', `${location.pathname}?${params}`);
window.__macCaptureDriven = false;
window.__canvasLab = { ready: false };
await import('./render.mjs');

const fixture = live ? null : createCanvasFixture({ points });
const frameTimes = new Float64Array(600);
const renderTimes = new Float64Array(600);
let frameCount = 0, sampleCount = 0, previousFrame = 0, frameStarted = 0, lastTick = 0;
let context, controls, captureCalls = 0, autoOrbit = true;
const originalCapture = window.__macCapture;
window.__macCapture = (...args) => { captureCalls++; return originalCapture(...args); };
const percentile = (samples, fraction) => {
  const values = Array.from(samples.subarray(0, Math.min(sampleCount, samples.length))).sort((a, b) => a - b);
  return values.length ? values[Math.floor((values.length - 1) * fraction)] : null;
};

window.__macRegisterLayer({
  init(ctx) {
    context = ctx;
    stage.prepend(ctx.renderer.domElement);
    ctx.root.children.filter(child => child.type === 'GridHelper').forEach(child => { child.visible = true; });
    const framing = Math.max(1, 1 / ctx.camera.aspect);
    ctx.camera.position.set(9 * framing, 1.15 + 5.05 * framing, 10.5 * framing);
    controls = new OrbitControls(ctx.camera, ctx.renderer.domElement);
    controls.target.set(0, 1.15, 0);
    controls.enableDamping = true;
    controls.dampingFactor = .09;
    controls.autoRotate = true;
    controls.autoRotateSpeed = .65;
    controls.addEventListener('change', ctx.requestFrame);
    controls.addEventListener('start', () => setAutoOrbit(false));
    controls.update();
    const gl = ctx.renderer.getContext();
    const extension = gl.getExtension('WEBGL_debug_renderer_info');
    window.__canvasLab.gpuRenderer = extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    const resize = new ResizeObserver(() => {
      ctx.renderer.setSize(innerWidth, innerHeight, false);
      ctx.camera.aspect = innerWidth / innerHeight;
      ctx.camera.updateProjectionMatrix();
      ctx.requestFrame();
    });
    resize.observe(stage);
    window.addEventListener('pagehide', () => { resize.disconnect(); controls.dispose(); });
  },
  poll(ctx, now) {
    frameStarted = performance.now();
    const changed = controls.update(lastTick ? (now - lastTick) / 1000 : 1 / 60);
    lastTick = now;
    return changed || autoOrbit;
  },
  post() {
    const now = performance.now(), index = sampleCount % frameTimes.length;
    if (previousFrame) {
      frameTimes[index] = now - previousFrame;
      renderTimes[index] = now - frameStarted;
      sampleCount++;
    }
    previousFrame = now;
    frameCount++;
  },
});
if (fixture) {
  window.__macRegisterLayer(fixture);
  await fixture.ready;
  fixture.setStyle('points');
  // ?budget=auto (default) holds ~60fps by thinning the drawn subset;
  // ?budget=<n> pins a fixed draw cap; ?budget=off always draws everything.
  fixture.setDrawBudget(params.get('budget') ?? 'auto');
}

function setAutoOrbit(value) {
  autoOrbit = value;
  controls.autoRotate = value;
  sampleCount = 0; previousFrame = 0;
  context.requestFrame();
}
async function setPointCount(value) {
  await fixture?.setPointCount(Math.min(MAX_CANVAS_POINTS, value));
  sampleCount = 0; previousFrame = 0;
  context.requestFrame();
}
function stats() {
  const data = fixture ? fixture.metrics : { points: window.__mac.points, triangles: window.__mac.triangles, source: window.__mac.source };
  const frames = Math.min(sampleCount, frameTimes.length);
  const mean = frames ? frameTimes.subarray(0, frames).reduce((sum, value) => sum + value, 0) / frames : 0;
  return { ...data, max_points: MAX_CANVAS_POINTS, frame_count: frameCount, samples: frames, fps: mean ? 1000 / mean : 0,
    frame_ms_p50: percentile(frameTimes, .5), frame_ms_p95: percentile(frameTimes, .95),
    render_ms_p50: percentile(renderTimes, .5), render_ms_p95: percentile(renderTimes, .95),
    gpu_renderer: window.__canvasLab.gpuRenderer, capture_calls: captureCalls,
    draw_calls: context.renderer.info.render.calls, drawn_points: context.renderer.info.render.points,
    grid_visible: context.root.children.some(child => child.type === 'GridHelper' && child.visible),
    canvas_width: context.renderer.domElement.width, canvas_height: context.renderer.domElement.height,
    antialias: context.renderer.getContext().getContextAttributes().antialias,
    preserve_drawing_buffer: context.renderer.getContext().getContextAttributes().preserveDrawingBuffer,
    source_mode: live ? 'live' : 'fixture', scope: 'Direct desktop GPU canvas; no encode, headset, or physical latency measurement' };
}
Object.assign(window.__canvasLab, { ready: true, stats, setPointCount, setAutoOrbit });
window.addEventListener('pagehide', () => fixture?.dispose());
