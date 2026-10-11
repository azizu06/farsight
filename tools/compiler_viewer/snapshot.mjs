// Headless 4-pose snapshotter for a compiled output dir.
//   node tools/compiler_viewer/snapshot.mjs --out <out-dir> [--shots <dir>] [--mode textured]
//             [--quality high] [--splatcap N] [--port 8799] [--bin path/to/farsight-compile] [--url http://...]
// Poses (all relative to scene bounds): overview, wall-graze, floor-graze, corner.
// Uses __viz.shot() (synchronous composer render + toDataURL) — no font or
// screenshot-stability waits, reliable under SwiftShader/llvmpipe.
import { chromium } from '../../quest/node_modules/playwright/index.mjs';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const get = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const OUT = get('--out', 'compiled');
const SHOTS = get('--shots', path.join(OUT, 'snapshots'));
const MODE = get('--mode', 'textured');
const QUALITY = get('--quality', 'high');
const SPLATCAP = get('--splatcap', '0');
const URL_ARG = get('--url', null);
const PORT = get('--port', '8801');
const BIN = get('--bin', path.resolve(process.cwd(), 'rust/target/release/farsight-compile'));

mkdirSync(SHOTS, { recursive: true });

let server = null;
if (!URL_ARG) {
  server = spawn(BIN, ['view', OUT, '--port', PORT], { stdio: ['ignore', 'inherit', 'inherit'] });
  await new Promise((r) => setTimeout(r, 700));
}
const base = URL_ARG || `http://127.0.0.1:${PORT}`;

// 4 poses from scene bounds {min,max,center,diag,floorY}.
// Grazing = camera ~coplanar with the plane, looking along it — the view that
// exposes ripple/terracing on flat walls and floors.
const POSES = [
  ['overview', (b) => {
    const [cx, cy, cz] = b.center, d = b.diag;
    return { pos: [cx + 0.45 * d, cy + 0.35 * d, cz + 0.45 * d], tgt: [cx, cy, cz] };
  }],
  ['wall-graze', (b) => {
    const [cx, cy, cz] = b.center, d = b.diag;
    const sx = b.max[0] - b.min[0], sz = b.max[2] - b.min[2];
    // hug the -x wall, look down +z almost parallel to its plane
    const wx = b.min[0] + sx * 0.045;
    return { pos: [wx, cy, cz - sz * 0.38], tgt: [wx, cy + d * 0.01, cz + sz * 0.38] };
  }],
  ['floor-graze', (b) => {
    const [cx, , cz] = b.center, d = b.diag;
    const sx = b.max[0] - b.min[0];
    const fy = b.floorY + d * 0.015;
    return { pos: [cx - sx * 0.36, fy, cz], tgt: [cx + sx * 0.36, fy + d * 0.005, cz] };
  }],
  ['corner', (b) => {
    const d = b.diag;
    const crn = [b.min[0], b.floorY, b.min[2]];
    return { pos: [crn[0] + d * 0.16, crn[1] + d * 0.13, crn[2] + d * 0.16], tgt: [crn[0], crn[1] + d * 0.02, crn[2]] };
  }],
];

const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const consoleErrs = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrs.push(m.text()); });
  page.on('pageerror', (e) => consoleErrs.push(String(e)));
  await page.goto(`${base}/?mode=${MODE}&q=${QUALITY}&splatcap=${SPLATCAP}`, { waitUntil: 'commit' });
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 60000 });
  await page.waitForTimeout(800); // env/AO warmup

  const bounds = await page.evaluate(() => window.__viz.bounds());
  if (!bounds) { console.error('[snap] no scene bounds — is there any artefact in', OUT); process.exitCode = 1; }
  else {
    for (const [name, pose] of POSES) {
      const { pos, tgt } = pose(bounds);
      await page.evaluate(({ pos, tgt }) => {
        window.__viz.setCamera(pos[0], pos[1], pos[2], tgt[0], tgt[1], tgt[2]);
      }, { pos, tgt });
      await page.waitForTimeout(500); // a few frames for sort/shadow settle
      const dataUrl = await page.evaluate(() => window.__viz.shot());
      const b64 = dataUrl.split(',')[1];
      const f = path.join(SHOTS, `${name}.png`);
      writeFileSync(f, Buffer.from(b64, 'base64'));
      console.log(`[snap] ${name} -> ${f}`);
    }
  }
  const errs = consoleErrs.filter((e) => !/favicon|404|net::ERR_FILE/.test(e));
  console.log(`[snap] console errors: ${errs.length}`);
  if (errs.length) { for (const e of errs.slice(0, 6)) console.log('   ', e.slice(0, 200)); process.exitCode = 1; }
} finally {
  await browser.close();
  server?.kill();
}
