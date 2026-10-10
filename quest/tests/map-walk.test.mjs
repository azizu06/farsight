import assert from 'node:assert/strict';
import test from 'node:test';
import { MapWalk, floorHeight } from '../src/map-walk.js';

/** World position where a map point is drawn: Ry(yaw) * p + position. */
const draw = (walk, [x, y, z]) => {
  const { yaw, position } = walk.rootTransform();
  const s = Math.sin(yaw), c = Math.cos(yaw);
  return [c * x + s * z + position[0], y + position[1], -s * x + c * z + position[2]];
};
const near = (actual, expected) => actual.forEach((value, i) => assert.ok(Math.abs(value - expected[i]) < 1e-9, `${actual} != ${expected}`));

test('the viewer starts at the rover start pose with the map floor on the headset floor', () => {
  const walk = new MapWalk();
  walk.setFloor(-0.3);
  walk.reset();
  near(draw(walk, [0, -0.3, 0]), [0, 0, 0]);
  near(draw(walk, [1, 0.7, -2]), [1, 1, -2]);  // 1:1 scale, no rotation
});

test('moving forward follows the gaze heading and keeps 1:1 scale', () => {
  const walk = new MapWalk();
  walk.observeHead(0, 0, 0);               // facing -Z
  walk.moveViewer(2);
  near([walk.x, walk.z], [0, -2]);
  near(draw(walk, [0, 0, -2]), [0, 0, 0]);   // the point two metres ahead is now underfoot
  walk.observeHead(0, 0, Math.PI / 2);     // facing -X
  walk.moveViewer(1, 0.5);                 // forward 1 (-X), right 0.5 (-Z)
  near([walk.x, walk.z], [-1, -2.5]);
});

test('turning rotates the map about the head, not the world origin', () => {
  const walk = new MapWalk();
  walk.observeHead(1, -1, 0);
  const under = [1, 0, -1];               // map point under the head before turning
  near(draw(walk, under), [1, 0, -1]);
  walk.moveViewer(0, 0, 0, Math.PI / 3);
  near(draw(walk, under), [1, 0, -1]);
  // Forward motion after a turn goes along the turned map direction.
  walk.observeHead(0, 0, 0);
  const before = [walk.x, walk.z];
  walk.moveViewer(1);
  const moved = [walk.x - before[0], walk.z - before[1]];
  near([Math.hypot(...moved)], [1]);
  near(moved, [-Math.sin(Math.PI / 3), -Math.cos(Math.PI / 3)]);
});

test('vertical moves survive floor updates; reset returns to the start; bad input is ignored', () => {
  const walk = new MapWalk();
  walk.setFloor(-0.2); walk.moveViewer(0, 0, 0.5);
  assert.equal(walk.y, 0.3);
  walk.setFloor(-0.25);
  assert.equal(walk.y, 0.25);
  walk.moveViewer(NaN); walk.setFloor(NaN);
  assert.equal(walk.y, 0.25);
  walk.moveViewer(3, 1, 0, 1); walk.reset();
  assert.deepEqual([walk.x, walk.z, walk.yaw, walk.y], [0, 0, 0, -0.25]);
  assert.throws(() => walk.setMode('fly'));
  walk.setMode('walk'); assert.equal(walk.walking, true);
});

test('floor height is a low percentile that ignores a few stray low points', () => {
  const positions = [];
  for (let i = 0; i < 1000; i++) positions.push(i % 7, i % 10 < 4 ? -0.3 : 1.2, -i * 0.01);
  positions.push(0, -9, 0);                // one outlier below the floor
  assert.equal(floorHeight(new Float32Array(positions)), Math.fround(-0.3));
  assert.ok(Number.isNaN(floorHeight(new Float32Array())));
});
