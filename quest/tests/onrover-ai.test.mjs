import test from "node:test";
import assert from "node:assert/strict";
import { onRoverAiLine } from "../src/onrover-ai.js";

const models = [
  { role: "detector", name: "YOLO11n-pose", loaded: true, last_ms: 20 },
  { role: "vision", name: "Qwen3-VL-2B", loaded: true, last_ms: 912 },
  { role: "stt", name: "Whisper base.en", loaded: true, last_ms: 304 },
  { role: "tts", name: "Piper en_US-lessac-low", loaded: true, last_ms: 87 },
  { role: "tts-es", name: "Piper es_MX-claude-high", loaded: true, last_ms: null },
];

test("lists the voice-question models in stage order with their latest latency", () => {
  assert.equal(onRoverAiLine(models), "On-rover AI: Whisper base.en 0.3 s · Qwen3-VL-2B 0.9 s · Piper 0.1 s");
});

test("unmeasured stages show a dash and unloaded models are left out", () => {
  const fresh = models.map(model => ({ ...model, last_ms: null }));
  fresh[1].loaded = false;
  assert.equal(onRoverAiLine(fresh), "On-rover AI: Whisper base.en – · Piper –");
});

test("older servers without a models list show nothing", () => {
  assert.equal(onRoverAiLine(undefined), "");
  assert.equal(onRoverAiLine([{ role: "detector", name: "YOLO11n-pose", loaded: true }]), "");
});
