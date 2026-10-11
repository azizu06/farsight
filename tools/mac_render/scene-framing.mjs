/** Fit retained measured geometry without modifying its buffers or the camera.
 * The returned vectors are applied by the caller with position.copy/lookAt.
 */
export function fitSceneBounds(THREE, camera, bounds, padding = 1.12,
  direction = new THREE.Vector3(1, .5, 1)) {
  if (bounds.isEmpty()) return null;
  const target = bounds.getCenter(new THREE.Vector3());
  // Match the camera's lookAt orientation, including its configured up vector.
  const basis = new THREE.Matrix4().lookAt(direction, new THREE.Vector3(), camera.up);
  const right = new THREE.Vector3().setFromMatrixColumn(basis, 0);
  const up = new THREE.Vector3().setFromMatrixColumn(basis, 1);
  const away = new THREE.Vector3().setFromMatrixColumn(basis, 2);
  const vertical = Math.tan(THREE.MathUtils.degToRad(camera.getEffectiveFOV()) / 2);
  const horizontal = vertical * camera.aspect;
  // The measured dot display has a 16 mm diameter; leave its 8 mm footprint
  // inside the frame and clear the camera's near plane for tiny measured boxes.
  const radius = .008;
  const offset = new THREE.Vector3();
  let distance = 0;
  for (const x of [bounds.min.x, bounds.max.x]) {
    for (const y of [bounds.min.y, bounds.max.y]) {
      for (const z of [bounds.min.z, bounds.max.z]) {
        offset.set(x - target.x, y - target.y, z - target.z);
        const depth = offset.dot(away);
        distance = Math.max(distance,
          depth + (Math.abs(offset.dot(right)) + radius) * padding / horizontal,
          depth + (Math.abs(offset.dot(up)) + radius) * padding / vertical,
          depth + camera.near + radius);
      }
    }
  }
  return { position: target.clone().addScaledVector(away, distance), target, distance };
}
