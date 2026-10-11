import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from '../../quest/node_modules/three/build/three.module.js';
import { fitSceneBounds } from '../mac_render/scene-framing.mjs';

const corners = box => {
  const result = [];
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) {
    for (const z of [box.min.z, box.max.z]) result.push(new THREE.Vector3(x, y, z));
  }
  return result;
};
const fitted = (camera, fit) => {
  const copy = camera.clone();
  copy.position.copy(fit.position);
  copy.lookAt(fit.target);
  copy.updateMatrixWorld();
  copy.updateProjectionMatrix();
  return copy;
};
function assertInside(camera, box, fit, padding = 1.12) {
  const copy = fitted(camera, fit), inverse = copy.matrixWorldInverse;
  for (const point of corners(box)) {
    const projected = point.clone().project(copy);
    assert.ok(Math.abs(projected.x) <= 1 / padding + 1e-12, `horizontal corner ${projected.x}`);
    assert.ok(Math.abs(projected.y) <= 1 / padding + 1e-12, `vertical corner ${projected.y}`);
    assert.ok(projected.z >= -1 - 1e-12 && projected.z <= 1 + 1e-12);
    // The finite dot footprint also fits, rather than only its centre.
    const local = point.clone().applyMatrix4(inverse);
    for (const [dx, dy] of [[.008, 0], [-.008, 0], [0, .008], [0, -.008]]) {
      const edge = local.clone().add(new THREE.Vector3(dx, dy, 0)).applyMatrix4(copy.projectionMatrix);
      assert.ok(Math.abs(edge.x) <= 1 / padding + 1e-12);
      assert.ok(Math.abs(edge.y) <= 1 / padding + 1e-12);
    }
  }
}

test('an empty measured box has no invented scene', () => {
  assert.equal(fitSceneBounds(THREE, new THREE.PerspectiveCamera(), new THREE.Box3()), null);
});

test('all eight corners and dot footprints fit portrait, square and wide views', () => {
  const bounds = new THREE.Box3(new THREE.Vector3(-3, -1, -6), new THREE.Vector3(4, 2, 1));
  for (const aspect of [.45, 1, 16 / 9, 3]) for (const fov of [35, 50, 85]) {
    const camera = new THREE.PerspectiveCamera(fov, aspect, .01, 2000);
    const fit = fitSceneBounds(THREE, camera, bounds);
    assertInside(camera, bounds, fit);
    assert.deepEqual(fit.target.toArray(), [.5, .5, -2.5]);
  }
});

test('translated measured room is centred nearby rather than at the initial distant camera', () => {
  const center = new THREE.Vector3(-1.329, 1.7835, -3.7765);
  const bounds = new THREE.Box3(center.clone().add(new THREE.Vector3(-3.5665, -1.5, -1.3)),
    center.clone().add(new THREE.Vector3(3.5665, 1.5, 1.3)));
  const camera = new THREE.PerspectiveCamera(50, 16 / 9, .01, 2000);
  camera.position.set(9, 6, 9);
  const fit = fitSceneBounds(THREE, camera, bounds);
  assert.ok(fit.target.distanceTo(center) < 1e-12);
  assert.ok(fit.distance < camera.position.distanceTo(center) * .6);
  assertInside(camera, bounds, fit);
  const copy = fitted(camera, fit);
  assert.ok(center.clone().project(copy).length() < 1.01); // X/Y exactly central, Z in clip range.
  const projectedCenter = center.clone().project(copy);
  assert.ok(Math.abs(projectedCenter.x) < 1e-12 && Math.abs(projectedCenter.y) < 1e-12);
});

test('tiny measured bounds and a single measured position keep their true extents', () => {
  const camera = new THREE.PerspectiveCamera(50, 1, .01, 2000);
  const center = new THREE.Vector3(12, -3, -17);
  for (const radius of [0, .0001, .005]) {
    const bounds = new THREE.Box3(center.clone().addScalar(-radius), center.clone().addScalar(radius));
    const fit = fitSceneBounds(THREE, camera, bounds);
    assertInside(camera, bounds, fit);
    assert.ok(fit.distance < .05, 'tiny geometry must not inherit a metre-scale span');
    assert.ok(fit.target.distanceTo(center) < 1e-12);
  }
  camera.near = .1;
  const point = new THREE.Box3(center.clone(), center.clone());
  assertInside(camera, point, fitSceneBounds(THREE, camera, point));
});

test('effective FOV respects camera zoom and an alternate look direction/up vector', () => {
  const bounds = new THREE.Box3(new THREE.Vector3(-2, -3, -1), new THREE.Vector3(3, 1, 2));
  const camera = new THREE.PerspectiveCamera(65, .7, .01, 2000);
  camera.zoom = 2;
  camera.up.set(0, 0, 1);
  camera.updateProjectionMatrix();
  const direction = new THREE.Vector3(-2, 1, 3), fit = fitSceneBounds(THREE, camera, bounds, 1.3, direction);
  assertInside(camera, bounds, fit, 1.3);
  const normal = camera.clone(); normal.zoom = 1; normal.updateProjectionMatrix();
  assert.ok(fit.distance > fitSceneBounds(THREE, normal, bounds, 1.3, direction).distance);
});

test('the selected distance is tight for its padded dot footprint', () => {
  const bounds = new THREE.Box3(new THREE.Vector3(-3, -1, -2), new THREE.Vector3(3, 1, 2));
  const camera = new THREE.PerspectiveCamera(50, .6, .01, 2000);
  const fit = fitSceneBounds(THREE, camera, bounds), copy = fitted(camera, fit);
  const away = fit.position.clone().sub(fit.target).normalize();
  copy.position.addScaledVector(away, -1e-5); copy.updateMatrixWorld();
  let overflow = false;
  for (const point of corners(bounds)) {
    const local = point.clone().applyMatrix4(copy.matrixWorldInverse);
    for (const [dx, dy] of [[.008, 0], [-.008, 0], [0, .008], [0, -.008]]) {
      const edge = local.clone().add(new THREE.Vector3(dx, dy, 0)).applyMatrix4(copy.projectionMatrix);
      overflow ||= Math.abs(edge.x) > 1 / 1.12 || Math.abs(edge.y) > 1 / 1.12;
    }
  }
  assert.equal(overflow, true, 'moving even slightly closer violates the requested frame padding');
});

test('fitting is pure and equal inputs give stable equal results', () => {
  const camera = new THREE.PerspectiveCamera(50, 2, .01, 2000);
  camera.position.set(9, 6, 9); camera.lookAt(0, 0, 0); camera.updateMatrixWorld();
  const bounds = new THREE.Box3(new THREE.Vector3(-4, 1, -8), new THREE.Vector3(2, 4, -1));
  const direction = new THREE.Vector3(1, .5, 1);
  const original = { camera: camera.toJSON(), bounds: bounds.clone(), direction: direction.clone() };
  const a = fitSceneBounds(THREE, camera, bounds, 1.12, direction);
  const b = fitSceneBounds(THREE, camera, bounds, 1.12, direction);
  assert.deepEqual(a.position.toArray(), b.position.toArray());
  assert.deepEqual(a.target.toArray(), b.target.toArray());
  assert.equal(a.distance, b.distance);
  assert.deepEqual(camera.toJSON(), original.camera);
  assert.ok(bounds.equals(original.bounds)); assert.ok(direction.equals(original.direction));
});
