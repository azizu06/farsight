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
      // Whisper present: voice controls only appear when the rover can transcribe.
      body: JSON.stringify({ replay: false, rover: true, camera: true, camera_source: "SIMULATED", control: { simulated: true }, assistant: { whisper: true } }),
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
    // The mocked /health reports Whisper; apply it now instead of racing the async health poll.
    subject.voice.setAvailable(true);
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

  // Camera mount: ?mount=fixed persists, wins over a rover that reports a pan servo,
  // and keeps head yaw out of the pan command so the server never blocks forward.
  const mountContext = await browser.newContext();
  const controlMessages = [];
  await mountContext.routeWebSocket("**/ws/**", (socket) => {
    if (!socket.url().endsWith("/ws/control")) return socket.close();
    let session = `mount-${controlMessages.length}-${Math.random()}`;
    socket.onMessage((data) => {
      const message = JSON.parse(String(data));
      controlMessages.push(message);
      // Mirrors jetson/control/state.py forward_blocked(): pan off-center blocks forward.
      const blocked = Math.abs(message.pan) > 10;
      socket.send(JSON.stringify({
        type: "status", session_id: session, permit: `permit-${message.seq}`, permit_ms: 250, seq: message.seq,
        rover: true, simulated: true, range_cm: 120, estop: blocked,
        reason: message.deadman && message.v > 0 && blocked ? "forward_blocked" : null,
      }));
    });
  });
  await mountContext.route("**/camera.jpg", (route) => route.fulfill({ status: 503, body: "" }));
  let reportsPan = true;
  await mountContext.route("**/health", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ replay: false, rover: true, camera: true, camera_source: "SIMULATED", control: { simulated: true },
      ...(reportsPan === null ? {} : { camera_pan: reportsPan }) }),
  }));
  const mountPage = await mountContext.newPage();
  const mountValue = () => mountPage.locator('select[aria-label="Camera mounting mode"]').first().inputValue();
  await mountPage.goto(`${origin}/?mount=fixed`);
  await mountPage.waitForTimeout(500);
  assert.equal(await mountValue(), "fixed", "?mount=fixed beats camera_pan:true");
  await mountPage.goto(origin);
  await mountPage.waitForTimeout(500);
  assert.equal(await mountValue(), "fixed", "reload without the query keeps the saved mount");
  await mountPage.evaluate(async () => {
    const { RoverExperience } = await import("/src/rover.ts");
    const THREE = await import("/node_modules/three/build/three.module.js");
    const renderer = new THREE.WebGLRenderer();
    const camera = new THREE.PerspectiveCamera(75, 4 / 3, 0.01, 100);
    const subject = new RoverExperience(renderer, new THREE.Scene(), camera, () => "camera", () => {});
    const right = { handedness: "right", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: [{ pressed: false }] } };
    const left = { handedness: "left", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, -1], buttons: [{ pressed: false }] } };
    const session = { visibilityState: "visible", inputSources: [right, left] };
    renderer.xr.isPresenting = true;
    renderer.xr.getSession = () => session;
    renderer.xr.getReferenceSpace = () => ({});
    renderer.xr.getCamera = () => camera;
    const head = { orientation: { x: 0, y: 0, z: 0, w: 1 }, position: { x: 0, y: 0, z: 0 } };
    const frame = { getViewerPose: () => ({ transform: head }), getPose: () => ({}) };
    let frameId = 0;
    // Deadman and head pose are injected; camera freshness is faked. Software evidence only.
    const drive = async (ms, held) => {
      const stops = new Set(), end = performance.now() + ms;
      while (performance.now() < end) {
        right.gamepad.buttons[0].pressed = held;
        subject.gate.acceptFrame(String(++frameId), 0, performance.now());
        subject.lastCanvasDraw = performance.now();
        subject.update(performance.now(), frame);
        stops.add(subject.stopKey);
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      return [...stops];
    };
    window.mountTest = { subject, drive, head };
    await drive(100, false); // recenter on the forward-facing head
    const yaw = (40 * Math.PI) / 180;
    Object.assign(head.orientation, { y: Math.sin(yaw / 2), w: Math.cos(yaw / 2) });
    await drive(400, false);
  });
  const driveMount = async () => {
    controlMessages.length = 0;
    const stops = await mountPage.evaluate(() => window.mountTest.drive(900, true));
    const drove = controlMessages.filter((message) => message.deadman && message.v > 0);
    return { stops, drove, pans: [...new Set(controlMessages.map((message) => Math.round(message.pan)))] };
  };
  const fixedDrive = await driveMount();
  assert.equal(await mountPage.evaluate(() => window.mountTest.subject.mountSelect.value), "fixed");
  assert.ok(fixedDrive.drove.length >= 5, `held deadman drove forward: ${fixedDrive.drove.length} commands; stops ${fixedDrive.stops}`);
  assert.deepEqual(fixedDrive.pans, [0], "fixed mount sends pan 0 despite 40° head yaw");
  assert.ok(!fixedDrive.stops.some((key) => key.includes("Forward blocked")), `no forward_blocked stop: ${fixedDrive.stops}`);
  // Negative control: the same 40° yaw in servo mode is sent as pan and blocks forward.
  await mountPage.evaluate(async () => {
    const { subject, drive } = window.mountTest;
    subject.mountSelect.value = "servo";
    subject.mountSelect.dispatchEvent(new Event("change"));
    await drive(400, false);
  });
  const servoDrive = await driveMount();
  assert.ok(servoDrive.drove.length >= 5 && servoDrive.drove.every((message) => Math.round(message.pan) === -40),
    `servo mount sends head yaw as pan: ${servoDrive.pans}`);
  assert.ok(servoDrive.stops.some((key) => key.includes("Forward blocked")), "servo yaw shows forward_blocked");
  await mountPage.close();
  // No query and nothing saved: the rover's camera_pan picks the default mount.
  const freshMount = async (cameraPan) => {
    const context = await browser.newContext();
    await context.routeWebSocket("**/ws/**", (socket) => socket.close());
    await context.route("**/camera.jpg", (route) => route.fulfill({ status: 503, body: "" }));
    await context.route("**/health", (route) => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ rover: true, camera: true, camera_source: "SIMULATED", control: { simulated: true },
        ...(cameraPan === undefined ? {} : { camera_pan: cameraPan }) }),
    }));
    const fresh = await context.newPage();
    await fresh.goto(origin);
    await fresh.waitForTimeout(500);
    const value = await fresh.locator('select[aria-label="Camera mounting mode"]').inputValue();
    const hud = await fresh.locator("#rover-details").textContent();
    await context.close();
    return { value, hud };
  };
  const noPan = await freshMount(false);
  assert.equal(noPan.value, "fixed", "camera_pan:false defaults to fixed");
  assert.match(noPan.hud, /Fixed mount · digital pan 0° · JPEG video/);
  assert.equal((await freshMount(true)).value, "servo");
  assert.equal((await freshMount(undefined)).value, "servo", "older servers keep the servo default");
  await mountContext.close();
  result.cameraMount = {
    queryBeatsRover: true, reloadKeepsChoice: true, roverDefault: "fixed when camera_pan is false",
    fixedYaw40: { commands: fixedDrive.drove.length, pans: fixedDrive.pans, forwardBlocked: false },
    servoYaw40: { commands: servoDrive.drove.length, pans: servoDrive.pans, forwardBlocked: true },
  };

  // Navigation view: mocked `nav` in control status (docs/interfaces.md). The planner, Nav2 and
  // the rover are absent; this proves drawing, goal JSON and deadman gating in software only.
  const navContext = await browser.newContext({ viewport: { width: 960, height: 600 } });
  const navControl = [], goals = [], cancels = [];
  let navFixture = { state: "driving", mode: "goto", goal: { x: 2, z: 0 }, path: [[0, 0], [1, 0], [2, 0]],
    frontiers: [[3, 1], [-1, 2]], backend: "nav2", message: "" };
  await navContext.routeWebSocket("**/ws/**", (socket) => {
    if (!socket.url().endsWith("/ws/control")) return socket.close();
    const session = `nav-${Math.random()}`;
    socket.onMessage((data) => {
      const message = JSON.parse(String(data));
      navControl.push(message);
      socket.send(JSON.stringify({ type: "control_status", session_id: session, permit: `permit-${message.seq}`, permit_ms: 250,
        seq: message.seq, rover: true, simulated: true, range_cm: 150, estop: false, reason: null, mode: message.mode ?? "teleop", nav: navFixture }));
    });
  });
  await navContext.route("**/camera.jpg", (route) => route.fulfill({ status: 503, body: "" }));
  await navContext.route("**/health", (route) => route.fulfill({ contentType: "application/json",
    body: JSON.stringify({ replay: false, rover: true, camera: true, camera_source: "SIMULATED", control: { simulated: true } }) }));
  await navContext.route("**/nav/goal", (route) => {
    const body = JSON.parse(route.request().postData());
    goals.push({ body, contentType: route.request().headers()["content-type"] });
    navFixture = { ...navFixture, state: "planning", goal: body, path: [] };
    return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(navFixture) });
  });
  await navContext.route("**/nav/cancel", (route) => {
    cancels.push(route.request().method());
    navFixture = { ...navFixture, state: "idle", mode: null, goal: null, path: [] };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(navFixture) });
  });
  const navPage = await navContext.newPage();
  const navErrors = [];
  navPage.on("pageerror", (error) => navErrors.push(error.message));
  await navPage.goto(`${origin}/?view=map`);
  const navLine = navPage.locator("#rover-nav");
  await navLine.waitFor({ state: "visible", timeout: 5000 });
  assert.match(await navLine.textContent(), /^Nav2 · Go-to driving · 2\.0 m path · 2 frontiers/);
  assert.equal(await navLine.getAttribute("data-path-points"), "3", "desktop map drew the 3-point path");
  assert.equal(await navLine.getAttribute("data-backend"), "nav2");
  // Desktop: double-click the floor under the 3D view's centre (camera looks at the origin).
  const box = await navPage.locator("#scene canvas").boundingBox();
  await navPage.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
  await navPage.waitForTimeout(300);
  assert.equal(goals.length, 1, "double-click posts one goal");
  assert.deepEqual(Object.keys(goals[0].body), ["x", "z"]);
  assert.ok(Math.abs(goals[0].body.x) < 0.05 && Math.abs(goals[0].body.z) < 0.05, `centre goal near origin: ${JSON.stringify(goals[0].body)}`);
  assert.equal(goals[0].contentType, "application/json");
  assert.match(await navPage.locator("#rover-details").textContent(), /Go-to armed/);
  await navPage.locator("#rover-nav-cancel").click();
  await navPage.waitForTimeout(200);
  await navPage.keyboard.press("KeyC");
  await navPage.waitForTimeout(200);
  assert.deepEqual(cancels, ["POST", "POST"], "cancel button and C both POST /nav/cancel");
  assert.doesNotMatch(await navPage.locator("#rover-details").textContent(), /Go-to armed/);
  const desktopGoal = goals[0].body;

  navFixture = { state: "driving", mode: "goto", goal: { x: 2, z: 0 }, path: [[0, 0], [1, 0], [2, 0]],
    frontiers: [[3, 1], [-1, 2]], backend: "nav2", message: "" };
  goals.length = 0; cancels.length = 0;
  const vrNav = await navPage.evaluate(async () => {
    const { RoverExperience } = await import("/src/rover.ts");
    const THREE = await import("/node_modules/three/build/three.module.js");
    const renderer = new THREE.WebGLRenderer({ preserveDrawingBuffer: true });
    renderer.setSize(200, 200);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 100);
    const subject = new RoverExperience(renderer, scene, camera, () => "camera", () => {});
    scene.add(subject.navLayer);
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    for (let i = 0; i < 50 && subject.nav?.state !== "driving"; i++) await sleep(20);
    subject.update(performance.now());
    // Top-down render: the glowing path runs along x at z = 0 from 0 to 2 m.
    camera.position.set(1, 3, 0); camera.up.set(0, 0, -1); camera.lookAt(1, 0, 0);
    renderer.render(scene, camera);
    const gl = renderer.getContext();
    const pixel = (x, y) => { const value = new Uint8Array(4); gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, value); return [...value]; };
    const drawn = { points: subject.navOverlay.path.userData.points, frontiers: subject.navOverlay.frontiers.count,
      goal: subject.navOverlay.goal.visible, onPath: pixel(100, 100), offPath: pixel(100, 20) };
    const right = { handedness: "right", gripSpace: {}, targetRaySpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0],
      buttons: [0, 1, 2, 3, 4, 5].map(() => ({ pressed: false })) } };
    const left = { handedness: "left", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: [{ pressed: false }] } };
    const session = { visibilityState: "visible", inputSources: [right, left] };
    renderer.xr.isPresenting = true;
    renderer.xr.getSession = () => session;
    renderer.xr.getReferenceSpace = () => ({});
    renderer.xr.getCamera = () => camera;
    const head = { orientation: { x: 0, y: 0, z: 0, w: 1 }, position: { x: 0, y: 1.6, z: 0 } };
    // Right controller at (1, 1.5, 0) aimed 45° down toward -z: the floor hit is (1, -1.5).
    const aim = { position: { x: 1, y: 1.5, z: 0 }, orientation: { x: -Math.sin(Math.PI / 8), y: 0, z: 0, w: Math.cos(Math.PI / 8) } };
    let tracked = true;
    const frame = { getViewerPose: () => ({ transform: head }),
      getPose: (space) => space === right.targetRaySpace ? { transform: aim } : tracked || space !== right.gripSpace ? {} : null };
    let frameId = 0;
    const tick = async (ms, set = () => {}) => {
      const end = performance.now() + ms;
      while (performance.now() < end) {
        set();
        subject.gate.acceptFrame(String(++frameId), 0, performance.now());
        subject.lastCanvasDraw = performance.now();
        subject.update(performance.now(), frame);
        await sleep(20);
      }
    };
    await tick(100);
    // Live camera view: the grip only explains how to aim; nothing is posted.
    await tick(100, () => { right.gamepad.buttons[1].pressed = true; });
    await tick(60, () => { right.gamepad.buttons[1].pressed = false; });
    const liveGrip = { mode: subject.autonomy.mode, hint: subject.navMessage, reticle: subject.navOverlay.reticle.visible };
    subject.xrLive = false; // A toggles to the map view
    await tick(60);
    const reticle = subject.navOverlay.reticle.visible ? [subject.navOverlay.reticle.position.x, subject.navOverlay.reticle.position.z] : null;
    await tick(100, () => { right.gamepad.buttons[1].pressed = true; });
    await tick(150, () => { right.gamepad.buttons[1].pressed = false; });
    const armed = { mode: subject.autonomy.mode, mapMotion: subject.gate.decide(true, performance.now(), true, subject.inputValid, subject.lastXRAt) };
    // Back to the live view: the view-change stop keeps the accepted goal armed; the deadman gates it.
    subject.xrLive = true; subject.stop("View changed: release trigger");
    window.navMessages = [];
    await tick(200, () => { right.gamepad.buttons[0].pressed = false; });
    await tick(400, () => { right.gamepad.buttons[0].pressed = true; });
    await tick(200, () => { right.gamepad.buttons[0].pressed = false; });
    const afterGoal = subject.autonomy.mode;
    // Grip held through tracking loss never fires on recovery.
    subject.xrLive = false;
    tracked = false;
    await tick(60, () => { right.gamepad.buttons[1].pressed = true; });
    tracked = true;
    await tick(100, () => { right.gamepad.buttons[1].pressed = true; });
    await tick(60, () => { right.gamepad.buttons[1].pressed = false; });
    subject.xrLive = true;
    // Right thumbstick click cancels.
    await tick(80, () => { right.gamepad.buttons[3].pressed = true; });
    await tick(150, () => { right.gamepad.buttons[3].pressed = false; });
    const cancelled = subject.autonomy.mode;
    subject.dispose();
    return { drawn, liveGrip, reticle, armed, afterGoal, cancelled };
  });
  assert.deepEqual([vrNav.drawn.points, vrNav.drawn.frontiers, vrNav.drawn.goal], [3, 2, true]);
  assert.ok(vrNav.drawn.onPath[1] > 120 && vrNav.drawn.onPath[2] > 120, `glowing path pixel: ${vrNav.drawn.onPath}`);
  assert.ok(vrNav.drawn.offPath[1] < 40 && vrNav.drawn.offPath[2] < 40, `background off the path: ${vrNav.drawn.offPath}`);
  assert.deepEqual(vrNav.liveGrip, { mode: "teleop", hint: "Go-to: press A for the map, point at the floor, then right grip", reticle: false });
  assert.ok(vrNav.reticle && Math.abs(vrNav.reticle[0] - 1) < 1e-3 && Math.abs(vrNav.reticle[1] + 1.5) < 1e-3, `reticle at aim: ${vrNav.reticle}`);
  assert.equal(goals.length, 1, "one goal for one grip press; a grip held through tracking loss does not post");
  assert.deepEqual(goals[0].body, { x: 1, z: -1.5 });
  assert.deepEqual(vrNav.armed, { mode: "goto", mapMotion: false });
  const held = navControl.filter((message) => message.deadman);
  const gotoHeld = held.filter((message) => message.mode === "goto");
  assert.ok(gotoHeld.length >= 5, `held deadman sends go-to heartbeats: ${gotoHeld.length}`);
  assert.ok(gotoHeld.every((message) => message.v === 0 && message.w === 0 && message.permit && message.video_fresh));
  assert.ok(navControl.filter((message) => !message.deadman).every((message) => message.mode === "teleop" && message.v === 0 && message.w === 0),
    "releases never carry go-to");
  assert.equal(vrNav.afterGoal, "goto");
  assert.deepEqual(cancels, ["POST"], "right stick click posts /nav/cancel");
  assert.equal(vrNav.cancelled, "teleop");
  assert.deepEqual(navErrors, []);
  await navContext.close();
  result.navigation = {
    desktop: { pathPoints: 3, doubleClickGoal: desktopGoal, cancelButtonAndKey: true },
    vr: { pathPixel: vrNav.drawn.onPath, goal: { x: 1, z: -1.5 }, gotoHeartbeats: gotoHeld.length, latchedGrip: true, stickClickCancel: true },
  };
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
