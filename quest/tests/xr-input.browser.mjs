import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "@playwright/test";

// Software-only rehearsal. No backend or hardware connection is needed.
const origin = "http://127.0.0.1:5257";
const server = spawn(
  process.execPath,
  [
    fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url)),
    "--host",
    "127.0.0.1",
    "--port",
    "5257",
    "--strictPort",
  ],
  { cwd: new URL("..", import.meta.url), stdio: "pipe" },
);
let serverLog = "";
server.stdout.on("data", (data) => {
  serverLog += data;
});
server.stderr.on("data", (data) => {
  serverLog += data;
});
let browser;
const mapTimers = new Set();
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error(serverLog);
    try {
      ready = (await fetch(origin)).ok;
    } catch {}
    if (ready) break;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.ok(ready, `Vite did not start: ${serverLog}`);
  browser = await chromium.launch({
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
    ],
  });
  const page = await browser.newPage();
  await page.routeWebSocket("**/ws/**", (socket) => socket.close());
  await page.route("**/camera.jpg", (route) =>
    route.fulfill({ status: 503, body: "" }),
  );
  // Explicit simulator health: the banner must say SIMULATED, never LIVE.
  await page.route("**/health", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ replay: false, rover: true, camera: true, camera_source: "SIMULATED", control: { simulated: true } }),
    }),
  );
  let asks = 0;
  await page.route("**/ask", (route) => {
    asks++;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ question: "who is there", answer: "One person ahead.", audio_url: null, action: { type: "seek", target_id: 7 } }),
    });
  });
  await page.goto(origin);
  const result = await page.evaluate(async () => {
    // Stub microphone and recorder before construction (headless Chromium may lack both).
    const permissions = [], recorders = [], tracks = [];
    const fakeStream = () => {
      const track = { stopped: 0, stop() { this.stopped++; } };
      tracks.push(track);
      return { getTracks: () => [track] };
    };
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      getUserMedia: () => new Promise((resolve, reject) => permissions.push({ resolve, reject })),
    } });
    window.MediaRecorder = class {
      static isTypeSupported(type) { return type.startsWith("audio/webm"); }
      constructor(stream, options) { this.mimeType = options?.mimeType ?? "audio/webm"; this.state = "inactive"; recorders.push(this); }
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        setTimeout(() => { this.ondataavailable?.({ data: new Blob(["audio"], { type: "audio/webm" }) }); this.onstop?.(); }, 0);
      }
    };
    const { RoverExperience } = await import("/src/rover.ts");
    const THREE = await import("/node_modules/three/build/three.module.js");
    const renderer = new THREE.WebGLRenderer({ preserveDrawingBuffer: true });
    renderer.setSize(320, 240);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, 4 / 3, 0.01, 100);
    const subject = new RoverExperience(
      renderer,
      scene,
      camera,
      () => "camera",
      () => {},
    );
    const pixel = () => {
      const value = new Uint8Array(4);
      const gl = renderer.getContext();
      gl.readPixels(160, 120, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, value);
      return [...value];
    };
    const draw = async (width, height, color) => {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      context.fillStyle = color;
      context.fillRect(0, 0, width, height);
      const image = new Image();
      image.src = canvas.toDataURL();
      await image.decode();
      const previous = subject.cameraTexture;
      subject.frame = image;
      subject.drawCamera(performance.now());
      subject.cockpit.visible = true;
      renderer.render(scene, camera);
      return {
        pixel: pixel(),
        replaced: previous !== subject.cameraTexture,
        width: subject.canvas.width,
        height: subject.canvas.height,
      };
    };
    const first = await draw(640, 480, "#f02010");
    const second = await draw(1280, 720, "#1020f0");
    const reference = {};
    const right = {
      handedness: "right",
      gripSpace: {},
      gamepad: {
        connected: true,
        axes: [0, 0, 0, 0],
        buttons: [{ pressed: true }],
      },
    };
    const left = {
      handedness: "left",
      gripSpace: {},
      gamepad: {
        connected: true,
        axes: [0, 0, 0, -1],
        buttons: [{ pressed: false }],
      },
    };
    const session = { visibilityState: "visible", inputSources: [right, left] };
    const xr = renderer.xr;
    xr.isPresenting = true;
    xr.getSession = () => session;
    xr.getReferenceSpace = () => reference;
    xr.getCamera = () => camera;
    let loseLeft = false,
      viewerY = 0,
      viewerOn = true;
    const frame = {
      getViewerPose: () =>
        viewerOn
          ? {
              transform: {
                orientation: { x: 0, y: 0, z: 0, w: 1 },
                position: { x: 0, y: viewerY, z: 0 },
              },
            }
          : null,
      getPose: (space) => (loseLeft && space === left.gripSpace ? null : {}),
    };
    subject.update(100, frame);
    const tracked = {
      valid: subject.inputValid,
      held: subject.input.held,
      sampledAt: subject.lastXRAt,
    };
    loseLeft = true;
    viewerY = 0.25;
    subject.update(110, frame);
    const lost = {
      valid: subject.inputValid,
      held: subject.input.held,
      v: subject.input.v,
      needsRelease: subject.gate.needsRelease,
      stale: !Number.isFinite(subject.lastXRAt),
      displayVisible: subject.cockpit.visible,
      headY: subject.cockpit.position.y,
    };
    loseLeft = false;
    subject.update(120, frame);
    const recovered = {
      valid: subject.inputValid,
      held: subject.input.held,
      needsRelease: subject.gate.needsRelease,
    };
    right.gamepad.connected = false;
    subject.update(130, frame);
    const disconnected = { valid: subject.inputValid, v: subject.input.v };
    right.gamepad.connected = true;
    subject.update(140);
    const missingFrame = { valid: subject.inputValid, v: subject.input.v };
    const arm = (now) => {
      subject.gate.acceptStatus(
        {
          session_id: "software-only",
          permit: "test-permit",
          permit_ms: 250,
          rover: true,
        },
        now,
      );
      subject.gate.acceptFrame(String(now), 0, now);
      right.gamepad.buttons[0].pressed = false;
      subject.update(now, frame);
      subject.gate.decide(
        false,
        now,
        true,
        subject.inputValid,
        subject.lastXRAt,
      );
      right.gamepad.buttons[0].pressed = true;
      subject.update(now + 1, frame);
      return subject.gate.decide(
        true,
        now + 1,
        true,
        subject.inputValid,
        subject.lastXRAt,
      );
    };
    const initiallyArmed = arm(150);
    right.gamepad.axes = [0, 0, 0, -1];
    session.inputSources = [right];
    subject.update(170, frame);
    const removedLeft = {
      initiallyArmed,
      moving: subject.gate.decide(
        true,
        170,
        true,
        subject.inputValid,
        subject.lastXRAt,
      ),
      needsRelease: subject.gate.needsRelease,
    };
    session.inputSources = [right, left];
    const rearmed = arm(180);
    const replacement = {
      ...right,
      gripSpace: {},
      gamepad: { ...right.gamepad, buttons: [{ pressed: true }] },
    };
    session.inputSources = [replacement, left];
    subject.update(200, frame);
    const replacedRight = {
      rearmed,
      moving: subject.gate.decide(
        true,
        200,
        true,
        subject.inputValid,
        subject.lastXRAt,
      ),
      needsRelease: subject.gate.needsRelease,
    };
    viewerOn = false;
    subject.update(210, frame);
    const missingViewer = {
      valid: subject.inputValid,
      displayVisible: subject.cockpit.visible,
      v: subject.input.v,
    };
    viewerOn = true;
    session.inputSources = [right, left];
    const rearmedVisible = arm(220);
    session.visibilityState = "visible-blurred";
    subject.update(230, frame);
    const blurred = {
      rearmedVisible,
      valid: subject.inputValid,
      moving: subject.gate.decide(true, 230, true, subject.inputValid, subject.lastXRAt),
    };
    session.visibilityState = "visible";
    subject.update(240, frame);
    const refocused = {
      held: subject.input.held,
      moving: subject.gate.decide(true, 240, true, subject.inputValid, subject.lastXRAt),
      needsRelease: subject.gate.needsRelease,
    };
    // Left Y (buttons[5]) held across tracking loss must not fire scan on recovery.
    let scans = 0;
    subject.startScan = async () => { scans++; };
    left.gamepad.buttons = [0, 1, 2, 3, 4, 5].map(() => ({ pressed: false }));
    left.gamepad.buttons[5].pressed = true;
    loseLeft = true;
    subject.update(250, frame);
    loseLeft = false;
    subject.update(260, frame);
    const scanWhileStillHeld = scans;
    left.gamepad.buttons[5].pressed = false;
    subject.update(270, frame);
    left.gamepad.buttons[5].pressed = true;
    subject.update(280, frame);
    const scanLatch = { scanWhileStillHeld, scanAfterRelease: scans };

    const grant = (label) => {
      const next = permissions.shift();
      if (!next) throw new Error(`No microphone request at ${label}: phase ${subject.voice.phase}, ${subject.voice.statusText}`);
      return next;
    };
    const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
    const voice = subject.voice;
    left.gamepad.buttons[5].pressed = false;
    left.gamepad.axes = [0, 0, 0, 0]; // a deflected stick would (correctly) cancel a confirmed Seek
    subject.update(290, frame);
    const offPress = (() => {
      left.gamepad.buttons[0].pressed = true; subject.update(292, frame);
      left.gamepad.buttons[0].pressed = false; subject.update(294, frame);
      return { permissionCalls: permissions.length, phase: voice.phase };
    })();
    // main.ts also mounts a viewer; act on the subject's own controls.
    [...document.querySelectorAll("#voice-vr-enable")].at(-1).click();
    grant("enable").resolve(fakeStream()); await settle();
    const enabled = { vrEnabled: voice.vrEnabled, probeStopped: tracks[0].stopped };
    const armedBeforeTalk = arm(300);
    left.gamepad.buttons[0].pressed = true;
    subject.update(310, frame);
    const listeningStart = {
      armedBeforeTalk,
      capturing: voice.capturing,
      needsRelease: subject.gate.needsRelease,
      moving: subject.gate.decide(true, 310, true, subject.inputValid, subject.lastXRAt),
      panelVisible: (subject.drawHUD(310), subject.voicePanel.visible),
    };
    grant("listen").resolve(fakeStream()); await settle();
    const listening = { phase: voice.phase, recorder: recorders.at(-1).state };
    left.gamepad.buttons[0].pressed = false;
    subject.update(320, frame);
    const releasedTalk = { trackStopped: tracks.at(-1).stopped > 0, recorder: recorders.at(-1).state };
    await settle(); await settle();
    subject.update(330, frame);
    const answered = {
      ...releasedTalk,
      phase: voice.phase,
      answer: voice.answerText,
      pending: voice.pendingAction,
      // Right trigger stayed held throughout: finishing the question cannot resume driving.
      moving: subject.gate.decide(true, 330, true, subject.inputValid, subject.lastXRAt),
      autonomy: subject.autonomy.mode,
    };
    left.gamepad.buttons[0].pressed = true;
    subject.update(340, frame);
    const confirmed = {
      mode: subject.autonomy.mode,
      target: subject.autonomy.targetId,
      pending: voice.pendingAction,
      permissionCalls: permissions.length,
      moving: subject.gate.decide(true, 340, true, subject.inputValid, subject.lastXRAt),
    };
    left.gamepad.buttons[0].pressed = false;
    subject.update(350, frame);
    const askCountAfterAnswer = recorders.length;
    left.gamepad.buttons[0].pressed = true;
    subject.update(360, frame);
    grant("deny").reject(new DOMException("Permission denied", "NotAllowedError")); await settle();
    const denied = { phase: voice.phase, capturing: voice.capturing, recorders: recorders.length - askCountAfterAnswer };
    left.gamepad.buttons[0].pressed = false;
    subject.update(370, frame);
    left.gamepad.buttons[0].pressed = true;
    subject.update(380, frame);
    grant("loss").resolve(fakeStream()); await settle();
    const beforeLoss = voice.phase;
    loseLeft = true;
    subject.update(390, frame);
    await settle();
    const trackingLoss = { beforeLoss, phase: voice.phase, trackStopped: tracks.at(-1).stopped > 0, recorder: recorders.at(-1).state };
    loseLeft = false;
    subject.update(400, frame);
    const heldThroughLoss = { capturing: voice.capturing, permissionCalls: permissions.length };
    left.gamepad.buttons[0].pressed = false;
    subject.update(410, frame);

    // Source banner: server truth only, mirrored on desktop DOM and the headset panel.
    const banner = subject.sourceBanner;
    subject.drawHUD(420);
    const simulated = { kind: banner.dataset.kind, title: banner.querySelector("strong").textContent, vr: subject.bannerPanel.visible && subject.bannerPanel.parent === subject.cockpit };
    // The headset banner repaints on a later frame than the HUD, and never for unchanged text.
    const queued = !!subject.bannerPending;
    subject.bannerPending.at = -Infinity;
    subject.paint(421);
    const painted = !subject.bannerPending;
    const bannerPixel = [...subject.bannerCanvas.getContext("2d").getImageData(1010, 100, 1, 1).data].slice(0, 3);
    subject.drawHUD(421); // identical text, including the displayed age
    const headsetBanner = { queued, painted, bannerPixel, unchangedQueued: !!subject.bannerPending };
    subject.sourceTruth.accept({ replay: true }, 430);
    subject.drawHUD(430);
    const replay = banner.dataset.kind;
    subject.sourceTruth.accept({ replay: false, rover: true, camera: true, camera_source: "USB CAMERA", control: { simulated: false } }, 440);
    subject.status = { ...subject.status, simulated: false };
    subject.frameSource = "USB CAMERA"; subject.frameId = "9001";
    subject.gate.acceptFrame("9001", 40, 440);
    subject.drawHUD(440);
    const live = { kind: banner.dataset.kind, detail: banner.querySelector("span").textContent };
    subject.drawHUD(9000);
    const liveExpired = banner.dataset.kind;
    xr.isPresenting = false;
    subject.dispose();
    renderer.dispose();
    return {
      first,
      second,
      tracked,
      lost,
      recovered,
      disconnected,
      missingFrame,
      removedLeft,
      replacedRight,
      missingViewer,
      blurred,
      refocused,
      scanLatch,
      offPress,
      enabled,
      listeningStart,
      listening,
      answered,
      confirmed,
      denied,
      trackingLoss,
      heldThroughLoss,
      banners: { simulated, replay, live, liveExpired },
      headsetBanner,
    };
  });
  assert.deepEqual(result.headsetBanner, { queued: true, painted: true, bannerPixel: [0x7c, 0x4a, 0x03], unchangedQueued: false });
  assert.ok(result.first.pixel[0] > 200);
  assert.ok(result.second.pixel[2] > 200);
  assert.ok(result.second.pixel[0] < 50);
  assert.equal(result.second.replaced, true);
  assert.deepEqual([result.second.width, result.second.height], [1280, 720]);
  assert.deepEqual(result.tracked, { valid: true, held: true, sampledAt: 100 });
  assert.deepEqual(result.lost, {
    valid: false,
    held: true,
    v: 0,
    needsRelease: true,
    stale: true,
    displayVisible: true,
    headY: 0.25,
  });
  assert.deepEqual(result.recovered, {
    valid: true,
    held: true,
    needsRelease: true,
  });
  assert.deepEqual(result.disconnected, { valid: false, v: 0 });
  assert.deepEqual(result.missingFrame, { valid: false, v: 0 });
  assert.deepEqual(result.removedLeft, {
    initiallyArmed: true,
    moving: false,
    needsRelease: true,
  });
  assert.deepEqual(result.replacedRight, {
    rearmed: true,
    moving: false,
    needsRelease: true,
  });
  assert.deepEqual(result.missingViewer, {
    valid: false,
    displayVisible: false,
    v: 0,
  });
  assert.deepEqual(result.blurred, { rearmedVisible: true, valid: false, moving: false });
  assert.deepEqual(result.refocused, { held: true, moving: false, needsRelease: true });
  assert.deepEqual(result.scanLatch, { scanWhileStillHeld: 0, scanAfterRelease: 1 });
  assert.deepEqual(result.offPress, { permissionCalls: 0, phase: "error" });
  assert.deepEqual(result.enabled, { vrEnabled: true, probeStopped: 1 });
  assert.deepEqual(result.listeningStart, { armedBeforeTalk: true, capturing: true, needsRelease: true, moving: false, panelVisible: true });
  assert.deepEqual(result.listening, { phase: "listening", recorder: "recording" });
  assert.deepEqual(result.answered, {
    trackStopped: true, recorder: "inactive", phase: "answer", answer: "One person ahead.",
    pending: { type: "seek", target_id: 7 }, moving: false, autonomy: "teleop",
  });
  assert.equal(asks, 1);
  assert.deepEqual(result.confirmed, { mode: "seek", target: 7, pending: null, permissionCalls: 0, moving: false });
  assert.deepEqual(result.denied, { phase: "error", capturing: false, recorders: 0 });
  assert.deepEqual(result.trackingLoss, { beforeLoss: "listening", phase: "error", trackStopped: true, recorder: "inactive" });
  assert.deepEqual(result.heldThroughLoss, { capturing: false, permissionCalls: 0 });
  assert.equal(asks, 1, "cancelled and denied questions never reach /ask");
  assert.deepEqual(result.banners.simulated, { kind: "SIMULATED", title: "SIMULATED", vr: true });
  assert.equal(result.banners.replay, "REPLAY");
  assert.equal(result.banners.live.kind, "LIVE");
  assert.match(result.banners.live.detail, /USB CAMERA · frame 9001 · 40 ms/);
  assert.equal(result.banners.liveExpired, "UNVERIFIED");
  // Exercise the real main viewer's transfer scheduling with fixture packets.
  // Deadman state is injected; this does not qualify physical input or motion.
  let pointPulls = 0, meshPulls = 0, jpegPulls = 0, mapCloses = 0;
  const mapUrls = [];
  await page.route("**/map/points.bin", route => {
    pointPulls++;
    return route.fulfill({ contentType: "application/octet-stream", body: Buffer.alloc(4) });
  });
  await page.route("**/map/mesh.bin", route => {
    meshPulls++;
    return route.fulfill({ contentType: "application/octet-stream", body: Buffer.alloc(8) });
  });
  await page.route("**/camera.jpg", route => {
    jpegPulls++;
    return route.fulfill({ status: 503, body: "" });
  });
  await page.routeWebSocket("**/ws/map?points=http", socket => {
    mapUrls.push(socket.url());
    let revision = 0;
    const timer = setInterval(() => socket.send(JSON.stringify({
      type: "status", source: "SIMULATED", device: "BUDGET FIXTURE", connected: true,
      fresh: true, generation: 1, revision: revision++, mesh_revision: revision,
      points_transport: "http", points: 0, triangles: 0, frames: revision,
      rejected: 0, dropped: 0, processing_ms: 0, age_ms: 0, tracking: "normal",
      position: [0, 0, 0], transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
      depth_range: null, reason: null,
    })), 50);
    mapTimers.add(timer);
    socket.onClose(() => { clearInterval(timer); mapTimers.delete(timer); mapCloses++; });
  });
  await page.locator("#settings-toggle").click();
  await page.getByText("H.264 video", { exact: false }).locator("input").first().check();
  await page.waitForTimeout(350);
  const jpegBefore = jpegPulls;
  await page.waitForTimeout(2200);
  assert.equal(jpegPulls, jpegBefore, "H.264 suppresses both JPEG polling paths");
  assert.ok(pointPulls > 0 && pointPulls <= 1, `bounded point pulls: ${pointPulls}`);
  assert.ok(meshPulls > 0 && meshPulls <= 1, `bounded mesh pulls: ${meshPulls}`);
  assert.ok(mapUrls.every(url => url.endsWith("?points=http")));
  await page.evaluate(async () => {
    const { RoverExperience } = await import("/src/rover.ts");
    window.budgetDeadmanDescriptor = Object.getOwnPropertyDescriptor(RoverExperience.prototype, "deadmanHeld");
    Object.defineProperty(RoverExperience.prototype, "deadmanHeld", { configurable: true, get: () => true });
  });
  await page.waitForTimeout(150);
  const pausedPulls = [pointPulls, meshPulls];
  await page.waitForTimeout(1200);
  assert.deepEqual([pointPulls, meshPulls], pausedPulls, "held deadman pauses map HTTP pulls");
  assert.ok(mapCloses > 0, "held deadman closes map status socket");
  await page.evaluate(async () => {
    const { RoverExperience } = await import("/src/rover.ts");
    Object.defineProperty(RoverExperience.prototype, "deadmanHeld", window.budgetDeadmanDescriptor);
  });
  await page.waitForTimeout(600);
  assert.ok(mapUrls.length >= 2, "released deadman resumes authoritative map subscription");
  result.cellularTransfers = { pointPulls, meshPulls, jpegSuppressed: true, deadmanPause: true, resumed: true };
  const report = {
    passed: true,
    environment:
      "Chromium/SwiftShader; simulated XR poses; generated camera pixels; no rover backend",
    ...result,
  };
  console.log(JSON.stringify(report, null, 2));
} finally {
  for (const timer of mapTimers) clearInterval(timer);
  await browser?.close();
  server.kill("SIGTERM");
}
