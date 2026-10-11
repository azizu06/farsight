import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULTS, SETTINGS_KEY, defaultH264, defaultMount, loadSettings, readSaved, resolveSettings, saveSettings } from "../src/viewer-settings.js";

const memory = (initial = {}) => {
  const data = new Map(Object.entries(initial));
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)), data };
};
const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };

test("defaults apply when neither query nor storage chooses", () => {
  const { values, origin, explicit } = resolveSettings("", {});
  assert.deepEqual(values, DEFAULTS);
  assert.deepEqual(origin, { mount: "default", h264: "default", voice: "default", diag: "default" });
  assert.deepEqual(explicit, {});
});

test("demo voice defaults on; strict mode and explicit opt-outs stay off", () => {
  assert.equal(resolveSettings("", {}).values.voice, true);
  assert.equal(resolveSettings("?demo=0", {}).values.voice, false);
  assert.equal(resolveSettings("?voice=0", {}).values.voice, false);
  assert.equal(resolveSettings("", { voice: false }).values.voice, false);
  assert.equal(resolveSettings("?demo=0&voice=1", {}).values.voice, true);
});

test("query wins over storage, storage over defaults, per setting", () => {
  const saved = { mount: "servo", h264: true, voice: true };
  const { values, origin } = resolveSettings("?mount=fixed&h264=0", saved);
  assert.deepEqual(values, { mount: "fixed", h264: false, voice: true, diag: false });
  assert.deepEqual(origin, { mount: "query", h264: "query", voice: "saved", diag: "default" });
});

test("malformed query and stored values are ignored, not coerced", () => {
  const { values, origin } = resolveSettings("?mount=gimbal&h264=yes&diag=1", { mount: "tripod", voice: "1", h264: 1 });
  assert.equal(values.mount, "servo"); assert.equal(origin.mount, "default");
  assert.equal(values.h264, false); assert.equal(origin.h264, "default");
  assert.equal(values.voice, true); assert.equal(origin.voice, "saved");
  assert.equal(values.diag, true); assert.equal(origin.diag, "query");
});

test("query choices are saved so the next plain load keeps them", () => {
  const storage = memory({ [SETTINGS_KEY]: JSON.stringify({ voice: true }) });
  const first = loadSettings("?mount=fixed&h264=1", storage);
  assert.equal(first.values.mount, "fixed");
  const reload = loadSettings("", storage);
  assert.deepEqual(reload.values, { mount: "fixed", h264: true, voice: true, diag: false });
  assert.deepEqual(reload.origin, { mount: "saved", h264: "saved", voice: "saved", diag: "default" });
});

test("UI changes merge into saved settings", () => {
  const storage = memory();
  assert.ok(saveSettings(storage, { mount: "fixed" }));
  assert.ok(saveSettings(storage, { diag: true }));
  assert.deepEqual(readSaved(storage), { mount: "fixed", diag: true });
});

test("blocked or corrupt storage falls back to query and defaults without throwing", () => {
  assert.deepEqual(readSaved(throwing), {});
  assert.equal(saveSettings(throwing, { mount: "fixed" }), false);
  assert.equal(saveSettings(null, { mount: "fixed" }), false);
  assert.equal(loadSettings("?mount=fixed", throwing).values.mount, "fixed");
  assert.deepEqual(readSaved(memory({ [SETTINGS_KEY]: "{not json" })), {});
  assert.deepEqual(readSaved(memory({ [SETTINGS_KEY]: "42" })), {});
});

test("rover health picks the default mount only when it reports camera_pan", () => {
  assert.equal(defaultMount({ camera_pan: false }), "fixed");
  assert.equal(defaultMount({ camera_pan: true }), "servo");
  assert.equal(defaultMount({}), null);
  assert.equal(defaultMount(null), null);
});

test("rover health turns H.264 on by default only when it offers enabled video", () => {
  assert.equal(defaultH264({ video: { enabled: true, ready: false } }), true);
  assert.equal(defaultH264({ video: { enabled: false } }), false);
  assert.equal(defaultH264({}), null);
  assert.equal(defaultH264(null), null);
});
