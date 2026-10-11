/** Real Three.js cameras and projection math; stub raster/state, no GPU or headset. */
import test from "node:test";
import assert from "node:assert/strict";
import * as THREE from "../../quest/node_modules/three/build/three.module.js";
import { StereoPass, SBS_LAYOUT } from "../mac_render/stereo-pass.mjs";

function eyes() {
  const projection = new THREE.PerspectiveCamera(83, .8, .05, 120).projectionMatrix.toArray();
  projection[8] = .11; projection[9] = -.07; // Genuine asymmetric frustum.
  return ["right", "left"].map(eye => ({ eye, position: [eye === "left" ? -.032 : .032, 1.6, 0],
    quaternion: [0, 0, 0, 2], projection: [...projection] }));
}
const near = (a, b, label) => a.forEach((value, index) => assert.ok(Math.abs(value - b[index]) < 1e-10, `${label}[${index}]: ${value} vs ${b[index]}`));

class Renderer {
  autoClear = true;
  target = { original: true }; face = 3; mip = 2;
  viewport = new THREE.Vector4(7, 8, 90, 91);
  scissor = new THREE.Vector4(1, 2, 30, 31); scissorTest = false;
  renders = []; clears = []; throwEye = null;
  info = { autoReset: true, render: { calls: 9, points: 40, triangles: 30, lines: 0 } };
  getRenderTarget() { return this.target; }
  getActiveCubeFace() { return this.face; }
  getActiveMipmapLevel() { return this.mip; }
  getViewport(out) { return out.copy(this.viewport); }
  getScissor(out) { return out.copy(this.scissor); }
  getScissorTest() { return this.scissorTest; }
  setRenderTarget(target, face = 0, mip = 0) { this.target = target; this.face = face; this.mip = mip; }
  setViewport(value) { this.viewport.copy(value); }
  setScissor(value) { this.scissor.copy(value); }
  setScissorTest(value) { this.scissorTest = value; }
  state() { return { target: this.target, face: this.face, mip: this.mip, viewport: this.viewport.toArray(),
    scissor: this.scissor.toArray(), scissorTest: this.scissorTest, autoClear: this.autoClear }; }
  clear(...flags) { this.clears.push({ ...this.state(), flags }); }
  render(scene, camera) {
    this.renders.push({ ...this.state(), scene, camera });
    if (camera === this.throwEye) throw new Error("raster failed");
    const add = this.info.autoReset ? 0 : 1;
    for (const [key, value] of Object.entries({ calls: 2, points: 100, triangles: 20, lines: 1 }))
      this.info.render[key] = this.info.render[key] * add + value;
  }
}
const setup = () => { const renderer = new Renderer(); return { renderer, pass: new StereoPass({ THREE, renderer }), scene: new THREE.Scene() }; };
const dims = { width: 1280, height: 720 };

test("arbitrary eye order uses exact asymmetric projection, inverse and normalized real camera pose", () => {
  const { pass, renderer, scene } = setup(), pair = eyes(), original = structuredClone(pair);
  const leftCamera = pass.cameras.left;
  pass.render(scene, pair, dims);
  assert.equal(pass.cameras.left, leftCamera, "camera reused");
  assert.equal(renderer.renders[0].camera, pass.cameras.left);
  assert.equal(renderer.renders[1].camera, pass.cameras.right);
  for (const eye of pair) {
    const camera = pass.cameras[eye.eye];
    near(camera.position.toArray(), eye.position, "position");
    near(camera.quaternion.toArray(), [0, 0, 0, 1], "quaternion");
    assert.deepEqual(camera.projectionMatrix.toArray(), eye.projection);
    near(camera.projectionMatrix.clone().multiply(camera.projectionMatrixInverse).toArray(), new THREE.Matrix4().toArray(), "projection inverse");
    near(camera.matrixWorld.clone().multiply(camera.matrixWorldInverse).toArray(), new THREE.Matrix4().toArray(), "pose inverse");
  }
  assert.deepEqual(pair, original, "caller eye packet remains unchanged");
});

test("actual Three.js camera math has depth-dependent stereo parallax and retains lens offsets", () => {
  const { pass, scene } = setup(); pass.render(scene, eyes(), dims);
  const project = (eye, z) => new THREE.Vector3(0, 1.6, z).project(pass.cameras[eye]);
  const nearLeft = project("left", -1), nearRight = project("right", -1);
  const farLeft = project("left", -10), farRight = project("right", -10);
  const disparity = nearLeft.x - nearRight.x;
  assert.ok(disparity > .01, "two eye cameras must not duplicate center view");
  assert.ok(Math.abs((farLeft.x - farRight.x) * 10 - disparity) < 1e-10);
  assert.ok(Math.abs((nearLeft.x + nearRight.x) / 2 + .11) < 1e-10, "horizontal asymmetric projection preserved");
  assert.ok(Math.abs(nearLeft.y - .07) < 1e-10, "vertical asymmetric projection preserved");
});

test("SBS halves are separately cleared and layer callbacks receive their actual eye camera", () => {
  const { pass, renderer, scene } = setup(), events = [];
  const result = pass.render(scene, eyes(), { ...dims,
    pre: context => events.push(["pre", context.eye, context.camera, context.viewport.toArray()]),
    post: context => events.push(["post", context.eye, context.camera, context.viewport.toArray()]),
  });
  assert.deepEqual(events.map(value => value.slice(0, 2)), [["pre", "left"], ["post", "left"], ["pre", "right"], ["post", "right"]]);
  for (const [i, eye] of ["left", "right"].entries()) {
    const viewport = [i * 640, 0, 640, 720];
    assert.deepEqual(renderer.renders[i].viewport, viewport);
    assert.deepEqual(renderer.renders[i].scissor, viewport);
    assert.equal(renderer.renders[i].target, null); assert.equal(renderer.renders[i].scissorTest, true);
    assert.equal(renderer.renders[i].autoClear, false);
    assert.deepEqual(renderer.clears[i].viewport, viewport); assert.deepEqual(renderer.clears[i].flags, [true, true, true]);
    assert.equal(events[i * 2][2], pass.cameras[eye]);
  }
  assert.deepEqual(result.metadata, { stereo: true, layout: SBS_LAYOUT, width: 1280, height: 720, eye_width: 640, eye_height: 720 });
  assert.deepEqual(result.eye_draws, { left: { calls: 2, points: 100, triangles: 20, lines: 1 }, right: { calls: 2, points: 100, triangles: 20, lines: 1 } });
  assert.deepEqual(result.main_scene_renders, { left: 1, right: 1 });
  assert.equal(result.completed_pairs, 1);
  assert.equal(Object.hasOwn(result, "unique_points"), false);
});

test("full pair validation precedes all drawing, state changes and camera reconfiguration", () => {
  const invalid = [eyes().slice(0, 1), [eyes()[0], eyes()[0]],
    eyes().map(view => ({ ...view, eye: undefined })),
    [eyes()[0], { ...eyes()[1], projection: new Array(16).fill(1) }],
    [eyes()[0], { ...eyes()[1], position: [NaN, 0, 0] }],
    [eyes()[0], { ...eyes()[1], quaternion: [0, 0, 0, 0] }]];
  for (const pair of invalid) {
    const { pass, renderer, scene } = setup(), before = renderer.state();
    assert.throws(() => pass.render(scene, pair, dims));
    assert.deepEqual(renderer.state(), before); assert.equal(renderer.renders.length, 0); assert.equal(renderer.clears.length, 0);
    assert.deepEqual(pass.cameras.right.position.toArray(), [0, 0, 0]);
  }
});

test("canvas LOD preserves coded metadata only for a uniform scale; malformed dimensions fail closed", () => {
  const { pass, renderer, scene } = setup();
  const result = pass.render(scene, eyes(), { width: 960, height: 540, codedWidth: 1920, codedHeight: 1080 });
  assert.equal(result.metadata.eye_width, 960); assert.equal(result.metadata.eye_height, 1080);
  assert.deepEqual(renderer.renders.map(frame => frame.viewport), [[0, 0, 480, 540], [480, 0, 480, 540]]);
  for (const size of [{ ...dims, codedWidth: 1281 }, { ...dims, height: 0 }, { ...dims, width: Infinity },
    { width: 1282, height: 720, codedWidth: 1282, codedHeight: 720 },
    { ...dims, codedWidth: 1920, codedHeight: 720 }]) {
    const fresh = setup(); assert.throws(() => fresh.pass.render(fresh.scene, eyes(), size));
    assert.equal(fresh.renderer.renders.length, 0);
  }
});

test("offscreen pre hooks cannot redirect either main eye draw; all original renderer state is restored", () => {
  const { pass, renderer, scene } = setup(), before = renderer.state(), offscreen = {};
  pass.render(scene, eyes(), { ...dims, pre: () => {
    renderer.setRenderTarget(offscreen); renderer.setViewport(new THREE.Vector4(0, 0, 32, 32));
    renderer.setScissorTest(false); renderer.autoClear = true;
  }, post: () => { renderer.setRenderTarget(offscreen); } });
  assert.ok(renderer.renders.every(frame => frame.target === null && frame.scissorTest && !frame.autoClear));
  assert.deepEqual(renderer.state(), before);
});

test("pre, raster and post failures restore target face/mip, viewport, scissor and autoClear", () => {
  for (const fault of ["pre", "raster", "post"]) {
    const { pass, renderer, scene } = setup(), before = renderer.state();
    if (fault === "raster") renderer.throwEye = pass.cameras.right;
    const hooks = fault === "raster" ? {} : { [fault]: ({ eye }) => { if (eye === "right") throw new Error("hook failed"); } };
    assert.throws(() => pass.render(scene, eyes(), { ...dims, ...hooks }));
    assert.deepEqual(renderer.state(), before); assert.equal(pass.pairs, 0, "no completed pair claim");
    assert.equal(pass.draws.left, 1); assert.equal(pass.draws.right, fault === "post" ? 1 : 0);
  }
});

test("non-resetting renderer counters report per-eye deltas rather than cumulative or unique points", () => {
  const { pass, renderer, scene } = setup(); renderer.info.autoReset = false;
  const result = pass.render(scene, eyes(), dims);
  assert.equal(result.eye_draws.left.points, 100); assert.equal(result.eye_draws.right.points, 100);
  assert.equal(result.eye_draws.left.calls, 2); assert.equal(result.eye_draws.right.calls, 2);
});

test("camera instances and projection updates are reused across paired frames", () => {
  const { pass, scene } = setup(), original = { ...pass.cameras };
  pass.render(scene, eyes(), dims);
  const next = eyes(); next[0].position[2] = -1; next[0].projection[8] = -.25;
  const result = pass.render(scene, next, dims);
  assert.equal(pass.cameras.right, original.right); assert.equal(pass.cameras.left, original.left);
  assert.equal(pass.cameras.right.position.z, -1); assert.equal(pass.cameras.right.projectionMatrix.elements[8], -.25);
  assert.equal(result.completed_pairs, 2); assert.deepEqual(result.main_scene_renders, { left: 2, right: 2 });
});
