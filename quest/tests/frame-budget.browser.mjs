// Frame-budget profile of the canonical app (main.ts + RoverExperience) under
// emulated Quest 2 WebXR (IWER). Needs a running simulator upstream, e.g.
//   python -m jetson.mapping.server --demo --simulate-rover --detector pose \
//     --detector-model models/yolo11n-pose.onnx --h264 --host 127.0.0.1
// then `npm run build && npm run perf` (FARSIGHT_UPSTREAM overrides 8443).
// Desktop Chrome numbers are not Quest numbers: compare before/after on the same
// machine and keep the Quest 2 budget (72 Hz = 13.9 ms) as the ceiling.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const port = 5258, origin = `http://127.0.0.1:${port}`;
const seconds = Number(process.env.PERF_SECONDS ?? 10);
const gpu = process.env.PERF_GL ?? "metal"; // "swiftshader" matches the regression harness
const iwer = readFileSync(createRequire(import.meta.url).resolve("iwer/build/iwer.min.js"), "utf8");
const preview = spawn(process.execPath, [fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url)),
  "preview", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: new URL("..", import.meta.url), stdio: "pipe" });
let log = ""; preview.stdout.on("data", d => { log += d; }); preview.stderr.on("data", d => { log += d; });

// Installed before the app: Quest 2 runtime plus counters on rAF, WebGL and image decode.
const probe = `${iwer}
window.__device = new IWER.XRDevice(IWER.metaQuest2); window.__device.installRuntime({ forceInstall: true });
(() => {
  const p = window.__perf = { frames: [], draws: 0, uploads: 0, uploadBytes: 0, uploadMs: 0, decodes: [], frameDraws: [], frameUploads: [], bySize: {} };
  const wrapFrame = cb => (...args) => { const d0 = p.draws, u0 = p.uploadBytes, t = performance.now(); p.kinds = []; try { return cb(...args); } finally {
    (p.byKind ??= {}); const kind = p.kinds.sort().join("+") || "none"; (p.byKind[kind] ??= []).push(performance.now() - t);
    p.frames.push(performance.now() - t); p.frameDraws.push(p.draws - d0); p.frameUploads.push(p.uploadBytes - u0); } };
  const rAF = window.requestAnimationFrame.bind(window);
  window.__wrapWindowRaf = () => { window.requestAnimationFrame = cb => rAF(wrapFrame(cb)); };
  const xrRaf = XRSession.prototype.requestAnimationFrame;
  XRSession.prototype.requestAnimationFrame = function (cb) { return xrRaf.call(this, wrapFrame(cb)); };
  for (const Ctx of [WebGLRenderingContext, WebGL2RenderingContext]) {
    for (const name of ["drawArrays", "drawElements", "drawArraysInstanced", "drawElementsInstanced"]) {
      const original = Ctx.prototype[name]; if (!original) continue;
      Ctx.prototype[name] = function (...args) { p.draws++; return original.apply(this, args); };
    }
    for (const name of ["texImage2D", "texSubImage2D"]) {
      const original = Ctx.prototype[name];
      Ctx.prototype[name] = function (...args) {
        const source = args.find(a => a && typeof a === "object" && ("width" in a || "videoWidth" in a));
        const w = source ? (source.videoWidth || source.width) : args[3], h = source ? (source.videoHeight || source.height) : args[4];
        const t = performance.now(); try { return original.apply(this, args); } finally {
          if (Number(w) * Number(h) > 0) { p.uploads++; p.uploadBytes += w * h * 4; const key = w + "x" + h; p.kinds?.push(key); p.bySize[key] = (p.bySize[key] ?? 0) + 1; p.uploadMs += performance.now() - t; } }
      };
    }
  }
  const decode = HTMLImageElement.prototype.decode;
  HTMLImageElement.prototype.decode = function () { const t = performance.now(); return decode.call(this).finally(() => p.decodes.push(performance.now() - t)); };
})();`;

const stats = values => {
  const s = [...values].sort((a, b) => a - b), at = q => s.length ? s[Math.min(s.length - 1, Math.floor(q * s.length))] : 0;
  return { n: s.length, mean: s.length ? s.reduce((a, b) => a + b, 0) / s.length : 0, p50: at(.5), p95: at(.95), p99: at(.99), max: s.at(-1) ?? 0 };
};
const f = n => n.toFixed(2);

let browser;
try {
  for (let i = 0; i < 100 && !(await fetch(origin).then(r => r.ok, () => false)); i++) {
    if (preview.exitCode !== null) throw new Error(log);
    await new Promise(r => setTimeout(r, 100));
  }
  browser = await chromium.launch({ channel: "chrome", headless: true, args: gpu === "swiftshader"
    ? ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    : ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist", "--autoplay-policy=no-user-gesture-required"] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  page.on("pageerror", e => console.error("pageerror", e.message));
  await page.addInitScript(probe);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  // Quest 2's XR2 runs page JS several times slower than a desktop core; throttle to approximate it.
  const throttle = Number(process.env.PERF_CPU_THROTTLE ?? 4);
  if (throttle > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: throttle });
  await page.goto(process.env.PERF_DIAG ? `${origin}/?diag=1` : origin);
  await page.waitForSelector("#rover-message");
  const load = await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => {
    const nav = performance.getEntriesByType("navigation")[0], bytes = performance.getEntriesByType("resource").reduce((sum, r) => sum + (r.transferSize || 0), nav.transferSize || 0);
    resolve({ domContentLoaded: nav.domContentLoadedEventEnd, load: nav.loadEventEnd, firstFrame: performance.now(), bytes });
  })));
  console.log(`load (local preview, CPU throttle ${throttle}x): DOMContentLoaded ${f(load.domContentLoaded)} ms, load ${f(load.load)} ms, first frame ${f(load.firstFrame)} ms, ${f(load.bytes / 1024)} KiB transferred`);
  const gl = await page.evaluate(() => { const c = document.createElement("canvas").getContext("webgl2"); const e = c.getExtension("WEBGL_debug_renderer_info"); return e ? c.getParameter(e.UNMASKED_RENDERER_WEBGL) : "unknown"; });

  const measure = async label => {
    await page.evaluate(() => { const p = window.__perf; Object.assign(p, { frames: [], frameDraws: [], frameUploads: [], decodes: [], draws: 0, uploads: 0, uploadBytes: 0, uploadMs: 0, bySize: {}, byKind: {} }); });
    const metric = async () => Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map(m => [m.name, m.value]));
    const before = await metric();
    await browser.startTracing(page, { categories: ["v8", "disabled-by-default-v8.gc"] });
    if (process.env.PERF_PROFILE) { await cdp.send("Profiler.enable"); await cdp.send("Profiler.setSamplingInterval", { interval: 200 }); await cdp.send("Profiler.start"); }
    // Natural head motion so digital pan, HUD and pose all change.
    const started = Date.now();
    while (Date.now() - started < seconds * 1000) {
      await page.evaluate(t => { const d = window.__device; if (d?.quaternion) { const a = Math.sin(t / 900) * .35; d.quaternion.set(0, Math.sin(a / 2), 0, Math.cos(a / 2)); } }, Date.now() - started);
      await new Promise(r => setTimeout(r, 50));
    }
    if (process.env.PERF_PROFILE) {
      const { profile } = await cdp.send("Profiler.stop"), self = new Map(), byId = new Map(profile.nodes.map(n => [n.id, n]));
      const dt = profile.timeDeltas, total = profile.samples.length;
      profile.samples.forEach(id => { const n = byId.get(id), c = n.callFrame, k = `${c.functionName || "(anon)"} ${c.url.split("/").pop()}:${c.lineNumber}`; self.set(k, (self.get(k) ?? 0) + 1);
        if (!c.url && process.env.PERF_PROFILE === "callers") { const parent = profile.nodes.find(x => x.children?.includes(id)); const pc = parent?.callFrame; if (pc) { const pk = `  <- ${c.functionName} from ${pc.functionName || "(anon)"}:${pc.lineNumber}`; self.set(pk, (self.get(pk) ?? 0) + 1); } } });
      // Inclusive time for the frame's main stages.
      const parent = new Map(); profile.nodes.forEach(n => n.children?.forEach(c => parent.set(c, n.id)));
      const inclusive = new Map(), stages = /^(update|render|drawCamera|drawHUD|drawSource|drawStop|drawVoice|drawDetections|uploadTexture|texSubImage2D|texImage2D|drawImage|fillText|measureText|pump|send|acceptStatus|onmessage|socket2\.onmessage|updateFeeds|syncMapConnection)$/;
      profile.samples.forEach(id => { const seen = new Set(); for (let n = id; n !== undefined; n = parent.get(n)) { const name = byId.get(n).callFrame.functionName; if (stages.test(name) && !seen.has(name)) { seen.add(name); inclusive.set(name, (inclusive.get(name) ?? 0) + 1); } } });
      const ms = profile.timeDeltas.reduce((a, b) => a + b, 0) / 1e3 / total;
      console.log("inclusive ms/s: " + [...inclusive].sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${f(v * ms / (profile.endTime - profile.startTime) * 1e6)}`).join(", "));
      console.log(`top self time (${total} samples, ${f(dt.reduce((a, b) => a + b, 0) / 1e6)} s):`);
      for (const [k, v] of [...self].sort((a, b) => b[1] - a[1]).filter(([k]) => !k.startsWith("(idle)")).slice(0, 30)) console.log(`  ${f(v / total * 100)}% ${k}`);
    }
    const trace = JSON.parse((await browser.stopTracing()).toString());
    const after = await metric();
    const result = await page.evaluate(() => {
      const p = window.__perf, video = document.querySelector("video");
      const q = video?.getVideoPlaybackQuality?.();
      return { frames: p.frames, frameDraws: p.frameDraws, frameUploads: p.frameUploads, decodes: p.decodes, byKind: p.byKind, bySize: p.bySize, uploads: p.uploads, uploadBytes: p.uploadBytes, uploadMs: p.uploadMs,
        details: document.querySelector("#rover-details")?.textContent ?? "", headline: document.querySelector("#rover-message")?.textContent ?? "",
        banner: document.querySelector(".source-banner strong")?.textContent ?? "", video: q ? { total: q.totalVideoFrames, dropped: q.droppedVideoFrames } : null,
        presenting: !!document.querySelector("#xr-slot button")?.textContent?.match(/EXIT/i) };
    });
    const gc = (trace.traceEvents ?? []).filter(e => e.ph === "X" && /^(MinorGC|MajorGC|V8\.GC_SCAVENGER|V8\.GC_MARK_COMPACTOR|V8\.GCScavenger|V8\.GCFinalizeMC)$/.test(e.name));
    const elapsed = after.Timestamp - before.Timestamp;
    const frame = stats(result.frames), draws = stats(result.frameDraws), up = stats(result.frameUploads);
    const busy = (k) => (after[k] - before[k]) / elapsed * 100;
    console.log(`\n## ${label} (${gl}, CPU throttle ${throttle}x, ${f(elapsed)} s)`);
    console.log(`frames ${frame.n} (${f(frame.n / elapsed)} fps)  rAF JS ms p50 ${f(frame.p50)} p95 ${f(frame.p95)} p99 ${f(frame.p99)} max ${f(frame.max)} mean ${f(frame.mean)}; over 11.1 ms ${result.frames.filter(t => t > 11.1).length}, over 13.9 ms ${result.frames.filter(t => t > 13.9).length}`);
    console.log(`draw calls/frame p50 ${draws.p50} max ${draws.max}  texture uploads ${f(result.uploads / elapsed)}/s ${f(result.uploadBytes / elapsed / 1e6)} MB/s, sync upload ${f(result.uploadMs / elapsed)} ms/s; upload bytes/frame p95 ${f(up.p95 / 1e6)} MB; per texture/s ${Object.entries(result.bySize).map(([k, v]) => `${k} ${f(v / elapsed)}`).join(", ")}`);
    for (const [kind, times] of Object.entries(result.byKind ?? {})) { const k = stats(times); console.log(`  frames uploading [${kind}] n=${k.n} mean ${f(k.mean)} p95 ${f(k.p95)} max ${f(k.max)}`); }
    console.log(`main thread busy: task ${f(busy("TaskDuration"))}% script ${f(busy("ScriptDuration"))}% layout ${f(busy("LayoutDuration"))}% style ${f(busy("RecalcStyleDuration"))}%  heap ${f(after.JSHeapUsedSize / 1e6)} MB`);
    console.log(`GC events ${gc.length} (${f(gc.length / elapsed)}/s), ${f(gc.reduce((a, e) => a + e.dur, 0) / 1000 / elapsed)} ms/s; image decode ms p50 ${f(stats(result.decodes).p50)} (n=${result.decodes.length}); video ${JSON.stringify(result.video)}`);
    console.log(`banner ${result.banner} | ${result.headline} | ${result.details.slice(0, 220)}`);
    return { frame, draws, result, gc, elapsed };
  };

  // H.264 path, as in the judge demo; JPEG when PERF_JPEG=1.
  if (!process.env.PERF_JPEG) await page.evaluate(() => { const box = [...document.querySelectorAll("#settings label.check input")].find(i => i.parentElement.textContent.includes("H.264")); box.click(); });
  await page.waitForTimeout(4000);
  await page.evaluate(() => window.__wrapWindowRaf());
  await measure("desktop camera view");
  await page.evaluate(() => document.querySelector("#xr-slot button").click());
  await page.waitForTimeout(1500);
  // IWER reports emulated gamepads as disconnected; the app requires connected, tracked controllers.
  // Hold the right trigger (release first, then press) so the deadman/driving path runs.
  await page.evaluate(async () => {
    const d = window.__device, wait = ms => new Promise(r => setTimeout(r, ms));
    for (const hand of ["left", "right"]) Object.defineProperty(Object.getPrototypeOf(d.controllers[hand].inputSource.gamepad), "connected", { configurable: true, get: () => true });
    d.controllers.right.updateButtonValue("trigger", 0); await wait(500);
    d.controllers.right.updateButtonValue("trigger", 1);
  });
  await page.waitForTimeout(1500);
  await measure("immersive VR camera view (IWER Quest 2)");
} finally {
  await browser?.close();
  preview.kill();
}
