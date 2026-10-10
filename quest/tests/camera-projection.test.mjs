import test from "node:test";
import assert from "node:assert/strict";
import { cameraRayFromPixel, cameraProjectionGrid } from "../src/camera-projection.js";

const intrinsics = { fx: 200, fy: 100, cx: 200, cy: 100 };
const close = (actual, expected, tolerance = 1e-12) =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`);

test("the principal point looks forward along negative Z", () => {
  assert.deepEqual(cameraRayFromPixel(200, 100, intrinsics), [0, 0, -1]);
});

test("focal offsets map to normalized right and up rays", () => {
  const right = cameraRayFromPixel(400, 100, intrinsics);
  right.forEach((value, axis) => close(value, [0.7071067811865475, 0, -0.7071067811865475][axis]));
  const upperRight = cameraRayFromPixel(400, 0, intrinsics);
  upperRight.forEach((value, axis) => close(value, [0.5773502691896258, 0.5773502691896258, -0.5773502691896258][axis]));
  const lowerLeft = cameraRayFromPixel(0, 200, intrinsics);
  lowerLeft.forEach((value, axis) => close(value, [-0.5773502691896258, -0.5773502691896258, -0.5773502691896258][axis]));
});

test("pixel rays use the supplied off-center principal point", () => {
  const ray = cameraRayFromPixel(300, 150, { fx: 200, fy: 100, cx: 100, cy: 50 });
  ray.forEach((value, axis) => close(value, [0.5773502691896258, -0.5773502691896258, -0.5773502691896258][axis]));
});

test("the grid maps image corners onto the sphere with upright Three UVs", () => {
  const grid = cameraProjectionGrid(400, 200, intrinsics, { columns: 2, rows: 2, radius: 10 });
  assert.ok(grid.positions instanceof Float32Array);
  assert.ok(grid.uvs instanceof Float32Array);
  assert.ok(grid.indices instanceof Uint32Array);
  assert.equal(grid.positions.length, 27);
  assert.equal(grid.uvs.length, 18);
  assert.equal(grid.indices.length, 24);
  for (const [vertex, expectedPosition, expectedUV] of [
    [0, [-5.7735026919, 5.7735026919, -5.7735026919], [0, 1]],
    [4, [0, 0, -10], [0.5, 0.5]],
    [8, [5.7735026919, -5.7735026919, -5.7735026919], [1, 0]],
  ]) {
    Array.from(grid.positions.slice(vertex * 3, vertex * 3 + 3)).forEach((value, axis) => close(value, expectedPosition[axis], 1e-6));
    assert.deepEqual(Array.from(grid.uvs.slice(vertex * 2, vertex * 2 + 2)), expectedUV);
  }
});

test("every triangle faces the viewer at the sphere center", () => {
  const { positions, indices } = cameraProjectionGrid(400, 200, intrinsics, { columns: 3, rows: 2, radius: 10 });
  assert.equal(indices.length, 36);
  for (let offset = 0; offset < indices.length; offset += 3) {
    const vertices = Array.from(indices.slice(offset, offset + 3));
    assert.equal(new Set(vertices).size, 3);
    assert.ok(vertices.every(vertex => vertex >= 0 && vertex < positions.length / 3));
    const [a, b, c] = vertices.map(vertex => Array.from(positions.slice(vertex * 3, vertex * 3 + 3)));
    const ab = b.map((value, axis) => value - a[axis]);
    const ac = c.map((value, axis) => value - a[axis]);
    const normal = [ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0]];
    const towardViewer = normal.reduce((sum, value, axis) => sum - value * a[axis], 0);
    assert.ok(towardViewer > 0, "triangle must face inward");
  }
});

test("default grid sizing and radius cover only finite front-facing rays", () => {
  const { positions, uvs, indices } = cameraProjectionGrid(400, 200, intrinsics);
  assert.equal(positions.length, 825 * 3);
  assert.equal(uvs.length, 825 * 2);
  assert.equal(indices.length, 4608);
  for (let offset = 0; offset < positions.length; offset += 3) {
    close(Math.hypot(...positions.slice(offset, offset + 3)), 100, 1e-5);
    assert.ok(positions[offset + 2] < 0);
  }
  assert.ok(uvs.every(value => Number.isFinite(value) && value >= 0 && value <= 1));
});

test("pixel rays reject malformed calibration and pixel coordinates", () => {
  for (const key of ["fx", "fy"]) {
    for (const value of [0, -1, NaN, Infinity, "200", undefined])
      assert.throws(() => cameraRayFromPixel(200, 100, { ...intrinsics, [key]: value }), RangeError);
  }
  for (const key of ["cx", "cy"]) {
    for (const value of [NaN, Infinity, "100", undefined])
      assert.throws(() => cameraRayFromPixel(200, 100, { ...intrinsics, [key]: value }), RangeError);
  }
  for (const calibration of [undefined, null, {}])
    assert.throws(() => cameraRayFromPixel(200, 100, calibration), RangeError);
  for (const value of [NaN, Infinity, "100", undefined]) {
    assert.throws(() => cameraRayFromPixel(value, 100, intrinsics), RangeError);
    assert.throws(() => cameraRayFromPixel(200, value, intrinsics), RangeError);
  }
});

test("grid dimensions reject zero, negative and non-finite sizes", () => {
  for (const value of [0, -1, NaN, Infinity, "400", undefined]) {
    assert.throws(() => cameraProjectionGrid(value, 200, intrinsics), RangeError);
    assert.throws(() => cameraProjectionGrid(400, value, intrinsics), RangeError);
  }
});

test("grid options reject unsafe subdivisions and radius", () => {
  for (const key of ["columns", "rows"]) {
    for (const value of [0, -1, 1.5, 257, NaN, Infinity, "2"])
      assert.throws(() => cameraProjectionGrid(400, 200, intrinsics, { [key]: value }), RangeError);
  }
  for (const radius of [0, -1, 1001, NaN, Infinity, "100"])
    assert.throws(() => cameraProjectionGrid(400, 200, intrinsics, { radius }), RangeError);
  assert.throws(() => cameraProjectionGrid(400, 200, intrinsics, null), RangeError);
  assert.throws(() => cameraProjectionGrid(400, 200, { ...intrinsics, fy: 0 }), RangeError);
});
