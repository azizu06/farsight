// node --test tools/tests/measured_surface.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { buildMeasuredSurface } from "../mac_render/measured-surface.worker.mjs";

function cloud(parts) {
  const positions = [], colors = [];
  for (const [points, color] of parts) for (const p of points) { positions.push(...p); colors.push(...color); }
  return [new Float32Array(positions), new Float32Array(colors), positions.length / 3];
}
function grid(nx, ny, at) {
  const points = [];
  for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) points.push(at(i / 100, j / 100));
  return points;
}

test("dense planar samples become connected triangles coloured from the points", () => {
  const floor = grid(100, 100, (x, z) => [x, 0, z]);
  const result = buildMeasuredSurface(...cloud([[floor, [0.5, 0.25, 0.125]]]));
  // A 1 m x 1 m patch at 4 cm cells: ~25 x 25 vertices, two triangles per interior quad.
  assert.ok(result.mesh.triangles > 2 * 23 * 23, `triangles ${result.mesh.triangles}`);
  assert.equal(result.points.count, 0, "samples under faces are not drawn twice");
  for (let i = 0; i < result.mesh.vertices; i++) {
    assert.ok(Math.abs(result.mesh.positions[i * 3 + 1]) < 1e-4);
    assert.ok(Math.abs(result.mesh.colors[i * 3] - 0.5) < 1e-6);
  }
});

test("faces never bridge a depth discontinuity", () => {
  // Two parallel walls 30 cm apart in depth, touching in x: no triangle may join them.
  const near = grid(60, 60, (x, y) => [x, y, 0]);
  const far = grid(60, 60, (x, y) => [0.6 + x, y, -0.3]);
  const result = buildMeasuredSurface(...cloud([[near, [1, 0, 0]], [far, [0, 0, 1]]]));
  const { positions, indices } = result.mesh;
  assert.ok(result.mesh.triangles > 0);
  for (let t = 0; t < indices.length; t += 3) {
    const zs = [0, 1, 2].map(k => positions[indices[t + k] * 3 + 2]);
    assert.ok(Math.max(...zs) - Math.min(...zs) < 0.05, `triangle spans ${zs}`);
  }
});

test("isolated samples are dropped and sparse samples get a wider, bounded radius", () => {
  const dense = grid(40, 40, (x, z) => [x, 0, z]);
  const sparse = [[2, 1, 2], [2.03, 1, 2], [2, 1, 2.03], [2.03, 1, 2.03], [2.015, 1.01, 2.015]];
  const lone = [[5, 5, 5]];
  const result = buildMeasuredSurface(...cloud([[dense, [1, 1, 1]], [sparse, [0, 1, 0]], [lone, [1, 0, 0]]]),
    { minPoints: 6 });
  assert.equal(result.stats.outlier_cells, 1);
  const kept = [];
  for (let i = 0; i < result.points.count; i++) kept.push(result.points.positions.slice(i * 3, i * 3 + 3));
  assert.ok(!kept.some(p => p[0] === 5), "the lone sample is gone");
  assert.equal(result.points.count, sparse.length);
  for (const r of result.points.radius) {
    assert.ok(r > 0.01 && r <= 0.05, `radius ${r}`);
  }
});
