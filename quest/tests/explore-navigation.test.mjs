import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ExploreNavigation, EXPLORE_TURN, EXPLORE_SPEED } from '../src/explore-navigation.js';
import { MapWalk } from '../src/map-walk.js';
const pad = (handedness, x = 0, y = 0) => ({ handedness, gamepad: { axes: [0, 0, x, y],
  buttons: Array.from({ length: 6 }, () => ({ pressed: false })) } });
const run = (navigation, sources, keys = new Set(), frames = 120) => {
  for (let i = 0; i < frames; i++) navigation.update(1 / 60, sources, keys);
};
test('head-relative movement crosses the mapped world with a normalized diagonal', () => {
  const walk = new MapWalk(), navigation = new ExploreNavigation(walk);
  walk.observeHead(0, 0, Math.PI / 2); run(navigation, [pad('left', 1, -1)]);
  assert.ok(walk.x < 0 && walk.z < 0);
  assert.ok(Math.hypot(walk.x, walk.z) < EXPLORE_SPEED * 2);
  assert.ok(Math.hypot(walk.x, walk.z) > EXPLORE_SPEED * 1.8);
});
test('one snap per flick turns about the physically displaced head', () => {
  const walk = new MapWalk(), navigation = new ExploreNavigation(walk);
  walk.observeHead(.7, -.4, 0); run(navigation, [pad('right', 1)]);
  assert.equal(walk.yaw, EXPLORE_TURN);
  const transform = walk.rootTransform();
  const x = Math.cos(walk.yaw) * (.7 - walk.x) + Math.sin(walk.yaw) * (-.4 - walk.z);
  const z = -Math.sin(walk.yaw) * (.7 - walk.x) + Math.cos(walk.yaw) * (-.4 - walk.z);
  assert.ok(Math.abs(x - .7) < 1e-9 && Math.abs(z + .4) < 1e-9);
  navigation.update(1 / 60, [pad('right')]); run(navigation, [pad('right', 1)]);
  assert.equal(walk.yaw, EXPLORE_TURN * 2);
  assert.equal(transform.yaw, EXPLORE_TURN);
});
test('vertical movement preserves the map floor and home clears lift', () => {
  const walk = new MapWalk(), navigation = new ExploreNavigation(walk);
  walk.setFloor(-1.2); run(navigation, [pad('right', 0, -1)]);
  assert.ok(walk.y > -.1); assert.equal(walk.floorY, -1.2);
  walk.reset(); navigation.reset(); assert.equal(walk.y, -1.2);
});
test('A and B emit once per press; keyboard movement shares the same coordinates', () => {
  const walk = new MapWalk(), navigation = new ExploreNavigation(walk), right = pad('right');
  right.gamepad.buttons[4].pressed = right.gamepad.buttons[5].pressed = true;
  assert.deepEqual(navigation.update(1 / 60, [right]), { help: true, home: true });
  assert.deepEqual(navigation.update(1 / 60, [right]), { help: false, home: false });
  run(navigation, [], new Set(['KeyW', 'KeyE']));
  assert.ok(walk.z < -2.5 && walk.y > 1.4);
});
