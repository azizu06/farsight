// Judge-flow smoke test (#57): docs/judge-demo.md against the simulator and the replay fixture.
//
//   npm --prefix quest run test:judge            # or: node quest/tests/judge-flow.e2e.mjs [--out report.json]
//   --trace-control saves bounded, credential-free scan timing observations in the report.
//
// Starts `python -m tools.demo --simulate --build` (simulated rover, ray-cast room) and a
// `--replay tools/fixtures/replay-demo` server behind its own operator relay, then drives the
// served Quest page in headless Chromium/SwiftShader with the desktop controls (Space deadman,
// WASD, E explore, click-to-seek, Settings). XR-only inputs are covered by xr-input.browser.mjs.
// Every result is SIMULATED or REPLAY; nothing here touches or proves hardware.
import { spawn } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import net from "node:net";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

const root = fileURLToPath(new URL("../..", import.meta.url));
const python = process.env.FARSIGHT_PYTHON ?? (existsSync(`${root}.venv/bin/python`) ? `${root}.venv/bin/python` : "python3");
const outIndex = process.argv.indexOf("--out");
const outPath = outIndex > 0 ? process.argv[outIndex + 1] : null;
const traceControl = process.argv.includes("--trace-control");
const SYNTHETIC_ID = 900001;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => { const { port } = server.address(); server.close(() => resolve(port)); });
  });
}

const processes = [];
function start(name, args) {
  // Own process group so the whole tree (tools.demo children included) can be stopped.
  const child = spawn(python, args, { cwd: root, detached: true, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (data) => { log = (log + data).slice(-4000); });
  child.stderr.on("data", (data) => { log = (log + data).slice(-4000); });
  child.on("error", (error) => { log += `\nspawn failed: ${error.message}`; });
  const entry = { name, child, log: () => log };
  processes.push(entry);
  return entry;
}
function stopAll(signal = "SIGTERM") {
  for (const { child } of processes) {
    if (child.pid && child.exitCode === null) { try { process.kill(-child.pid, signal); } catch {} }
  }
}
async function stopAndWait() {
  stopAll();
  const deadline = performance.now() + 5000;
  while (performance.now() < deadline && processes.some(({ child }) => child.pid && child.exitCode === null)) await sleep(100);
  stopAll("SIGKILL");
}

async function waitFor(label, check, timeoutMs, intervalMs = 100) {
  const deadline = performance.now() + timeoutMs;
  let last;
  while (performance.now() < deadline) {
    try {
      const value = await check();
      if (value) return value;
    } catch (error) {
      if (error.fatal) throw error;
      last = error;
    }
    await sleep(intervalMs);
  }
  throw new Error(`${label} (waited ${timeoutMs} ms${last ? `; last error: ${last.message}` : ""})`);
}

async function json(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url} HTTP ${response.status}`);
  return response.json();
}

async function waitHttp(url, entry, timeoutMs) {
  return waitFor(`${entry.name} did not serve ${url}`, async () => {
    if (entry.child.exitCode !== null || entry.log().includes("spawn failed")) {
      throw Object.assign(new Error(`${entry.name} exited ${entry.child.exitCode}: ${entry.log()}`), { fatal: true });
    }
    return (await fetch(url)).ok;
  }, timeoutMs, 150);
}

const results = [];
let liveSeen = null;
let hudPage = null; // page whose HUD text is appended to a failure
const controlTraces = new WeakMap();
const scanControlTraces = [];
async function failure(error) {
  const trace = hudPage ? controlTraces.get(hudPage) : null;
  if (trace?.enabled) {
    trace.enabled = false;  // freeze before diagnostic HTTP/DOM reads can overwrite the first cause
    scanControlTraces.push({ state: "failed", firstStop: trace.firstStop, records: [...trace.records],
                            camera: [...trace.camera] });
  }
  const hud = hudPage ? await hudPage.evaluate(() => `${document.querySelector("#rover-message")?.textContent} | ${document.querySelector("#rover-details")?.textContent.split(" · ").at(-1)}`).catch(() => null) : null;
  const control = hudPage ? await json(new URL("/control/status", hudPage.url()).href).catch(() => null) : null;
  return error.message.split("\n")[0] + (hud ? ` [HUD: ${hud}]` : "")
    + (control ? ` [control: ${control.reason}, mode ${control.mode}, seq ${control.seq}, release ${control.requires_release}]` : "")
    + (trace?.firstStop ? ` [first client stop: ${trace.firstStop.reason}; cause ${trace.firstStop.cause}]` : "");
}
// Held-deadman steps get one visible retry: on a loaded machine the viewer's own freshness guards
// (500 ms video, 250 ms display) can invalidate a hold, exactly as they would on a slow rig.
// Fatal errors (a server-side abort) are never retried.
async function step(name, run, { retries = 0 } = {}) {
  const started = performance.now();
  const attempts = [];
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      if (hudPage) await releaseAll(hudPage);
      const detail = await run();
      const note = attempts.length ? ` (passed on retry; first attempt: ${attempts.join("; ")})` : "";
      results.push({ step: name, result: "PASS", ms: Math.round(performance.now() - started), detail: String(detail ?? "") + note });
      return;
    } catch (error) {
      attempts.push(await failure(error));
      if (error.fatal) break;
    }
  }
  results.push({ step: name, result: "FAIL", ms: Math.round(performance.now() - started), detail: attempts.join(" | then: ") });
}

async function openViewer(browser, origin, { injectPerson = () => false, injectNav = () => null, counters = {} } = {}) {
  const page = await browser.newPage({ viewport: { width: 960, height: 600 } });
  const trace = { enabled: false, started: 0, firstStop: null, records: [], camera: [] };
  controlTraces.set(page, trace);
  const record = (kind, fields) => {
    if (!trace.enabled) return;
    const value = { ms: Math.round((performance.now() - trace.started) * 10) / 10, kind, ...fields };
    trace.records.push(value);
    if (trace.records.length > 64) trace.records.shift();
    if (kind === "gate" && trace.firstStop === null) trace.firstStop = value;
  };
  if (traceControl) {
    await page.exposeBinding("__recordControlSafety", (_, value) => {
      if (value.event === "frame") record("frame", { browser_ms: value.browser_ms, age: value.age });
      else record("gate", { browser_ms: value.browser_ms, reason: value.reason, cause: value.cause,
                            needsRelease: value.needsRelease, hardLatch: value.hardLatch });
    });
    await page.addInitScript(() => {
      globalThis.__farsightControlSafetyTrace = value => {
        void globalThis.__recordControlSafety({ ...value, browser_ms: performance.now() }).catch(() => {});
      };
    });
  }
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  counters.frames = 0; counters.detections = 0;
  const cameraRequests = new WeakMap();
  if (traceControl) page.on("request", request => {
    if (request.url().includes("/camera.jpg")) cameraRequests.set(request, performance.now());
  });
  page.on("response", (response) => {
    if (response.url().includes("/camera.jpg") && response.status() === 200) counters.frames++;
    if (!trace.enabled || !response.url().includes("/camera.jpg")) return;
    const at = performance.now(), started = cameraRequests.get(response.request());
    void Promise.all([response.headerValue("x-frame-id"), response.headerValue("x-frame-age-ms"),
                      response.headerValue("x-frame-server-wait-ms")]).then(([id, age, wait]) => {
      if (!trace.enabled) return;
      trace.camera.push({ ms: Math.round((at - trace.started) * 10) / 10, frame: id,
                          serverAge: age === null ? null : Number(age),
                          serverWait: wait === null ? null : Number(wait),
                          requestMs: started === undefined ? null : Math.round((at - started) * 10) / 10,
                          status: response.status() });
      if (trace.camera.length > 32) trace.camera.shift();
    }).catch(() => {});
  });
  // Pass real detector packets through unchanged; when asked, append one clearly synthetic person
  // box (the ray-cast room has no people) so box rendering and click-to-seek can be exercised.
  await page.routeWebSocket("**/ws/detections", (ws) => {
    const server = ws.connectToServer();
    server.onMessage((message) => {
      counters.detections++;
      if (!injectPerson()) return ws.send(message);
      try {
        const packet = JSON.parse(String(message));
        packet.boxes = [...(packet.boxes ?? []), { id: SYNTHETIC_ID, cls: "person", conf: 0.9, x: 0.4, y: 0.25, w: 0.2, h: 0.55, synthetic: true }];
        ws.send(JSON.stringify(packet));
      } catch { ws.send(message); }
    });
  });
  // The simulator has no planner yet; when asked, add a fixture `nav` object (docs/interfaces.md) to
  // its real control status so the path overlay and go-to controls can be exercised end to end.
  await page.routeWebSocket("**/ws/control", (ws) => {
    const server = ws.connectToServer();
    if (traceControl) ws.onMessage((message) => {
      try {
        const packet = JSON.parse(String(message));
        record("out", { seq: packet.seq, mode: packet.mode, deadman: packet.deadman,
                        videoFresh: packet.video_fresh, videoAge: packet.video_age_ms });
      } catch { /* Observation never changes delivery, including malformed input. */ }
      server.send(message);
    });
    server.onMessage((message) => {
      try {
        const packet = JSON.parse(String(message));
        record("in", { seq: packet.seq, mode: packet.mode, reason: packet.reason,
                       requiresRelease: packet.requires_release });
      } catch { /* Observation never changes delivery. */ }
      const nav = injectNav();
      if (!nav) return ws.send(message);
      try { ws.send(JSON.stringify({ ...JSON.parse(String(message)), nav })); } catch { ws.send(message); }
    });
  });
  await page.goto(origin);
  return { page, errors };
}

const bannerKind = (page) => page.locator(".source-banner").getAttribute("data-kind");
async function noteBanner(page) {
  const kind = await bannerKind(page);
  if (kind === "LIVE") liveSeen = kind;
  return kind;
}
const roverDetails = (page) => page.locator("#rover-details").textContent();
const roverMessage = (page) => page.locator("#rover-message").textContent();

async function releaseAll(page) {
  for (const key of ["KeyW", "KeyA", "KeyS", "KeyD", "Space"]) await page.keyboard.up(key);
}
// Like the operator: release, wait until the HUD is ready and the rover has seen the release, then hold Space.
async function holdDeadman(page, origin, ...keys) {
  await releaseAll(page);
  await waitFor("HUD never reached ready-to-drive", async () => /Ready: hold deadman/.test(await page.locator("#rover-message").textContent())
    && (await json(`${origin}/control/status`)).requires_release === false, 4000, 50);
  await page.keyboard.down("Space");
  for (const key of keys) await page.keyboard.down(key);
}

async function main() {
  const [simPort, simJetson, replayPort, replayJetson] = await Promise.all([freePort(), freePort(), freePort(), freePort()]);
  const sim = start("tools.demo --simulate", ["-m", "tools.demo", "--simulate", "--build", "--port", String(simPort), "--jetson-port", String(simJetson)]);
  const replayServer = start("jetson.server --replay", ["-m", "jetson.server", "--replay", "tools/fixtures/replay-demo", "--host", "127.0.0.1", "--port", String(replayJetson)]);
  const simOrigin = `http://127.0.0.1:${simPort}`;
  const replayOrigin = `http://127.0.0.1:${replayPort}`;
  await waitHttp(`${simOrigin}/relay/health`, sim, 60000);
  await waitHttp(`http://127.0.0.1:${replayJetson}/health`, replayServer, 30000);
  const replayRelay = start("operator_relay (replay)", ["-m", "tools.operator_relay", "--upstream", `http://127.0.0.1:${replayJetson}`, "--port", String(replayPort)]);
  await waitHttp(`${replayOrigin}/relay/health`, replayRelay, 30000);

  const browser = await chromium.launch({
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
  });
  try {
    let inject = false, nav = null;
    const counters = {};
    const status = () => json(`${simOrigin}/control/status`);
    const { page, errors } = await openViewer(browser, simOrigin, { injectPerson: () => inject, injectNav: () => nav, counters });
    hudPage = page;

    await step("page loads", async () => {
      await page.locator("#rover-message").waitFor({ timeout: 10000 });
      await page.locator("#rover-camera").waitFor({ state: "attached", timeout: 10000 });
      if (errors.length) throw new Error(`page errors: ${errors.join("; ")}`);
      return `${await page.title()} served by tools.demo relay`;
    });

    await step("source banner says SIMULATED", async () => {
      const health = await json(`${simOrigin}/health`);
      if (health.source !== "SIMULATED" || health.control?.simulated !== true) throw new Error(`server health source=${health.source} simulated=${health.control?.simulated}`);
      await waitFor("banner never became SIMULATED", async () => (await noteBanner(page)) === "SIMULATED", 8000);
      return `banner "${await page.locator(".source-banner strong").textContent()}"; /health source SIMULATED, control.simulated true`;
    });

    await step("video frames arrive", async () => {
      const before = counters.frames;
      await waitFor("fewer than 5 new /camera.jpg frames", () => counters.frames - before >= 5, 8000);
      const age = await waitFor("HUD video age unavailable", async () => (await roverDetails(page)).match(/JPEG video (\d+) ms/), 5000);
      const lit = await page.evaluate(() => {
        const canvas = document.querySelector("#rover-camera");
        const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
        let sum = 0; for (let i = 0; i < data.length; i += 4) sum += data[i] + data[i + 1] + data[i + 2];
        return sum / (data.length / 4);
      });
      if (!(lit > 10)) throw new Error(`camera canvas is blank (mean ${lit.toFixed(1)})`);
      return `${counters.frames - before} frames; HUD video age ${age[1]} ms; canvas mean RGB ${lit.toFixed(0)}`;
    });

    await step("detections render", async () => {
      const real = counters.detections;
      await waitFor("no /ws/detections packets from the server", () => counters.detections - real >= 3, 5000);
      inject = true;
      await waitFor("HUD never showed the injected box", async () => (await roverDetails(page)).match(/(\d+) detections/)?.[1] === "1", 5000);
      // The detector (~3 fps) is slower than the camera (~15 Hz); the nearest same-source packet within
      // a few frames must keep the box on screen between detector frames (#64), not 1 frame in 5.
      let shownSamples = 0;
      for (let i = 0; i < 20; i++) { if (/\b1 detections/.test(await roverDetails(page))) shownSamples++; await sleep(100); }
      if (shownSamples < 18) throw new Error(`box shown in only ${shownSamples}/20 HUD samples over 2 s`);
      return `${counters.detections - real} real detector packets (ray-cast room: 0 people); synthetic box #${SYNTHETIC_ID} rendered → HUD "1 detections" in ${shownSamples}/20 samples over 2 s`;
    });

    await step("deadman + drive moves rover, release stops it", async () => {
      await holdDeadman(page, simOrigin, "KeyW");
      const moving = await waitFor("simulated rover never received forward PWM", async () => {
        const s = await status();
        return s.left_pwm > 0 && s.right_pwm > 0 && s.reason === "active" && s;
      }, 4000, 50);
      const heldAt = performance.now();
      let heldSamples = 0;
      while (performance.now() - heldAt < 3000) {
        const s = await status();
        if (!(s.left_pwm > 0 && s.right_pwm > 0 && s.reason === "active"))
          throw new Error(`held forward drive interrupted after ${Math.round(performance.now() - heldAt)} ms: ${s.reason}, PWM ${s.left_pwm}/${s.right_pwm}`);
        heldSamples++;
        await sleep(25);
      }
      await releaseAll(page);
      const releasedAt = performance.now();
      const stopped = await waitFor("rover did not stop after release", async () => {
        const s = await status();
        return s.left_pwm === 0 && s.right_pwm === 0 && s;
      }, 2000, 20);
      return `PWM ${moving.left_pwm}/${moving.right_pwm}; ${heldSamples} moving samples over 3 s held; 0/0 (${stopped.reason}) ${Math.round(performance.now() - releasedAt)} ms after release (simulated rover)`;
    }, { retries: 1 });

    await step("Explore select", async () => {
      await page.keyboard.press("KeyE");
      await holdDeadman(page, simOrigin);
      const s = await waitFor("server never entered explore", async () => { const v = await status(); return v.mode === "explore" && v; }, 4000, 50);
      const heldAt = performance.now();
      let heldSamples = 0;
      const phases = new Set();
      while (performance.now() - heldAt < 6000) {
        const current = await status();
        if (current.mode !== "explore" || !(current.left_pwm || current.right_pwm))
          throw new Error(`clear-room Explore interrupted after ${Math.round(performance.now() - heldAt)} ms: ${current.reason}, ${current.autonomy_state}, PWM ${current.left_pwm}/${current.right_pwm}`);
        phases.add(current.autonomy_state);
        heldSamples++;
        await sleep(25);
      }
      if (!phases.has("forward") || !phases.has("turning"))
        throw new Error(`Explore did not exercise forward and turn phases: ${[...phases]}`);
      await releaseAll(page);
      await waitFor("explore did not stop on release", async () => { const v = await status(); return v.left_pwm === 0 && v.right_pwm === 0; }, 2000, 20);
      await waitFor("Explore would not disarm", async () => {
        if (await page.locator("#rover-explore").getAttribute("aria-pressed") !== "true") return true;
        await page.keyboard.press("KeyE");
        return false;
      }, 2000, 200);
      return `server mode ${s.mode}; ${heldSamples} moving samples over 6 s, phases ${[...phases]}; stopped on release`;
    }, { retries: 1 });

    await step("Seek select (click synthetic person)", async () => {
      const point = await page.evaluate(() => {
        const canvas = document.querySelector("#rover-camera"), rect = canvas.getBoundingClientRect();
        const scale = Math.min(rect.width / canvas.width, rect.height / canvas.height);
        const left = rect.left + (rect.width - canvas.width * scale) / 2, top = rect.top + (rect.height - canvas.height * scale) / 2;
        return { x: left + 0.5 * canvas.width * scale, y: top + 0.5 * canvas.height * scale };
      });
      // A box is only clickable while a same-source packet within a few frames is younger than 500 ms; retry like an operator would.
      await waitFor("click never armed Seek", async () => {
        if ((await roverDetails(page)).includes(`Seek #${SYNTHETIC_ID} armed`)) return true;
        await page.mouse.click(point.x, point.y);
        return false;
      }, 4000, 250);
      await holdDeadman(page, simOrigin);
      const s = await waitFor("server never entered seek", async () => { const v = await status(); return v.mode === "seek" && v.target_id === SYNTHETIC_ID && v; }, 4000, 50);
      await releaseAll(page);
      await waitFor("seek did not stop on release", async () => { const v = await status(); return v.left_pwm === 0 && v.right_pwm === 0; }, 2000, 20);
      inject = false;
      return `server mode seek target #${s.target_id}, autonomy_state ${s.autonomy_state} (server tracker has no such person)`;
    }, { retries: 1 });

    await step("drill start/score", async () => {
      if (!(await page.locator("#settings").evaluate((d) => d.open))) await page.locator("#settings-toggle").click();
      await page.locator("#start-drill").click();
      const drill = await waitFor("server drill never became active", async () => { const d = await json(`${simOrigin}/mission/drill`); return d.active && d; }, 4000)
        .catch(async (error) => { throw new Error(`${error.message}; UI drill status "${await page.locator("#drill-status").textContent()}"`); });
      const line = await waitFor("drill status never showed a score", async () => (await page.locator("#drill-status").textContent()).match(/victims \d+\/\d+ · e-stops \d+/), 4000);
      return `active, ${drill.remaining_s}s left; UI "${line[0]}"`;
    }, { retries: 1 });

    await step("ask (text path)", async () => {
      const answer = page.locator(".local-assistant p[role=status]").first();
      await page.getByLabel("Question for rover").fill("What do you see?");
      await page.locator(".local-assistant form button").click();
      const text = await waitFor("no assistant answer", async () => { const t = await answer.textContent(); return t && t !== "Asking rover…" && !t.startsWith("Uses observed") && t; }, 15000);
      if (text.startsWith("Assistant unavailable")) throw new Error(text);
      if (!/simulated/i.test(text)) throw new Error(`answer does not say simulated: ${text.slice(0, 120)}`);
      if (!/Offline observation summary · \d+ ms · on rover, no internet/.test(text)) throw new Error(`answer lacks source/timing/offline label: ${text.slice(-120)}`);
      if (await page.locator("#voice-record").isVisible()) throw new Error("voice controls visible although the rover has no Whisper");
      return `"${text.slice(0, 90)}…"`;
    });

    await step("survivor phrase reaches rover through relay (never 405)", async () => {
      const result = await page.evaluate(async () => {
        const response = await fetch("/speak", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phrase: "help_coming", lang: "en" }) });
        return { status: response.status, body: (await response.text()).slice(0, 120) };
      });
      if (result.status === 405 || result.status === 404) throw new Error(`relay did not proxy /speak: ${result.status}`);
      return `upstream status ${result.status}: ${result.body}`;
    });

    await step("mission report opens, labelled SIMULATED", async () => {
      await page.locator("#open-mission-report").click();
      const dialog = page.locator("dialog.mission-report");
      await waitFor("report never loaded", async () => !/Loading/.test(await dialog.locator(".mission-content").textContent()), 6000);
      const summary = await dialog.locator(".mission-content p").first().textContent();
      if (!(await dialog.locator(".mission-simulated").count())) throw new Error("report lacks the SIMULATED label");
      // Always-on control sampler: min range, e-stop count and the release from the drive step.
      const evidence = (await dialog.locator(".mission-summary li").allTextContents()).join(" | ");
      if (!/Min range \d+ cm at \+\d+:\d\d/.test(evidence)) throw new Error(`report lacks min range: ${evidence}`);
      if (!/E-stops \d+/.test(evidence)) throw new Error(`report lacks an e-stop line: ${evidence}`);
      if (!/deadman_released ×\d+/.test(evidence)) throw new Error(`report lacks the drive-step release: ${evidence}`);
      await dialog.getByRole("button", { name: "Close mission report" }).click();
      await page.locator("#close-settings").click();
      return `${summary} · ${evidence}`;
    });

    await step("scan", async () => {
      await waitFor("settings still open", async () => !(await page.locator("#settings").evaluate((d) => d.open)), 2000);
      await holdDeadman(page, simOrigin);
      await waitFor("deadman not ready for scan", async () => { const v = await status(); return v.reason === "active"; }, 4000, 50);
      const trace = controlTraces.get(page);
      Object.assign(trace, { enabled: traceControl, started: performance.now(), firstStop: null, records: [], camera: [] });
      await page.locator("#rover-scan").click();
      const done = await waitFor("scan never completed", async () => {
        const r = await json(`${simOrigin}/scan`);
        // Known open race (docs/judge-demo.md): server can abort at view 0 when a teleop heartbeat lands after POST /scan.
        if (r.state === "aborted") throw Object.assign(new Error(`server aborted scan after ${r.views?.length ?? 0} views: ${r.error}`), { fatal: true });
        return r.state === "complete" && r;
      }, 30000, 100);
      // The viewer reports completion and opens the map before the operator lets go.
      await waitFor("viewer never reported the completed scan", async () => {
        const details = await roverDetails(page);
        // Regression (docs/judge-demo.md): the next held heartbeat replaces "scan_finished" with
        // "release_required" before most status ticks; the viewer must still treat the scan as complete.
        if (/Scan cancelled/.test(details)) throw Object.assign(new Error("viewer cancelled a scan the server completed (status reason release_required replaced scan_finished)"), { fatal: true });
        return /points/.test(details) || /View changed/.test(await roverMessage(page));
      }, 3000);
      await releaseAll(page);
      trace.enabled = false;
      if (traceControl) scanControlTraces.push({ state: "complete", firstStop: trace.firstStop, records: [...trace.records],
                                                camera: [...trace.camera] });
      if (!done.simulated) throw new Error("scan result not flagged simulated");
      return `${done.views?.length} views, ${done.points} points, ${(done.scan_ms / 1000).toFixed(1)} s, simulated=${done.simulated}, backend ${done.backend}`;
    }, { retries: 1 });

    await step("nav path overlay + go-to goal (mocked nav status)", async () => {
      const goals = [], cancels = [];
      await page.route("**/nav/goal", (route) => {
        goals.push({ body: JSON.parse(route.request().postData()), type: route.request().headers()["content-type"] });
        return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ ...nav, state: "planning", goal: goals.at(-1).body, path: [] }) });
      });
      await page.route("**/nav/cancel", (route) => { cancels.push(route.request().method()); return route.fulfill({ contentType: "application/json", body: JSON.stringify({ state: "idle", backend: "nav2" }) }); });
      nav = { state: "driving", mode: "goto", goal: { x: 1.5, z: -1 }, path: [[0, 0], [0.8, -0.4], [1.5, -1]], frontiers: [[2, 2]], backend: "nav2", message: "" };
      try {
        await page.locator("#rover-map").click();
        const line = await waitFor("nav status never shown", async () => {
          const text = await page.locator("#rover-nav").textContent();
          return /^Nav2 · Go-to driving · \d+\.\d m path · 1 frontier/.test(text) && text;
        }, 4000);
        const points = await page.locator("#rover-nav").getAttribute("data-path-points");
        if (points !== "3") throw new Error(`path overlay drew ${points} points, expected 3`);
        const box = await page.locator("#scene canvas").boundingBox();
        await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
        await waitFor("double-click never posted a goal", () => goals.length === 1, 3000);
        const { body, type } = goals[0];
        if (type !== "application/json" || Object.keys(body).join() !== "x,z" || !Object.values(body).every(Number.isFinite))
          throw new Error(`goal request ${type} ${JSON.stringify(body)}`);
        await waitFor("HUD never showed Go-to armed", async () => /Go-to armed/.test(await roverDetails(page)), 2000);
        const server = await status();
        if (server.mode === "goto" || server.left_pwm || server.right_pwm) throw new Error(`goal moved the rover without the deadman: ${JSON.stringify(server)}`);
        await page.locator("#rover-nav-cancel").click();
        await waitFor("cancel never posted", () => cancels.length === 1, 3000);
        await waitFor("Go-to stayed armed after cancel", async () => !/Go-to armed/.test(await roverDetails(page)), 2000);
        return `"${line}" · 3-point path drawn · double-click → POST /nav/goal ${JSON.stringify(body)} · no motion without deadman · cancel → POST /nav/cancel (planner mocked; sim has no nav)`;
      } finally {
        nav = null;
        await page.unroute("**/nav/goal"); await page.unroute("**/nav/cancel");
      }
    });

    if (errors.length) results.push({ step: "no page errors (simulator)", result: "FAIL", ms: 0, detail: errors.join("; ") });
    await page.close();

    const replayCounters = {};
    const replay = await openViewer(browser, replayOrigin, { counters: replayCounters });
    hudPage = replay.page;
    await step("replay mode shows REPLAY, motors inert", async () => {
      const health = await json(`${replayOrigin}/health`);
      if (health.replay !== true) throw new Error(`/health replay=${health.replay}`);
      await waitFor("banner never became REPLAY", async () => (await noteBanner(replay.page)) === "REPLAY", 8000);
      await waitFor("no replay frames", () => replayCounters.frames >= 3, 6000);
      // Replay never reaches "ready", so hold directly; the assertion is that nothing moves.
      await replay.page.keyboard.down("Space");
      await replay.page.keyboard.down("KeyW");
      let maxPwm = 0;
      for (let i = 0; i < 15; i++) {
        const s = await json(`${replayOrigin}/control/status`);
        maxPwm = Math.max(maxPwm, Math.abs(s.left_pwm ?? 0), Math.abs(s.right_pwm ?? 0));
        await sleep(60);
      }
      await releaseAll(replay.page);
      if (maxPwm !== 0) throw new Error(`replay produced PWM ${maxPwm}`);
      return `banner REPLAY; ${replayCounters.frames} recorded frames; deadman+W kept PWM 0`;
    });
    if (replay.errors.length) results.push({ step: "no page errors (replay)", result: "FAIL", ms: 0, detail: replay.errors.join("; ") });

    results.push(liveSeen
      ? { step: "never claims LIVE", result: "FAIL", ms: 0, detail: "banner showed LIVE during a simulated run" }
      : { step: "never claims LIVE", result: "PASS", ms: 0, detail: "banner was SIMULATED/REPLAY at every check" });
  } finally {
    await browser.close();
  }
}

process.on("SIGINT", () => { stopAll(); process.exit(130); });
const began = performance.now();
let fatal = null;
try {
  await main();
} catch (error) {
  fatal = error;
} finally {
  await stopAndWait();
}
const width = Math.max(...results.map((r) => r.step.length), 4);
console.log("\nJudge-flow smoke (SIMULATED + REPLAY; no hardware, not live evidence)\n");
for (const r of results) console.log(`${r.result}  ${r.step.padEnd(width)}  ${String(r.ms).padStart(5)} ms  ${r.detail}`);
const failed = results.filter((r) => r.result !== "PASS").length;
if (fatal) console.log(`\nSETUP FAILED: ${fatal.message}\n${processes.map((p) => `--- ${p.name}\n${p.log()}`).join("\n")}`);
console.log(`\n${results.length - failed}/${results.length} passed in ${((performance.now() - began) / 1000).toFixed(1)} s`);
if (outPath) writeFileSync(outPath, JSON.stringify({ environment: "Chromium/SwiftShader; desktop controls; tools.demo --simulate and --replay fixture; synthetic person box for render/seek", results, scanControlTraces, fatal: fatal?.message ?? null }, null, 2));
else if (scanControlTraces.length) console.log(`Scan control trace: ${JSON.stringify(scanControlTraces)}`);
process.exit(fatal || failed ? 1 : 0);
