import test from "node:test";
import assert from "node:assert/strict";
import { SourceTruth } from "../src/source-banner.js";

const liveHealth = { replay: false, rover: true, camera: true, camera_source: "USB CAMERA", control: { simulated: false } };
const reply = body => async () => ({ ok: true, json: async () => body });
const offline = async () => { throw new Error("offline"); };
const view = (now, extra = {}) => ({ now, frameSource: "USB CAMERA", frameId: "41", videoAgeMs: 120, ...extra });

test("LIVE only with fresh health proving real camera, real rover, and a fresh displayed frame", async () => {
  const truth = new SourceTruth();
  assert.equal(truth.state(view(0)).kind, "UNVERIFIED");
  await truth.refresh(reply(liveHealth), 1000);
  const live = truth.state(view(1000));
  assert.equal(live.kind, "LIVE");
  assert.match(live.detail, /USB CAMERA · frame 41 · 120 ms/);
  for (const [patch, reason] of [
    [{ rover: false }, /rover not connected/],
    [{ camera: false }, /camera not fresh/],
    [{ camera_source: "WAITING" }, /camera source WAITING/],
    [{ control: {} }, /simulation state unreported/],
    [{ replay: undefined }, /replay state unreported/],
  ]) {
    const partial = new SourceTruth();
    await partial.refresh(reply({ ...liveHealth, ...patch }), 1000);
    const state = partial.state(view(1000));
    assert.equal(state.kind, "UNVERIFIED", JSON.stringify(patch));
    assert.match(state.detail, reason);
  }
  assert.match(truth.state(view(1000, { videoAgeMs: 800 })).detail, /displayed frame stale/);
  assert.match(truth.state(view(1000, { frameSource: "Waiting for camera" })).detail, /no verified frame/);
  // Health that stops arriving cannot keep claiming LIVE.
  await truth.refresh(offline, 4000);
  assert.equal(truth.state(view(7000)).kind, "UNVERIFIED");
});

test("a delayed health response remains fresh for five seconds after successful receipt", async t => {
  let now = 1000;
  t.mock.method(performance, "now", () => now);
  const truth = new SourceTruth();
  const delayed = async () => {
    now = 1500;
    return { ok: true, json: async () => { now = 2500; return liveHealth; } };
  };
  await truth.refresh(delayed);
  assert.equal(truth.state(view(6000)).kind, "LIVE", "request latency must not consume received health freshness");
  assert.equal(truth.state(view(7499)).kind, "LIVE");
  assert.match(truth.state(view(7500)).detail, /rover health unavailable/);
});

test("an explicitly supplied health timestamp is preserved, including zero", async t => {
  t.mock.method(performance, "now", () => 2500);
  const truth = new SourceTruth();
  await truth.refresh(reply(liveHealth), 0);
  assert.equal(truth.state(view(4999)).kind, "LIVE");
  assert.match(truth.state(view(5000)).detail, /rover health unavailable/);
});

test("REPLAY is sticky through disconnects and outranks everything", async () => {
  const truth = new SourceTruth();
  await truth.refresh(reply({ ...liveHealth, replay: true }), 0);
  assert.equal(truth.state(view(0)).kind, "REPLAY");
  await truth.refresh(offline, 10000);
  assert.equal(truth.state(view(60000)).kind, "REPLAY");
  await truth.refresh(reply(liveHealth), 60000);
  assert.equal(truth.state(view(60000)).kind, "LIVE");
  assert.equal(truth.state(view(60000, { frameSource: "REPLAY: USB CAMERA" })).kind, "REPLAY");
  truth.observe("REPLAY: USB CAMERA");
  assert.equal(truth.state(view(60000)).kind, "REPLAY");
});

test("SIMULATED is sticky and only cleared by an explicit complete non-simulated report", async () => {
  const truth = new SourceTruth();
  await truth.refresh(reply({ ...liveHealth, control: { simulated: true } }), 0);
  assert.equal(truth.state(view(0)).kind, "SIMULATED");
  await truth.refresh(offline, 1000);
  await truth.refresh(reply({ replay: false }), 2000);
  assert.equal(truth.state(view(2000)).kind, "SIMULATED");
  await truth.refresh(reply(liveHealth), 3000);
  assert.equal(truth.state(view(3000)).kind, "LIVE");
  for (const evidence of [{ camera_source: "SIMULATED" }, { source: "SIMULATED" }]) {
    const other = new SourceTruth();
    await other.refresh(reply({ ...liveHealth, ...evidence }), 0);
    assert.equal(other.state(view(0)).kind, "SIMULATED");
  }
  const observed = new SourceTruth();
  await observed.refresh(reply(liveHealth), 0);
  observed.observe("USB CAMERA", { simulated: true });
  assert.equal(observed.state(view(0)).kind, "SIMULATED");
  assert.equal(new SourceTruth().state(view(0, { frameSource: "SIMULATED" })).kind, "SIMULATED");
});
