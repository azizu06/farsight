// Synthetic GPU/upload regression on the canonical app. No rig or Quest qualification.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const preview = spawn(process.execPath, [fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url)),
  "preview", "--host", "127.0.0.1", "--port", "5267", "--strictPort"], { cwd: new URL("..", import.meta.url), stdio: "pipe" });
let log = "", browser;
preview.stdout.on("data", value => { log += value; }); preview.stderr.on("data", value => { log += value; });
const origin = "http://127.0.0.1:5267", sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const chunks = Array.from({ length: 32 }, (_, i) => ({ id: `${i % 4 - 2}_0_${Math.floor(i / 4) - 4}`, revision: 1, lod: 0 }));

function chunkPacket(chunk, epoch) {
  const xyz = chunk.id.split("_").map(Number), count = 8192;
  const header = Buffer.from(JSON.stringify({ format: "xyz_mm_rgb8_op", epoch, revision: chunk.revision,
    chunk: xyz, lod: 0, points: count }));
  const body = Buffer.alloc(4 + header.length + count * 16);
  body.writeUInt32LE(header.length); header.copy(body, 4);
  for (let i = 0; i < count; i++) {
    const at = 4 + header.length + i * 16;
    body.writeInt32LE(xyz[0] * 2000 + i % 128 * 12, at);
    body.writeInt32LE(i % 64 * 20, at + 4);
    body.writeInt32LE(xyz[2] * 2000 + Math.floor(i / 128) * 20, at + 8);
    body[at + 12] = 240; body[at + 13] = 120; body[at + 14] = 60; body[at + 15] = 1;
  }
  return body;
}

async function wait(check, label) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) { if (await check()) return; await sleep(50); }
  throw new Error(`Timed out: ${label}`);
}

try {
  await wait(async () => { if (preview.exitCode !== null) throw new Error(log); return fetch(origin).then(r => r.ok, () => false); }, "preview");
  browser = await chromium.launch({ channel: "chrome", headless: true,
    args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    let epoch = "synthetic-1", refs = chunks.map(c => ({ ...c })), reads = 0, latest = {};
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (/THREE.WebGLProgram: Shader Error|VALIDATE_STATUS/.test(message.text())) errors.push(message.text()); });
    await page.addInitScript(() => {
      window.__denseGpu = { points: 0, updates: [] };
      const ctx = WebGL2RenderingContext.prototype;
      const draw = ctx.drawArrays;
      ctx.drawArrays = function(mode, first, count) { if (mode === this.POINTS) window.__denseGpu.points += count; return draw.call(this, mode, first, count); };
      const update = ctx.bufferSubData;
      ctx.bufferSubData = function(...args) { window.__denseGpu.updates.push(args[2]?.byteLength ?? 0); return update.apply(this, args); };
    });
    await page.route("**/dense/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/dense/manifest") return route.fulfill({ json: { epoch, chunks: refs, ttl_s: 30, source_kind: "rgbd" } });
      if (url.pathname === "/dense/chunk.bin") {
        reads++;
        const chunk = refs.find(c => c.id === url.searchParams.get("id"));
        return route.fulfill({ body: chunkPacket(chunk, epoch), contentType: "application/octet-stream" });
      }
      if (url.pathname === "/dense/metrics") { latest = route.request().postDataJSON(); return route.fulfill({ json: { ok: true } }); }
      return route.fulfill({ status: 404 });
    });
    await page.goto(`${origin}/?view=map`);
    await wait(() => latest.uploaded_points === 262144, "all bounded chunks uploaded");
    assert.equal(reads, 32);
    assert.ok(latest.points > 0);
    const gpu = await page.evaluate(() => window.__denseGpu);
    assert.ok(gpu.points > 0, "actual WebGL POINTS draw calls occurred");
    await sleep(1000); assert.equal(reads, 32, "unchanged chunks are not downloaded");
    refs[0].revision++;
    await wait(() => reads === 33, "one changed chunk refresh");
    await sleep(1000); assert.equal(reads, 33);
    assert.ok((await page.evaluate(() => window.__denseGpu.updates.length)) > 0, "existing GPU buffer updated incrementally");
    await page.screenshot({ path: new URL(`../../.cache/dense-${viewport.width}.png`, import.meta.url).pathname });
    epoch = "synthetic-2"; refs = [];
    await wait(() => latest.uploaded_points === 0, "session reset clears dense buffers");
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    console.log(JSON.stringify({ source: "SYNTHETIC_CHUNKS", viewport, uploaded_points: 262144,
      drawn_points_across_frames: gpu.points, chunk_reads: reads, reset_uploaded_points: latest.uploaded_points,
      desktop_fps: latest.fps, headset_fps: null, errors }));
    await page.close();
  }
} finally {
  await browser?.close(); preview.kill("SIGTERM");
}
