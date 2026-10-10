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
  await page.goto(origin);
  const result = await page.evaluate(async () => {
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
    };
  });
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
  const report = {
    passed: true,
    environment:
      "Chromium/SwiftShader; simulated XR poses; generated camera pixels; no rover backend",
    ...result,
  };
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser?.close();
  server.kill("SIGTERM");
}
