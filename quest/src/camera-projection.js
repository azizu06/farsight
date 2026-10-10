/** @typedef {{ fx: number, fy: number, cx: number, cy: number }} CameraIntrinsics */

function validateIntrinsics(intrinsics) {
  if (!intrinsics || ![intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy].every(Number.isFinite) ||
      intrinsics.fx <= 0 || intrinsics.fy <= 0) throw new RangeError("Invalid camera intrinsics");
}

/**
 * Pixels start at the image's top left; Three cameras look along negative Z.
 * This converts direction only: it supplies no depth or additional camera views.
 * @param {number} xPx
 * @param {number} yPx
 * @param {CameraIntrinsics} intrinsics
 * @returns {[number, number, number]}
 */
export function cameraRayFromPixel(xPx, yPx, intrinsics) {
  validateIntrinsics(intrinsics);
  if (![xPx, yPx].every(Number.isFinite)) throw new RangeError("Invalid camera pixel coordinates");
  const right = (xPx - intrinsics.cx) / intrinsics.fx;
  const up = (intrinsics.cy - yPx) / intrinsics.fy;
  const scale = Math.max(Math.abs(right), Math.abs(up), 1);
  if (!Number.isFinite(scale)) throw new RangeError("Camera ray exceeds numeric range");
  const x = right / scale, y = up / scale, z = -1 / scale;
  const length = Math.hypot(x, y, z);
  return [x / length, y / length, z / length];
}

/**
 * Project only the captured image onto an inward-facing spherical patch.
 * UVs match Three CanvasTexture: the top image row has v=1.
 * The caller owns the camera orientation, viewer translation and outside-FOV boundary.
 * @param {number} width
 * @param {number} height
 * @param {CameraIntrinsics} intrinsics
 * @param {{ columns?: number, rows?: number, radius?: number }} [options]
 */
export function cameraProjectionGrid(width, height, intrinsics, options = {}) {
  validateIntrinsics(intrinsics);
  if (![width, height].every(Number.isFinite) || width <= 0 || height <= 0)
    throw new RangeError("Invalid camera image dimensions");
  if (!options || typeof options !== "object" || Array.isArray(options))
    throw new RangeError("Invalid camera projection options");
  const { columns = 32, rows = 24, radius = 100 } = options;
  if (![columns, rows].every(value => Number.isInteger(value) && value >= 1 && value <= 256))
    throw new RangeError("Camera projection subdivisions must be integers from 1 to 256");
  if (!Number.isFinite(radius) || radius <= 0 || radius > 1000)
    throw new RangeError("Camera projection radius must be positive and at most 1000");

  const stride = columns + 1, vertices = stride * (rows + 1);
  const positions = new Float32Array(vertices * 3);
  const uvs = new Float32Array(vertices * 2);
  const indices = new Uint32Array(columns * rows * 6);
  for (let row = 0; row <= rows; row++) {
    for (let column = 0; column <= columns; column++) {
      const vertex = row * stride + column, u = column / columns, v = row / rows;
      const ray = cameraRayFromPixel(u * width, v * height, intrinsics);
      positions.set(ray.map(value => value * radius), vertex * 3);
      uvs.set([u, 1 - v], vertex * 2);
    }
  }
  for (let row = 0; row < rows; row++) {
    for (let column = 0; column < columns; column++) {
      const a = row * stride + column, b = a + 1, c = a + stride, d = c + 1;
      indices.set([a, c, b, b, c, d], (row * columns + column) * 6);
    }
  }
  return { positions, uvs, indices };
}
