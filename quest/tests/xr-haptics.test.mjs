import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { hapticPulse } from "../src/xr-haptics.js";

test("haptic pulse uses the WebXR actuator, then the vibration actuator, else nothing", () => {
  const calls = [];
  assert.equal(hapticPulse({ hapticActuators: [{ pulse: (i, ms) => calls.push(["pulse", i, ms]) }] }), true);
  assert.equal(hapticPulse({ vibrationActuator: { playEffect: (kind, o) => calls.push([kind, o.duration]) } }, 0.5, 40), true);
  assert.deepEqual(calls, [["pulse", 0.6, 60], ["dual-rumble", 40]]);
  assert.equal(hapticPulse({}), false);
  assert.equal(hapticPulse(undefined), false);
  assert.equal(hapticPulse({ hapticActuators: [{ pulse() { throw new Error("busy"); } }] }), false, "a failing actuator never throws into control");
});

test("rover.ts maps only the left trigger to Talk, latched across left tracking loss", () => {
  const source = readFileSync(new URL("../src/rover.ts", import.meta.url), "utf8");
  assert.match(source, /if \(leftTracked\) \{\s*const talk = !!left\?\.gamepad\?\.buttons\[0\]\?\.pressed;\s*if \(talk && !this\.talkDown\) \{ void this\.intercom\.toggle\(\); hapticPulse\(left\?\.gamepad\); \}\s*this\.talkDown = talk;/);
  assert.match(source, /latchLeftButtons\(\) \{\s*this\.recenterDown = this\.scanDown = this\.drillDown = this\.talkDown = true;/);
  assert.equal((source.match(/left\?\.gamepad\?\.buttons\[/g) ?? []).length, 1, "no other left-controller button is read here");
});
