// Headless render check for compiler_viewer.
//   node tools/compiler_viewer/test/render_check.mjs --out /tmp/viewbench --shots /tmp/viewshots [--emit-glb]
// Spawns `farsight-compile view <out>` (or --url to reuse one), loads the page,
// walks every mode, screenshots each, and reports console errors + fps.
import { chromium } from '../../../quest/node_modules/playwright/index.mjs';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const get = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const OUT = get('--out', 'compiled');
const SHOTS = get('--shots', path.join(OUT, 'shots'));
const URL_ARG = get('--url', null);
const EMIT_GLB = args.includes('--emit-glb');
const PORT = get('--port', '8799');
const BIN = get('--bin', path.resolve(process.cwd(), 'rust/target/release/farsight-compile'));

const MODES = ['textured', 'vcolor', 'heatmap', 'normals', 'wire', 'points', 'splat', 'blend'];

mkdirSync(SHOTS, { recursive: true });

let server = null;
if (!URL_ARG) {
  server = spawn(BIN, ['view', OUT, '--port', PORT], { stdio: ['ignore', 'inherit', 'inherit'] });
  await new Promise((r) => setTimeout(r, 700));
}
const base = URL_ARG || `http://127.0.0.1:${PORT}`;

const results = { modes: {}, errors: [], fps: {} };
const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const consoleErrs = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrs.push(m.text()); });
  page.on('pageerror', (e) => consoleErrs.push(String(e)));
  const splatcap = get('--splatcap', '40000');
  await page.goto(`${base}/?splatcap=${splatcap}`, { waitUntil: 'commit' });
  await page.waitForFunction(() => window.__viz && window.__viz.ready, null, { timeout: 45000 });

  const files = await page.evaluate(() => window.__viz.files());
  console.log('[check] artefacts:', files.join(', '));

  // settle + warmup
  await page.waitForTimeout(1500);

  for (const mode of MODES) {
    const before = consoleErrs.length;
    const ok = await page.evaluate((m) => { try { window.__viz.setMode(m); return true; } catch (e) { return String(e); } }, mode);
    if (ok !== true) { results.modes[mode] = { skipped: ok }; continue; }
    await page.waitForTimeout(1200);
    const info = await page.evaluate(() => window.__viz.info());
    await page.screenshot({ path: path.join(SHOTS, `${mode}.png`), timeout: 90000 });
    results.modes[mode] = { calls: info.calls, triangles: info.triangles,
      errors: consoleErrs.slice(before) };
    results.fps[mode] = +(await page.evaluate(() => window.__viz.fps)).toFixed(1);
    console.log(`[check] ${mode}: calls=${info.calls} tris=${info.triangles} fps=${results.fps[mode]} newErr=${results.modes[mode].errors.length}`);
  }

  // walk presets
  for (const c of ['human', 'rover']) {
    await page.evaluate((c) => window.__viz.setCamMode(c), c);
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(SHOTS, `walk-${c}.png`), timeout: 90000 });
  }
  await page.evaluate(() => window.__viz.setCamMode('orbit'));

  // quality sweep on textured-ish mode
  for (const q of ['medium', 'low', 'high']) {
    await page.evaluate((q) => window.__viz.setQuality(q), q);
    await page.waitForTimeout(900);
    await page.screenshot({ path: path.join(SHOTS, `q-${q}.png`), timeout: 90000 });
    results.fps[`q-${q}`] = +(await page.evaluate(() => window.__viz.fps)).toFixed(1);
    console.log(`[check] quality ${q}: fps=${results.fps[`q-${q}`]}`);
  }

  if (EMIT_GLB) {
    const b64 = await page.evaluate(async () => {
      const mod = await import('/viewer/vendor/three/addons/exporters/GLTFExporter.js');
      const exp = new mod.GLTFExporter();
      const obj = window.__viz._exportTarget?.() || null;
      if (!obj) return null;
      const glb = await exp.parseAsync(obj, { binary: true });
      const u8 = new Uint8Array(glb);
      let s = ''; for (let i = 0; i < u8.length; i += 8192) s += String.fromCharCode(...u8.subarray(i, i + 8192));
      return btoa(s);
    });
    if (b64) { writeFileSync(path.join(OUT, 'scene.glb'), Buffer.from(b64, 'base64')); console.log('[check] wrote scene.glb'); }
    else console.log('[check] no export target for GLB');
  }

  results.errors = consoleErrs;
  const vizErr = await page.evaluate(() => window.__viz.errors);
  console.log('[check] console errors:', consoleErrs.length, 'viz errors:', JSON.stringify(vizErr));
  writeFileSync(path.join(SHOTS, 'results.json'), JSON.stringify(results, null, 2));
  const bad = consoleErrs.filter((e) => !/favicon|404|net::ERR_FILE/.test(e));
  console.log(bad.length ? `[check] FAIL ${bad.length} errors` : '[check] PASS');
  for (const e of bad.slice(0, 10)) console.log('   ', e.slice(0, 200));
  process.exitCode = bad.length ? 1 : 0;
} finally {
  await browser.close();
  server?.kill();
}
