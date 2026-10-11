/**
 * Atomic side-by-side raster pass. Inject the renderer's THREE namespace so the
 * browser and portable tests use the same real Three.js camera implementation.
 *
 *   const stereo = new StereoPass({ THREE, renderer });
 *   // Caller updates/polls geometry ONCE, then renders both eyes synchronously:
 *   const result = stereo.render(scene, pose.eyes, {
 *     width: cssWidth, height: cssHeight, codedWidth, codedHeight,
 *     pre: ({ camera, eye, viewport }) => layers.forEach(l => l.pre?.({ ...ctx, camera, eye, viewport })),
 *     post: ({ camera, eye, viewport }) => layers.forEach(l => l.post?.({ ...ctx, camera, eye, viewport })),
 *   });
 *   Object.assign(captureMetadata, result.metadata);
 *   // Read JPEG pixels only AFTER render returns; attach caller's exact poseEcho.
 *
 * width/height are total renderer CSS dimensions; codedWidth/codedHeight are the
 * fixed encoder dimensions (default CSS dimensions). A uniform scale is allowed
 * for canvas LOD, never a different packed aspect. Canvas sizing, renderer pixel
 * ratio, capture identity, freshness, polling and dense updates remain caller-owned.
 * Missing/invalid eye data throws BEFORE renderer state changes or drawing.
 * Hooks must be synchronous; post-render failures still restore renderer state.
 * Counts describe per-eye main-scene draw submissions, NOT unique map points.
 */
export const SBS_LAYOUT = "sbs-left-right";
const finiteArray = (value, size) => Array.isArray(value) && value.length === size &&
  value.every(item => typeof item === "number" && Number.isFinite(item));
const counters = renderer => Object.fromEntries(["calls", "points", "triangles", "lines"].map(key => {
  const value = renderer.info?.render?.[key];
  return [key, Number.isSafeInteger(value) && value >= 0 ? value : null];
}));

function dimensions(width, height, codedWidth, codedHeight) {
  if (![width, height, codedWidth, codedHeight].every(value => Number.isSafeInteger(value) && value > 0) ||
      codedWidth % 4 || codedHeight % 2 || width * codedHeight !== height * codedWidth)
    throw new Error("Stereo dimensions require positive integers, even-width eyes, even height and one uniform scale");
  return { stereo: true, layout: SBS_LAYOUT, width: codedWidth, height: codedHeight,
    eye_width: codedWidth / 2, eye_height: codedHeight };
}

function validatePair(THREE, eyes) {
  if (!Array.isArray(eyes) || eyes.length !== 2) throw new Error("Stereo requires exactly two labeled eyes");
  const pair = {};
  for (const view of eyes) {
    if (!view || !["left", "right"].includes(view.eye) || pair[view.eye] ||
        !finiteArray(view.position, 3) || !finiteArray(view.quaternion, 4) || !finiteArray(view.projection, 16))
      throw new Error("Stereo requires valid, distinct left/right eyes");
    const length = Math.hypot(...view.quaternion);
    if (!Number.isFinite(length) || length < 1e-6) throw new Error("Stereo eye quaternion is zero or excessive");
    const projection = new THREE.Matrix4().fromArray(view.projection);
    const scale = Math.max(...view.projection.map(Math.abs));
    if (!scale) throw new Error("Stereo eye projection is singular");
    const normalized = new THREE.Matrix4().fromArray(view.projection.map(value => value / scale));
    const determinant = normalized.determinant();
    if (!Number.isFinite(determinant) || determinant === 0) throw new Error("Stereo eye projection is singular");
    const inverse = projection.clone().invert();
    if (!inverse.elements.every(Number.isFinite) || inverse.elements.every(value => value === 0))
      throw new Error("Stereo eye projection has no finite inverse");
    pair[view.eye] = { position: [...view.position], quaternion: view.quaternion.map(value => value / length), projection, inverse };
  }
  return pair;
}

export class StereoPass {
  constructor({ THREE, renderer }) {
    this.THREE = THREE; this.renderer = renderer;
    this.cameras = { left: new THREE.PerspectiveCamera(), right: new THREE.PerspectiveCamera() };
    this.draws = { left: 0, right: 0 }; this.pairs = 0;
  }

  render(scene, eyes, { width, height, codedWidth = width, codedHeight = height, pre, post, target = null } = {}) {
    // Validate the WHOLE pair and dimensions before configuring either camera.
    const metadata = dimensions(width, height, codedWidth, codedHeight);
    const pair = validatePair(this.THREE, eyes);
    for (const eye of ["left", "right"]) {
      const camera = this.cameras[eye], view = pair[eye];
      camera.position.fromArray(view.position); camera.quaternion.fromArray(view.quaternion);
      camera.scale.set(1, 1, 1); camera.updateMatrixWorld(true);
      camera.projectionMatrix.copy(view.projection); camera.projectionMatrixInverse.copy(view.inverse);
      // Never call updateProjectionMatrix(): that would erase asymmetric XR projections.
    }
    const renderer = this.renderer, Vector4 = this.THREE.Vector4;
    const saved = { target: renderer.getRenderTarget(),
      face: renderer.getActiveCubeFace?.() ?? 0, mip: renderer.getActiveMipmapLevel?.() ?? 0,
      viewport: renderer.getViewport(new Vector4()), scissor: renderer.getScissor(new Vector4()),
      scissorTest: renderer.getScissorTest(), autoClear: renderer.autoClear };
    const counts = {};
    try {
      renderer.autoClear = false;
      for (const eye of ["left", "right"]) {
        const viewport = new Vector4(eye === "left" ? 0 : width / 2, 0, width / 2, height);
        const camera = this.cameras[eye];
        const context = { eye, camera, viewport, renderer, scene };
        const bind = () => {
          renderer.setRenderTarget(target); renderer.setViewport(viewport);
          renderer.setScissor(viewport); renderer.setScissorTest(true); renderer.autoClear = false;
        };
        bind(); renderer.clear(true, true, true);
        const beforeHook = pre?.(context);
        if (beforeHook?.then) throw new Error("Stereo layer hooks must be synchronous");
        // A pre hook may render an offscreen target; main scene always returns to its half.
        bind();
        const before = counters(renderer);
        renderer.render(scene, camera);
        this.draws[eye]++;
        const after = counters(renderer);
        counts[eye] = Object.fromEntries(Object.keys(after).map(key => [key,
          renderer.info?.autoReset === false ? (after[key] === null || before[key] === null ? null : Math.max(0, after[key] - before[key])) : after[key]]));
        const afterHook = post?.(context);
        if (afterHook?.then) throw new Error("Stereo layer hooks must be synchronous");
      }
      this.pairs++;
      return { metadata, eye_draws: counts, completed_pairs: this.pairs,
        main_scene_renders: { ...this.draws } };
    } finally {
      renderer.setRenderTarget(saved.target, saved.face, saved.mip);
      renderer.setViewport(saved.viewport); renderer.setScissor(saved.scissor);
      renderer.setScissorTest(saved.scissorTest); renderer.autoClear = saved.autoClear;
    }
  }
}
