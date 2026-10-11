import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chromium } from "@playwright/test";

// Software-only rehearsal. No backend or hardware connection is needed.
const origin = "http://127.0.0.1:5257";
const server = spawn(
  process.execPath,
  [
    "--input-type=module", "-e",
    "import { createServer } from 'vite'; const server = await createServer({server:{host:'127.0.0.1',port:5257,strictPort:true,watch:null}}); await server.listen(); server.printUrls();",
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
  await page.goto(`${origin}/?demo=0`); // guards on: this file pins the default safety behaviour
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
        axes: [0, 0, 1, -1],
        buttons: [{ pressed: true }],
      },
    };
    const left = {
      handedness: "left",
      gripSpace: {},
      gamepad: {
        connected: true,
        axes: [0, 0, -1, 1],
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
      loseRight = false,
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
      getPose: (space) =>
        (loseLeft && space === left.gripSpace) || (loseRight && space === right.gripSpace) ? null : {},
    };
    const arm = (now, controller = right) => {
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
      controller.gamepad.buttons[0].pressed = false;
      subject.update(now, frame);
      subject.gate.decide(false, now, true, subject.inputValid, subject.lastXRAt);
      controller.gamepad.buttons[0].pressed = true;
      subject.update(now + 1, frame);
      return subject.gate.decide(true, now + 1, true, subject.inputValid, subject.lastXRAt);
    };
    const initialArm = arm(90);
    subject.update(100, frame);
    const tracked = {
      initialArm,
      valid: subject.inputValid,
      held: subject.input.held,
      v: subject.input.v,
      w: subject.input.w,
      moving: subject.gate.decide(true, 100, true, subject.inputValid, subject.lastXRAt),
      sampledAt: subject.lastXRAt,
    };
    loseLeft = true;
    viewerY = 0.25;
    subject.update(110, frame);
    const lost = {
      valid: subject.inputValid,
      held: subject.input.held,
      v: subject.input.v,
      w: subject.input.w,
      moving: subject.gate.decide(true, 110, true, subject.inputValid, subject.lastXRAt),
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
    loseRight = true;
    subject.update(125, frame);
    const lostRight = {
      valid: subject.inputValid,
      held: subject.input.held,
      v: subject.input.v,
      w: subject.input.w,
      moving: subject.gate.decide(true, 125, true, subject.inputValid, subject.lastXRAt),
      needsRelease: subject.gate.needsRelease,
      stale: !Number.isFinite(subject.lastXRAt),
    };
    loseRight = false;
    right.gamepad.connected = false;
    subject.update(130, frame);
    const disconnected = { valid: subject.inputValid, v: subject.input.v };
    right.gamepad.connected = true;
    subject.update(140);
    const missingFrame = { valid: subject.inputValid, v: subject.input.v };
    const initiallyArmed = arm(150);
    session.inputSources = [right];
    subject.update(170, frame);
    const removedLeft = {
      initiallyArmed,
      valid: subject.inputValid,
      v: subject.input.v,
      w: subject.input.w,
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
    left.gamepad.connected = false;
    subject.update(175, frame);
    const disconnectedLeft = {
      valid: subject.inputValid,
      v: subject.input.v,
      w: subject.input.w,
      moving: subject.gate.decide(true, 175, true, subject.inputValid, subject.lastXRAt),
      needsRelease: subject.gate.needsRelease,
    };
    left.gamepad.connected = true;
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
    // A missing pose cannot count as the new controller's physical release.
    replacement.gamepad.connected = false;
    replacement.gamepad.buttons[0].pressed = false;
    subject.update(201, frame);
    subject.gate.decide(false, 201, true, subject.inputValid, subject.lastXRAt);
    replacement.gamepad.connected = true;
    replacement.gamepad.buttons[0].pressed = true;
    subject.update(202, frame);
    const replacementHeld = {
      moving: subject.gate.decide(true, 202, true, subject.inputValid, subject.lastXRAt),
      needsRelease: subject.gate.needsRelease,
    };
    const replacementReleased = arm(203, replacement);
    session.inputSources = [left];
    subject.update(206, frame);
    const missingRight = {
      valid: subject.inputValid,
      v: subject.input.v,
      w: subject.input.w,
      moving: subject.gate.decide(true, 206, true, subject.inputValid, subject.lastXRAt),
    };
    session.inputSources = [replacement, left];
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
    // Left Y (controller diagram) held across tracking loss must not reopen it on recovery.
    let scans = 0;
    subject.startScan = async () => { scans++; };
    left.gamepad.buttons = [0, 1, 2, 3, 4, 5].map(() => ({ pressed: false }));
    left.gamepad.buttons[5].pressed = true;
    loseLeft = true;
    subject.update(250, frame);
    loseLeft = false;
    subject.update(260, frame);
    const menuWhileStillHeld = Number.isFinite(subject.hintUntil);
    left.gamepad.buttons[5].pressed = false;
    subject.update(270, frame);
    left.gamepad.buttons[5].pressed = true;
    subject.update(280, frame);
    const menuLatch = { menuWhileStillHeld, menuAfterRelease: Number.isFinite(subject.hintUntil), scans };
    subject.hintUntil = -Infinity;

    const grant = (label) => {
      const next = permissions.shift();
      if (!next) throw new Error(`No microphone request at ${label}: phase ${subject.voice.phase}, ${subject.voice.statusText}`);
      return next;
    };
    const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
    const voice = subject.voice;
    left.gamepad.buttons[5].pressed = false;
    right.gamepad.axes = [0, 0, 0, 0]; // a deflected drive stick cancels a confirmed Seek
    left.gamepad.axes = [0, 0, 0, 0];
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
    const armedBeforeDisconnect = arm(295);
    left.gamepad.connected = false;
    left.gamepad.buttons[0].pressed = true;
    left.gamepad.buttons[5].pressed = true;
    subject.update(296, frame);
    const optionalLeftButtons = {
      armedBeforeDisconnect,
      disconnected: {
        moving: subject.gate.decide(true, 296, true, subject.inputValid, subject.lastXRAt),
        menu: subject.actionMenu.visible,
        capturing: voice.capturing,
        permissionCalls: permissions.length,
      },
    };
    left.gamepad.connected = true;
    subject.update(297, frame);
    optionalLeftButtons.recoveredStillHeld = {
      moving: subject.gate.decide(true, 297, true, subject.inputValid, subject.lastXRAt),
      menu: subject.actionMenu.visible,
      capturing: voice.capturing,
      permissionCalls: permissions.length,
    };
    left.gamepad.buttons[0].pressed = false;
    left.gamepad.buttons[5].pressed = false;
    subject.update(298, frame);
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
    subject.bannerKey = ""; // the chip only repaints when its source, mode or tone changes
    subject.drawHUD(420);
    const simulated = { kind: banner.dataset.kind, title: banner.querySelector("strong").textContent, vr: subject.bannerPanel.visible && subject.bannerPanel.parent === subject.cockpit };
    // The headset banner repaints on a later frame than the HUD, and never for unchanged text.
    const queued = !!subject.bannerPending;
    subject.bannerPending.at = -Infinity;
    // Keep the HUD text at 420 while exercising the later banner paint.
    subject.lastHudAt = 421;
    subject.paint(421);
    const painted = !subject.bannerPending;
    // The chip's source tag is filled with the SIMULATED colour somewhere on the canvas.
    const chipData = subject.bannerCanvas.getContext("2d").getImageData(0, 0, subject.bannerCanvas.width, subject.bannerCanvas.height).data;
    let bannerPixel = null;
    for (let i = 0; i < chipData.length; i += 4) if (chipData[i] === 0x7c && chipData[i + 1] === 0x4a && chipData[i + 2] === 0x03 && chipData[i + 3] === 255) { bannerPixel = [0x7c, 0x4a, 0x03]; break; }
    subject.drawHUD(420); // identical text, including the displayed age
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
      lostRight,
      disconnected,
      missingFrame,
      removedLeft,
      disconnectedLeft,
      replacedRight,
      replacementHeld,
      replacementReleased,
      missingRight,
      missingViewer,
      blurred,
      refocused,
      menuLatch,
      offPress,
      enabled,
      optionalLeftButtons,
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
  assert.ok(result.first.pixel[0] > 200);
  assert.ok(result.second.pixel[2] > 200);
  assert.ok(result.second.pixel[0] < 50);
  assert.equal(result.second.replaced, true);
  assert.deepEqual([result.second.width, result.second.height], [1280, 720]);
  assert.deepEqual(result.tracked, {
    initialArm: true, valid: true, held: true, v: 1, w: 1, moving: true, sampledAt: 100,
  }, "right Touch thumbstick drives despite opposite left-stick axes");
  assert.deepEqual(result.lost, {
    valid: true,
    held: true,
    v: 1,
    w: 1,
    moving: true,
    needsRelease: false,
    stale: false,
    displayVisible: true,
    headY: 0,
  }, "left tracking loss keeps otherwise valid right-controller driving enabled");
  assert.deepEqual(result.recovered, {
    valid: true,
    held: true,
    needsRelease: false,
  });
  assert.deepEqual(result.lostRight, {
    valid: false, held: true, v: 0, w: 0, moving: false, needsRelease: true, stale: true,
  }, "right grip tracking loss stops and requires a new release");
  assert.deepEqual(result.disconnected, { valid: false, v: 0 });
  assert.deepEqual(result.missingFrame, { valid: false, v: 0 });
  assert.deepEqual(result.removedLeft, {
    initiallyArmed: true,
    valid: true,
    v: 1,
    w: 1,
    moving: true,
    needsRelease: false,
  }, "removing the optional left controller does not interrupt right-controller driving");
  assert.deepEqual(result.disconnectedLeft, {
    valid: true, v: 1, w: 1, moving: true, needsRelease: false,
  }, "disconnected left controller does not invalidate a tracked right controller");
  assert.deepEqual(result.replacedRight, {
    rearmed: true,
    moving: false,
    needsRelease: true,
  });
  assert.deepEqual(result.replacementHeld, { moving: false, needsRelease: true },
    "an untracked release cannot rearm the replacement right controller");
  assert.equal(result.replacementReleased, true, "a fresh tracked release rearms the replacement right controller");
  assert.deepEqual(result.missingRight, { valid: false, v: 0, w: 0, moving: false },
    "left controller alone cannot supply steering or a right-trigger deadman");
  assert.deepEqual(result.headsetBanner, { queued: true, painted: true, bannerPixel: [0x7c, 0x4a, 0x03], unchangedQueued: false });
  assert.deepEqual(result.missingViewer, {
    valid: false,
    displayVisible: false,
    v: 0,
  });
  assert.deepEqual(result.blurred, { rearmedVisible: true, valid: false, moving: false });
  assert.deepEqual(result.refocused, { held: true, moving: false, needsRelease: true });
  assert.deepEqual(result.menuLatch, { menuWhileStillHeld: false, menuAfterRelease: true, scans: 0 });
  assert.deepEqual(result.offPress, { permissionCalls: 0, phase: "error" });
  assert.deepEqual(result.enabled, { vrEnabled: true, probeStopped: 1 });
  assert.deepEqual(result.optionalLeftButtons, {
    armedBeforeDisconnect: true,
    disconnected: { moving: true, menu: false, capturing: false, permissionCalls: 0 },
    recoveredStillHeld: { moving: true, menu: false, capturing: false, permissionCalls: 0 },
  }, "stale left Y/trigger presses cannot open actions or start voice through disconnection and recovery");
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

  // Real RoverExperience methods with tracked-pose/gamepad doubles and a local
  // send spy. No motor backend: freshness here is explicitly injected.
  const interactions = await page.evaluate(async () => {
    const { RoverExperience } = await import("/src/rover.ts");
    const THREE = await import("/node_modules/three/build/three.module.js");
    const renderer = new THREE.WebGLRenderer();
    const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(70, 1, .01, 100);
    let view = "camera";
    const subject = new RoverExperience(renderer, scene, camera, () => view, next => { view = next; });
    const buttons = () => Array.from({ length: 6 }, () => ({ pressed: false }));
    const right = { handedness: "right", gripSpace: {}, targetRaySpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: buttons() } };
    const left = { handedness: "left", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: buttons() } };
    const session = { visibilityState: "visible", inputSources: [right, left] }, reference = {};
    renderer.xr.isPresenting = true;
    renderer.xr.getSession = () => session;
    renderer.xr.getReferenceSpace = () => reference;
    renderer.xr.getCamera = () => camera;
    const orientation = new THREE.Quaternion(), position = new THREE.Vector3(0, 1.6, 0);
    const rayPosition = new THREE.Vector3(0, 1.6, 0), rayOrientation = new THREE.Quaternion();
    let tracked = true, rayTracked = true, frames = 0;
    const frame = {
      getViewerPose: () => ({ transform: { position, orientation } }),
      getPose: space => space === right.targetRaySpace ? rayTracked ? { transform: { position: rayPosition, orientation: rayOrientation } } : null
        : space === right.gripSpace && !tracked ? null : { transform: { position, orientation } },
    };
    const messages = [];
    subject.socket = { readyState: WebSocket.OPEN, bufferedAmount: 0, send(data) { messages.push(JSON.parse(data)); }, close() {} };
    const stops = [], originalStop = subject.stop.bind(subject);
    subject.stop = reason => { stops.push(reason); originalStop(reason); };
    let cancels = 0;
    subject.cancelNav = async () => { cancels++; subject.autonomy.clear(); };
    const tick = () => {
      const now = performance.now();
      // Each tick models a fresh server permit, so held sends are not coalesced as same-permit repeats.
      const status = { session_id: "interaction-fixture", permit: `fixture-token-${frames}`, permit_ms: 250, rover: true, simulated: true, requires_release: false };
      subject.gate.acceptStatus(status, now); subject.status = status;
      subject.gate.acceptFrame(String(++frames), 0, now);
      subject.lastCanvasDraw = now; subject.lastSendAt = now;
      subject.update(now, frame); subject.pump();
    };
    const press = (hand, index) => { hand.gamepad.buttons[index].pressed = false; tick(); hand.gamepad.buttons[index].pressed = true; tick(); };
    try {
      tick();
      const anchored = { position: subject.cockpit.position.toArray(), quaternion: subject.cockpit.quaternion.toArray() };
      position.set(.4, 1.9, .2);
      orientation.setFromEuler(new THREE.Euler(.3, .8, .2, "YXZ")); tick();
      const afterHeadMove = { position: subject.cockpit.position.toArray(), quaternion: subject.cockpit.quaternion.toArray() };
      right.gamepad.buttons[0].pressed = true; tick();
      press(left, 4);
      const recentered = { position: subject.cockpit.position.toArray(), quaternion: subject.cockpit.quaternion.toArray(), needsRelease: subject.gate.needsRelease };
      left.gamepad.buttons[4].pressed = false; tick();

      // A over a person rectangle switches to AUTO (never selecting Seek): Explore armed, map shown,
      // live camera kept as a small window, and a fresh trigger release required.
      subject.drawnBoxes = [{ id: 7, cls: "person", x: 0, y: 0, w: 1, h: 1 }];
      subject.panelPoint = () => ({ x: .5, y: .5 });
      press(right, 4);
      const viewWhileAHeld = view;
      right.gamepad.buttons[4].pressed = false; tick(); // a tap switches on release
      const aOverPerson = { view, mode: subject.autonomy.mode, release: subject.gate.needsRelease, viewWhileAHeld,
        auto: subject.freeRoam.active, cameraWindow: subject.cameraScreen.visible && subject.cameraScreen.scale.x < .5, mapShown: !subject.vrCameraView };
      // AUTO: deadman held with the stick fully deflected. Heartbeats carry Explore with v = w = 0; the stick only roams.
      right.gamepad.buttons[0].pressed = false; tick();
      right.gamepad.axes[3] = -1; right.gamepad.axes[2] = .9;
      right.gamepad.buttons[0].pressed = true; tick();
      subject.lastRoamAt -= 40; tick(); // 40 ms of walking (sub-ms ticks are below the timer resolution)
      const autoMessage = messages.at(-1);
      const autoDrive = { deadman: autoMessage.deadman, mode: autoMessage.mode, v: autoMessage.v, w: autoMessage.w, intent: subject.autonomy.mode,
        roamed: subject.freeRoam.x !== 0 || subject.freeRoam.z !== 0, turned: subject.freeRoam.yaw !== 0 };
      right.gamepad.axes[3] = 0; right.gamepad.axes[2] = 0;
      // A again: back to MANUAL, autonomy off, motion latched until the trigger is released.
      press(right, 4); right.gamepad.buttons[4].pressed = false; tick();
      const backToManual = { mode: subject.autonomy.mode, release: subject.gate.needsRelease, auto: subject.freeRoam.active, cameraView: subject.vrCameraView, deadman: messages.at(-1).deadman };

      // Hold right trigger + forward right stick, then B must stop and cancel.
      right.gamepad.axes[3] = -1;
      right.gamepad.buttons[0].pressed = false; tick();
      right.gamepad.buttons[0].pressed = true; tick();
      const drove = { deadman: messages.at(-1).deadman, v: messages.at(-1).v, w: messages.at(-1).w };
      right.gamepad.axes[3] = 0; // go-to uses a centered stick; an override would cancel before B
      // A drifting right stick inside the deadzone and a fully deflected left stick both keep Explore.
      subject.autonomy.toggleExplore();
      right.gamepad.axes[2] = .2; right.gamepad.axes[3] = -.2; left.gamepad.axes[3] = -1; tick();
      const drift = { mode: subject.autonomy.mode, v: messages.at(-1).v, w: messages.at(-1).w };
      right.gamepad.axes[3] = -.6; tick();
      const override = subject.autonomy.mode;
      right.gamepad.axes[2] = 0; right.gamepad.axes[3] = 0; left.gamepad.axes[3] = 0; tick();
      subject.autonomy.goto(); subject.nav = { state: "driving", mode: "goto", backend: "nav2", path: [], frontiers: [], goal: { x: 1, z: 0 } };
      press(right, 5);
      const bStopped = { command: messages.at(-1), mode: subject.autonomy.mode, latch: subject.gate.needsRelease, cancels };
      tick(); const heldAfterStop = messages.at(-1).deadman;
      tracked = false; tick(); tracked = true; tick();
      const bStopsAfterRecovery = stops.filter(reason => reason === "Stopped: release trigger").length;
      right.gamepad.buttons[5].pressed = false; tick();
      right.gamepad.buttons[0].pressed = false; tick();
      right.gamepad.buttons[0].pressed = true; tick();
      const resumedAfterRelease = messages.at(-1).deadman;

      // Holding right A opens Actions without flipping the view.
      const viewBeforeHold = view;
      right.gamepad.buttons[4].pressed = false; tick();
      right.gamepad.buttons[4].pressed = true; tick();
      const aStart = performance.now();
      while (performance.now() - aStart < 650) { await new Promise(resolve => setTimeout(resolve, 50)); tick(); }
      right.gamepad.buttons[4].pressed = false; tick();
      const holdA = { menu: subject.actionMenu.visible, view, viewBeforeHold };
      const menuOpened = subject.actionMenu.visible;
      right.gamepad.buttons[0].pressed = false; tick();
      right.gamepad.buttons[0].pressed = true; tick();
      const behindMenu = { motion: messages.at(-1).deadman, inputValid: subject.inputValid, visible: subject.actionMenu.visible };
      right.gamepad.buttons[0].pressed = false;
      rayTracked = false;
      press(right, 1);
      const missingRay = { menu: subject.actionMenu.visible, mode: subject.autonomy.mode, pointer: subject.pointer.visible };
      rayTracked = true; tick();
      const heldThroughRayRecovery = { menu: subject.actionMenu.visible, mode: subject.autonomy.mode };
      right.gamepad.buttons[1].pressed = false; tick();
      // Aim at the Controls card on the actual menu mesh, then a new grip press.
      subject.cockpit.updateMatrixWorld(true);
      const aim = subject.actionMenu.panel.localToWorld(new THREE.Vector3(0, .351, 0));
      rayPosition.copy(position);
      rayOrientation.setFromUnitVectors(new THREE.Vector3(0, 0, -1), aim.sub(rayPosition).normalize());
      tick();
      const hover = subject.updatePointer(frame, reference, right);
      right.gamepad.buttons[0].pressed = true;
      right.gamepad.buttons[1].pressed = true; tick();
      const flashed = subject.actionMenu.visible; // the chosen card flashes green before the menu closes
      subject.menuCloseAt -= 300; tick();
      const selected = { flashed, hover, mode: subject.autonomy.mode, menu: subject.actionMenu.visible, release: subject.gate.needsRelease, motion: messages.at(-1).deadman, hint: subject.hintPanel.visible };
      return { anchored, afterHeadMove, recentered, aOverPerson, autoDrive, backToManual, drove, drift, override, holdA, bStopped, heldAfterStop,
               bStopsAfterRecovery, resumedAfterRelease, menuOpened, behindMenu, missingRay,
               heldThroughRayRecovery, selected };
    } finally { renderer.xr.isPresenting = false; subject.dispose(); renderer.dispose(); }
  });
  assert.deepEqual(interactions.afterHeadMove, interactions.anchored, "head yaw/pitch/roll/translation cannot drag the anchored screen");
  assert.deepEqual(interactions.recentered.position, [.4, 1.9, .2]);
  assert.ok(Math.abs(interactions.recentered.quaternion[0]) < 1e-8 && Math.abs(interactions.recentered.quaternion[2]) < 1e-8, "recenter is yaw-only");
  assert.ok(Math.abs(2 * Math.atan2(interactions.recentered.quaternion[1], interactions.recentered.quaternion[3]) - .8) < 1e-6, "recenter faces the current head yaw");
  assert.equal(interactions.recentered.needsRelease, true);
  assert.deepEqual(interactions.aOverPerson, { view: "camera", mode: "explore", release: true, viewWhileAHeld: "camera", auto: true, cameraWindow: true, mapShown: true });
  assert.deepEqual(interactions.autoDrive, { deadman: true, mode: "explore", v: 0, w: 0, intent: "explore", roamed: true, turned: true },
    "AUTO: a deflected stick roams the map and never sends physical v/w or a stick override");
  assert.deepEqual(interactions.backToManual, { mode: "teleop", release: true, auto: false, cameraView: true, deadman: false });
  assert.deepEqual(interactions.drove, { deadman: true, v: 1, w: 0 }, "right stick forward drives");
  assert.deepEqual(interactions.drift, { mode: "explore", v: 0, w: 0 }, "drift inside the deadzone and the left stick never override Explore");
  assert.equal(interactions.override, "teleop", "a real right-stick push takes back control");
  assert.deepEqual(interactions.holdA, { menu: true, view: "camera", viewBeforeHold: "camera" });
  assert.deepEqual([interactions.bStopped.command.deadman, interactions.bStopped.command.v, interactions.bStopped.command.w], [false, 0, 0]);
  assert.deepEqual([interactions.bStopped.mode, interactions.bStopped.latch, interactions.bStopped.cancels], ["teleop", true, 1]);
  assert.equal(interactions.heldAfterStop, false);
  assert.equal(interactions.bStopsAfterRecovery, 1, "B held through tracking recovery is not a new press");
  assert.equal(interactions.resumedAfterRelease, true);
  assert.equal(interactions.menuOpened, true);
  assert.deepEqual(interactions.behindMenu, { motion: false, inputValid: false, visible: true });
  assert.deepEqual(interactions.missingRay, { menu: true, mode: "teleop", pointer: false });
  assert.deepEqual(interactions.heldThroughRayRecovery, { menu: true, mode: "teleop" });
  assert.deepEqual(interactions.selected, { flashed: true, hover: "help", mode: "teleop", menu: false, release: true, motion: false, hint: true });
  result.interactions = interactions;
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
      fresh: true, session_id: "budget-sim-only", map_epoch: 1, generation: 1, revision: 1, mesh_revision: 1,
      points_transport: "http", points: 0, triangles: 1, frames: revision++,
      rejected: 0, dropped: 0, processing_ms: 0, age_ms: 0, tracking: "normal",
      position: [0, 0, 0], transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
      depth_range: null, reason: null,
    })), 50);
    mapTimers.add(timer);
    socket.onClose(() => { clearInterval(timer); mapTimers.delete(timer); mapCloses++; });
  });
  await page.locator("#settings-toggle").click();
  await page.getByText("H.264 video", { exact: false }).locator("input").first().check();
  await page.locator('[data-view="3d"]').click();
  await page.waitForTimeout(350);
  const jpegBefore = jpegPulls;
  await page.waitForTimeout(2200);
  assert.equal(jpegPulls, jpegBefore, "H.264 suppresses both JPEG polling paths");
  assert.equal(pointPulls, 0, "hybrid derives points from its mesh without a duplicate download");
  assert.ok(meshPulls > 0 && meshPulls <= 1, `bounded mesh pulls: ${meshPulls}`);
  assert.ok(mapUrls.every(url => url.endsWith("?points=http")));
  await page.locator("#settings-toggle").click();
  await page.locator("#points-only").check();
  await page.waitForTimeout(3200);
  assert.equal(pointPulls, 1, "points-only downloads one snapshot after the shared five-second gate");
  assert.equal(meshPulls, 1, "points-only does not keep pulling meshes");
  await page.locator("#close-settings").click();
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
  const reconnectPulls = [pointPulls, meshPulls];
  await page.waitForTimeout(5200);
  assert.deepEqual([pointPulls, meshPulls], reconnectPulls, "same-source reconnect and unchanged revisions never redownload the map");
  result.cellularTransfers = { pointPulls, meshPulls, jpegSuppressed: true, deadmanPause: true, resumed: true };

  // Camera mount: ?mount=fixed persists, wins over a rover that reports a pan servo,
  // and keeps head yaw out of the pan command so the server never blocks forward.
  const mountContext = await browser.newContext();
  const controlMessages = [];
  await mountContext.routeWebSocket("**/ws/**", (socket) => {
    if (!socket.url().endsWith("/ws/control")) return socket.close();
    let session = `mount-${controlMessages.length}-${Math.random()}`;
    let latest = { seq: -1, deadman: false, v: 0, pan: 0 }, ordinal = 0;
    socket.onMessage((data) => {
      const message = JSON.parse(String(data));
      controlMessages.push(message);
      latest = message;
    });
    const publish = () => {
      const message = latest;
      // Mirrors jetson/control/state.py forward_blocked(): pan off-center blocks forward.
      const blocked = Math.abs(message.pan) > 10;
      socket.send(JSON.stringify({
        type: "status", session_id: session, permit: `permit-${++ordinal}`, permit_ms: 250, seq: message.seq,
        rover: true, simulated: true, range_cm: 120, estop: blocked,
        reason: message.deadman && message.v > 0 && blocked ? "forward_blocked" : null,
      }));
    };
    publish();
    const timer = setInterval(publish, 25);
    mapTimers.add(timer);
    socket.onClose(() => { clearInterval(timer); mapTimers.delete(timer); });
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
    const right = { handedness: "right", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, -1], buttons: [{ pressed: false }] } };
    const left = { handedness: "left", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: [{ pressed: false }] } };
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
        subject.update(performance.now(), frame); subject.afterRender(performance.now()); // display loop: render, then pump
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
  assert.ok(servoDrive.drove.length >= 5 && servoDrive.drove.slice(-3).every((message) => Math.round(message.pan) === -40),
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
    // The HUD line lists the mount only after the first /health and camera frame; poll instead of one fixed wait.
    const deadline = Date.now() + 3000;
    let hud = await fresh.locator("#rover-details").textContent();
    while (!/camera · full frame|Servo pan/.test(hud) || /Waiting for camera/.test(hud)) {
      if (Date.now() > deadline) break;
      await fresh.waitForTimeout(100); hud = await fresh.locator("#rover-details").textContent();
    }
    const value = await fresh.locator('select[aria-label="Camera mounting mode"]').inputValue();
    await context.close();
    return { value, hud };
  };
  const noPan = await freshMount(false);
  assert.equal(noPan.value, "fixed", "camera_pan:false defaults to fixed");
  assert.match(noPan.hud, /Fixed camera · full frame · JPEG video/);
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
    let latest = { seq: -1, mode: "teleop" }, ordinal = 0;
    socket.onMessage((data) => {
      const message = JSON.parse(String(data));
      navControl.push(message);
      latest = message;
    });
    const publish = () => {
      const message = latest;
      socket.send(JSON.stringify({ type: "control_status", session_id: session, permit: `permit-${++ordinal}`, permit_ms: 250,
        seq: message.seq, rover: true, simulated: true, range_cm: 150, estop: false, reason: null, mode: message.mode ?? "teleop", nav: navFixture }));
    };
    publish();
    const timer = setInterval(publish, 25);
    mapTimers.add(timer);
    socket.onClose(() => { clearInterval(timer); mapTimers.delete(timer); });
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
  await navPage.goto(`${origin}/?view=map&demo=0`);
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
    // The overview is now a small, tilted panel. Goal coordinates remain in
    // map metres rather than inheriting the scene's scale, offset or pitch.
    const overview = new THREE.Group(); overview.scale.setScalar(.18);
    overview.rotation.x = -Math.PI / 6; overview.position.set(0, .5, -1.6);
    scene.add(overview); overview.add(subject.navLayer); overview.updateMatrixWorld(true);
    const right = { handedness: "right", gripSpace: {}, targetRaySpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0],
      buttons: [0, 1, 2, 3, 4, 5].map(() => ({ pressed: false })) } };
    const left = { handedness: "left", gripSpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: [{ pressed: false }] } };
    const session = { visibilityState: "visible", inputSources: [right, left] };
    renderer.xr.isPresenting = true;
    renderer.xr.getSession = () => session;
    renderer.xr.getReferenceSpace = () => ({});
    renderer.xr.getCamera = () => camera;
    const head = { orientation: { x: 0, y: 0, z: 0, w: 1 }, position: { x: 0, y: 1.6, z: 0 } };
    const aimPosition = new THREE.Vector3(1, 1.5, 0);
    const goalWorld = subject.navLayer.localToWorld(new THREE.Vector3(1, 0, -1.5));
    const aimQuaternion = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, -1), goalWorld.sub(aimPosition).normalize());
    const aim = { position: aimPosition, orientation: aimQuaternion };
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
        subject.update(performance.now(), frame); subject.afterRender(performance.now()); // display loop: render, then pump
        await sleep(20);
      }
    };
    await tick(100);
    // Live camera view: the grip only explains how to aim; nothing is posted.
    await tick(100, () => { right.gamepad.buttons[1].pressed = true; });
    await tick(60, () => { right.gamepad.buttons[1].pressed = false; });
    const liveGrip = { mode: subject.autonomy.mode, hint: subject.navMessage, reticle: subject.navOverlay.reticle.visible };
    subject.auto = true; // AUTO: grip aims at the map floor
    await tick(60);
    const reticle = subject.navOverlay.reticle.visible ? [subject.navOverlay.reticle.position.x, subject.navOverlay.reticle.position.z] : null;
    await tick(100, () => { right.gamepad.buttons[1].pressed = true; });
    await tick(150, () => { right.gamepad.buttons[1].pressed = false; });
    const armed = { mode: subject.autonomy.mode, mapMotion: subject.gate.decide(true, performance.now(), true, subject.inputValid, subject.lastXRAt) };
    // Back to the live view: the view-change stop keeps the accepted goal armed; the deadman gates it.
    subject.auto = false; subject.stop("View changed: release trigger");
    window.navMessages = [];
    await tick(200, () => { right.gamepad.buttons[0].pressed = false; });
    await tick(400, () => { right.gamepad.buttons[0].pressed = true; });
    await tick(200, () => { right.gamepad.buttons[0].pressed = false; });
    const afterGoal = subject.autonomy.mode;
    // Grip held through tracking loss never fires on recovery.
    subject.auto = true;
    tracked = false;
    await tick(60, () => { right.gamepad.buttons[1].pressed = true; });
    tracked = true;
    await tick(100, () => { right.gamepad.buttons[1].pressed = true; });
    await tick(60, () => { right.gamepad.buttons[1].pressed = false; });
    subject.auto = false;
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
  assert.deepEqual(vrNav.liveGrip, { mode: "teleop", hint: "Point at a person, then right grip to select Seek", reticle: false });
  assert.ok(vrNav.reticle && Math.abs(vrNav.reticle[0] - 1) < 1e-3 && Math.abs(vrNav.reticle[1] + 1.5) < 1e-3, `reticle at aim: ${vrNav.reticle}`);
  assert.equal(goals.length, 1, "one goal for one grip press; a grip held through tracking loss does not post");
  assert.deepEqual(goals[0].body, { x: 1, z: -1.5 });
  // AUTO keeps the live camera window painted, so (unlike the old map view) a held deadman may drive the goal.
  assert.deepEqual(vrNav.armed, { mode: "goto", mapMotion: true });
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
  // Demo mode (the default build, captain's decision): relaxed client guards. `?demo=0` above pins the strict ones.
  const demoPage = await browser.newPage();
  await demoPage.goto(`${origin}/?demo=1`);
  const demo = await demoPage.evaluate(async () => {
    const { RoverExperience } = await import("/src/rover.ts");
    const THREE = await import("/node_modules/three/build/three.module.js");
    const renderer = new THREE.WebGLRenderer();
    const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(70, 1, .01, 100);
    const subject = new RoverExperience(renderer, scene, camera, () => "camera", () => {});
    const buttons = () => Array.from({ length: 6 }, () => ({ pressed: false, value: 0 }));
    const right = { handedness: "right", gripSpace: {}, targetRaySpace: {}, gamepad: { connected: true, axes: [0, 0, 0, 0], buttons: buttons() } };
    const session = { visibilityState: "visible", inputSources: [right] }, reference = {};
    renderer.xr.isPresenting = true; renderer.xr.getSession = () => session;
    renderer.xr.getReferenceSpace = () => reference; renderer.xr.getCamera = () => camera;
    const position = new THREE.Vector3(0, 1.6, 0), orientation = new THREE.Quaternion();
    let tracked = true, frames = 0, freshVideo = true;
    const frame = { getViewerPose: () => ({ transform: { position, orientation } }),
      getPose: space => space === right.gripSpace && !tracked ? null : { transform: { position, orientation } } };
    // Detach the page's real control socket so its close/reconnect cannot race the fixture.
    const real = subject.socket; if (real) { real.onclose = real.onerror = real.onmessage = null; real.close(); }
    const messages = [];
    subject.socket = { readyState: WebSocket.OPEN, bufferedAmount: 0, send(data) { messages.push(JSON.parse(data)); }, close() {} };
    const stops = [], originalStop = subject.stop.bind(subject);
    subject.stop = (reason, hard) => { stops.push(reason); originalStop(reason, hard); };
    subject.cancelNav = async () => { subject.autonomy.clear(); };
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const tick = () => {
      const now = performance.now();
      const status = { session_id: "demo-fixture", permit: `p-${frames}`, permit_ms: 250, rover: true, simulated: true, requires_release: false };
      subject.gate.acceptStatus(status, now); subject.status = status;
      if (freshVideo) subject.gate.acceptFrame(String(++frames), 0, now); else frames++;
      subject.lastCanvasDraw = now; subject.lastSendAt = now;
      subject.update(now, frame); subject.pump(); subject.drawHUD(now);
    };
    const run = async ms => { const end = performance.now() + ms; while (performance.now() < end) { tick(); await sleep(40); } };
    const trigger = held => { right.gamepad.buttons[0] = { pressed: held, value: held ? 1 : 0 }; };
    const press = async index => { right.gamepad.buttons[index].pressed = true; await run(80); right.gamepad.buttons[index].pressed = false; await run(80); };
    try {
      await run(120);
      trigger(true); right.gamepad.axes[3] = -1; await run(160);
      const driving = { deadman: messages.at(-1).deadman, v: messages.at(-1).v };
      // A 300 ms right-controller tracking blip: no stop, no re-press; manual v/w pause.
      const before = stops.length, mark = messages.length; tracked = false; await run(300);
      const blip = { stops: stops.slice(before), deadman: messages.slice(mark).every(m => m.deadman), v: messages.at(-1).v };
      tracked = true; await run(120);
      const afterBlip = { deadman: messages.at(-1).deadman, v: messages.at(-1).v };
      // Gone for more than 1 s still stops; when it returns with the trigger held the client resumes on its own.
      tracked = false; await run(1300);
      const lost = { deadman: messages.at(-1).deadman, stopped: stops.some(r => r.startsWith("XR tracking unavailable")) };
      const resumeMark = messages.length; tracked = true; await run(200);
      const resumed = { releaseTick: messages.slice(resumeMark).some(m => !m.deadman), deadman: messages.at(-1).deadman };
      // Stale video: the client keeps sending (truthfully video_fresh: false; the rover may still refuse) and warns.
      freshVideo = false; await run(700);
      const stale = { deadman: messages.at(-1).deadman, videoFresh: messages.at(-1).video_fresh, warning: subject.stopKey.includes("VIDEO STALE") };
      freshVideo = true; right.gamepad.axes[3] = 0; trigger(false); await run(160);
      // Hands-free AUTO: tap A with no trigger held; Explore heartbeats keep the deadman up with v = w = 0.
      await press(4); await run(200);
      const auto = { deadman: messages.at(-1).deadman, mode: messages.at(-1).mode, v: messages.at(-1).v, w: messages.at(-1).w, roaming: subject.freeRoam.active };
      // B: immediate stop, back to MANUAL, and a hard latch that a held trigger cannot skip.
      trigger(true); await press(5);
      const bStop = { deadman: messages.at(-1).deadman, auto: subject.freeRoam.active, mode: messages.at(-1).mode };
      await run(200);
      const heldAfterB = messages.at(-1).deadman;
      trigger(false); await run(120); trigger(true); await run(160);
      return { driving, blip, afterBlip, lost, resumed, stale, auto, bStop, heldAfterB, rePressed: messages.at(-1).deadman };
    } finally { renderer.xr.isPresenting = false; subject.dispose(); renderer.dispose(); }
  });
  assert.deepEqual(demo.driving, { deadman: true, v: 1 });
  assert.deepEqual(demo.blip, { stops: [], deadman: true, v: 0 }, "a short tracking blip neither stops nor needs a re-press");
  assert.deepEqual(demo.afterBlip, { deadman: true, v: 1 });
  assert.deepEqual(demo.lost, { deadman: false, stopped: true }, "right controller gone > 1 s stops");
  assert.deepEqual(demo.resumed, { releaseTick: true, deadman: true }, "held trigger resumes after one client-sent release");
  assert.deepEqual(demo.stale, { deadman: true, videoFresh: false, warning: true }, "stale video warns instead of blocking on the client");
  assert.deepEqual(demo.auto, { deadman: true, mode: "explore", v: 0, w: 0, roaming: true }, "AUTO is hands-free");
  assert.deepEqual(demo.bStop, { deadman: false, auto: false, mode: "teleop" });
  assert.equal(demo.heldAfterB, false, "after B a held trigger still needs a release");
  assert.equal(demo.rePressed, true);
  await demoPage.close();
  result.demoMode = demo;
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
