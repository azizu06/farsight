import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// Compile the actual browser component, then spy only its DOM/media boundaries.
const temporary = mkdtempSync(join(tmpdir(), "farsight-voice-test-"));
execFileSync(fileURLToPath(new URL("../node_modules/.bin/tsc", import.meta.url)), [
  fileURLToPath(new URL("../src/assistant-voice.ts", import.meta.url)),
  "--target", "ES2022", "--module", "ESNext", "--strict", "--skipLibCheck",
  "--lib", "ES2022,DOM", "--outDir", temporary,
]);
writeFileSync(join(temporary, "package.json"), '{"type":"module"}');
const { AssistantVoice } = await import(pathToFileURL(join(temporary, "assistant-voice.js")).href);
rmSync(temporary, { recursive: true, force: true });

const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture(fetchResponse = null, vrPushToTalk = true) {
  const originals = new Map(["document", "window", "navigator", "MediaRecorder", "fetch", "location", "Audio"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const listeners = new Map(), closeListeners = new Map(), buttons = new Map();
  const pendingPermissions = [], pendingDenials = [], recorders = [], stopEvents = [], timers = new Map(), uploads = [], playback = [];
  let permissionCalls = 0, requests = 0, timerID = 0;
  const media = { hidden: false, addEventListener: (key, value) => listeners.set(key, value), removeEventListener: key => listeners.delete(key),
    createElement: () => ({ innerHTML: "", className: "", querySelector: selector => {
      if (!buttons.has(selector)) buttons.set(selector, { disabled: false, textContent: "", onclick: null });
      return buttons.get(selector);
    } }) };
  const settings = { open: true, addEventListener: (key, value) => closeListeners.set(key, value), removeEventListener: key => closeListeners.delete(key) };
  class Recorder {
    static isTypeSupported(type) { return type.startsWith("audio/webm"); }
    constructor(stream, options) { this.stream = stream; this.mimeType = options?.mimeType ?? "audio/webm"; this.state = "inactive"; recorders.push(this); }
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; stopEvents.push(() => this.onstop?.()); }
  }
  const set = (key, value) => Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  set("document", media);
  set("window", { setTimeout: callback => { const id = ++timerID; timers.set(id, callback); return id; }, clearTimeout: id => timers.delete(id) });
  set("navigator", { mediaDevices: { getUserMedia: () => {
    permissionCalls++; return new Promise((resolve, reject) => { pendingPermissions.push(resolve); pendingDenials.push(reject); });
  } } });
  set("MediaRecorder", Recorder);
  set("location", { origin: "https://quest.test" });
  set("Audio", class { constructor(url) { this.url = url; } async play() { playback.push(this.url); } pause() {} });
  set("fetch", (url, options) => { requests++; uploads.push({ url, ...options }); if (fetchResponse) return Promise.resolve(fetchResponse); throw new Error("Cancelled recordings must not submit audio"); });
  const textButton = { disabled: false };
  const confirmed = [], starts = [];
  const voice = new AssistantVoice({ append() {} }, settings, textButton, () => starts.push(Date.now()), () => {}, action => confirmed.push(action), vrPushToTalk);
  const stream = () => { const track = { stops: 0, stop() { this.stops++; } }; return { track, getTracks: () => [track] }; };
  const cleanup = () => {
    voice.dispose();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  };
  const enable = async () => {
    buttons.get("#voice-vr-enable").onclick();
    const probe = stream(); pendingPermissions.shift()(probe); pendingDenials.shift(); await flush();
    return probe;
  };
  return { voice, confirmed, starts, enable, pendingDenials, textButton, buttons, settings, media, listeners, closeListeners, pendingPermissions, recorders, stopEvents, timers, stream, uploads, playback,
    permissionCalls: () => permissionCalls, requests: () => requests, cleanup };
}

test("microphone is never acquired automatically; late permission after dismiss is stopped", async () => {
  const f = fixture();
  try {
    assert.equal(f.permissionCalls(), 0);
    f.buttons.get("#voice-record").onclick();
    assert.equal(f.permissionCalls(), 1);
    f.settings.open = false; f.closeListeners.get("close")();
    const stream = f.stream(); f.pendingPermissions.shift()(stream); await flush();
    assert.equal(stream.track.stops, 1);
    assert.equal(f.recorders.length, 0);
    assert.equal(f.requests(), 0);
  } finally { f.cleanup(); }
});

test("cancelled recorder's late onstop cannot stop a newly started microphone", async () => {
  const f = fixture();
  try {
    f.buttons.get("#voice-record").onclick();
    const first = f.stream(); f.pendingPermissions.shift()(first); await flush();
    f.voice.cancelCapture();
    f.buttons.get("#voice-record").onclick();
    const second = f.stream(); f.pendingPermissions.shift()(second); await flush();
    assert.equal(f.recorders[1].state, "recording");
    f.stopEvents.shift()(); await flush();
    assert.equal(second.track.stops, 0);
    assert.equal(f.recorders[1].state, "recording");
    assert.equal(f.requests(), 0);
    f.media.hidden = true; f.listeners.get("visibilitychange")();
    assert.equal(second.track.stops, 1);
    assert.equal(f.recorders[1].state, "inactive");
    f.stopEvents.shift()(); await flush();
    assert.equal(f.requests(), 0);
  } finally { f.cleanup(); }
});

test("maximum recording timer stops tracks and submits one local question", async () => {
  const f = fixture({ ok: true, json: async () => ({ answer: "A measured scene answer", audio_url: null }) });
  try {
    f.buttons.get("#voice-record").onclick();
    const stream = f.stream(); f.pendingPermissions.shift()(stream); await flush();
    f.recorders[0].ondataavailable({ data: new Blob(["recorded audio"], { type: "audio/webm" }) });
    [...f.timers.values()][0]();
    assert.equal(f.recorders[0].state, "inactive");
    assert.equal(stream.track.stops, 1);
    f.stopEvents.shift()(); await flush();
    assert.equal(f.requests(), 1);
    assert.equal(f.buttons.get("#voice-play").disabled, false);
    assert.equal(f.buttons.get("#voice-record").textContent, "Ask by voice");
  } finally { f.cleanup(); }
});

test("voice cleanup cannot unlock an unrelated in-flight text request", () => {
  const f = fixture();
  try {
    f.textButton.disabled = true;
    f.closeListeners.get("close")();
    assert.equal(f.textButton.disabled, true);
    assert.equal(f.permissionCalls(), 0);
  } finally { f.cleanup(); }
});

test("a proposed action is only a prompt until the operator confirms it once", () => {
  const f = fixture();
  try {
    f.voice.setReply({ answer: "One person ahead. Confirm on the headset to seek person #3.", action: { type: "seek", target_id: 3 } });
    assert.match(f.buttons.get("#voice-action").textContent, /Seek person #3\?/);
    assert.equal(f.buttons.get("#voice-confirm").disabled, false);
    assert.deepEqual(f.confirmed, []);
    f.buttons.get("#voice-confirm").onclick();
    assert.deepEqual(f.confirmed, [{ type: "seek", target_id: 3 }]);
    assert.equal(f.buttons.get("#voice-confirm").disabled, true);
    f.buttons.get("#voice-confirm").onclick();
    assert.equal(f.confirmed.length, 1);
    f.voice.setReply({ answer: "Scanning needs confirmation.", action: { type: "scan" } });
    assert.match(f.buttons.get("#voice-action").textContent, /Scan the room\?/);
  } finally { f.cleanup(); }
});

test("dismissed, malformed, superseded, and stale proposals never confirm", () => {
  const f = fixture();
  const realNow = Date.now;
  try {
    f.voice.setReply({ answer: "a", action: { type: "seek", target_id: 3 } });
    f.buttons.get("#voice-dismiss").onclick();
    f.buttons.get("#voice-confirm").onclick();
    for (const action of [{ type: "launch" }, { type: "seek", target_id: "3" }, { type: "seek" }, null]) {
      f.voice.setReply({ answer: "a", action });
      assert.equal(f.buttons.get("#voice-confirm").disabled, true);
      f.buttons.get("#voice-confirm").onclick();
    }
    f.voice.setReply({ answer: "a", action: { type: "seek", target_id: 4 } });
    f.voice.cancelCapture();
    f.buttons.get("#voice-confirm").onclick();
    f.voice.setReply({ answer: "a", action: { type: "seek", target_id: 5 } });
    const later = realNow() + 20001; Date.now = () => later;
    f.buttons.get("#voice-confirm").onclick();
    assert.deepEqual(f.confirmed, []);
    assert.equal(f.buttons.get("#voice-confirm").disabled, true);
  } finally { Date.now = realNow; f.cleanup(); }
});

test("push-to-talk needs the settings opt-in; the opt-in microphone is kept for VR and released on dispose", async () => {
  const f = fixture();
  try {
    f.settings.open = false;
    assert.equal(f.voice.pushStart(), false);
    assert.equal(f.permissionCalls(), 0);
    assert.equal(f.voice.phase, "error");
    const probe = await f.enable();
    assert.equal(probe.track.stops, 0, "the Quest cannot show a permission prompt inside VR");
    assert.equal(f.voice.vrEnabled, true);
    assert.equal(f.recorders.length, 0);
    f.voice.dispose();
    assert.equal(probe.track.stops, 1);
  } finally { f.cleanup(); }
});

test("held left trigger records from the kept opt-in microphone without a new permission request", async () => {
  const f = fixture({ ok: true, json: async () => ({ answer: "A doorway ahead" }) });
  try {
    f.buttons.get("#voice-vr-enable").onclick();
    const track = { stops: 0, readyState: "live", stop() { this.stops++; this.readyState = "ended"; } };
    f.pendingPermissions.shift()({ track, getTracks: () => [track] }); f.pendingDenials.shift(); await flush();
    f.settings.open = false;
    assert.equal(f.voice.pushStart(), true);
    await flush();
    assert.equal(f.permissionCalls(), 1, "only the 2D opt-in asked for the microphone");
    assert.equal(f.voice.phase, "listening");
    f.recorders[0].ondataavailable({ data: new Blob(["audio"], { type: "audio/webm" }) });
    f.voice.pushEnd();
    f.stopEvents.shift()(); await flush(); await flush();
    assert.equal(f.requests(), 1);
    assert.equal(track.stops, 0, "the kept microphone stays open for the next question");
    assert.equal(f.voice.pushStart(), true);
    await flush();
    assert.equal(f.permissionCalls(), 1);
  } finally { f.cleanup(); }
});

test("held left trigger records in VR with settings closed; release sends one local question", async () => {
  const f = fixture({ ok: true, json: async () => ({ answer: "Two people ahead", audio_url: null }) });
  try {
    await f.enable(); f.settings.open = false;
    assert.equal(f.voice.pushStart(), true);
    assert.equal(f.starts.length, 1, "starting a question stops the rover");
    assert.equal(f.voice.capturing, true);
    const stream = f.stream(); f.pendingPermissions.shift()(stream); await flush();
    assert.equal(f.voice.phase, "listening");
    assert.equal(f.voice.pushStart(), false, "a second press cannot open another microphone");
    f.recorders[0].ondataavailable({ data: new Blob(["audio"], { type: "audio/webm" }) });
    f.voice.pushEnd();
    assert.equal(stream.track.stops, 1);
    assert.equal(f.voice.capturing, false);
    f.stopEvents.shift()(); await flush(); await flush();
    assert.equal(f.requests(), 1);
    assert.equal(f.voice.phase, "answer");
    assert.equal(f.voice.answerText, "Two people ahead");
    assert.equal(f.textButton.disabled, false);
  } finally { f.cleanup(); }
});

test("demo preference arms without a mic probe; held question uploads to /ask and plays rover speech on the headset", async () => {
  const f = fixture({ ok: true, json: async () => ({ question: "What do you see?", answer: "A doorway ahead", audio_url: "/tts/reply.wav" }) });
  try {
    f.voice.restoreVR(); f.settings.open = false;
    assert.equal(f.permissionCalls(), 0, "arming never opens the microphone");
    assert.equal(f.voice.pushStart(), true);
    assert.equal(f.permissionCalls(), 1, "holding the trigger requests headset audio");
    assert.equal(f.voice.busy, true);
    const stream = f.stream(); f.pendingPermissions.shift()(stream); await flush();
    f.recorders[0].ondataavailable({ data: new Blob(["Quest mic audio"], { type: "audio/webm" }) });
    f.voice.pushEnd();
    assert.equal(f.voice.busy, true, "release keeps motion held through processing");
    f.stopEvents.shift()(); await flush(); await flush();
    assert.equal(stream.track.stops >= 1, true);
    assert.equal(f.uploads[0].url, "/ask");
    assert.equal(f.uploads[0].method, "POST");
    assert.equal(await f.uploads[0].body.get("audio").text(), "Quest mic audio");
    assert.equal(f.voice.questionText, "What do you see?");
    assert.equal(f.voice.answerText, "A doorway ahead");
    assert.deepEqual(f.playback, ["https://quest.test/tts/reply.wav"]);
    assert.equal(f.voice.busy, false);
  } finally { f.cleanup(); }
});

test("denied microphone, early release and tracking loss stop every track and send nothing", async () => {
  const f = fixture();
  try {
    await f.enable(); f.settings.open = false;
    f.voice.pushStart();
    f.pendingPermissions.shift(); f.pendingDenials.shift()(new Error("NotAllowedError")); await flush();
    assert.equal(f.voice.phase, "error");
    assert.match(f.voice.statusText, /Microphone unavailable/);
    assert.equal(f.textButton.disabled, false);

    f.voice.pushStart(); f.voice.pushEnd();
    assert.equal(f.voice.capturing, false);
    const late = f.stream(); f.pendingPermissions.shift()(late); f.pendingDenials.shift(); await flush();
    assert.equal(late.track.stops, 1);
    assert.equal(f.recorders.length, 0);

    f.voice.pushStart();
    const live = f.stream(); f.pendingPermissions.shift()(live); f.pendingDenials.shift(); await flush();
    assert.equal(f.voice.phase, "listening");
    f.voice.stopListening("XR tracking lost: voice question cancelled");
    assert.equal(live.track.stops, 1);
    assert.equal(f.recorders.at(-1).state, "inactive");
    f.stopEvents.shift()(); await flush();
    assert.equal(f.requests(), 0);
    assert.match(f.voice.statusText, /tracking lost/);
  } finally { f.cleanup(); }
});

test("push-to-talk keeps the ten-second cap", async () => {
  const f = fixture({ ok: true, json: async () => ({ answer: "ok" }) });
  try {
    await f.enable(); f.settings.open = false;
    f.voice.pushStart();
    const stream = f.stream(); f.pendingPermissions.shift()(stream); await flush();
    f.recorders[0].ondataavailable({ data: new Blob(["audio"], { type: "audio/webm" }) });
    [...f.timers.values()].at(-1)();
    assert.equal(stream.track.stops, 1);
    f.voice.pushEnd();
    f.stopEvents.shift()(); await flush(); await flush();
    assert.equal(f.requests(), 1);
  } finally { f.cleanup(); }
});

test("in-VR confirmation acts once on a fresh proposal only", () => {
  const f = fixture();
  try {
    assert.equal(f.voice.confirmAction(), false);
    f.voice.setReply({ answer: "a", action: { type: "scan" } });
    assert.deepEqual(f.voice.pendingAction, { type: "scan" });
    assert.equal(f.voice.confirmAction(), true);
    assert.equal(f.voice.confirmAction(), false);
    f.voice.setReply({ answer: "a", action: { type: "seek", target_id: 2 } });
    f.voice.dismissAction();
    assert.equal(f.voice.pendingAction, null);
    assert.deepEqual(f.confirmed, [{ type: "scan" }]);
  } finally { f.cleanup(); }
});

test("without a rover Whisper the voice controls hide and the left trigger starts nothing", async () => {
  const f = fixture();
  try {
    f.voice.setAvailable(false);
    assert.equal(f.voice.voiceAvailable, false);
    for (const id of ["#voice-record", "#voice-cancel", "#voice-vr-enable"]) assert.equal(f.buttons.get(id).hidden, true, id);
    f.settings.open = false;
    assert.equal(f.voice.pushStart(), false);
    assert.equal(f.voice.phase, "idle");   // no VOICE ERROR panel
    assert.equal(f.starts.length, 0);
    f.voice.restoreVR();
    assert.equal(f.permissionCalls(), 0);   // a saved voice=1 never opens the mic either
    f.voice.setAvailable(true);
    for (const id of ["#voice-record", "#voice-cancel", "#voice-vr-enable"]) assert.equal(f.buttons.get(id).hidden, false, id);
  } finally { f.cleanup(); }
});

test("a page without VR push-to-talk keeps desktop questions without offering or restoring the removed control", async () => {
  const f = fixture({ ok: true, json: async () => ({ answer: "A doorway ahead" }) }, false);
  try {
    f.voice.setAvailable(false); f.voice.setAvailable(true);
    assert.equal(f.buttons.get("#voice-vr-enable").hidden, true);
    assert.equal(f.buttons.get("#voice-record").hidden, false);
    f.voice.restoreVR();
    f.buttons.get("#voice-vr-enable").onclick();
    assert.equal(f.voice.pushStart(), false);
    assert.equal(f.permissionCalls(), 0, "a saved VR preference cannot open an unsupported microphone control");
    assert.equal(f.voice.phase, "idle");

    f.buttons.get("#voice-record").onclick();
    assert.equal(f.permissionCalls(), 1, "the desktop record button still opens its microphone");
    const stream = f.stream(); f.pendingPermissions.shift()(stream); await flush();
    assert.equal(f.voice.phase, "listening");
    f.recorders[0].ondataavailable({ data: new Blob(["audio"], { type: "audio/webm" }) });
    f.buttons.get("#voice-record").onclick();
    f.stopEvents.shift()(); await flush(); await flush();
    assert.equal(stream.track.stops >= 1, true);
    assert.equal(f.requests(), 1);
    assert.equal(f.voice.answerText, "A doorway ahead");
    assert.equal(f.voice.phase, "answer");
    assert.equal(f.textButton.disabled, false);
  } finally { f.cleanup(); }
});
