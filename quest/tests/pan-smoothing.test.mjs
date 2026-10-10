import test from "node:test";
import assert from "node:assert/strict";
import { PanSmoother } from "../src/pan-smoothing.js";

test("0 to 60 degree step rises monotonically with bounded steps", () => {
  const s = new PanSmoother();
  let prev = s.reset(0);
  for (let i = 0; i < 120; i++) {
    const next = s.step(60, 1 / 60);
    assert.ok(next >= prev && next <= 60);
    assert.ok(next - prev <= s.maxRateDegPerSec / 60 + 1e-9);
    prev = next;
  }
  assert.ok(prev > 59);
});
test("clamps at 80 degrees both ways", () => {
  const s = new PanSmoother();
  for (let i = 0; i < 600; i++) s.step(170, 1 / 60);
  assert.ok(s.value <= 80 && s.value > 79.9);
  for (let i = 0; i < 1200; i++) s.step(-500, 1 / 60);
  assert.ok(s.value >= -80 && s.value < -79.9);
});
test("reset returns to center and bad input is ignored", () => {
  const s = new PanSmoother();
  s.step(60, 0.5);
  assert.ok(s.value > 0);
  assert.equal(s.reset(), 0);
  assert.equal(s.step(NaN, NaN), 0);
});
