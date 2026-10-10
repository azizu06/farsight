import test from "node:test";
import assert from "node:assert/strict";
import { PHRASE_LABELS, sbarLines, speakPhrase } from "../src/survivor-phrases.js";

const reply = (status, body) => async (url, init) => {
  reply.last = { url, body: JSON.parse(init.body) };
  return { ok: status < 400, status, json: async () => body };
};

test("rover speaker plays the phrase on the rover", async () => {
  const played = [];
  const line = await speakPhrase("help_coming", "es", {
    fetchImpl: reply(200, { played: true, text: "La ayuda viene en camino, no se mueva.", audio_url: "/tts/phrase-help_coming-es.wav" }),
    play: url => played.push(url),
  });
  assert.deepEqual(reply.last, { url: "/speak", body: { phrase: "help_coming", lang: "es" } });
  assert.match(line, /Rover speaker/);
  assert.deepEqual(played, []);
});

test("no rover speaker plays the WAV on the headset", async () => {
  const played = [];
  const line = await speakPhrase("help_coming", "en", {
    fetchImpl: reply(200, { played: false, text: "Help is coming, stay still.", audio_url: "/tts/phrase-help_coming-en.wav" }),
    play: url => played.push(url),
  });
  assert.deepEqual(played, ["/tts/phrase-help_coming-en.wav"]);
  assert.match(line, /headset/);
});

test("server errors and foreign audio URLs are not played", async () => {
  const played = [];
  const missing = await speakPhrase("help_coming", "es", {
    fetchImpl: reply(503, { detail: "No Spanish Piper voice installed" }), play: url => played.push(url),
  });
  assert.match(missing, /No Spanish Piper voice/);
  const foreign = await speakPhrase("help_coming", "en", {
    fetchImpl: reply(200, { played: false, audio_url: "https://example.com/x.wav" }), play: url => played.push(url),
  });
  assert.match(foreign, /no audio/);
  assert.deepEqual(played, []);
});

test("labels and SBAR lines", () => {
  assert.equal(PHRASE_LABELS.help_coming, "Help is coming, stay still");
  assert.deepEqual(sbarLines({ situation: "S1", background: "", assessment: "A1", recommendation: "R1" }),
    ["S: S1", "A: A1", "R: R1"]);
  assert.deepEqual(sbarLines(null), []);
});
