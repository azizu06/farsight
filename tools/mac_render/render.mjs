/**
 * Headless (or desktop) Three.js map. Same wire as the Quest viewer:
 * GET /map/points.bin, /map/mesh.bin and map status from ?upstream=.
 * Camera follows the newest pose from the parent server. Visual only.
 *
 * Perf structure:
 * - Geometry lives in pooled buffers that grow; snapshots write into them
 *   once (partial GPU re-upload via updateRanges), never new objects.
 * - Upload order is nested LOD prefixes: first quarter is every 4th item,
 *   first half is every 2nd, so setDrawRange alone changes fidelity with
 *   zero re-upload. The LOD ladder trades pixel ratio and draw fraction
 *   against a measured frame-cost EMA, and climbs back up (to a
 *   supersampled top rung) whenever the budget allows.
 * - The render loop only rasterizes when something changed; per-frame work
 *   allocates nothing.
 * - Plug-in layers (e.g. tools/mac_render/dense-splats.mjs) register via
 *   window.__macRegisterLayer({ init, poll, pre, post, quality, dispose })
 *   without touching this loop: poll runs every tick, pre/post wrap the
 *   main render, quality fires on every LOD change.
 */
window.__mac = { ready: false, error: null };
window.addEventListener("error", (event) => {
  window.__mac.error = String(event.message || event.error);
});
import * as THREE from "/quest/node_modules/three/build/three.module.js";
import { decodeMesh, decodePoints, KeyedCloudMerger } from "/quest/src/protocol.js";
import { mapResponseRevision } from "/quest/src/map-response.js";
import { DenseCloudState, DenseCloudReceiver, DenseGpuChunks } from "./dense-cloud.mjs";
import { StereoPass, SBS_LAYOUT } from "./stereo-pass.mjs";
import { DesktopPreviewCamera } from "./desktop-preview.mjs";
import { sceneAnchor } from "./scene-anchor.mjs";
import { fitSceneBounds } from "./scene-framing.mjs";
import { measuredSurfaceLayer, measuredSurfaceOptions } from "./measured-surface.mjs";

const params = new URLSearchParams(location.search);
const upstream = (params.get("upstream") || "").replace(/\/$/, "");
const width = Number(params.get("width") || 1280);
const height = Number(params.get("height") || 720);
const stereoRequested = params.get("layout") === SBS_LAYOUT;
const canvasPresentation = params.get("presentation") === "canvas";
const localFixture = params.get("fixture") === "1";
// Opt-in mesh budget (?mesh_triangles=N, 0 = all stored triangles) for a fast
// link to the map source; without it the source's wire cap applies.
const meshTrianglesParam = Number(params.get("mesh_triangles"));
const meshPath = params.has("mesh_triangles") && Number.isSafeInteger(meshTrianglesParam) && meshTrianglesParam >= 0
  ? `/map/mesh.bin?triangles=${meshTrianglesParam}` : "/map/mesh.bin";

const style = params.get("style") === "unlit" ? "unlit" : "lit";
// Cinematic is the default look; ?quality=flat reproduces the pre-cine
// baseline (no splats, no fill light, no grade). JPEG quality and MSAA
// have independent overrides: ?jq=0-1, ?aa=0|1.
const cine = params.get("quality") !== "flat";
// MSAA stays opt-in: measured ~2x capture cost on the capture-driven path,
// and the top LOD rung's 1.5x supersample already gives the stream better
// anti-aliasing than 4x MSAA. ?aa=1 enables it where the resolve is cheap.
const antialias = params.get("aa") === "1";
const jqParam = Number(params.get("jq"));
const jpegQuality = Number.isFinite(jqParam) && jqParam > 0
  ? Math.min(0.98, jqParam)
  : (cine ? 0.9 : 0.8);
const GRAPHITE = 0x1c2228;
const FOG = 0x242b31;
const ACCENT = 0xc4a574;

const renderer = new THREE.WebGLRenderer({
  antialias, alpha: false,
  // Stream capture retains its backbuffer. Direct display presents to the
  // compositor without retaining an additional capture buffer.
  preserveDrawingBuffer: !canvasPresentation,
  powerPreference: "high-performance",
});
renderer.setSize(width, height, false);
renderer.setPixelRatio(1);
renderer.setClearColor(GRAPHITE);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.shadowMap.enabled = style === "lit";
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
document.body.prepend(renderer.domElement);
const stereoPass = stereoRequested ? new StereoPass({ THREE, renderer }) : null;

const scene = new THREE.Scene();
scene.background = new THREE.Color(GRAPHITE);
// Aerial perspective: fog color sits just above the clear color so distant
// map falls off gently instead of clipping into the background.
const sceneFog = new THREE.Fog(FOG, 12, 48);
scene.fog = sceneFog;
const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 2000);
camera.position.set(9, 6, 9);
camera.lookAt(0, 0, 0);
const overviewPosition = camera.position.clone(), overviewQuaternion = camera.quaternion.clone();
const overviewBounds = new THREE.Box3();
let overviewOwned = true, overviewEpoch = null, overviewAt = -Infinity;
let overviewAspect, overviewFov;
const overviewView = { active: false, target: null };
const desktopPreview = new DesktopPreviewCamera({ THREE, camera, width, height });

const root = new THREE.Group();
scene.add(root);
const grid = new THREE.GridHelper(40, 40, 0x3a424a, 0x2c343c);
grid.position.y = -0.002;
root.add(grid);
// Lights and grid are display aids, never sensor geometry or counted data.
const hemi = new THREE.HemisphereLight(0xcfd6dc, 0x2a3036, style === "lit" ? 0.55 : 0);
const key = new THREE.DirectionalLight(0xfff4e6, style === "lit" ? 0.85 : 0);
key.position.set(8, 14, 6);
key.castShadow = style === "lit";
key.shadow.mapSize.set(1024, 1024);
key.shadow.camera.near = 0.5;
key.shadow.camera.far = 40;
key.shadow.camera.left = key.shadow.camera.bottom = -12;
key.shadow.camera.right = key.shadow.camera.top = 12;
key.shadow.bias = -0.0004;
scene.add(hemi, key);
// Cool fill opposite the warm key keeps shadowed faces legible without
// flattening the two-light ratio.
const fill = new THREE.DirectionalLight(0x8fa8c8, style === "lit" && cine ? 0.32 : 0);
fill.position.set(-9, 5, -8);
scene.add(fill);
if (style !== "lit") scene.add(new THREE.AmbientLight(0xffffff, 1));

// Round splats in cine: a radial-gradient sprite cut by alphaTest keeps the
// points in the opaque pass (depth write, no sorting) but reads as discs
// instead of squares, so dense regions shade like surfaces.
function makeSplatTexture() {
  const size = 64, half = size / 2;
  const sprite = document.createElement("canvas");
  sprite.width = sprite.height = size;
  const g = sprite.getContext("2d");
  const grad = g.createRadialGradient(half, half, 0, half, half, half);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.6, "rgba(255,255,255,1)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(sprite);
  texture.generateMipmaps = false;
  texture.minFilter = THREE.LinearFilter;
  return texture;
}
const splatTexture = cine ? makeSplatTexture() : null;
function pointMaterial(size, minPixel = 2) {
  const material = new THREE.PointsMaterial({
    size, vertexColors: true, toneMapped: cine ? false : style === "lit",
    map: splatTexture, alphaTest: splatTexture ? 0.5 : 0,
    depthFunc: cine ? THREE.LessDepth : THREE.LessEqualDepth,
  });
  material.userData.surfaceDepthBias = { value: 0 };
  material.userData.pointPixelFloor = { value: minPixel };
  material.onBeforeCompile = shader => {
    shader.uniforms.surfaceDepthBias = material.userData.surfaceDepthBias;
    shader.uniforms.pointPixelFloor = material.userData.pointPixelFloor;
    shader.vertexShader = "uniform float surfaceDepthBias, pointPixelFloor;\n" + shader.vertexShader;
    shader.vertexShader = shader.vertexShader.replace("#include <project_vertex>", `
      #include <project_vertex>
      if (surfaceDepthBias > 0.0) {
        vec4 splatBack = projectionMatrix * vec4(mvPosition.xyz - vec3(0.0, 0.0, surfaceDepthBias), 1.0);
        gl_Position.z = splatBack.z / splatBack.w * gl_Position.w;
      }
    `);
    if (cine) shader.vertexShader = shader.vertexShader.replace(
        "#include <logdepthbuf_vertex>",
        "gl_PointSize = clamp(gl_PointSize * projectionMatrix[1][1], pointPixelFloor, 64.0);\n#include <logdepthbuf_vertex>",
      );
  };
  return material;
}
// Coarse 4cm cells need overlapping discs, not the same 2cm footprint as
// the independently retained 1cm dense samples. Native camera colors bypass
// scene lighting/tone mapping so points and RGB textures agree.
const flatPointSize = style === "lit" ? 0.014 : 0.025;
const pointsMaterial = pointMaterial(cine ? 0.072 : flatPointSize);
// Slight overlap closes a 1 cm lattice without spreading each colour over
// several measured cells. Retain small/distant details with the native floor.
const densePointsMaterial = pointMaterial(cine ? 0.016 : flatPointSize, 1.25);
const pointMaterials = [pointsMaterial, densePointsMaterial];
const cloudGeometry = new THREE.BufferGeometry();
const cloud = new THREE.Points(cloudGeometry, pointsMaterial);
cloud.renderOrder = 2;
root.add(cloud);
// Full measured RGB-D detail stays on the Mac. Older relays retain the bounded
// snapshot fallback when they do not provide the incremental dense endpoint.
const denseState = new DenseCloudState({ capacity: 5_000_000 });
const denseReceiver = new DenseCloudReceiver({ state: denseState, endpoint: `${upstream}/dense/delta.bin` });
const hasDenseSource = () => denseReceiver.identity !== null && denseReceiver.identity !== "waiting" && !status?.scan;
const denseGpu = new DenseGpuChunks({ state: denseState, THREE, material: densePointsMaterial, parent: root });
denseGpu.group.renderOrder = 2;
const surfaceMaterial = style === "lit"
  ? new THREE.MeshStandardMaterial({
    color: 0xffffff, vertexColors: true, side: THREE.DoubleSide,
    roughness: 0.85, metalness: 0,
    // Retain all measured points in their buffers/counts; surface depth bias
    // keeps coincident mesh vertices from stippling the shaded faces.
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  })
  : new THREE.MeshBasicMaterial({
    vertexColors: true, side: THREE.DoubleSide, toneMapped: false,
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  });
const surfaceGeometry = new THREE.BufferGeometry();
const surfaces = new THREE.Mesh(surfaceGeometry, surfaceMaterial);
surfaces.renderOrder = 1;
surfaces.castShadow = style === "lit";
surfaces.receiveShadow = style === "lit";
if (style === "lit") {
  surfaceMaterial.onBeforeCompile = (shader) => {
    shader.fragmentShader = shader.fragmentShader.replace(
      "#include <emissivemap_fragment>",
      "#include <emissivemap_fragment>\ntotalEmissiveRadiance += vColor * 0.45;",
    );
  };
}
root.add(surfaces);

// Frustum culling would need an up-to-date boundingSphere per snapshot (an
// O(n) CPU pass); the map fills the view anyway, so pin a huge sphere and
// skip per-object culling. Raycast is unused on this page.
const PINNED_BOUNDS = new THREE.Sphere(new THREE.Vector3(), 1e9);
cloudGeometry.boundingSphere = PINNED_BOUNDS;
surfaceGeometry.boundingSphere = PINNED_BOUNDS;
cloud.frustumCulled = false;
surfaces.frustumCulled = false;

// Pooled attributes: capacity grows 1.5x, snapshots write into the existing
// typed arrays and mark the used range for a partial upload. No per-snapshot
// BufferGeometry/attribute churn.
function pooledAttribute(geometry, name, itemSize, Type) {
  let capacity = 0;
  let attribute = null;
  return {
    ensure(elements) {
      if (capacity >= elements) return attribute;
      capacity = Math.max(elements, capacity + (capacity >> 1), 1024);
      attribute = new THREE.BufferAttribute(new Type(capacity), itemSize);
      attribute.setUsage(THREE.DynamicDrawUsage);
      geometry.setAttribute(name, attribute);
      return attribute;
    },
    get array() { return attribute.array; },
    flush(elements) {
      attribute.clearUpdateRanges();
      attribute.addUpdateRange(0, elements);
      attribute.needsUpdate = true;
    },
  };
}
const meshPosition = pooledAttribute(surfaceGeometry, "position", 3, Float32Array);
const meshColor = pooledAttribute(surfaceGeometry, "color", 3, Float32Array);
const meshNormal = style === "lit" ? pooledAttribute(surfaceGeometry, "normal", 3, Float32Array) : null;
let indexAttribute = null;
let indexCapacity = 0;
let indexUsed = 0;
function ensureIndex(count) {
  if (indexCapacity < count) {
    indexCapacity = Math.max(count, indexCapacity + (indexCapacity >> 1), 3072);
    indexAttribute = new THREE.BufferAttribute(new Uint32Array(indexCapacity), 1);
    indexAttribute.setUsage(THREE.DynamicDrawUsage);
    surfaceGeometry.setIndex(indexAttribute);
    indexUsed = 0;
  }
  return indexAttribute;
}

// The cloud lives in a voxel-keyed merger: full snapshots rebuild it
// wholesale (authoritative for evictions) and /map/points.delta records
// overwrite their cell's slot in place. Order is merge order — LOD prefixes
// still cut by drawRange fraction.
const cloudMerger = new KeyedCloudMerger();
const cloudAttribute = (array, itemSize) =>
  new THREE.BufferAttribute(array, itemSize).setUsage(THREE.DynamicDrawUsage);
let cloudCount = 0;
let cloudHalf = 0;
let cloudQuarter = 0;
let meshTris = 0;

function syncCloud() {
  cloudMerger.sync(cloudGeometry, cloudAttribute);
  cloudCount = cloudMerger.used;
  cloudQuarter = (cloudCount + 3) >> 2;
  cloudHalf = (cloudCount + 1) >> 1;
  cloudGeometry.setDrawRange(0, cloudDrawCount());
  frameDirty = true;
}

function uploadCloud(data) {
  cloudMerger.applyFull(data, cloudVoxelM);
  syncCloud();
}

function uploadCloudDelta(data, voxelM) {
  cloudMerger.applyDelta(data, voxelM);
  syncCloud();
}

function uploadMesh(data) {
  const vertices = data.count;
  meshPosition.ensure(vertices * 3);
  meshColor.ensure(vertices * 3);
  meshPosition.array.set(data.positions);
  meshColor.array.set(data.colors);
  meshPosition.flush(vertices * 3);
  meshColor.flush(vertices * 3);
  const triangles = data.indices.length / 3;
  const index = ensureIndex(data.indices.length);
  const dstIndex = index.array;
  // Meshes keep full topology: a scattered prefix is a perforated surface,
  // not a coarser one (point clouds can scatter; triangles cannot). The
  // LOD ladder sheds mesh cost through pixelRatio and the fx rungs instead.
  dstIndex.set(data.indices);
  // Stale indices beyond the new range would feed computeVertexNormals;
  // zero them so they fold into a degenerate (0,0,0) triangle.
  if (indexUsed > data.indices.length) dstIndex.fill(0, data.indices.length, indexUsed);
  indexUsed = data.indices.length;
  index.clearUpdateRanges();
  index.addUpdateRange(0, data.indices.length);
  index.needsUpdate = true;
  if (meshNormal) {
    meshNormal.ensure(vertices * 3);
    surfaceGeometry.computeVertexNormals();
    meshNormal.flush(vertices * 3);
  }
  meshTris = triangles;
  surfaceGeometry.setDrawRange(0, meshDrawCount());
  frameDirty = true;
}

function clearGeometry() {
  sceneView = null; sceneAnchorAt = -Infinity;
  cloudMerger.clear();
  cloudMerger.sync(cloudGeometry, cloudAttribute);
  cloudCount = cloudHalf = cloudQuarter = 0;
  meshTris = 0;
  indexUsed = 0;
  cloudGeometry.setDrawRange(0, 0);
  surfaceGeometry.setDrawRange(0, 0);
  appliedPoints = appliedMesh = -1;
  pointsEtag = meshEtag = null;
  lastFullAt = -Infinity;
  frameDirty = true;
}

// Adaptive quality ladder: spend measured headroom on fidelity, surrender it
// under load. Top rung supersamples (a 1.5x backing store downsampled into
// the same canvas = free anti-aliasing for the stream) at full geometry.
const LADDER = [
  // dropMs is the sustained frame cost a rung tolerates before stepping
  // down. The supersampled rung costs more by design, so it gets the most
  // headroom; otherwise it would drop the moment it engaged and thrash
  // against the native rung.
  // fx is the cinematic budget gate: it sheds the extras that cost real
  // raster time before the last geometry does — 2 keeps the shadow pass
  // (a second raster of the whole mesh), 1 drops shadows but keeps fog, 0
  // drops both. The flat path never touches these (pre-cine baseline).
  { pixelRatio: 1.5, fraction: 1, dropMs: 26, fx: 2 },
  { pixelRatio: 1.0, fraction: 1, dropMs: 14, fx: 2 },
  { pixelRatio: 1.0, fraction: 0.5, dropMs: 14, fx: 1 },
  { pixelRatio: 0.75, fraction: 0.5, dropMs: 16, fx: 1 },
  { pixelRatio: 0.75, fraction: 0.25, dropMs: Infinity, fx: 0 },
];
const LOD_CLIMB_MS = 9;      // sustained cost below this -> next rung up
const LOD_DROP_DWELL = 12;   // frames between drops
const LOD_CLIMB_DWELL = 150; // frames of headroom before climbing
const lodParam = params.get("lod");
const lodPinned = lodParam !== null && lodParam !== "" && lodParam !== "adaptive";
let lodIndex = lodPinned
  ? Math.min(LADDER.length - 1, Math.max(0, Number(lodParam) || 0))
  : 1;

function cloudDrawCount() {
  const fraction = LADDER[lodIndex].fraction;
  return fraction === 1 ? cloudCount : fraction === 0.5 ? cloudHalf : cloudQuarter;
}
function meshDrawCount() {
  return meshTris * 3;
}

let frameDirty = true;
const lodView = { index: lodIndex, pixelRatio: 1, fraction: 1, fx: 2, adaptive: !lodPinned };
let fxTier = -1;
function applyFxTier(fx) {
  if (!cine || fx === fxTier) return;
  fxTier = fx;
  const shadows = style === "lit" && fx >= 2;
  if (renderer.shadowMap.enabled !== shadows) {
    renderer.shadowMap.enabled = shadows;
    key.castShadow = shadows;
    // The shader variant switch is automatic (enabled is part of the
    // program cache key); needsUpdate just makes it land this frame.
    surfaceMaterial.needsUpdate = true;
    pointsMaterial.needsUpdate = true;
    densePointsMaterial.needsUpdate = true;
  }
  const fog = fx >= 1 ? sceneFog : null;
  if (scene.fog !== fog) scene.fog = fog;
}
function applyLod() {
  const level = LADDER[lodIndex];
  if (renderer.getPixelRatio() !== level.pixelRatio) {
    renderer.setPixelRatio(level.pixelRatio);
    renderer.setSize(width, height, false);
  }
  cloudGeometry.setDrawRange(0, cloudDrawCount());
  surfaceGeometry.setDrawRange(0, meshDrawCount());
  applyFxTier(level.fx);
  lodView.index = lodIndex;
  lodView.pixelRatio = level.pixelRatio;
  lodView.fraction = level.fraction;
  lodView.fx = cine ? level.fx : 2;
  for (const layer of layers) layer.quality?.(layerContext, lodView);
  frameDirty = true;
}

let costEma = 0;
let stableFrames = 0;
let framesSinceChange = 1e9;
function observeFrame(cost) {
  costEma = costEma === 0 ? cost : costEma * 0.85 + cost * 0.15;
  framesSinceChange++;
  if (lodPinned) return;
  if (costEma > LADDER[lodIndex].dropMs && framesSinceChange >= LOD_DROP_DWELL && lodIndex < LADDER.length - 1) {
    lodIndex++;
    applyLod();
    framesSinceChange = 0;
    stableFrames = 0;
  } else if (costEma < LOD_CLIMB_MS) {
    if (++stableFrames >= LOD_CLIMB_DWELL && framesSinceChange >= LOD_CLIMB_DWELL && lodIndex > 0) {
      lodIndex--;
      applyLod();
      framesSinceChange = 0;
      stableFrames = 0;
    }
  } else stableFrames = 0;
}

const rayPositions = new Float32Array(6);
const rayLine = new THREE.Line(
  new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(rayPositions, 3)),
  new THREE.LineBasicMaterial({ color: ACCENT }),
);
rayPositions[4] = -1; // direction (0,0,-1) end
rayLine.geometry.attributes.position.needsUpdate = true;
rayLine.visible = false;
scene.add(rayLine);

// In-framebuffer vignette: one fullscreen quad in NDC space, transparent so
// it composites after every opaque object in the same render call. Living
// inside the framebuffer means __macCapture JPEGs, CDP screencast and layer
// post() output all carry the grade — no post pass, no extra render call.
let gradeTris = 0;
if (cine) {
  const gradeMaterial = new THREE.ShaderMaterial({
    transparent: true, depthTest: false, depthWrite: false,
    vertexShader: "varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }",
    fragmentShader: [
      "varying vec2 vUv;",
      "void main() {",
      "  vec2 p = (vUv - vec2(0.5, 0.46)) * vec2(1.15, 1.0);",
      "  float v = smoothstep(0.40, 0.85, length(p));",
      "  gl_FragColor = vec4(0.018, 0.026, 0.036, v * 0.4);",
      "}",
    ].join("\n"),
  });
  const gradeQuad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), gradeMaterial);
  gradeQuad.frustumCulled = false;
  gradeQuad.renderOrder = 999;
  scene.add(gradeQuad);
  gradeTris = 2;
}

const banner = document.getElementById("banner");
let status = null;
let appliedPoints = -1;
let appliedMesh = -1;
let lastSnapshotKind = null;
// Delta fast lane: 300 ms status polls merge voxel-keyed cell updates while
// full pulls stay the eviction authority on their old cadence. Session flag
// drops permanently if the endpoint 400/404s behind a delta-flagged status.
let cloudVoxelM = 0;
let deltaSupported = true;
let lastFullAt = -Infinity;
let lastDeltaAt = -Infinity;
let pointsEtag = null;
let meshEtag = null;
let pose = null;
const tmp = new THREE.Quaternion();
const viewPosition = [0, 0, 0];

// Plug-in layer seam. A layer registers once and hooks every tick without
// editing this loop: init(ctx) once; poll(ctx, now) every tick — return
// truthy or call ctx.requestFrame() to mark the frame dirty; pre(ctx) before
// the main render (e.g. refresh an offscreen target); post(ctx) after it
// (shade/composite pass); quality(ctx, lod) on every LOD change; dispose()
// on unregister.
const layers = [];
const layerContext = {
  THREE, renderer, scene, camera, root, params, style, width, height,
  getSceneBounds: () => measuredSceneBounds(),
  sceneEpoch: () => hasDenseSource() ? denseReceiver.epoch : mapIdentity,
  sceneBootstrapComplete: () => denseReceiver.cursor.snapshot !== "true",
  requestFrame: () => { frameDirty = true; },
  quality: () => lodView,
  registerLayer: (layer) => registerLayer(layer),
};
function registerLayer(layer) {
  layers.push(layer);
  layer.init?.(layerContext);
  frameDirty = true;
  return () => {
    const i = layers.indexOf(layer);
    if (i >= 0) layers.splice(i, 1);
    layer.dispose?.();
    frameDirty = true;
  };
}
window.__macRegisterLayer = registerLayer;

function measuredSceneBounds() {
  const bounds = new THREE.Box3();
  if (hasDenseSource()) {
    // These boxes are already updated by bounded GPU chunk uploads.
    for (const slot of denseGpu.slots.values()) bounds.union(slot.points.geometry.boundingBox);
  } else if (cloudCount) {
    bounds.setFromArray(cloudGeometry.getAttribute("position").array.subarray(0, cloudCount * 3));
  }
  return bounds;
}

function frameOverview(now) {
  overviewView.active = false;
  if (localFixture || stereoRequested || incomingPose || pose || !hasDenseSource() || !overviewOwned) return;
  // A layer or desktop interaction owns any explicit camera change.
  if (!camera.position.equals(overviewPosition) || !camera.quaternion.equals(overviewQuaternion)) {
    overviewOwned = false;
    return;
  }
  if (denseReceiver.cursor.snapshot === "true" || !denseState.size) return;
  if (denseReceiver.epoch === overviewEpoch && now - overviewAt < 1000) {
    overviewView.active = true;
    return;
  }
  overviewAt = now;
  const bounds = measuredSceneBounds();
  if (bounds.equals(overviewBounds) && denseReceiver.epoch === overviewEpoch &&
      overviewAspect === camera.aspect && overviewFov === camera.getEffectiveFOV()) {
    overviewView.active = true;
    return;
  }
  const frame = fitSceneBounds(THREE, camera, bounds);
  if (!frame) return;
  camera.position.copy(frame.position);
  camera.lookAt(frame.target);
  overviewPosition.copy(camera.position); overviewQuaternion.copy(camera.quaternion);
  overviewBounds.copy(bounds); overviewEpoch = denseReceiver.epoch;
  overviewAspect = camera.aspect; overviewFov = camera.getEffectiveFOV();
  overviewView.active = true; overviewView.target = frame.target.toArray();
  frameDirty = true;
}

// The live default draws every measured dot with its original colour. The
// averaged density mesh remains an explicit ?msurf=1 diagnostic comparison.
const measuredSurface = params.get("msurf") === "1" ? measuredSurfaceLayer({
  THREE, state: denseState, denseGroup: denseGpu.group, options: measuredSurfaceOptions(params),
}) : null;
if (measuredSurface) {
  registerLayer(measuredSurface);
  window.__macMeasuredSurface = measuredSurface;
}

// Dense crew's raw-texture layer (#170/#173 contract): the bundle is optional —
// when .cache/dense-layer-dist is not built the import fails and no layer
// registers. Raw networking is opt-in (default off): ?raw=1 enables pulls,
// window.__macRawLayer.setEnabled(v) toggles at runtime. The layer fetches
// /phone/frame.bin through this server's upstream passthrough.
import("/dense-layer/raw-texture-layer.mjs")
  .then((m) => {
    const layer = m.rawTextureLayer({
      endpoint: "/phone/frame.bin",
      enabled: params.get("raw") === "1",
    });
    window.__macRawLayer = layer;
    registerLayer(layer);
  })
  .catch(() => {});

const mapStatusView = {};
let sceneAnchorAt = -Infinity, sceneView = null;
let bannerText = "";
let cloudShown = -1;
let trisShown = -1;
let sourceShown = "";
let layoutShown = "";
let retainedShown = null;
let errorShown = null;
function exposeMap() {
  const mac = window.__mac;
  const statusAge = status && Number.isFinite(status.age_ms) ? status.age_ms + Math.max(0, performance.now() - statusReceivedAt) : Infinity;
  const fresh = status?.fresh === true && statusAge < 2000;
  const points = hasDenseSource() ? denseState.size : cloudCount || 0;
  const densityTris = measuredSurface?.view.triangles || 0;
  const triangles = (surfaces.visible ? meshTris : 0) + densityTris;
  const sourceLabel = status?.geometry_source || status?.source || "WAITING";
  const source = hasDenseSource() ? `${sourceLabel} · ${denseReceiver.sourceLabel}` : sourceLabel;
  const retained = points > 0 && (mapError !== null || !fresh || status?.geometry?.retained === true);
  mac.points = points;
  mac.triangles = triangles;
  mac.measuredSurface = measuredSurface?.view ?? null;
  mac.source = source;
  mac.retained = retained;
  mac.mapError = mapError;
  mac.lod = lodView;
  if (status) {
    const now = performance.now();
    if (now - sceneAnchorAt >= 1000) {
      sceneAnchorAt = now;
      sceneView = sceneAnchor(denseState.chunks, cloudGeometry.getAttribute("position")?.array, cloudCount);
    }
    mapStatusView.source = status.source;
    mapStatusView.geometry_source = status.geometry_source ?? null;
    mapStatusView.points = status.points;
    mapStatusView.triangles = status.triangles;
    mapStatusView.generation = status.generation;
    mapStatusView.session_id = status.session_id ?? null;
    mapStatusView.map_epoch = status.map_epoch ?? null;
    mapStatusView.scene = sceneView;
    mapStatusView.revision = status.revision;
    mapStatusView.mesh_revision = status.mesh_revision;
    mapStatusView.applied_points_revision = appliedPoints;
    mapStatusView.applied_mesh_revision = appliedMesh;
    mapStatusView.points_delta = status.points_delta === true;
    mapStatusView.fresh = fresh;
    mapStatusView.age_ms = Number.isFinite(statusAge) ? statusAge : null;
    mapStatusView.connected = status.connected === true;
    mapStatusView.retained = retained;
    mapStatusView.partial = status.geometry?.partial === true;
    mac.mapStatus = mapStatusView;
  } else mac.mapStatus = null;
  // DOM writes only when the banner actually changes — never per frame.
  const layoutLabel = window.__mac.stereo ? "STEREO · SBS" : stereoRequested ? "STEREO UNAVAILABLE" : "NOT STEREO";
  if (points !== cloudShown || triangles !== trisShown || source !== sourceShown || layoutLabel !== layoutShown ||
      retained !== retainedShown || mapError !== errorShown) {
    cloudShown = points; trisShown = triangles; sourceShown = source;
    retainedShown = retained; errorShown = mapError;
    layoutShown = layoutLabel;
    bannerText = `MAC RENDER · ${cine ? "CINE" : "FLAT"} · ${source} · ${points} pts · ${triangles} tris${retained ? " · RETAINED" : ""}${mapError ? " · map unavailable" : ""} · ${layoutLabel} · GRID: DISPLAY GUIDE`;
    banner.textContent = bannerText;
  }
}
async function boundedBody(response, limit, signal) {
  if (!response.ok) throw new Error(`Map HTTP ${response.status}`);
  const reader = response.body.getReader();
  const chunks = []; let length = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (signal.aborted) throw new Error("Map refresh cancelled");
      if (done) break;
      length += value.byteLength;
      if (length > limit) throw new Error("Map response exceeds limit");
      chunks.push(value);
    }
    const body = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    return body.buffer;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
async function pull(path, expected, signal, etag = null) {
  const response = await fetch(`${upstream}${path}`, {
    cache: "no-store", signal,
    headers: etag ? { "If-None-Match": etag } : undefined,
  });
  // Revision ETag hit: the authority says the snapshot we applied is still
  // current — ~100 B instead of the full compressed blob.
  if (response.status === 304) return { notModified: true };
  const revision = mapResponseRevision(response.headers, expected);
  if (revision === null) {
    await response.body?.cancel().catch(() => {});
    throw new Error("Map snapshot identity/revision mismatch");
  }
  return { revision, etag: response.headers.get("ETag"), body: await boundedBody(response, 12 * 1024 * 1024, signal) };
}
// Delta records share the points.bin record layout; only the response
// headers differ (X-Map-Revision/Generation, no session/epoch headers).
async function pullDelta(after, signal) {
  const response = await fetch(`${upstream}/map/points.delta?after=${after}`, { cache: "no-store", signal });
  if (response.status === 400 || response.status === 404) return { unsupported: true };
  if (!response.ok) throw new Error(`Map HTTP ${response.status}`);
  const revision = Number(response.headers.get("X-Map-Revision"));
  const generation = Number(response.headers.get("X-Map-Generation"));
  return { revision, generation, body: await boundedBody(response, 12 * 1024 * 1024, signal) };
}
function validStatus(next) {
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  if (!next || typeof next !== "object" || typeof next.source !== "string" ||
      ![next.generation, next.revision, next.mesh_revision, next.points, next.triangles].every(integer)) {
    throw new Error("Invalid map status");
  }
  return next;
}
function sourceIdentity(next) {
  return JSON.stringify([next.source, next.session_id?.toLowerCase() ?? null, next.map_epoch ?? null,
    next.geometry_source ?? null]);
}
function acceptMapStatus(next) {
  const nextIdentity = sourceIdentity(next);
  if (mapIdentity !== nextIdentity || mapGeneration !== next.generation) {
    if (mapIdentity === nextIdentity && next.generation < mapGeneration) return false;
    clearGeometry(); mapIdentity = nextIdentity; mapGeneration = next.generation;
  } else if (status && (next.revision < status.revision || next.mesh_revision < status.mesh_revision)) return false;
  status = next; statusReceivedAt = performance.now();
  if (cine) {
    pointsMaterial.size = (next.voxel_m || 0.04) * 1.8;
    frameDirty = true;
  }
  return true;
}
async function readStatus(signal) {
  const response = await fetch(`${upstream}/map/status`, { cache: "no-store", signal });
  return validStatus(JSON.parse(textDecoder.decode(await boundedBody(response, 65536, signal))));
}
async function snapshotStillCurrent(expectedIdentity, expectedGeneration, signal) {
  const latest = await readStatus(signal);
  return acceptMapStatus(latest) && !signal.aborted && mapIdentity === expectedIdentity && mapGeneration === expectedGeneration;
}
// Which full pull to run on the authority cadence. Mirrors the pre-delta
// alternation, plus a revalidation pick when deltas keep revisions current:
// ETag 304s are ~100 B and the pulls age server-side evictions out.
function pickFullKind(next, meshStale, pointsStale, deltaServer) {
  if (meshStale && !(next.capped && lastSnapshotKind === "mesh" && pointsStale)) return "mesh";
  if (pointsStale) return "points";
  if (!deltaServer) return null;
  if (next.triangles > 0 && lastSnapshotKind === "points") return "mesh";
  if (next.points > 0) return "points";
  if (next.triangles > 0) return "mesh";
  return null;
}
async function refresh(force = false) {
  if (refreshBusy) return false;
  refreshBusy = true;
  window.__mac.refreshBusy = true;
  const abort = new AbortController();
  const timer = window.setTimeout(() => abort.abort(), 60000);
  try {
    const next = await readStatus(abort.signal);
    const nextIdentity = sourceIdentity(next);
    if (!acceptMapStatus(next)) return false;
    if (hasDenseSource()) {
      // Keep pose/status updates, but leave bulk bandwidth to the displayed
      // dense stream instead of downloading hidden coarse replacements.
      mapError = null;
      return true;
    }
    const now = performance.now();
    const voxelM = Number(next.voxel_m) > 0 ? next.voxel_m : 0;
    if (voxelM > 0) cloudVoxelM = voxelM;
    const deltaServer = next.points_delta === true && deltaSupported;
    if (!next.points && !next.triangles) {
      clearGeometry(); appliedPoints = next.revision; appliedMesh = next.mesh_revision;
    } else {
      const meshStale = next.triangles > 0 && next.mesh_revision > appliedMesh;
      const pointsStale = next.points > 0 && next.revision > appliedPoints;
      // Delta servers keep the five-second bulk cadence; fallback keeps the
      // two-second one. appliedPoints < 0 always means fill now, and a manual
      // __macRefreshMap() must pull a stale revision on that call — the
      // pre-delta contract was pull-when-stale on every refresh, with the
      // interval only scheduling calls.
      const fullDue = force || appliedPoints < 0 || now - lastFullAt >= (deltaServer ? 5000 : 2000);
      const kind = fullDue ? pickFullKind(next, meshStale, pointsStale, deltaServer) : null;
      if (kind === "mesh") {
        lastFullAt = now;
        const result = await pull(meshPath, { ...next, revision: next.mesh_revision }, abort.signal, meshEtag);
        if (result.notModified) appliedMesh = next.mesh_revision;
        else {
          if (!await snapshotStillCurrent(nextIdentity, next.generation, abort.signal)) return false;
          const mesh = decodeMesh(result.body);
          uploadMesh(mesh);
          if (result.etag) meshEtag = result.etag;
          if (!next.capped || next.geometry_source) {
            // On a delta server the keyed cloud is authoritative — seed it
            // from the mesh only until the first real points.bin lands, so
            // voxel keys never mix with approximate mesh-vertex keys.
            if (!deltaServer || appliedPoints < 0) {
              uploadCloud(mesh); appliedPoints = deltaServer ? -1 : next.revision;
            }
          }
          appliedMesh = result.revision;
        }
        lastSnapshotKind = "mesh";
      } else if (kind === "points") {
        lastFullAt = now;
        const result = await pull("/map/points.bin", next, abort.signal, pointsEtag);
        if (result.notModified) appliedPoints = next.revision;
        else {
          if (!await snapshotStillCurrent(nextIdentity, next.generation, abort.signal)) return false;
          uploadCloud(decodePoints(result.body));
          if (result.etag) pointsEtag = result.etag;
          appliedPoints = result.revision;
          if (!next.triangles) { meshTris = 0; indexUsed = 0; surfaceGeometry.setDrawRange(0, 0); appliedMesh = next.mesh_revision; }
        }
        lastSnapshotKind = "points";
      } else if (deltaServer && voxelM > 0 && appliedPoints >= 0 && next.revision > appliedPoints &&
                 now - lastDeltaAt >= 300) {
        lastDeltaAt = now;
        const result = await pullDelta(appliedPoints, abort.signal);
        if (result.unsupported) deltaSupported = false;
        else {
          if (!Number.isSafeInteger(result.revision) || result.revision < appliedPoints ||
              result.generation !== next.generation) throw new Error("Map delta watermark mismatch");
          if (!await snapshotStillCurrent(nextIdentity, next.generation, abort.signal)) return false;
          uploadCloudDelta(decodePoints(result.body), voxelM);
          appliedPoints = result.revision;
        }
      }
    }
    mapError = null;
    return true;
  } catch (error) {
    mapError = String(error).slice(0, 160);
    return false;
  } finally {
    window.clearTimeout(timer); refreshBusy = false; window.__mac.refreshBusy = false; exposeMap();
  }
}

let refreshBusy = false;
let mapIdentity = null;
let mapGeneration = -1;
let mapError = null;
let statusReceivedAt = -Infinity;
let incomingPose = null;
const textDecoder = new TextDecoder();

function clearPose() {
  // Only the visible->hidden transition dirties a frame; repeated idle clears stay free.
  if (rayLine.visible || window.__mac.poseValid) frameDirty = true;
  rayLine.visible = false; window.__mac.poseEcho = null; window.__mac.poseValid = false;
}
function finiteVector(value, count) {
  return Array.isArray(value) && value.length === count && value.every(item => typeof item === "number" && Number.isFinite(item));
}
Object.defineProperty(window, "__macPose", {
  configurable: true,
  get: () => incomingPose,
  set: next => {
    // This setter runs inside page.evaluate, so this receipt and RAF use one
    // browser monotonic clock. Python's monotonic epoch is never compared here.
    incomingPose = next ? { ...next, browserReceiptMs: performance.now() } : null;
    clearPose();
  },
});
function applyPose() {
  const next = incomingPose;
  const age = next ? next.receiptAgeMs + performance.now() - next.browserReceiptMs : Infinity;
  if (!next) { clearPose(); return; }
  // Per-object validation cache: the pose setter replaces the object on each
  // update, so a repeated object needs only the age check re-evaluated.
  let valid = next === pose ? next.__valid !== false : undefined;
  if (valid === undefined) {
    const q = next.quaternion;
    const quaternionLength = finiteVector(q, 4) ? Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]) : 0;
    valid = next.tracked === true && Number.isFinite(next.receiptAgeMs) && next.receiptAgeMs >= 0 &&
        finiteVector(next.position, 3) && quaternionLength >= 1e-6 &&
        [next.sentMonoMs, next.sentWallMs, next.receivedMonoMs].every(value =>
          typeof value === "number" && Number.isFinite(value)) &&
        (!next.ray || (finiteVector(next.ray.origin, 3) && finiteVector(next.ray.direction, 3) &&
          Math.hypot(next.ray.direction[0], next.ray.direction[1], next.ray.direction[2]) >= 1e-6));
    next.__valid = valid;
  }
  if (!valid || !Number.isFinite(age) || age < 0 || age >= 250) { clearPose(); return; }
  window.__mac.poseValid = true;
  if (next !== pose) {
    pose = next;
    const p = next.position;
    // Quest places the captured picture at the pose identified by poseEcho.
    // Rendering from an intermediate camera would break that reprojection.
    camera.position.set(p[0], p[1], p[2]);
    const q = next.quaternion;
    tmp.set(q[0], q[1], q[2], q[3]).normalize();
    camera.quaternion.copy(tmp);
    if (Number.isFinite(next.fov_y_deg) && next.fov_y_deg >= 1 && next.fov_y_deg <= 170) camera.fov = next.fov_y_deg;
    if (Number.isFinite(next.aspect) && next.aspect >= .1 && next.aspect <= 5) camera.aspect = next.aspect;
    camera.updateProjectionMatrix();
    if (next.ray) {
      const o = next.ray.origin, d = next.ray.direction;
      const scale = 8 / Math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
      rayPositions[0] = o[0]; rayPositions[1] = o[1]; rayPositions[2] = o[2];
      rayPositions[3] = o[0] + d[0] * scale; rayPositions[4] = o[1] + d[1] * scale; rayPositions[5] = o[2] + d[2] * scale;
      rayLine.geometry.attributes.position.needsUpdate = true;
      rayLine.visible = true;
    } else rayLine.visible = false;
    next.__echo = { sent_mono_ms: next.sentMonoMs, sent_wall_ms: next.sentWallMs,
      received_mono_ms: next.receivedMonoMs, session_id: next.session_id,
      frame_id: next.frame_id, pose_id: next.pose_id, client_nonce: next.client_nonce,
      space: next.space, map_revision: next.map_revision, eyes: next.eyes };
    window.__mac.fovY = camera.fov;
    window.__mac.aspect = camera.aspect;
    frameDirty = true;
  }
  window.__mac.poseEcho = next.__echo;
}

window.__mac = { ready: true, stereo: false, renderMs: 0, captureMs: 0, draws: 0, points: 0, source: "WAITING", poseEcho: null };
let lastTickAt = 0;
let densePagesSeen = 0;
let denseResetSeen = -1;
let cloudVisibleSeen = true;
let surfacesVisibleSeen = true;
const denseMetricsView = {};
function drawScene(measure) {
  const started = performance.now();
  if (!localFixture) denseReceiver.update();
  // Dense deltas, resets and TTL expiries are the dense lane's "changed"
  // signal; without one a clean frame may skip rasterizing entirely.
  if (denseState.dirty.size > 0 || denseReceiver.pages !== densePagesSeen ||
      denseState.resetVersion !== denseResetSeen) frameDirty = true;
  densePagesSeen = denseReceiver.pages;
  denseResetSeen = denseState.resetVersion;
  denseGpu.flush();
  if (!measuredSurface) denseGpu.group.visible = hasDenseSource();
  // An authoritative empty dense scene must stay empty. Falling back to old
  // coarse dots here would undo a revisit or reset that removed the last cell.
  cloud.visible = !hasDenseSource();
  // Coarse face colours interpolate away fine measured markings. Once the
  // dense source is identified, its dots own the live scene; legacy sources
  // retain their coarse mesh fallback. Native camera textures are independent.
  surfaces.visible = !hasDenseSource();
  if (cloud.visible !== cloudVisibleSeen || surfaces.visible !== surfacesVisibleSeen) {
    cloudVisibleSeen = cloud.visible;
    surfacesVisibleSeen = surfaces.visible;
    frameDirty = true;
  }
  frameOverview(performance.now());
  const now = performance.now();
  applyPose();
  for (const layer of layers) {
    if (layer.poll?.(layerContext, now)) frameDirty = true;
  }
  const rawSurfaces = window.__macRawLayer?.stats();
  const textured = rawSurfaces?.raw_enabled && rawSurfaces.raw_surfaces && rawSurfaces.raw_patches > 0;
  // A splat has a finite footprint. Use its back depth while native faces
  // are visible, so neighboring/quantized samples do not paint discs over
  // those faces. Positions, coverage, point buffers and counts stay intact.
  for (const material of pointMaterials) {
    const bias = textured ? material.size / 2 : 0;
    if (material.userData.surfaceDepthBias.value !== bias) frameDirty = true;
    material.userData.surfaceDepthBias.value = bias;
  }
  if (frameDirty || measure === "capture") {
    denseGpu.beginFrame();
    if (stereoPass) {
      window.__mac.stereo = false;
      window.__mac.stereo_eye_draws = null;
      window.__mac.layout = SBS_LAYOUT;
      window.__mac.eye_width = width / 2; window.__mac.eye_height = height;
      try {
        if (!window.__mac.poseValid || !window.__mac.poseEcho) {
          window.__mac.poseEcho = desktopPreview.next();
          window.__mac.previewMode = "desktop-preview";
        } else window.__mac.previewMode = "tracked-eyes";
        const eyes = window.__mac.poseEcho.eyes;
        if (!Array.isArray(eyes) || eyes.length !== 2 || eyes[0]?.eye !== "left" || eyes[1]?.eye !== "right")
          throw new Error("Stereo wire eyes must be ordered left then right");
        const eyeContext = view => ({ ...layerContext, ...view });
        const pair = stereoPass.render(scene, window.__mac.poseEcho.eyes, { width, height,
          pre: view => { for (const layer of layers) layer.pre?.(eyeContext(view)); },
          post: view => { for (const layer of layers) layer.post?.(eyeContext(view)); },
        });
        Object.assign(window.__mac, pair.metadata);
        window.__mac.stereo_eye_draws = pair.eye_draws;
        window.__mac.stereo_error = null;
        window.__mac.drawnPoints = (pair.eye_draws.left.points || 0) + (pair.eye_draws.right.points || 0);
        window.__mac.drawnTriangles = Math.max(0, (pair.eye_draws.left.triangles || 0) - gradeTris)
          + Math.max(0, (pair.eye_draws.right.triangles || 0) - gradeTris);
      } catch (error) {
        // No partial or duplicate mono pair may leave the encoder as stereo.
        window.__mac.stereo_error = String(error).slice(0, 160);
        window.__mac.drawnPoints = window.__mac.drawnTriangles = 0;
      }
    } else {
      for (const layer of layers) layer.pre?.(layerContext);
      renderer.render(scene, camera);
      for (const layer of layers) layer.post?.(layerContext);
      window.__mac.drawnPoints = renderer.info.render.points;
      window.__mac.drawnTriangles = renderer.info.render.triangles - gradeTris;
    }
    denseGpu.endFrame();
    frameDirty = false;
  }
  Object.assign(denseMetricsView, denseReceiver.metrics(), denseGpu.metrics());
  window.__mac.dense = denseMetricsView;
  window.__mac.renderMs = performance.now() - started;
  window.__mac.rayVisible = rayLine.visible;
  viewPosition[0] = camera.position.x;
  viewPosition[1] = camera.position.y;
  viewPosition[2] = camera.position.z;
  window.__mac.viewPosition = viewPosition;
  window.__mac.sceneOverview = overviewView;
  window.__mac.draws = (window.__mac.draws || 0) + 1;
  exposeMap();
  if (measure === "raf") {
    // Vsync overshoot is the only GPU-cost signal available without a
    // readback; idle frames stay near it, saturated ones stretch past it.
    if (lastTickAt > 0) {
      const gap = started - lastTickAt;
      observeFrame(window.__mac.renderMs + Math.max(0, gap - 17));
    }
    lastTickAt = started;
  }
}
// Pixels and provenance are read in one JavaScript turn, without an intervening
// screenshot/RAF. Geometry callbacks cannot replace buffers during this call.
window.__macCapture = () => {
  // "capture" forces a draw in this same task, so toDataURL never reads a cleared buffer.
  drawScene("capture");
  window.__mac.surfaceDisplay = { coarse_splat_m: pointsMaterial.size,
    dense_splat_m: densePointsMaterial.size,
    coarse_depth_bias_m: pointsMaterial.userData.surfaceDepthBias.value,
    dense_depth_bias_m: densePointsMaterial.userData.surfaceDepthBias.value,
    raw: window.__macRawLayer?.stats() ?? null };
  const captureStarted = performance.now();
  const data = renderer.domElement.toDataURL("image/jpeg", jpegQuality).split(",")[1];
  window.__mac.captureMs = performance.now() - captureStarted;
  // The readback is where rasterization actually bills: render+capture is the
  // real per-frame cost in the server-driven pipeline.
  observeFrame(window.__mac.renderMs + window.__mac.captureMs);
  return { data, meta: { ...window.__mac,
    mapStatus: window.__mac.mapStatus ? { ...window.__mac.mapStatus } : null,
    poseEcho: window.__mac.poseEcho ? { ...window.__mac.poseEcho } : null } };
};
renderer.setAnimationLoop(() => { if (!window.__macCaptureDriven) drawScene("raf"); });
window.__macRefreshMap = () => refresh(true);
applyLod();
if (!localFixture) refresh();
// 300 ms polls drive the delta fast lane; full pulls pace themselves.
const refreshTimer = localFixture ? null : window.setInterval(refresh, 300);
window.addEventListener("pagehide", () => {
  window.clearInterval(refreshTimer); incomingPose = null; clearPose();
  denseReceiver.dispose(); denseGpu.dispose();
});
