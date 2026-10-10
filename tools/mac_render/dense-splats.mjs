/**
 * Dense measured map on the Mac GPU: every retained 1 cm RGB-D cell from the relay's
 * /dense/delta.bin (docs/interfaces.md), drawn as world-sized round splats, then
 * shaded in screen space (depth-derived normals + eye-dome lighting) so the room
 * reads as solid lit surfaces instead of dots. Measured samples only: nothing is
 * interpolated or invented; the splat radius only closes the gaps between them.
 */
import * as THREE from "/quest/node_modules/three/build/three.module.js";

const RECORD_BYTES = 16;
const OFFSET = 65536; // fine-cell keys stay within ±655 m, so three 17-bit fields fit a double exactly

export function parseDelta(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 4) throw new Error("dense delta too short");
  const length = view.getUint32(0, true);
  if (4 + length > buffer.byteLength) throw new Error("dense delta header truncated");
  const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, length)));
  const start = 4 + length;
  const count = Math.floor((buffer.byteLength - start) / RECORD_BYTES);
  return { meta, view, start, count };
}

export function cellKey(xmm, ymm, zmm) {
  const kx = Math.floor(xmm / 10) + OFFSET, ky = Math.floor(ymm / 10) + OFFSET, kz = Math.floor(zmm / 10) + OFFSET;
  return (kx * 131072 + ky) * 131072 + kz;
}

const srgbToLinear = new Float32Array(256).map((_, i) => {
  const c = i / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
});

/** One dense source (RGB-D, or the gray native ARKit cloud) as a growable splat buffer. */
export class DenseLayer {
  constructor(source, capacity = 4_000_000, gray = false) {
    this.source = source;
    this.capacity = capacity;
    this.gray = gray;
    this.positions = new Float32Array(capacity * 3);
    this.colors = new Float32Array(capacity * 3);
    this.index = new Map();
    this.free = [];
    this.used = 0;
    this.epoch = null; this.cursor = 0; this.anchor = null; this.snapshot = true;
    this.dirtyLow = Infinity; this.dirtyHigh = -1;
    this.geometry = new THREE.BufferGeometry();
    this.position = new THREE.BufferAttribute(this.positions, 3).setUsage(THREE.DynamicDrawUsage);
    this.color = new THREE.BufferAttribute(this.colors, 3).setUsage(THREE.DynamicDrawUsage);
    this.geometry.setAttribute("position", this.position);
    this.geometry.setAttribute("color", this.color);
    this.geometry.setDrawRange(0, 0);
    this.geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 1e6);
  }

  get count() { return this.index.size; }

  clear() {
    this.index.clear(); this.free = []; this.used = 0;
    this.geometry.setDrawRange(0, 0);
  }

  mark(slot) { this.dirtyLow = Math.min(this.dirtyLow, slot); this.dirtyHigh = Math.max(this.dirtyHigh, slot); }

  apply({ view, start, count }) {
    for (let i = 0; i < count; i += 1) {
      const at = start + i * RECORD_BYTES;
      const x = view.getInt32(at, true), y = view.getInt32(at + 4, true), z = view.getInt32(at + 8, true);
      const op = view.getUint8(at + 15);
      const key = cellKey(x, y, z);
      let slot = this.index.get(key);
      if (op === 0) {
        if (slot === undefined) continue;
        this.index.delete(key);
        // Park the removed splat far away; the slot is reused by the next upsert.
        this.positions[slot * 3 + 1] = -1e6;
        this.free.push(slot); this.mark(slot);
        continue;
      }
      if (slot === undefined) {
        slot = this.free.length ? this.free.pop() : this.used < this.capacity ? this.used++ : -1;
        if (slot < 0) continue;
        this.index.set(key, slot);
      }
      this.positions[slot * 3] = x / 1000; this.positions[slot * 3 + 1] = y / 1000; this.positions[slot * 3 + 2] = z / 1000;
      if (this.gray) this.colors.fill(0.55, slot * 3, slot * 3 + 3);
      else {
        this.colors[slot * 3] = srgbToLinear[view.getUint8(at + 12)];
        this.colors[slot * 3 + 1] = srgbToLinear[view.getUint8(at + 13)];
        this.colors[slot * 3 + 2] = srgbToLinear[view.getUint8(at + 14)];
      }
      this.mark(slot);
    }
  }

  /** Upload only the changed slot range. */
  flush() {
    if (this.dirtyHigh < 0) return;
    const from = this.dirtyLow * 3, length = (this.dirtyHigh - this.dirtyLow + 1) * 3;
    for (const attribute of [this.position, this.color]) {
      attribute.clearUpdateRanges(); attribute.addUpdateRange(from, length); attribute.needsUpdate = true;
    }
    this.geometry.setDrawRange(0, this.used);
    this.dirtyLow = Infinity; this.dirtyHigh = -1;
  }

  /** Follow the delta journal until caught up. Returns records applied. */
  async poll(fetchDelta, maxPages = 64) {
    let applied = 0;
    for (let page = 0; page < maxPages; page += 1) {
      const query = new URLSearchParams({ after: String(this.cursor), limit: "8192" });
      if (this.epoch) query.set("epoch", this.epoch);
      if (this.snapshot) query.set("snapshot", "true");
      if (this.anchor !== null) query.set("anchor", String(this.anchor));
      if (this.source !== "rgbd") query.set("source", this.source);
      const delta = parseDelta(await fetchDelta(query));
      const { meta } = delta;
      if (meta.reset) this.clear();
      this.apply(delta); applied += delta.count;
      this.epoch = meta.epoch ?? this.epoch;
      this.cursor = Number.isFinite(meta.cursor) ? meta.cursor : this.cursor;
      this.snapshot = !!meta.snapshot;
      this.anchor = meta.snapshot ? meta.anchor ?? null : null;
      if (!meta.more) break;
    }
    this.flush();
    return applied;
  }
}

/** Round, world-sized splats: one per measured cell, written to colour + depth. */
export function splatMaterial(size) {
  return new THREE.ShaderMaterial({
    uniforms: { size: { value: size }, scale: { value: 1 } },
    vertexShader: `
      uniform float size; uniform float scale;
      attribute vec3 color; varying vec3 vColor;
      void main() {
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = clamp(size * scale / max(-mv.z, 0.05), 1.5, 64.0);
        vColor = color;
      }`,
    fragmentShader: `
      varying vec3 vColor;
      void main() {
        vec2 d = gl_PointCoord * 2.0 - 1.0;
        float r = dot(d, d);
        if (r > 1.0) discard;
        gl_FragColor = vec4(vColor, 1.0);
      }`,
  });
}

/**
 * Screen-space shading of the splat render: normals from the depth buffer, a key
 * light, a soft fill and eye-dome lighting for contact edges, then sRGB out.
 */
export function shadePass(target, camera, background) {
  const material = new THREE.ShaderMaterial({
    depthTest: false, depthWrite: false,
    uniforms: {
      colorTex: { value: target.texture }, depthTex: { value: target.depthTexture },
      texel: { value: new THREE.Vector2(1 / target.width, 1 / target.height) },
      near: { value: camera.near }, far: { value: camera.far },
      inverseProjection: { value: new THREE.Matrix4() },
      background: { value: new THREE.Color(background) },
      edl: { value: 0.25 },
    },
    vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }`,
    fragmentShader: `
      uniform sampler2D colorTex; uniform sampler2D depthTex; uniform vec2 texel;
      uniform float near; uniform float far; uniform mat4 inverseProjection;
      uniform vec3 background; uniform float edl;
      varying vec2 vUv;
      vec3 viewPos(vec2 uv) {
        float d = texture2D(depthTex, uv).x;
        vec4 clip = vec4(uv * 2.0 - 1.0, d * 2.0 - 1.0, 1.0);
        vec4 v = inverseProjection * clip;
        return v.xyz / v.w;
      }
      float logDepth(vec2 uv) { return log2(max(-viewPos(uv).z, 1e-4)); }
      vec3 toSrgb(vec3 c) { return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c)); }
      void main() {
        vec2 uv = vUv;
        float d = texture2D(depthTex, uv).x;
        if (d >= 1.0) {
          // Close sampling gaps between measured splats: a background pixel mostly
          // surrounded by surface takes its nearest neighbour; real silhouettes stay open.
          float nearest = 1.0; vec2 pick = uv; int hits = 0;
          for (int i = 0; i < 8; i++) {
            float a = float(i) * 0.785398;
            for (int r = 2; r <= 6; r += 2) {
              vec2 o = uv + vec2(cos(a), sin(a)) * texel * float(r);
              float there = texture2D(depthTex, o).x;
              if (there < 1.0) { hits++; if (there < nearest) { nearest = there; pick = o; } break; }
            }
          }
          if (hits < 6) { gl_FragColor = vec4(background, 1.0); return; }
          uv = pick; d = nearest;
        }
        vec3 base = texture2D(colorTex, uv).rgb;
        vec3 p = viewPos(uv);
        // Central differences several pixels wide, so the normal follows the surface
        // across neighbouring splats rather than one flat sprite.
        vec2 ox = vec2(texel.x * 6.0, 0.0), oy = vec2(0.0, texel.y * 6.0);
        bool edge = texture2D(depthTex, uv + ox).x >= 1.0 || texture2D(depthTex, uv - ox).x >= 1.0
          || texture2D(depthTex, uv + oy).x >= 1.0 || texture2D(depthTex, uv - oy).x >= 1.0;
        vec3 n = edge ? normalize(-p) : normalize(cross(viewPos(uv + ox) - viewPos(uv - ox), viewPos(uv + oy) - viewPos(uv - oy)));
        if (dot(n, -p) < 0.0) n = -n;
        vec3 key = normalize(vec3(-0.35, 0.8, 0.45));
        float diffuse = max(dot(n, key), 0.0);
        float facing = max(dot(n, normalize(-p)), 0.0);
        float light = 0.42 + 0.38 * diffuse + 0.25 * facing;
        // Eye-dome lighting: darken where neighbours are nearer (edges, creases).
        float here = logDepth(uv), response = 0.0;
        for (int i = 0; i < 8; i++) {
          float a = float(i) * 0.785398;
          vec2 o = vec2(cos(a), sin(a)) * texel * 1.5;
          // Gaps are not occluders: only nearer measured surface darkens an edge.
          if (texture2D(depthTex, uv + o).x < 1.0) response += max(0.0, here - logDepth(uv + o));
        }
        float shade = exp(-(response / 8.0) * 300.0 * edl);
        gl_FragColor = vec4(toSrgb(clamp(base * light * shade, 0.0, 1.0)), 1.0);
      }`,
  });
  const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material);
  quad.frustumCulled = false;
  const scene = new THREE.Scene();
  scene.add(quad);
  const ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  return {
    material,
    render(renderer) {
      material.uniforms.inverseProjection.value.copy(camera.projectionMatrixInverse);
      material.uniforms.near.value = camera.near; material.uniforms.far.value = camera.far;
      renderer.setRenderTarget(null);
      renderer.render(scene, ortho);
    },
  };
}
