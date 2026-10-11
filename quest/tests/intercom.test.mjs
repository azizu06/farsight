import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const temporary = mkdtempSync(join(tmpdir(), "farsight-intercom-test-"));
execFileSync(fileURLToPath(new URL("../node_modules/.bin/tsc", import.meta.url)), [
  fileURLToPath(new URL("../src/intercom.ts", import.meta.url)),
  "--target", "ES2022", "--module", "ESNext", "--strict", "--skipLibCheck",
  "--lib", "ES2022,DOM", "--outDir", temporary,
]);
writeFileSync(join(temporary, "package.json"), '{"type":"module"}');
const intercomModule = await import(pathToFileURL(join(temporary, "intercom.js")).href);
rmSync(temporary, { recursive: true, force: true });

test("Quest mic audio becomes exact little-endian 20 ms PCM16 frames", () => {
  const encoder = new intercomModule.PCMFrameEncoder(48_000);
  const samples = new Float32Array(960);
  samples[0] = -1; samples[3] = 1; samples[6] = 0.5; samples[9] = -0.5;
  const [packet] = encoder.push(samples);
  assert.equal(packet.byteLength, 640);
  const view = new DataView(packet);
  assert.equal(view.getInt16(0, true), -32768);
  assert.equal(view.getInt16(2, true), 32767);
  assert.equal(view.getInt16(4, true), 16383);
  assert.equal(view.getInt16(6, true), -16384);
});

test("audio packetization carries partial samples across browser audio callbacks", () => {
  const encoder = new intercomModule.PCMFrameEncoder(48_000);
  assert.deepEqual(encoder.push(new Float32Array(500)), []);
  const frames = encoder.push(new Float32Array(460));
  assert.equal(frames.length, 1);
  assert.equal(frames[0].byteLength, 640);
});

test("only explicit talk and peer control messages are recognized", () => {
  assert.deepEqual(intercomModule.parseIntercomControl('{"type":"talk","active":true}'), { type: "talk", active: true });
  assert.deepEqual(intercomModule.parseIntercomControl('{"type":"peer","connected":true}'), { type: "peer", connected: true });
  for (const text of ['{"type":"talk","active":1}', '{"type":"peer","connected":"yes"}', '{"type":"other"}', "nope"]) {
    assert.equal(intercomModule.parseIntercomControl(text), null);
  }
});

test("Quest audio socket stays same-origin and stale buffered audio is dropped", () => {
  assert.equal(intercomModule.intercomSocketURL("https://localhost:8080/base"), "wss://localhost:8080/ws/intercom?role=quest");
  assert.equal(intercomModule.intercomSocketURL("http://localhost:8080"), "ws://localhost:8080/ws/intercom?role=quest");
  assert.equal(intercomModule.canSendIntercomAudio({ readyState: 1, bufferedAmount: 2559 }), true);
  assert.equal(intercomModule.canSendIntercomAudio({ readyState: 1, bufferedAmount: 2560 }), false);
  assert.equal(intercomModule.canSendIntercomAudio({ readyState: 0, bufferedAmount: 0 }), false);
});

test("the explicit headset toggle gates both mics and both speakers", async () => {
  const keys = ["navigator", "window", "location", "WebSocket", "AudioContext"];
  const originals = new Map(keys.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const sockets = [], contexts = [], constraints = [];
  class Socket {
    static OPEN = 1; static CONNECTING = 0; static CLOSED = 3;
    readyState = 0; bufferedAmount = 0; binaryType = ""; sent = [];
    constructor(url) {
      this.url = url; sockets.push(this);
      queueMicrotask(() => {
        this.readyState = 1; this.onopen?.();
        this.onmessage?.({ data: JSON.stringify({ type: "peer", connected: true }) });
      });
    }
    send(value) {
      this.sent.push(value);
      if (typeof value === "string" && value.includes('"type":"talk"'))
        queueMicrotask(() => this.onmessage?.({ data: value }));
    }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  class AudioNode { connect() {} disconnect() {} }
  class Context {
    state = "running"; sampleRate = 48_000; currentTime = 0; destination = {};
    createMediaStreamSource() { return new AudioNode(); }
    createScriptProcessor() { const node = new AudioNode(); contexts.at(-1).processor = node; return node; }
    createGain() { return Object.assign(new AudioNode(), { gain: { value: 1 } }); }
    createBuffer(_channels, length) { return { samples: new Float32Array(length), getChannelData() { return this.samples; } }; }
    createBufferSource() {
      const node = Object.assign(new AudioNode(), { starts: [], stops: 0, start(at) { this.starts.push(at); }, stop() { this.stops++; this.onended?.(); } });
      contexts.at(-1).speakers.push(node); return node;
    }
    resume() { return Promise.resolve(); }
    close() { this.state = "closed"; return Promise.resolve(); }
    constructor() { this.speakers = []; contexts.push(this); }
  }
  const track = { stopped: 0, stop() { this.stopped++; } };
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia(options) { constraints.push(options); return Promise.resolve({ getTracks: () => [track] }); },
  } } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    setTimeout(callback, ms) { const timer = setTimeout(callback, ms); timer.unref?.(); return timer; },
    clearTimeout,
  } });
  Object.defineProperty(globalThis, "location", { configurable: true, value: { origin: "https://localhost:8080" } });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: Socket });
  Object.defineProperty(globalThis, "AudioContext", { configurable: true, value: Context });
  try {
    const states = [];
    const intercom = new intercomModule.QuestIntercom(value => states.push(value));
    assert.equal(sockets.length, 0, "ordinary desktop browsing must not claim the single Quest relay peer");
    assert.equal(constraints.length, 0);
    intercom.connectIfNeeded();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(track.stopped, 0);
    assert.equal(constraints.length, 0, "microphone permission remains untouched until the user toggles Talk");

    await intercom.toggle();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(constraints.length, 1);
    assert.equal(constraints[0].audio.echoCancellation, true);
    assert.equal(sockets[0].sent[0], JSON.stringify({ type: "talk", active: true }));
    contexts[0].processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(960).fill(.25) } });
    assert.equal(sockets[0].sent.at(-1).byteLength, 640, "Quest mic sends framed PCM only after the relay marks talk active");

    sockets[0].onmessage({ data: new ArrayBuffer(640) });
    assert.equal(contexts[0].speakers.length, 1, "iPhone mic audio is scheduled to Quest speakers while the same talk session is active");
    assert.equal(contexts[0].speakers[0].starts.length, 1);

    // Cellular delivery can put several successive frames in one browser turn.
    // Active buffers must still represent one speaker, not overlapping speech.
    for(let i=0;i<10;i++) sockets[0].onmessage({data:new ArrayBuffer(640)});
    const active=contexts[0].speakers.filter(node=>node.stops===0).map(node=>node.starts[0]).sort((a,b)=>a-b);
    assert.ok(active.length>0);
    for(let i=1;i<active.length;i++) assert.ok(active[i]-active[i-1]>=.02-1e-9,
      'cellular packet batches must not schedule simultaneous speech frames');
    assert.ok(contexts[0].speakers.some(node=>node.stops>0),'resynchronizing cancels superseded playback');
    // Outgoing mic frames still flow while incoming playback catches up.
    contexts[0].processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(960).fill(.1)}});
    assert.equal(sockets[0].sent.at(-1).byteLength,640);

    await intercom.toggle();
    assert.equal(sockets[0].sent.at(-1), JSON.stringify({ type: "talk", active: false }));
    assert.equal(track.stopped, 1);
    assert.equal(contexts[0].speakers[0].stops, 1);
    intercom.dispose();
    assert.ok(states.some(state => state.enabled));
    assert.ok(states.some(state => !state.enabled));
  } finally {
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
    }
  }
});
