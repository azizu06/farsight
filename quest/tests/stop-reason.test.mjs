import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { explainStop, STOP_CODES } from "../src/stop-reason.js";
import { ControlSafety } from "../src/control-safety.js";

const latched = (reason, extra = {}) => explainStop({ latched: true, status: { requires_release: true, reason }, ...extra });

test("every server stop code the Python control service emits has a message and recovery", () => {
  const sources = ["../../jetson/control/state.py", "../../jetson/control/service.py", "../../jetson/control/serial_link.py"]
    .map(path => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
  const emitted = new Set([...sources.matchAll(/(?:\.stop|set_connected\(False,)\s*\(?\s*"([a-z_]+)"/g)].map(m => m[1]));
  emitted.add("release_required"); emitted.add("serial_disconnected");
  assert.ok(emitted.size >= 25, `found ${emitted.size} codes`);
  for (const code of emitted) assert.ok(STOP_CODES.server.includes(code), `unmapped server reason ${code}`);
});

test("each server reason maps to one short human cause and an action", () => {
  for (const code of STOP_CODES.server) {
    const stop = latched(code);
    assert.equal(stop.code, code);
    assert.equal(stop.tone, "stop");
    assert.ok(stop.cause && !stop.cause.includes("_"), `${code} cause is human text`);
    assert.ok(stop.cause.length <= 54, `${code} cause fits VR panel`);
    assert.ok(stop.recovery.length > 0 && stop.recovery.length <= 66, `${code} recovery fits VR panel`);
    assert.doesNotMatch(stop.recovery, /\{deadman\}/);
  }
  assert.equal(latched("lease_expired").cause, "Command lease expired (link lag > 250 ms)");
  assert.match(latched("serial_disconnected").recovery, /Uno USB/);
  assert.match(latched("camera_stale").recovery, /Wait for live video/);
});

test("recovery names the deadman for the active device", () => {
  assert.equal(latched("deadman_released", { xr: true }).recovery, "Release, then hold right trigger");
  assert.equal(latched("deadman_released", { xr: false }).recovery, "Release, then hold Space (or Hold)");
});

test("each readiness block explains the condition and waits or fixes, not just release", () => {
  for (const unsafe of STOP_CODES.unsafe) {
    const stop = explainStop({ unsafe, latched: true, cause: "deadman_released" });
    assert.equal(stop.code, unsafe, "an active unsafe condition outranks the latch");
    assert.equal(stop.tone, "stop");
  }
  assert.deepEqual(explainStop({ unsafe: "Control disconnected" }),
    { code: "Control disconnected", cause: "Control link down", recovery: "Check rover Wi-Fi/USB; reconnects every 1 s", tone: "stop" });
  assert.match(explainStop({ unsafe: "Camera stale: motion stopped" }).cause, /Camera stale/);
  assert.match(explainStop({ unsafe: "Waiting for fresh control permit" }).recovery, /^Wait/);
});

test("a live input-inactive reason outranks an older latch cause", () => {
  const stop = explainStop({ latched: true, cause: "Stopped: release deadman", reason: "Input or display inactive: release deadman" });
  assert.equal(stop.cause, "Viewer not accepting input");
  assert.match(stop.recovery, /^Close dialogs, back to camera view/);
  assert.equal(explainStop({ latched: true, cause: "Stopped: release deadman", reason: "Release deadman to continue" }).cause, "Stopped");
});

test("every message fits the VR stop panel on both devices", () => {
  const all = [...STOP_CODES.server.map(code => ({ latched: true, cause: code })), ...STOP_CODES.unsafe.map(unsafe => ({ unsafe })),
    ...STOP_CODES.client.map(cause => ({ latched: true, cause }))];
  for (const state of all) for (const xr of [true, false]) {
    const stop = explainStop({ ...state, xr });
    assert.ok(stop.cause.length <= 54 && stop.recovery.length <= 66, `${stop.code}: ${stop.cause} / ${stop.recovery}`);
  }
});

test("after an unsafe condition clears, only the release action remains", () => {
  const stop = explainStop({ latched: true, cause: "Camera stale: motion stopped", xr: true });
  assert.equal(stop.cause, "Camera stale (> 500 ms)");
  assert.equal(stop.recovery, "Release, then hold right trigger");
});

test("each client latch maps to a cause and recovery", () => {
  for (const text of STOP_CODES.client) {
    const stop = explainStop({ latched: true, cause: text, xr: true });
    assert.ok(stop.cause && stop.recovery, text);
  }
  assert.deepEqual(explainStop({ latched: true, cause: "XR tracking unavailable: release trigger", xr: true }), {
    code: "XR tracking unavailable: release trigger", cause: "Controller tracking lost",
    recovery: "Bring both controllers into view; release and hold right trigger", tone: "stop" });
  assert.equal(explainStop({ latched: true, cause: "Voice question: release trigger" }).cause, "Listening to voice question");
  assert.equal(explainStop({ latched: true, cause: "Viewer closed" }).recovery, "Reopen the viewer");
});

test("other client text splits into cause and its own recovery", () => {
  const generic = explainStop({ latched: true, cause: "Controller changed: release trigger", xr: true });
  assert.equal(generic.cause, "Controller changed");
  assert.equal(generic.recovery, "Release, then hold right trigger");
  assert.equal(explainStop({ latched: true, cause: "H.264 restarted: motion disabled" }).recovery, "Release, then hold Space (or Hold)");
  const scan = explainStop({ latched: true, cause: "Scan confirmed: release, then hold deadman in live view" });
  assert.equal(scan.cause, "Scan confirmed");
  assert.equal(scan.recovery, "release, then hold deadman in live view");
});

test("autonomy stops, forward block and slow detector are warnings, not stop latches", () => {
  const lost = explainStop({ status: { autonomy_state: "target_lost", target_id: 4 } });
  assert.deepEqual(lost, { code: "target_lost", cause: "Seek target #4 lost", recovery: "Point at the person and press A again", tone: "warn" });
  assert.equal(explainStop({ status: { autonomy_state: "target_missing" } }).cause, "Seek target not visible");
  assert.equal(explainStop({ status: { autonomy_state: "estop" } }).tone, "warn");
  for (const state of ["forward", "turning", "tracking", "arrived", "idle", "stick_override"])
    assert.equal(explainStop({ status: { autonomy_state: state } }), null, state);
  const blocked = explainStop({ held: true, status: { estop: true, reason: "forward_blocked" }, xr: true });
  assert.equal(blocked.code, "forward_blocked");
  assert.match(blocked.recovery, /press X/);
  assert.equal(explainStop({ held: false, status: { estop: true } }), null, "idle range block is not a stop");
  assert.equal(explainStop({ autonomyArmed: true, detectorStale: true }).code, "detector_stale");
  assert.equal(explainStop({ autonomyArmed: false, detectorStale: true }), null);
});

test("nothing is shown while ready or driving", () => {
  assert.equal(explainStop({}), null);
  assert.equal(explainStop({ held: true, status: { reason: "active", requires_release: false, estop: false } }), null);
  assert.equal(explainStop({ status: { reason: "deadman_released", requires_release: false } }), null);
});

test("ControlSafety keeps the first specific latch cause across echoed server releases", () => {
  const gate = new ControlSafety();
  gate.acceptStatus({ session_id: "s", permit: "p", permit_ms: 250, rover: true, requires_release: true, reason: "release_required" }, 0);
  assert.equal(gate.cause, "release_required", "generic startup causes give way to specific ones");
  gate.decide(false, 0); // released
  assert.equal(gate.needsRelease, false);
  assert.equal(gate.cause, null);
  gate.invalidate("XR tracking unavailable: release trigger");
  gate.acceptStatus({ session_id: "s", permit: "p", permit_ms: 250, rover: true, requires_release: true, reason: "deadman_released" }, 10);
  gate.acceptStatus({ session_id: "s", permit: "p", permit_ms: 250, rover: true, requires_release: true, reason: "lease_expired" }, 20);
  assert.equal(gate.cause, "XR tracking unavailable: release trigger");
  assert.equal(gate.reason, "lease_expired", "safety reason text unchanged");
  assert.equal(gate.needsRelease, true);
});

test("a server stop while driving becomes the latch cause", () => {
  const gate = new ControlSafety();
  gate.decide(false, 0);
  gate.acceptStatus({ session_id: "s", permit: "p", permit_ms: 250, rover: true, requires_release: true, reason: "lease_expired" }, 1);
  assert.equal(gate.cause, "lease_expired");
  assert.equal(explainStop({ latched: gate.needsRelease, cause: gate.cause }).cause, "Command lease expired (link lag > 250 ms)");
});
