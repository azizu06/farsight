import "./style.css";
import { ControlLink } from "./controls";
import { RescueDrill } from "./drill";
import { drawHud, parseDetections, summarizeHud, type HudState } from "./hud";
import type { DetectionPacket } from "./protocol";
import { RoverScene } from "./scene";
import { receiveMjpeg } from "./stream";

const source = new URLSearchParams(location.search).get("source");
const sourceLabel =
  source === "mock"
    ? "MOCK · test source"
    : source === "replay"
      ? "REPLAY · recorded source"
      : "LIVE source · Jetson";
document.querySelector<HTMLDivElement>("#app")!.innerHTML = `
  <header><a class="wordmark" href="/">FARSIGHT<span>ROVER VIEW</span></a><span id="source-badge">${sourceLabel}</span><span class="network">ON-ROBOT PERCEPTION</span></header>
  <main>
    <section class="viewer" aria-label="Rover camera viewer">
      <canvas id="view" tabindex="0" aria-label="Curved camera panel desktop preview"></canvas>
      <div class="view-top"><span class="eyebrow">01 / THROUGH THE ROVER'S EYES</span><span id="video-status" role="status">Connecting camera…</span></div>
      <div class="view-bottom"><span>Mono camera · curved panel</span><span id="frame-count">0 frames received</span></div>
    </section>
    <aside>
      <p class="eyebrow">FARSIGHT / OPERATOR</p><h1>See first.<br>Then move.</h1>
      <p class="intro">Live video and detections from the Jetson. The rover keeps its own stop lease and obstacle check.</p>
      <div class="session-buttons"><button id="enter-vr" disabled>Checking VR…</button><button id="exit-vr" hidden>Exit VR</button><button id="recenter" class="secondary">Recenter panel</button></div>
      <p id="vr-status" class="help">Desktop preview works without a headset.</p>
      <section class="telemetry" aria-label="Live telemetry"><div><span>LINK</span><strong id="link" data-state="unknown">UNKNOWN</strong></div><div><span>AHEAD</span><strong id="range">unknown</strong></div><div><span>E-STOP</span><strong id="estop" data-state="unknown">UNKNOWN</strong></div><div><span>DETECTOR</span><strong id="fps">unknown</strong></div><div><span>REPORTED LINK</span><strong id="latency">unknown</strong></div><div><span>BOXES</span><strong id="boxes">0</strong></div></section>
      <label class="drive-toggle"><input id="drive-enabled" type="checkbox"> Enable driving</label>
      <p id="input-status" class="help">Drive disabled. Release the deadman before enabling.</p>
      <p class="help">Quest: right trigger + right stick. Head yaw pans the camera. Left trigger selects the menu. Desktop: click the preview, then hold Space + WASD.</p>
      <button id="stop" class="danger">STOP ROVER</button>
      <section class="drill"><div class="drill-heading"><h2>Room-search drill</h2><span id="timer">90 s</span></div><p>For responders practising how to search a room before entering.</p>
        <label class="total">Victims placed <input id="total" type="number" min="1" max="100" placeholder="unknown"></label>
        <div class="session-buttons"><button id="start-drill" class="secondary">Start 90 s drill</button><button id="end-drill" class="secondary" disabled>End drill</button></div>
        <p id="drill-status">Not started.</p><p id="face-status" class="help">Face scoring awaits the detector contract.</p><p id="drill-score" class="help"></p><p id="route-note" class="help"></p><ol id="return-trail"></ol>
      </section>
      <button id="reconnect" class="secondary small">Reconnect feeds</button>
    </aside>
  </main><footer>FARSIGHT / KNIGHT HACKS IX <span id="detector-status">Waiting for detections</span></footer>`;

const element = <T extends HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const driveEnabled = element<HTMLInputElement>("drive-enabled");
const keys = new Set<string>();
const websocketUrl = (path: string) => {
  const url = new URL(path, location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
};
const drill = new RescueDrill({
  faceClassApproved: import.meta.env.VITE_FACE_CLASS_APPROVED === "1",
});
let detections: DetectionPacket | null = null,
  detectionsAt: number | null = null;
let lastFrameAt: number | null = null,
  receivedFrames = 0;
let feedController: AbortController | null = null,
  detectorSocket: WebSocket | null = null;
let detectorReconnect: ReturnType<typeof setTimeout> | undefined,
  streamReconnect: ReturnType<typeof setTimeout> | undefined;
let generation = 0,
  imageEpoch = 0,
  decoding = false,
  pendingFrame: {
    blob: Blob;
    at: number;
    generation: number;
    epoch: number;
  } | null = null;
let scene: RoverScene | null = null,
  control: ControlLink;
let dirty = true,
  lastHudAt = 0,
  videoState = "Connecting",
  hadVideo = false,
  finishedDrill = false;
let connectingVr = false,
  destroyed = false,
  graphicsAvailable = true;

function newControl() {
  return new ControlLink(websocketUrl("/ws/control"), {
    onReply: (reply, at) => {
      drill.updateReply(reply, at);
      dirty = true;
    },
    onState: () => {
      dirty = true;
    },
    onCommand: (command, at) => {
      drill.recordCommand(command, at);
      dirty = true;
    },
  });
}
control = newControl();

function stopDrive(reason: string, disable = false) {
  keys.clear();
  control.stop(reason);
  if (disable) driveEnabled.checked = false;
  dirty = true;
}

function startDrill() {
  const total = Number(element<HTMLInputElement>("total").value);
  drill.setExpectedTotal(
    Number.isInteger(total) && total > 0 && total <= 100 ? total : null,
  );
  drill.start();
  finishedDrill = false;
  dirty = true;
}

function endDrill() {
  drill.finish();
  stopDrive("Drill ended", true);
  dirty = true;
}

function jpegDimensions(bytes: Uint8Array): { width: number; height: number } {
  // Bound decoded dimensions before asking the browser to allocate an image.
  for (let offset = 2; offset + 8 < bytes.length;) {
    if (bytes[offset++] !== 255) throw new Error("Malformed JPEG header.");
    while (bytes[offset] === 255) offset++;
    const marker = bytes[offset++];
    if (marker === 217 || marker === 218) break;
    if (marker === 1 || (marker >= 208 && marker <= 215)) continue;
    const length = bytes[offset] * 256 + bytes[offset + 1];
    if (length < 2 || offset + length > bytes.length) break;
    if (
      [
        192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207,
      ].includes(marker)
    ) {
      const height = bytes[offset + 3] * 256 + bytes[offset + 4],
        width = bytes[offset + 5] * 256 + bytes[offset + 6];
      if (
        !width ||
        !height ||
        width > 4096 ||
        height > 4096 ||
        width * height > 16_000_000
      )
        throw new Error("Camera JPEG dimensions exceed their limit.");
      return { width, height };
    }
    offset += length;
  }
  throw new Error("Camera JPEG has no valid dimensions.");
}

async function decodePending() {
  if (decoding) return;
  decoding = true;
  try {
    while (pendingFrame) {
      const next = pendingFrame;
      pendingFrame = null;
      try {
        const dimensions = jpegDimensions(
          new Uint8Array(await next.blob.arrayBuffer()),
        );
        const width = Math.min(1280, dimensions.width);
        const bitmap = await createImageBitmap(next.blob, {
          resizeWidth: width,
          resizeHeight: Math.round(
            (width * dimensions.height) / dimensions.width,
          ),
          resizeQuality: "low",
        });
        if (
          next.generation !== generation ||
          next.epoch !== imageEpoch ||
          destroyed ||
          !graphicsAvailable ||
          document.hidden ||
          !scene ||
          performance.now() - next.at > 2000
        ) {
          bitmap.close();
          continue;
        }
        scene.setFrame(bitmap);
        lastFrameAt = next.at;
        receivedFrames++;
        videoState = "Receiving";
        dirty = true;
      } catch (error) {
        if (next.generation === generation && next.epoch === imageEpoch) {
          loseVideo(
            error instanceof Error
              ? error.message
              : "Cannot decode camera JPEG.",
          );
          feedController?.abort();
        }
      }
    }
  } finally {
    decoding = false;
  }
}

function loseVideo(message: string) {
  imageEpoch++;
  pendingFrame = null;
  feedController?.abort();
  lastFrameAt = null;
  videoState = message;
  scene?.clearFrame(message);
  drill.updateDetections(null);
  stopDrive("Camera unavailable", true);
  dirty = true;
}

function connectStream() {
  if (destroyed || !graphicsAvailable || document.hidden) return;
  const current = generation;
  const epoch = ++imageEpoch;
  const controller = new AbortController();
  feedController = controller;
  videoState = "Connecting camera…";
  dirty = true;
  void receiveMjpeg("/stream.mjpg", controller.signal, (blob, at) => {
    if (
      current !== generation ||
      epoch !== imageEpoch ||
      controller.signal.aborted
    )
      return;
    pendingFrame = { blob, at, generation: current, epoch };
    void decodePending();
  }).catch((error) => {
    if (current !== generation || destroyed || document.hidden) return;
    loseVideo(
      error instanceof Error ? error.message : "Camera connection lost.",
    );
    streamReconnect = setTimeout(connectStream, 2000);
  });
}

function connectDetections() {
  if (destroyed || !graphicsAvailable || document.hidden) return;
  const current = generation;
  const socket = new WebSocket(websocketUrl("/ws/detections"));
  detectorSocket = socket;
  socket.addEventListener("message", (event) => {
    if (current !== generation || socket !== detectorSocket) return;
    const packet = parseDetections(event.data);
    // Duplicate/out-of-order frame IDs cannot refresh evidence freshness or dwell scoring.
    if (packet && detections && packet.frame <= detections.frame) return;
    detections = packet;
    detectionsAt = packet ? performance.now() : null;
    const live =
      lastFrameAt !== null && performance.now() - lastFrameAt <= 2000;
    drill.updateDetections(
      live ? packet : null,
      detectionsAt ?? performance.now(),
    );
    dirty = true;
  });
  socket.addEventListener("close", () => {
    if (current !== generation || destroyed) return;
    detections = null;
    detectionsAt = null;
    drill.updateDetections(null);
    dirty = true;
    detectorReconnect = setTimeout(connectDetections, 2000);
  });
  socket.addEventListener("error", () => {
    socket.close();
  });
}

function disconnectFeeds() {
  generation++;
  clearTimeout(streamReconnect);
  clearTimeout(detectorReconnect);
  feedController?.abort();
  feedController = null;
  detectorSocket?.close();
  detectorSocket = null;
  pendingFrame = null;
  detections = null;
  detectionsAt = null;
  control.dispose();
  loseVideo("Feeds disconnected");
}

function connectFeeds() {
  disconnectFeeds();
  if (destroyed || !graphicsAvailable || document.hidden) return;
  control = newControl();
  control.connect();
  connectStream();
  connectDetections();
}

function refresh(_frameTime: number, session: XRSession | null, yaw: number) {
  if (destroyed || !graphicsAvailable) return;
  // RAF/XR timestamps describe frame start and can precede JPEG arrival callbacks.
  // Compare all received evidence using its performance.now() clock instead.
  const now = performance.now();
  const live =
    lastFrameAt !== null && now >= lastFrameAt && now - lastFrameAt <= 2000;
  if (!live && hadVideo) loseVideo("Camera stalled — old image cleared");
  hadVideo = live;
  if (driveEnabled.checked && live && session) control.sampleXR(session, yaw);
  else if (!session)
    control.setKeyboard(driveEnabled.checked && live ? keys : new Set());
  else control.stop("Drive disabled or video unavailable");
  const snapshot = drill.tick(now);
  if (snapshot.status === "finished" && !finishedDrill) {
    stopDrive("Drill finished", true);
    finishedDrill = true;
  }
  if (!dirty && now - lastHudAt < 100) return;
  dirty = false;
  lastHudAt = now;
  const state: HudState = {
    detections,
    detectionsAt,
    reply: control.reply,
    replyAt: control.replyReceivedAt,
    now,
    videoLive: live,
    drill: snapshot,
  };
  const hud = summarizeHud(state);
  scene?.draw((ctx, width, height) => {
    drawHud(ctx, width, height, state);
    if (!live) {
      ctx.fillStyle = "#d5e1e8";
      ctx.textAlign = "center";
      ctx.font = `${width / 32}px system-ui`;
      ctx.fillText(videoState, width / 2, height / 2);
    }
    ctx.fillStyle = driveEnabled.checked ? "#62f5bd" : "#ffc96d";
    ctx.textAlign = "right";
    ctx.font = `${width / 43}px system-ui`;
    ctx.fillText(
      driveEnabled.checked ? "HOLD RIGHT TRIGGER TO DRIVE" : "DRIVE DISABLED",
      width - 12,
      height - 40,
    );
    if (source === "mock" || source === "replay") {
      ctx.textAlign = "left";
      ctx.fillStyle = "#ffc96d";
      ctx.fillText(sourceLabel, 12, height - 40);
    }
  });
  element("video-status").textContent = live ? "VIDEO RECEIVING" : videoState;
  element("video-status").dataset.state = live ? "live" : "lost";
  element("frame-count").textContent = `${receivedFrames} frames received`;
  element("link").textContent = hud.linkText;
  element("link").dataset.state = hud.link;
  element("range").textContent = hud.rangeText;
  element("estop").textContent = hud.estopText;
  element("estop").dataset.state = hud.estop;
  element("fps").textContent = hud.detectorFpsText;
  element("latency").textContent = hud.linkMsText;
  element("boxes").textContent = String(hud.activeBoxes.length);
  element("detector-status").textContent = hud.activeBoxes.length
    ? `${hud.activeBoxes.length} detection(s) · independently timed video`
    : "No fresh detections";
  element("input-status").textContent = driveEnabled.checked
    ? `Controls: ${control.inputStatus} · ${control.stopReason || "ready"}.`
    : `Drive disabled · control link ${control.state}. Release the deadman before enabling.`;
  element("input-status").dataset.controlState = control.state;
  element("input-status").dataset.stopReason = control.stopReason;
  element("timer").textContent = `${Math.ceil(snapshot.remainingMs / 1000)} s`;
  element("drill-status").textContent =
    snapshot.status === "running"
      ? "Drill running"
      : snapshot.status === "finished"
        ? `Drill finished · ${(snapshot.elapsedMs / 1000).toFixed(1)} s`
        : "Not started.";
  element("face-status").textContent =
    snapshot.faceSupport === "ready"
      ? "Faces count after one continuous second in view."
      : "Face scoring pending: detector owner must approve and publish stable face IDs.";
  element("drill-score").textContent =
    `${snapshot.foundCount}/${snapshot.expectedTotal ?? "?"} faces found · ${snapshot.safetyStops} safety stops`;
  element("route-note").textContent = snapshot.breadcrumbs.length
    ? snapshot.routeLabel
    : "Return trail needs commanded movement. No measured position is available.";
  element<HTMLButtonElement>("start-drill").disabled =
    snapshot.status === "running";
  element<HTMLButtonElement>("end-drill").disabled =
    snapshot.status !== "running";
  const list = element("return-trail");
  if (snapshot.status === "finished") {
    list.replaceChildren(
      ...snapshot.returnTrail.slice(0, 8).map((point) => {
        const item = document.createElement("li");
        item.textContent = `Approximate reverse breadcrumb: forward ${point.forward.toFixed(2)}, turn ${point.turn.toFixed(2)} command-seconds`;
        return item;
      }),
    );
  } else list.replaceChildren();
}

try {
  scene = new RoverScene(element<HTMLCanvasElement>("view"), {
    onFrame: refresh,
    onAction: (action) => (action === "drill" ? startDrill() : endDrill()),
    onSessionEnd: () => {
      stopDrive("VR session ended", true);
      element<HTMLButtonElement>("exit-vr").hidden = true;
      element<HTMLButtonElement>("enter-vr").hidden = false;
      element("vr-status").textContent = "VR ended. Desktop preview restored.";
    },
  });
  connectFeeds();
} catch (error) {
  element("video-status").textContent =
    error instanceof Error ? error.message : "Graphics unavailable.";
}

void (async () => {
  const supported =
    window.isSecureContext &&
    navigator.xr &&
    (await navigator.xr.isSessionSupported("immersive-vr").catch(() => false));
  const button = element<HTMLButtonElement>("enter-vr");
  button.disabled = !supported || !scene;
  button.textContent = supported ? "Enter VR" : "VR unavailable here";
  element("vr-status").textContent = supported
    ? "Quest browser ready. Enable driving only when you intend to move the rover."
    : "Desktop preview active. Enter VR needs a supported Quest browser and trusted HTTPS.";
})();

element("enter-vr").addEventListener("click", async () => {
  if (!scene || connectingVr) return;
  connectingVr = true;
  stopDrive("Entering VR");
  const button = element<HTMLButtonElement>("enter-vr");
  button.disabled = true;
  try {
    await scene.enterVR();
    button.hidden = true;
    element<HTMLButtonElement>("exit-vr").hidden = false;
    element("vr-status").textContent =
      "VR active. Left trigger selects the menu; right trigger drives when enabled.";
  } catch (error) {
    element("vr-status").textContent =
      error instanceof Error ? error.message : "Could not enter VR.";
  } finally {
    button.disabled = false;
    connectingVr = false;
  }
});
element("view").addEventListener("webglcontextlost", (event) => {
  event.preventDefault();
  graphicsAvailable = false;
  stopDrive("Graphics context lost", true);
  disconnectFeeds();
  element("video-status").textContent = "Graphics lost — driving disabled";
  element("video-status").dataset.state = "lost";
  element<HTMLButtonElement>("enter-vr").disabled = true;
  driveEnabled.disabled = true;
  element<HTMLButtonElement>("reconnect").disabled = true;
  void scene?.exitVR();
});
element("view").addEventListener("webglcontextrestored", () => {
  if (destroyed) return;
  graphicsAvailable = true;
  connectFeeds();
  driveEnabled.disabled = false;
  element<HTMLButtonElement>("reconnect").disabled = false;
  void navigator.xr?.isSessionSupported("immersive-vr").then((supported) => {
    element<HTMLButtonElement>("enter-vr").disabled = !supported;
  });
});
element("exit-vr").addEventListener("click", () => {
  stopDrive("Exiting VR", true);
  void scene?.exitVR();
});
element("recenter").addEventListener("click", () => {
  stopDrive("Recentering camera", true);
  scene?.recenter();
});
element("stop").addEventListener("click", () =>
  stopDrive("Operator stop", true),
);
element("reconnect").addEventListener("click", () => {
  stopDrive("Reconnecting", true);
  connectFeeds();
});
element("start-drill").addEventListener("click", startDrill);
element("end-drill").addEventListener("click", endDrill);
driveEnabled.addEventListener("change", () => {
  stopDrive("Drive enable changed", !graphicsAvailable);
  if (graphicsAvailable) control.setKeyboard(keys);
});
window.addEventListener("keydown", (event) => {
  if ((event.target as HTMLElement)?.matches("input,textarea,button")) return;
  if (["Space", "KeyW", "KeyA", "KeyS", "KeyD"].includes(event.code)) {
    event.preventDefault();
    if (event.repeat || !graphicsAvailable) return;
    keys.add(event.code);
    if (
      driveEnabled.checked &&
      lastFrameAt !== null &&
      performance.now() - lastFrameAt <= 2000
    )
      control.setKeyboard(keys);
  }
});
window.addEventListener("keyup", (event) => {
  keys.delete(event.code);
  control.setKeyboard(
    driveEnabled.checked &&
      graphicsAvailable &&
      lastFrameAt !== null &&
      performance.now() - lastFrameAt <= 2000
      ? keys
      : new Set(),
  );
});
window.addEventListener("blur", () => stopDrive("Window lost focus", true));
document.addEventListener("visibilitychange", () => {
  stopDrive("Visibility changed", true);
  if (document.hidden) disconnectFeeds();
  else connectFeeds();
});
window.addEventListener("pagehide", () => {
  destroyed = true;
  disconnectFeeds();
  scene?.dispose();
  scene = null;
});
