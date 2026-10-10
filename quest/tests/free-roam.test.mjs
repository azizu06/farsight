import test from "node:test";
import assert from "node:assert/strict";
import { FreeRoam, applyRoam, ROAM_SNAP, ROAM_SPEED } from "../src/free-roam.js";

const close = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-9, `${message}: ${actual} vs ${expected}`);

test("drift inside the deadzone never moves or turns the operator", () => {
  const roam = new FreeRoam();
  for (let i = 0; i < 100; i++) roam.step(0.016, 0.2, -0.2, 0);
  assert.deepEqual([roam.x, roam.z, roam.yaw], [0, 0, 0]);
});

test("forward stick moves along the head heading at the roam speed", () => {
  const roam = new FreeRoam();
  roam.step(0.1, 0, -1, 0); // facing -z
  close(roam.z, -ROAM_SPEED * 0.1, "forward is -z"); close(roam.x, 0, "no sideways drift");
  roam.reset(); roam.step(0.1, 0, -1, Math.PI / 2); // facing -x
  close(roam.x, -ROAM_SPEED * 0.1, "head yaw steers"); close(roam.z, 0, "no z");
  roam.reset(); roam.step(5, 0, 1, 0); // a long frame is clamped
  close(roam.z, ROAM_SPEED * 0.1, "back, clamped dt");
});

test("sideways stick snap-turns once per flick", () => {
  const roam = new FreeRoam();
  roam.step(0.016, 1, 0, 0); roam.step(0.016, 1, 0, 0);
  close(roam.yaw, -ROAM_SNAP, "one snap while held right");
  roam.step(0.016, 0, 0, 0); roam.step(0.016, -1, 0, 0);
  close(roam.yaw, 0, "release and flick left turns back");
});

test("applyRoam moves the map opposite to the operator and pivots turns about the head", () => {
  const group = () => ({ position: { x: 0, y: 0, z: -2 }, quaternion: { x: 0, y: 0, z: 0, w: 1, set(x, y, z, w) { Object.assign(this, { x, y, z, w }); } } });
  const moved = group();
  applyRoam(moved, { x: 0, z: -1, yaw: 0 }, { x: 0, z: 0 });
  close(moved.position.z, -1, "walking 1 m forward brings the map 1 m closer");
  const turned = group();
  applyRoam(turned, { x: 0, z: 0, yaw: -Math.PI / 2 }, { x: 0, z: 0 });
  // Turning right by 90°: what was ahead (-z) is now on the operator's left (-x).
  close(turned.position.x, -2, "ahead swings to the left"); close(turned.position.z, 0, "about the head");
  close(turned.quaternion.y, Math.sin(Math.PI / 4), "map yaw follows"); close(turned.quaternion.w, Math.cos(Math.PI / 4), "unit quaternion");
});
