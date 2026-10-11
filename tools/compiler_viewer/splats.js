// Compact 3D-Gaussian-splat renderer: instanced screen-space quads, splat data
// in float textures, far->near order streamed from a worker. WebGL2 + three.js.

import * as THREE from 'three';

const SH_C0 = 0.28209479177387814;

const VERT = /* glsl */`precision highp float;
precision highp int;
precision highp sampler2D;

in vec2 corner;      // quad corner, -1..1
in float splatIndex; // instanced: sorted splat id

uniform mat4 uView;
uniform mat4 uProj;
uniform vec2 uFocal;     // fx, fy in pixels
uniform vec2 uViewport;  // px
uniform highp sampler2D uPosOp; // x,y,z,opacity(after sigmoid)
uniform highp sampler2D uScale; // sx,sy,sz (linear, metres)
uniform highp sampler2D uRot;   // quaternion xyzw
uniform highp sampler2D uColor; // rgb linear
uniform float uAlpha;
uniform vec2 uTexSize;

out vec4 vColor;
out vec2 vOffset; // fragment offset from splat centre, pixels
out vec3 vConic;  // inverse 2D covariance (a,b,c)

vec4 texelAt(sampler2D t, float i) {
  float x = mod(i, uTexSize.x);
  float y = floor(i / uTexSize.x);
  return texelFetch(t, ivec2(int(x), int(y)), 0);
}

mat3 quatToMat(vec4 q) {
  float x = q.x, y = q.y, z = q.z, w = q.w;
  return mat3(
    1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z),     2.0 * (x * z + w * y),
    2.0 * (x * y + w * z),     1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x),
    2.0 * (x * z - w * y),     2.0 * (y * z + w * x),     1.0 - 2.0 * (x * x + y * y));
}

void main() {
  vec4 p_op = texelAt(uPosOp, splatIndex);
  vec4 t = uView * vec4(p_op.xyz, 1.0);
  if (t.z > -0.15) { // behind / too close: emit degenerate quad
    gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
    vColor = vec4(0.0); vOffset = vec2(0.0); vConic = vec3(0.0);
    return;
  }

  vec4 q = texelAt(uRot, splatIndex);
  vec3 s = texelAt(uScale, splatIndex).xyz;
  mat3 R = quatToMat(q);
  mat3 M = R * mat3(s.x, 0.0, 0.0, 0.0, s.y, 0.0, 0.0, 0.0, s.z);
  mat3 cov3d = M * transpose(M);
  mat3 V = mat3(uView);
  mat3 cv = V * cov3d * transpose(V);

  float z = t.z;
  mat3 J = mat3(
    uFocal.x / z, 0.0, -(uFocal.x * t.x) / (z * z),
    0.0, uFocal.y / z, -(uFocal.y * t.y) / (z * z),
    0.0, 0.0, 0.0);
  mat3 cov = transpose(J) * cv * J;
  float c0 = cov[0][0] + 0.3; // low-pass filter
  float c1 = cov[0][1];
  float c2 = cov[1][1] + 0.3;

  float det = c0 * c2 - c1 * c1;
  if (det <= 0.0) { gl_Position = vec4(0.0, 0.0, 2.0, 1.0); vColor = vec4(0.0); vOffset = vec2(0.0); vConic = vec3(0.0); return; }
  float inv = 1.0 / det;
  vec3 conic = vec3(c2 * inv, -c1 * inv, c0 * inv);

  float mid = 0.5 * (c0 + c2);
  float disc = sqrt(max(1e-6, mid * mid - det));
  float l1 = mid + disc, l2 = mid - disc;
  float r1 = 3.0 * sqrt(max(l1, 0.0)); // major radius, px (3 sigma)
  float r2 = 3.0 * sqrt(max(l2, 0.0));
  vec2 e1 = (abs(l1 - c0) + abs(c1) < 1e-6) ? vec2(1.0, 0.0) : normalize(vec2(c1, l1 - c0));
  vec2 e2 = vec2(-e1.y, e1.x);

  vec2 off = corner.x * r1 * e1 + corner.y * r2 * e2;
  vOffset = off;
  vConic = conic;
  vec4 clip = uProj * t;
  gl_Position = vec4(clip.xy + off * 2.0 / uViewport * clip.w, clip.z, clip.w);

  vec3 rgb = texelAt(uColor, splatIndex).rgb;
  vColor = vec4(rgb, p_op.a * uAlpha);
}
`;

const FRAG = /* glsl */`precision highp float;
in vec4 vColor;
in vec2 vOffset;
in vec3 vConic;
out vec4 fragColor;
void main() {
  float power = -0.5 * (vConic.x * vOffset.x * vOffset.x + vConic.z * vOffset.y * vOffset.y)
              - vConic.y * vOffset.x * vOffset.y;
  float alpha = vColor.a * exp(min(0.0, power));
  if (alpha < 0.004) discard;
  fragColor = vec4(vColor.rgb * alpha, alpha);
}
`;

export class SplatCloud extends THREE.Mesh {
  constructor(fields, workerUrl = '/viewer/splatsort.js') {
    const { n, positions, colors, opacities, scales, rotations } = fields;
    const geo = new THREE.InstancedBufferGeometry();
    geo.setAttribute('corner', new THREE.BufferAttribute(new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), 2));
    geo.setIndex([0, 1, 2, 2, 1, 3]);
    const idx = new Float32Array(n);
    for (let i = 0; i < n; i++) idx[i] = i;
    geo.setAttribute('splatIndex', new THREE.InstancedBufferAttribute(idx, 1));
    geo.instanceCount = n;

    const texN = Math.ceil(Math.sqrt(n));
    const mk = (data) => {
      const t = new THREE.DataTexture(data, texN, texN, THREE.RGBAFormat, THREE.FloatType);
      t.needsUpdate = true; t.magFilter = t.minFilter = THREE.NearestFilter;
      return t;
    };
    const posOp = new Float32Array(texN * texN * 4);
    const sc = new Float32Array(texN * texN * 4);
    const rot = new Float32Array(texN * texN * 4);
    const col = new Float32Array(texN * texN * 4);
    for (let i = 0; i < n; i++) {
      posOp.set([positions[3 * i], positions[3 * i + 1], positions[3 * i + 2], opacities[i]], 4 * i);
      sc.set([scales[3 * i], scales[3 * i + 1], scales[3 * i + 2], 0], 4 * i);
      rot.set([rotations[4 * i], rotations[4 * i + 1], rotations[4 * i + 2], rotations[4 * i + 3]], 4 * i);
      col.set([colors[3 * i], colors[3 * i + 1], colors[3 * i + 2], 0], 4 * i);
    }
    const mat = new THREE.RawShaderMaterial({
      glslVersion: THREE.GLSL3,
      vertexShader: VERT,
      fragmentShader: FRAG,
      uniforms: {
        uView: { value: new THREE.Matrix4() },
        uProj: { value: new THREE.Matrix4() },
        uFocal: { value: new THREE.Vector2(1, 1) },
        uViewport: { value: new THREE.Vector2(1, 1) },
        uAlpha: { value: 1.0 },
        uTexSize: { value: new THREE.Vector2(texN, texN) },
        uPosOp: { value: mk(posOp) },
        uScale: { value: mk(sc) },
        uRot: { value: mk(rot) },
        uColor: { value: mk(col) },
      },
      transparent: true,
      depthTest: true,
      depthWrite: false,
      blending: THREE.CustomBlending,
      blendEquation: THREE.AddEquation,
      blendSrc: THREE.OneFactor,
      blendDst: THREE.OneMinusSrcAlphaFactor,
      blendSrcAlpha: THREE.OneFactor,
      blendDstAlpha: THREE.OneMinusSrcAlphaFactor,
    });
    super(geo, mat);
    this.frustumCulled = false;
    this.renderOrder = 5;
    this.count = n;
    this._worker = new Worker(workerUrl);
    this._worker.onmessage = (e) => {
      if (e.data.type === 'sorted') {
        const o = e.data.order;
        const attr = this.geometry.getAttribute('splatIndex');
        attr.array.set(o);
        attr.needsUpdate = true;
        this._sortPending = false;
      }
    };
    this._worker.postMessage({ type: 'init', positions: positions.slice(), count: n });
    this._sortPending = false;
    this._lastSortView = new Float32Array(4);
    this._nextSortAt = 0;
  }

  requestSort(camera, far) {
    const e = camera.matrixWorldInverse.elements;
    const v = [e[2], e[6], e[10], e[14]]; // z_view row of view matrix
    const now = performance.now();
    if (this._sortPending || now < this._nextSortAt) return;
    if (Math.abs(v[0] - this._lastSortView[0]) + Math.abs(v[1] - this._lastSortView[1]) +
        Math.abs(v[2] - this._lastSortView[2]) + Math.abs(v[3] - this._lastSortView[3]) < 1e-7) return;
    this._lastSortView.set(v);
    this._sortPending = true;
    this._nextSortAt = now + 40;
    this._worker.postMessage({ type: 'sort', view: [v[0], v[1], v[2], v[3], far] });
  }

  update(camera, renderer) {
    const u = this.material.uniforms;
    u.uView.value.copy(camera.matrixWorldInverse);
    u.uProj.value.copy(camera.projectionMatrix);
    const size = renderer.getDrawingBufferSize(new THREE.Vector2());
    u.uViewport.value.set(size.x, size.y);
    const fy = size.y / (2 * Math.tan(camera.fov * Math.PI / 360));
    u.uFocal.value.set(fy * camera.aspect, fy);
  }

  setAlpha(a) { this.material.uniforms.uAlpha.value = a; }

  dispose() { this._worker.terminate(); }
}

// Convert parsed PLY (from ply.js) into splat fields. Handles INRIA 3DGS names
// (f_dc_*, opacity, scale_*, rot_*) and plain rgb/radius fallbacks.
export function splatFieldsFromPLY(ply) {
  const v = ply.vertex;
  const p = v.props, n = v.count;
  const positions = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) { positions[3 * i] = p.x[i]; positions[3 * i + 1] = p.y[i]; positions[3 * i + 2] = p.z[i]; }

  const colors = new Float32Array(n * 3);
  const f0 = p.f_dc_0, f1 = p.f_dc_1, f2 = p.f_dc_2;
  if (f0 && f1 && f2) {
    for (let i = 0; i < n; i++) {
      colors[3 * i] = Math.min(1, Math.max(0, 0.5 + SH_C0 * f0[i]));
      colors[3 * i + 1] = Math.min(1, Math.max(0, 0.5 + SH_C0 * f1[i]));
      colors[3 * i + 2] = Math.min(1, Math.max(0, 0.5 + SH_C0 * f2[i]));
    }
  } else if (p.red && p.green && p.blue) {
    for (let i = 0; i < n; i++) {
      colors[3 * i] = p.red[i] / 255; colors[3 * i + 1] = p.green[i] / 255; colors[3 * i + 2] = p.blue[i] / 255;
    }
  } else colors.fill(0.8);

  const op = p.opacity;
  const opacities = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const o = op ? op[i] : 10;
    opacities[i] = 1 / (1 + Math.exp(-o));
  }

  const s0 = p.scale_0, s1 = p.scale_1, s2 = p.scale_2;
  const scales = new Float32Array(n * 3);
  if (s0 && s1 && s2) {
    for (let i = 0; i < n; i++) {
      scales[3 * i] = Math.exp(Math.min(s0[i], 4));
      scales[3 * i + 1] = Math.exp(Math.min(s1[i], 4));
      scales[3 * i + 2] = Math.exp(Math.min(s2[i], 4));
    }
  } else {
    const rad = p.radius;
    for (let i = 0; i < n; i++) {
      const r = rad ? Math.max(rad[i], 0.002) : 0.01;
      scales[3 * i] = scales[3 * i + 1] = scales[3 * i + 2] = r;
    }
  }

  const rotations = new Float32Array(n * 4);
  const r0 = p.rot_0, r1 = p.rot_1, r2 = p.rot_2, r3 = p.rot_3;
  if (r0 && r1 && r2 && r3) {
    for (let i = 0; i < n; i++) {
      const l = Math.hypot(r0[i], r1[i], r2[i], r3[i]) || 1;
      rotations[4 * i] = r0[i] / l; rotations[4 * i + 1] = r1[i] / l;
      rotations[4 * i + 2] = r2[i] / l; rotations[4 * i + 3] = r3[i] / l;
    }
  } else {
    for (let i = 0; i < n; i++) { rotations[4 * i + 3] = 1; }
  }
  return { n, positions, colors, opacities, scales, rotations };
}
