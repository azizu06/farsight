/**
 * Measured-surface builder for the Mac renderer. Runs off the main thread.
 *
 * Input: the retained 1 cm dense samples (positions in metres, linear RGB).
 * Output, all derived from measured samples only:
 * - points: samples that survive a min-neighbour filter, each with a world
 *   radius from its local sample spacing (cell / sqrt(samples in cell));
 * - mesh: one vertex per well-sampled planar cell (centroid of its samples,
 *   PCA normal, mean colour), joined to grid neighbours into triangles.
 *   An edge is kept only when both ends lie on each other's fitted plane
 *   and their normals agree, so faces never bridge a depth discontinuity.
 */

export const DEFAULTS = Object.freeze({
  cell: 0.04,         // surface patch size, metres
  minPoints: 3,       // samples a cell needs before it gets a vertex
  minNeighbors: 4,    // samples within the 3x3x3 cell block below which a cell is an outlier
  maxThickness: 0.7,  // block plane-fit residual (sqrt of smallest eigenvalue), in cells
  planeTolerance: 0.7,// neighbour offset along a normal, in cells
  minNormalDot: 0.6,
  maxEdge: 2.2,       // longest triangle edge, in cells
  maxRadius: 0.05,    // world radius cap for a sample disc, metres
  coveredPoints: false, // keep samples whose cell is meshed
});

function eigenSymmetric3(m) {
  // Cyclic Jacobi on [a00 a01 a02; a11 a12; a22]; returns values and column vectors.
  const a = [[m[0], m[1], m[2]], [m[1], m[3], m[4]], [m[2], m[4], m[5]]];
  const v = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (let sweep = 0; sweep < 12; sweep++) {
    const off = Math.abs(a[0][1]) + Math.abs(a[0][2]) + Math.abs(a[1][2]);
    if (off < 1e-14) break;
    for (let p = 0; p < 2; p++) for (let q = p + 1; q < 3; q++) {
      if (Math.abs(a[p][q]) < 1e-18) continue;
      const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
      const t = Math.sign(theta || 1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
      const c = 1 / Math.sqrt(t * t + 1), s = t * c;
      for (let k = 0; k < 3; k++) {
        const akp = a[k][p], akq = a[k][q];
        a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq;
      }
      for (let k = 0; k < 3; k++) {
        const apk = a[p][k], aqk = a[q][k];
        a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk;
      }
      for (let k = 0; k < 3; k++) {
        const vkp = v[k][p], vkq = v[k][q];
        v[k][p] = c * vkp - s * vkq; v[k][q] = s * vkp + c * vkq;
      }
    }
  }
  return { values: [a[0][0], a[1][1], a[2][2]], vectors: v };
}

export function buildMeasuredSurface(positions, colors, count, options = {}) {
  const o = { ...DEFAULTS, ...options };
  const started = performance.now();
  const cell = o.cell;
  if (!count) return emptyResult(started);
  let minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  for (let i = 0; i < count; i++) {
    const x = positions[i * 3], y = positions[i * 3 + 1], z = positions[i * 3 + 2];
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
    if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
  }
  // One spare cell on every side keeps neighbour keys inside the grid.
  minX -= cell; minY -= cell; minZ -= cell;
  const nx = Math.floor((maxX - minX) / cell) + 3, ny = Math.floor((maxY - minY) / cell) + 3;
  const nz = Math.floor((maxZ - minZ) / cell) + 3;
  const strideY = nx, strideZ = nx * ny;
  const keyOf = (i) => Math.floor((positions[i * 3] - minX) / cell)
    + strideY * Math.floor((positions[i * 3 + 1] - minY) / cell)
    + strideZ * Math.floor((positions[i * 3 + 2] - minZ) / cell);

  // Cell assignment and per-cell sums, relative to the grid origin so neighbouring
  // cells' sums can be added for a smoothed fit.
  const cells = new Map();
  const pointCell = new Int32Array(count);
  let cellCount = 0;
  let keys = new Float64Array(1 << 16);
  let stats = new Float64Array((1 << 16) * 13);
  for (let i = 0; i < count; i++) {
    const key = keyOf(i);
    let c = cells.get(key);
    if (c === undefined) {
      c = cellCount++;
      if (c >= keys.length) {
        const grownKeys = new Float64Array(keys.length * 2); grownKeys.set(keys); keys = grownKeys;
        const grownStats = new Float64Array(stats.length * 2); grownStats.set(stats); stats = grownStats;
      }
      keys[c] = key; cells.set(key, c);
    }
    pointCell[i] = c;
    const s = c * 13;
    const x = positions[i * 3] - minX, y = positions[i * 3 + 1] - minY, z = positions[i * 3 + 2] - minZ;
    stats[s] += 1; stats[s + 1] += x; stats[s + 2] += y; stats[s + 3] += z;
    stats[s + 4] += x * x; stats[s + 5] += x * y; stats[s + 6] += x * z;
    stats[s + 7] += y * y; stats[s + 8] += y * z; stats[s + 9] += z * z;
    stats[s + 10] += colors[i * 3]; stats[s + 11] += colors[i * 3 + 1]; stats[s + 12] += colors[i * 3 + 2];
  }

  // Min-neighbour outlier filter: only sparse cells need the 27-cell sum.
  const outlier = new Uint8Array(cellCount);
  let outlierCells = 0;
  for (let c = 0; c < cellCount; c++) {
    if (stats[c * 13] >= o.minNeighbors) continue;
    let total = 0;
    const key = keys[c];
    for (let dz = -1; dz <= 1 && total < o.minNeighbors; dz++)
      for (let dy = -1; dy <= 1 && total < o.minNeighbors; dy++)
        for (let dx = -1; dx <= 1; dx++) {
          const n = cells.get(key + dx + dy * strideY + dz * strideZ);
          if (n !== undefined) total += stats[n * 13];
        }
    if (total < o.minNeighbors) { outlier[c] = 1; outlierCells++; }
  }

  // Plane fit per well-sampled cell. The vertex is the cell's own centroid; the
  // normal and thickness come from the 3x3x3 block, since a cell holds only a
  // handful of 1 cm samples and its own fit is noisy.
  const vertexOf = new Int32Array(cellCount).fill(-1);
  const axisOf = new Uint8Array(cellCount);
  const vPos = [], vCol = [], vNrm = [];
  let vertexCount = 0;
  const maxThickness2 = (o.maxThickness * cell) ** 2;
  const minSpread2 = (0.15 * cell) ** 2;
  const cov = new Float64Array(6), block = new Float64Array(10);
  for (let c = 0; c < cellCount; c++) {
    const s = c * 13, n = stats[s];
    if (n < o.minPoints || outlier[c]) continue;
    block.fill(0);
    const key = keys[c];
    for (let dz = -1; dz <= 1; dz++) for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
      const neighbor = cells.get(key + dx + dy * strideY + dz * strideZ);
      if (neighbor === undefined || outlier[neighbor]) continue;
      for (let k = 0; k < 10; k++) block[k] += stats[neighbor * 13 + k];
    }
    const bn = block[0];
    const bx = block[1] / bn, by = block[2] / bn, bz = block[3] / bn;
    cov[0] = block[4] / bn - bx * bx; cov[1] = block[5] / bn - bx * by; cov[2] = block[6] / bn - bx * bz;
    cov[3] = block[7] / bn - by * by; cov[4] = block[8] / bn - by * bz; cov[5] = block[9] / bn - bz * bz;
    const mx = stats[s + 1] / n, my = stats[s + 2] / n, mz = stats[s + 3] / n;
    const { values, vectors } = eigenSymmetric3(cov);
    let lo = 0;
    if (values[1] < values[lo]) lo = 1;
    if (values[2] < values[lo]) lo = 2;
    const mid = Math.min(values[(lo + 1) % 3], values[(lo + 2) % 3]);
    if (values[lo] > maxThickness2 || mid < minSpread2) continue; // not a surface patch
    let nxv = vectors[0][lo], nyv = vectors[1][lo], nzv = vectors[2][lo];
    const ax = Math.abs(nxv), ay = Math.abs(nyv), az = Math.abs(nzv);
    const axis = ax >= ay && ax >= az ? 0 : ay >= az ? 1 : 2;
    // Orient along +dominant axis; triangles are wound to match.
    if ((axis === 0 ? nxv : axis === 1 ? nyv : nzv) < 0) { nxv = -nxv; nyv = -nyv; nzv = -nzv; }
    vPos.push(minX + mx, minY + my, minZ + mz);
    vCol.push(stats[s + 10] / n, stats[s + 11] / n, stats[s + 12] / n);
    vNrm.push(nxv, nyv, nzv);
    vertexOf[c] = vertexCount++;
    axisOf[c] = axis;
  }

  // Grid triangulation in each cell's dominant-axis plane.
  const strides = [1, strideY, strideZ];
  const planeTol = o.planeTolerance * cell, maxEdge2 = (o.maxEdge * cell) ** 2;
  const findVertex = (key, axis) => {
    for (const step of [0, -1, 1]) {
      const c = cells.get(key + step * strides[axis]);
      if (c !== undefined && vertexOf[c] >= 0) return vertexOf[c];
    }
    return -1;
  };
  const edgeOk = (a, b) => {
    const dx = vPos[b * 3] - vPos[a * 3], dy = vPos[b * 3 + 1] - vPos[a * 3 + 1], dz = vPos[b * 3 + 2] - vPos[a * 3 + 2];
    if (dx * dx + dy * dy + dz * dz > maxEdge2) return false;
    const dot = vNrm[a * 3] * vNrm[b * 3] + vNrm[a * 3 + 1] * vNrm[b * 3 + 1] + vNrm[a * 3 + 2] * vNrm[b * 3 + 2];
    if (Math.abs(dot) < o.minNormalDot) return false;
    const offA = Math.abs(vNrm[a * 3] * dx + vNrm[a * 3 + 1] * dy + vNrm[a * 3 + 2] * dz);
    const offB = Math.abs(vNrm[b * 3] * dx + vNrm[b * 3 + 1] * dy + vNrm[b * 3 + 2] * dz);
    return offA <= planeTol && offB <= planeTol;
  };
  const indices = [];
  const emit = (a, b, c) => {
    if (!edgeOk(a, b) || !edgeOk(b, c) || !edgeOk(c, a)) return;
    // Wind so the face normal follows the vertex normal of `a`.
    const ux = vPos[b * 3] - vPos[a * 3], uy = vPos[b * 3 + 1] - vPos[a * 3 + 1], uz = vPos[b * 3 + 2] - vPos[a * 3 + 2];
    const wx = vPos[c * 3] - vPos[a * 3], wy = vPos[c * 3 + 1] - vPos[a * 3 + 1], wz = vPos[c * 3 + 2] - vPos[a * 3 + 2];
    const face = (uy * wz - uz * wy) * vNrm[a * 3] + (uz * wx - ux * wz) * vNrm[a * 3 + 1] + (ux * wy - uy * wx) * vNrm[a * 3 + 2];
    if (face >= 0) indices.push(a, b, c); else indices.push(a, c, b);
  };
  for (let c = 0; c < cellCount; c++) {
    const a = vertexOf[c];
    if (a < 0) continue;
    const axis = axisOf[c], key = keys[c];
    const su = strides[(axis + 1) % 3], sv = strides[(axis + 2) % 3];
    const b = findVertex(key + su, axis), d = findVertex(key + sv, axis), e = findVertex(key + su + sv, axis);
    if (b >= 0 && e >= 0 && d >= 0) { emit(a, b, e); emit(a, e, d); }
    else if (b >= 0 && d >= 0) emit(a, b, d);
    else if (b >= 0 && e >= 0) emit(a, b, e);
    else if (e >= 0 && d >= 0) emit(a, e, d);
  }

  // Points: drop outliers, optionally samples already shown as faces; radius from local spacing.
  const meshed = new Uint8Array(cellCount);
  if (!o.coveredPoints && indices.length) {
    const used = new Uint8Array(vertexCount);
    for (const index of indices) used[index] = 1;
    for (let c = 0; c < cellCount; c++) if (vertexOf[c] >= 0 && used[vertexOf[c]]) meshed[c] = 1;
  }
  let kept = 0;
  for (let i = 0; i < count; i++) if (!outlier[pointCell[i]] && !meshed[pointCell[i]]) kept++;
  const outPositions = new Float32Array(kept * 3), outColors = new Float32Array(kept * 3), outRadius = new Float32Array(kept);
  // Sample spacing over the 3x3x3 block, treated as a surface patch: a sparse
  // lattice gets discs wide enough to close its gaps, a dense patch small ones.
  const minRadius = 0.004, maxRadius = o.maxRadius;
  const blockSpacing = new Float32Array(cellCount);
  for (let i = 0, j = 0; i < count; i++) {
    const c = pointCell[i];
    if (outlier[c] || meshed[c]) continue;
    if (!blockSpacing[c]) {
      let total = 0;
      const key = keys[c];
      for (let dz = -1; dz <= 1; dz++) for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const neighbor = cells.get(key + dx + dy * strideY + dz * strideZ);
        if (neighbor !== undefined) total += stats[neighbor * 13];
      }
      blockSpacing[c] = 3 * cell / Math.sqrt(total);
    }
    outPositions[j * 3] = positions[i * 3]; outPositions[j * 3 + 1] = positions[i * 3 + 1]; outPositions[j * 3 + 2] = positions[i * 3 + 2];
    outColors[j * 3] = colors[i * 3]; outColors[j * 3 + 1] = colors[i * 3 + 1]; outColors[j * 3 + 2] = colors[i * 3 + 2];
    outRadius[j] = Math.min(maxRadius, Math.max(minRadius, 0.55 * blockSpacing[c]));
    j++;
  }
  let meshedCells = 0;
  for (let c = 0; c < cellCount; c++) meshedCells += meshed[c];
  return {
    points: { positions: outPositions, colors: outColors, radius: outRadius, count: kept },
    mesh: { positions: new Float32Array(vPos), colors: new Float32Array(vCol), normals: new Float32Array(vNrm),
      indices: new Uint32Array(indices), vertices: vertexCount, triangles: indices.length / 3 },
    stats: { input_points: count, cells: cellCount, outlier_cells: outlierCells, surface_cells: vertexCount,
      meshed_cells: meshedCells, triangles: indices.length / 3, points: kept, cell_m: cell,
      build_ms: Math.round(performance.now() - started) },
  };
}

function emptyResult(started) {
  return {
    points: { positions: new Float32Array(0), colors: new Float32Array(0), radius: new Float32Array(0), count: 0 },
    mesh: { positions: new Float32Array(0), colors: new Float32Array(0), normals: new Float32Array(0),
      indices: new Uint32Array(0), vertices: 0, triangles: 0 },
    stats: { input_points: 0, cells: 0, outlier_cells: 0, surface_cells: 0, meshed_cells: 0, triangles: 0, points: 0,
      cell_m: 0, build_ms: Math.round(performance.now() - started) },
  };
}

if (typeof self !== "undefined" && typeof window === "undefined" && typeof self.postMessage === "function") {
  self.onmessage = ({ data }) => {
    try {
      const result = buildMeasuredSurface(data.positions, data.colors, data.count, data.options);
      const { points, mesh } = result;
      self.postMessage({ id: data.id, ...result }, [points.positions.buffer, points.colors.buffer, points.radius.buffer,
        mesh.positions.buffer, mesh.colors.buffer, mesh.normals.buffer, mesh.indices.buffer]);
    } catch (error) {
      self.postMessage({ id: data.id, error: String(error) });
    }
  };
}
