import { chromium } from '@playwright/test';
import fs from 'node:fs';
const base = process.env.BASE || 'http://127.0.0.1:8095';
const out = process.env.OUT || '/private/tmp/claude-501/msurf';
const tag = process.env.TAG || 'run';
const back = Number(process.env.BACK || 1.2), up = Number(process.env.UP || 0.4);
const extra = process.env.EXTRA || '';
const status = await (await fetch(`${base}/map/status`)).json();
const m = status.transform; // column-major camera-to-world (ARKit camera looks down -Z, like three)
const col = i => [m[i*4], m[i*4+1], m[i*4+2]];
const [xA, yA, zA, p] = [col(0), col(1), col(2), col(3)];
let pos = [0,1,2].map(i => p[i] + zA[i]*back + yA[i]*up);
let r = [[xA[0], yA[0], zA[0]], [xA[1], yA[1], zA[1]], [xA[2], yA[2], zA[2]]];
if (process.env.POS && process.env.TARGET) {
  pos = JSON.parse(process.env.POS); const t = JSON.parse(process.env.TARGET);
  const norm = v => { const l = Math.hypot(...v); return v.map(x => x / l); };
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const z = norm(pos.map((v, i) => v - t[i])), x = norm(cross([0, 1, 0], z)), y = cross(z, x);
  r = [[x[0], y[0], z[0]], [x[1], y[1], z[1]], [x[2], y[2], z[2]]];
}
const tr = r[0][0] + r[1][1] + r[2][2]; let q;
if (tr > 0) { const s = Math.sqrt(tr + 1) * 2; q = [(r[2][1]-r[1][2])/s, (r[0][2]-r[2][0])/s, (r[1][0]-r[0][1])/s, s/4]; }
else if (r[0][0] > r[1][1] && r[0][0] > r[2][2]) { const s = Math.sqrt(1+r[0][0]-r[1][1]-r[2][2])*2; q = [s/4, (r[0][1]+r[1][0])/s, (r[0][2]+r[2][0])/s, (r[2][1]-r[1][2])/s]; }
else if (r[1][1] > r[2][2]) { const s = Math.sqrt(1+r[1][1]-r[0][0]-r[2][2])*2; q = [(r[0][1]+r[1][0])/s, s/4, (r[1][2]+r[2][1])/s, (r[0][2]-r[2][0])/s]; }
else { const s = Math.sqrt(1+r[2][2]-r[0][0]-r[1][1])*2; q = [(r[0][2]+r[2][0])/s, (r[1][2]+r[2][1])/s, s/4, (r[1][0]-r[0][1])/s]; }
const browser = await chromium.launch({ args: ['--use-angle=metal', '--enable-gpu', '--ignore-gpu-blocklist'] });
for (const mode of (process.env.MODES || 'before,after').split(',')) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  const q2 = mode === 'before' ? '&msurf=0' : '';
  await page.goto(`${base}/render/render.html?upstream=${encodeURIComponent(base)}&width=1280&height=720&camera=snap${q2}${extra}`);
  await page.waitForFunction(() => window.__mac?.ready || window.__mac?.error);
  await page.waitForTimeout(Number(process.env.WAIT || 12000));
  if (mode !== 'before') await page.waitForFunction(() => (window.__mac.measuredSurface?.builds || 0) > 0, null, { timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const result = await page.evaluate(([pos, q]) => {
    const now = performance.now();
    window.__macCaptureDriven = true;
    window.__macPose = { tracked: true, position: pos, quaternion: q, receiptAgeMs: 0, sentMonoMs: now, sentWallMs: Date.now(), receivedMonoMs: now, fov_y_deg: 70, aspect: 1280/720 };
    const t0 = performance.now();
    const shot = window.__macCapture();
    return { data: shot.data, ms: performance.now() - t0, points: shot.meta.points, triangles: shot.meta.triangles,
      drawnPoints: shot.meta.drawnPoints, drawnTriangles: shot.meta.drawnTriangles, renderMs: shot.meta.renderMs,
      ms_view: window.__mac.measuredSurface ? { ...window.__mac.measuredSurface } : null, mapStatus: shot.meta.mapStatus };
  }, [pos, q]);
  fs.writeFileSync(`${out}/${tag}-${mode}.jpg`, Buffer.from(result.data, 'base64'));
  delete result.data;
  console.log(mode, JSON.stringify({ ...result, mapStatus: { points: result.mapStatus?.points, triangles: result.mapStatus?.triangles }, errors: errors.slice(0, 3) }));
  await page.close();
}
await browser.close();
