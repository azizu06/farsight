import test from "node:test";
import assert from "node:assert/strict";
import { VoiceAssistant, parseAskReply, actionLabel, pickMimeType } from "../src/assistant.js";

function harness(reply = { question: "what do you see", answer: "A hallway.", action: null, model: "m", ms: 5 }, options = {}) {
  const calls = { fetch: [], played: [], spoken: [], confirmed: [], states: [] };
  let clock = 1000;
  const recorder = {
    state: "inactive", mimeType: "audio/webm;codecs=opus",
    start() { this.state = "recording"; },
    stop() { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["opus"]) }); this.onstop?.(); },
  };
  const assistant = new VoiceAssistant({
    fetch: async (url, init) => {
      calls.fetch.push({ url, init });
      if (options.status) return { ok: false, status: options.status, json: async () => ({ detail: "Speech recognition unavailable" }) };
      return { ok: true, status: 200, json: async () => reply };
    },
    getMicrophone: async () => ({ id: "mic" }),
    createRecorder: () => recorder,
    play: async url => { calls.played.push(url); if (options.playFails) throw new Error("blocked"); },
    speak: text => calls.spoken.push(text),
    onChange: view => calls.states.push(view.state),
    onConfirm: action => { calls.confirmed.push(action); return options.note; },
    now: () => clock,
  });
  return { assistant, calls, recorder, advance: ms => { clock += ms; } };
}

test("hold to talk records, posts multipart audio, and speaks the answer", async () => {
  const { assistant, calls, recorder, advance } = harness();
  await assistant.press();
  assert.equal(recorder.state, "recording");
  assert.equal(assistant.state, "listening");
  advance(800);
  await assistant.release();
  assert.equal(calls.fetch[0].url, "/ask");
  const audio = calls.fetch[0].init.body.get("audio");
  assert.equal(audio.type, "audio/webm;codecs=opus");
  assert.equal(assistant.state, "answer");
  assert.equal(assistant.answer, "A hallway.");
  assert.deepEqual(calls.spoken, ["A hallway."]);
  assert.deepEqual(calls.states.slice(0, 3), ["listening", "thinking", "answer"]);
});

test("a tap shorter than the minimum is not sent", async () => {
  const { assistant, calls, advance } = harness();
  await assistant.press(); advance(100); await assistant.release();
  assert.equal(calls.fetch.length, 0);
  assert.equal(assistant.state, "idle");
});

test("server wav plays, and speechSynthesis is the fallback", async () => {
  const reply = { question: "q", answer: "One person.", action: null, model: "m", ms: 1, audio_url: "/tts/reply-ab.wav" };
  const played = harness(reply);
  await played.assistant.askText("q");
  assert.deepEqual(played.calls.played, ["/tts/reply-ab.wav"]);
  assert.deepEqual(played.calls.spoken, []);
  const blocked = harness(reply, { playFails: true });
  await blocked.assistant.askText("q");
  assert.deepEqual(blocked.calls.spoken, ["One person."]);
});

test("text questions post JSON", async () => {
  const { assistant, calls } = harness();
  await assistant.askText("  What do you see?  ");
  assert.equal(calls.fetch[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls.fetch[0].init.body), { text: "What do you see?" });
});

test("an action is only a prompt until confirmed, and confirmation expires", async () => {
  const seek = { question: "go to that person", answer: "Confirm to seek.", action: { type: "seek", target_id: 3 }, model: "m", ms: 1 };
  const { assistant, calls, advance } = harness(seek);
  await assistant.askText("go to that person");
  assert.deepEqual(assistant.action, { type: "seek", target_id: 3 });
  assert.equal(calls.confirmed.length, 0);
  assistant.confirm();
  assert.deepEqual(calls.confirmed, [{ type: "seek", target_id: 3 }]);
  assert.equal(assistant.action, null);
  await assistant.askText("go to that person");
  advance(20001);
  assistant.confirm();
  assert.equal(calls.confirmed.length, 1);
  assert.equal(assistant.action, null);
  await assistant.askText("go to that person");
  assistant.dismiss();
  assistant.confirm();
  assert.equal(calls.confirmed.length, 1);
});

test("the confirm handler can report what happened", async () => {
  const scan = { question: "q", answer: "a", action: { type: "scan" }, model: "m", ms: 1 };
  const { assistant } = harness(scan, { note: "Scan is not available yet" });
  await assistant.askText("scan");
  assistant.confirm();
  assert.equal(assistant.answer, "Scan is not available yet");
});

test("controller edges: hold talk, confirm and dismiss fire once per press", async () => {
  const seek = { question: "q", answer: "a", action: { type: "scan" }, model: "m", ms: 1 };
  const { assistant, calls, advance } = harness(seek);
  await assistant.input(true, false, false);
  await assistant.input(true, false, false);
  assert.equal(assistant.state, "listening");
  advance(500);
  await assistant.input(false, false, false);
  assert.equal(calls.fetch.length, 1);
  await assistant.input(false, true, false);
  await assistant.input(false, true, false);
  assert.deepEqual(calls.confirmed, [{ type: "scan" }]);
});

test("server errors are shown, not spoken as answers", async () => {
  const { assistant, calls } = harness(undefined, { status: 503 });
  await assistant.askText("q");
  assert.equal(assistant.state, "error");
  assert.match(assistant.answer, /Speech recognition unavailable/);
  assert.deepEqual(calls.spoken, []);
});

test("replies are validated", () => {
  assert.deepEqual(parseAskReply({ question: "q", answer: "a", action: null, model: "m", ms: 3 }).action, null);
  assert.equal(parseAskReply({ question: "q", answer: "a", action: { type: "seek", target_id: 2 }, audio_url: "/tts/reply-1.wav" }).audioUrl, "/tts/reply-1.wav");
  assert.throws(() => parseAskReply({ answer: 4 }));
  assert.equal(parseAskReply({ question: "q", answer: "a", action: { type: "launch" } }).action, null);
  assert.equal(parseAskReply({ question: "q", answer: "a", action: { type: "seek", target_id: "3" } }).action, null);
  assert.equal(parseAskReply({ question: "q", answer: "a", action: null, audio_url: "https://evil/x.wav" }).audioUrl, null);
});

test("labels and recorder format", () => {
  assert.equal(actionLabel({ type: "seek", target_id: 3 }), "Seek person #3");
  assert.equal(actionLabel({ type: "scan" }), "Scan the room");
  assert.equal(pickMimeType(type => type === "audio/webm;codecs=opus"), "audio/webm;codecs=opus");
  assert.equal(pickMimeType(() => false), "");
});

test("releasing while the microphone is still opening stops the recording", async () => {
  const { assistant, calls, recorder, advance } = harness();
  let grant;
  assistant.getMicrophone = () => new Promise(resolve => { grant = resolve; });
  const pressing = assistant.input(true, false, false);
  await assistant.input(false, false, false);
  advance(500);
  grant({ id: "mic" });
  await pressing;
  assert.equal(recorder.state, "inactive");
  assert.equal(assistant.state, "idle");
  assert.equal(calls.fetch.length, 0);
});
