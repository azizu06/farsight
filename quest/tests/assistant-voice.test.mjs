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
function fixture(fetchResponse = null) {
  const originals = new Map(["document", "window", "navigator", "MediaRecorder", "fetch"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const listeners = new Map(), closeListeners = new Map(), buttons = new Map();
  const pendingPermissions = [], recorders = [], stopEvents = [], timers = new Map();
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
    permissionCalls++; return new Promise(resolve => pendingPermissions.push(resolve));
  } } });
  set("MediaRecorder", Recorder);
  set("fetch", () => { requests++; if (fetchResponse) return Promise.resolve(fetchResponse); throw new Error("Cancelled recordings must not submit audio"); });
  const textButton = { disabled: false };
  const voice = new AssistantVoice({ append() {} }, settings, textButton, () => {}, () => {});
  const stream = () => { const track = { stops: 0, stop() { this.stops++; } }; return { track, getTracks: () => [track] }; };
  const cleanup = () => {
    voice.dispose();
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  };
  return { voice, textButton, buttons, settings, media, listeners, closeListeners, pendingPermissions, recorders, stopEvents, timers, stream,
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
