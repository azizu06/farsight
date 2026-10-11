/** Static, explicitly synthetic room fixture for the native canvas lab.
 * A retained mesh and one pooled point geometry share the same surfaces.
 * Camera movement changes uniforms only; point buffers upload on rebuild.
 */
export const MAX_CANVAS_POINTS = 20_000_000;

export function createCanvasFixture({ points = 250_000 } = {}) {
  let ctx, THREE, group, meshGroup, pointGeometry, pointMaterial, cloud;
  let positionAttribute, colorAttribute, capacity = 0;
  let rebuildQueue = Promise.resolve();
  let uploadStarted = 0, pendingUpload = false;
  const resources = [];
  const samples = [];
  const meshPositions = [], meshNormals = [], meshColors = [];
  const metrics = {
    source: 'SIMULATED', points: 0, uploaded_points: 0, triangles: 0,
    max_points: MAX_CANVAS_POINTS, buffer_bytes: 0,
    generate_ms: 0, upload_ms: 0, geometry_uploads: 0, rebuilding: false,
    style: 'hybrid',
  };
  let totalArea = 0;

  const layer = {
    metrics,
    ready: rebuildQueue,
    init(context) {
      ctx = context;
      THREE = ctx.THREE;
      group = new THREE.Group();
      group.name = 'SIMULATED · Canvas architecture fixture';
      meshGroup = new THREE.Group();
      group.add(meshGroup);
      ctx.root.add(group);
      buildRoom();
      pointGeometry = new THREE.BufferGeometry();
      pointGeometry.setDrawRange(0, 0);
      pointGeometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, 1.5, 0), 9);
      pointMaterial = new THREE.ShaderMaterial({
        uniforms: {
          pointSize: { value: 0.018 },
          colorGain: { value: 0.36 },
          viewportHeight: { value: ctx.renderer.domElement.height },
        },
        vertexColors: true, toneMapped: false, transparent: false,
        depthWrite: true, depthTest: true, alphaTest: 0.5,
        vertexShader: `
          uniform float pointSize;
          uniform float viewportHeight;
          varying vec3 vPointColor;
          void main() {
            vPointColor = color;
            vec4 eye = modelViewMatrix * vec4(position, 1.0);
            gl_Position = projectionMatrix * eye;
            gl_PointSize = clamp(pointSize * viewportHeight * projectionMatrix[1][1] / (-2.0 * eye.z), 1.5, 18.0);
          }
        `,
        fragmentShader: `
          varying vec3 vPointColor;
          uniform float colorGain;
          void main() {
            vec2 p = gl_PointCoord * 2.0 - 1.0;
            float edge = 1.0 - smoothstep(0.72, 1.0, dot(p, p));
            if (edge < 0.5) discard;
            gl_FragColor = vec4(vPointColor * colorGain * (0.94 + 0.06 * edge), 1.0);
            #include <tonemapping_fragment>
            #include <colorspace_fragment>
          }
        `,
      });
      resources.push(pointGeometry, pointMaterial);
      cloud = new THREE.Points(pointGeometry, pointMaterial);
      cloud.name = 'Synthetic surface samples · retained GPU buffers';
      cloud.frustumCulled = false;
      group.add(cloud);
      layer.ready = layer.setPointCount(points);
    },
    poll() { return false; },
    pre() {
      pointMaterial.uniforms.viewportHeight.value = ctx.renderer.domElement.height;
      if (pendingUpload) uploadStarted = performance.now();
    },
    quality() {
      pointMaterial.uniforms.viewportHeight.value = ctx.renderer.domElement.height;
    },
    setPointCount(count) {
      const requested = Math.min(MAX_CANVAS_POINTS, count);
      rebuildQueue = rebuildQueue.then(() => rebuild(requested));
      layer.ready = rebuildQueue;
      return rebuildQueue;
    },
    setStyle(style) {
      metrics.style = style;
      meshGroup.visible = style !== 'points';
      cloud.visible = style !== 'surface';
      pointMaterial.uniforms.colorGain.value = meshGroup.visible ? 0.36 : 1;
      ctx.requestFrame();
    },
    setPointsVisible(visible) { cloud.visible = visible; ctx.requestFrame(); },
    setMeshVisible(visible) {
      meshGroup.visible = visible;
      pointMaterial.uniforms.colorGain.value = visible ? 0.36 : 1;
      ctx.requestFrame();
    },
    dispose() {
      ctx.root.remove(group);
      for (const resource of resources) resource.dispose();
    },
  };

  function rgb(hex) {
    const c = new THREE.Color(hex);
    return [c.r, c.g, c.b];
  }

  function triangle(a, b, c, na, nb, nc, color, floor = false) {
    meshPositions.push(...a, ...b, ...c);
    meshNormals.push(...na, ...nb, ...nc);
    meshColors.push(...color, ...color, ...color);
    const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
    const vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
    totalArea += Math.hypot(cx, cy, cz) * 0.5;
    samples.push({ a, b, c, na, nb, nc, color, floor, end: totalArea });
  }

  function box(x, y, z, w, h, d, colorHex) {
    const color = rgb(colorHex);
    const lx = x - w / 2, rx = x + w / 2;
    const by = y - h / 2, ty = y + h / 2;
    const bz = z - d / 2, fz = z + d / 2;
    const faces = [
      [[lx, by, fz], [rx, by, fz], [rx, ty, fz], [lx, ty, fz], [0, 0, 1]],
      [[rx, by, bz], [lx, by, bz], [lx, ty, bz], [rx, ty, bz], [0, 0, -1]],
      [[rx, by, fz], [rx, by, bz], [rx, ty, bz], [rx, ty, fz], [1, 0, 0]],
      [[lx, by, bz], [lx, by, fz], [lx, ty, fz], [lx, ty, bz], [-1, 0, 0]],
      [[lx, ty, fz], [rx, ty, fz], [rx, ty, bz], [lx, ty, bz], [0, 1, 0]],
      [[lx, by, bz], [rx, by, bz], [rx, by, fz], [lx, by, fz], [0, -1, 0]],
    ];
    for (const [a, b, c, d0, n] of faces) {
      triangle(a, b, c, n, n, n, color);
      triangle(a, c, d0, n, n, n, color);
    }
  }

  function shape(geometry, x, y, z, colorHex, rotation = 0) {
    const p = geometry.attributes.position, n = geometry.attributes.normal;
    const index = geometry.index;
    const color = rgb(colorHex), cos = Math.cos(rotation), sin = Math.sin(rotation);
    const count = index ? index.count : p.count;
    for (let i = 0; i < count; i += 3) {
      const vertices = [], normals = [];
      for (let j = 0; j < 3; j++) {
        const k = index ? index.getX(i + j) : i + j;
        const px = p.getX(k), pz = p.getZ(k), nx = n.getX(k), nz = n.getZ(k);
        vertices.push([px * cos + pz * sin + x, p.getY(k) + y, -px * sin + pz * cos + z]);
        normals.push([nx * cos + nz * sin, n.getY(k), -nx * sin + nz * cos]);
      }
      triangle(...vertices, ...normals, color);
    }
    geometry.dispose();
  }

  function buildRoom() {
    // Open-front architectural model: all geometry is synthetic demo data.
    const ivory = 0xe8dfcb, concrete = 0xbebcb1, oak = 0xb18b5a;
    const teal = 0x226f73, tealLight = 0x43898a, amber = 0xc9853d;
    box(0, -0.13, 0, 11.4, 0.24, 8.4, 0xb0afa8);
    box(0, 1.73, -4.18, 11.4, 3.46, 0.16, ivory);
    box(-5.62, 0.53, 0, 0.16, 1.06, 8.4, concrete);
    box(5.62, 1.73, -2.34, 0.16, 3.46, 3.68, ivory);
    box(0, 0.13, -4.04, 11.2, 0.18, 0.12, 0xa29a88);
    box(5.48, 0.13, -2.34, 0.12, 0.18, 3.68, 0xa29a88);

    // Recessed opening, broad studio window and oak feature wall.
    box(-3.42, 1.96, -4.03, 3.45, 2.10, 0.08, 0x35474a);
    box(-3.42, 1.96, -3.97, 3.24, 1.90, 0.05, 0x8cbbb4);
    box(-3.42, 1.96, -3.90, 0.075, 1.92, 0.075, ivory);
    box(-3.42, 1.96, -3.90, 3.28, 0.075, 0.075, ivory);
    box(-3.42, 0.92, -3.84, 3.66, 0.13, 0.42, 0xe6dac0);
    for (let i = 0; i < 29; i++) box(0.25 + i * 0.165, 1.77, -4.005, 0.072, 3.22, 0.075, oak);
    box(2.45, 1.9, -3.92, 1.94, 1.52, 0.10, 0x3b4b48);
    box(2.45, 1.9, -3.85, 1.80, 1.38, 0.035, 0xe0c093);
    box(2.26, 1.90, -3.825, 0.50, 1.10, 0.015, teal);
    box(2.77, 1.58, -3.815, 0.62, 0.44, 0.015, amber);

    // Conversation area. Baked mesh = one draw call across all furniture.
    box(1.40, 0.018, 0.85, 4.45, 0.035, 3.72, 0xd2c8b2);
    for (let i = 0; i < 13; i++) box(1.40, 0.039, -0.82 + i * 0.28, 4.20, 0.008, 0.025, 0xbdb6a6);
    box(1.05, 0.38, -0.95, 3.62, 0.60, 1.16, teal);
    box(1.05, 0.86, -1.42, 3.62, 0.58, 0.28, teal);
    box(-0.69, 0.64, -0.95, 0.23, 0.55, 1.22, teal);
    box(2.79, 0.64, -0.95, 0.23, 0.55, 1.22, teal);
    for (let i = 0; i < 3; i++) box(-0.05 + i * 1.11, 0.70, -0.83, 1.04, 0.20, 0.96, tealLight);
    box(-0.10, 1.00, -1.13, 0.50, 0.43, 0.18, 0xc7af7e);
    box(2.13, 1.00, -1.12, 0.53, 0.43, 0.18, 0xdda459);
    shape(new THREE.CylinderGeometry(0.89, 0.89, 0.09, 48), 1.28, 0.54, 1.08, 0xc2a679);
    shape(new THREE.CylinderGeometry(0.47, 0.53, 0.47, 24), 1.28, 0.26, 1.08, 0x454f4c);
    box(1.12, 0.605, 1.0, 0.39, 0.045, 0.28, 0xf0e7d0);
    box(1.12, 0.634, 1.0, 0.34, 0.013, 0.25, teal);
    shape(new THREE.CylinderGeometry(0.12, 0.09, 0.20, 18), 1.58, 0.67, 1.1, amber);
    shape(new THREE.CylinderGeometry(0.45, 0.51, 0.64, 28), 3.88, 0.34, 1.72, amber);
    shape(new THREE.CylinderGeometry(0.44, 0.44, 0.11, 28), 3.88, 0.70, 1.72, 0xe0aa61);

    // Workbench, shelves, a simple desk light, and low-poly greenery.
    box(-3.75, 0.85, -1.05, 2.53, 0.10, 1.15, 0xc9b18d);
    for (const dx of [-1.06, 1.06]) {
      box(-3.75 + dx, 0.42, -1.38, 0.10, 0.83, 0.10, 0x4d5c59);
      box(-3.75 + dx, 0.42, -0.73, 0.10, 0.83, 0.10, 0x4d5c59);
    }
    box(-3.80, 1.28, -1.30, 0.98, 0.67, 0.065, 0x3c5152);
    box(-3.80, 1.28, -1.255, 0.88, 0.57, 0.02, 0x5aa5a4);
    box(-3.80, 0.98, -1.30, 0.08, 0.22, 0.08, 0x3c5152);
    box(-3.80, 0.92, -1.27, 0.45, 0.04, 0.29, 0x3c5152);
    box(-3.82, 0.923, -0.77, 0.70, 0.026, 0.22, 0x7e8e89);
    box(-2.78, 0.92, -0.89, 0.30, 0.04, 0.38, 0xe5d9bd);
    box(-4.73, 1.20, -1.03, 0.055, 0.56, 0.055, 0x53554a);
    shape(new THREE.ConeGeometry(0.19, 0.18, 20), -4.73, 1.50, -1.03, amber);
    box(-3.74, 0.44, 0.19, 0.68, 0.15, 0.67, teal);
    box(-3.74, 0.86, 0.48, 0.68, 0.68, 0.13, tealLight);
    shape(new THREE.CylinderGeometry(0.055, 0.055, 0.38, 12), -3.74, 0.20, 0.19, 0x465451);
    box(-3.74, 0.04, 0.19, 0.72, 0.07, 0.10, 0x465451);
    box(-3.74, 0.04, 0.19, 0.10, 0.07, 0.72, 0x465451);
    for (const height of [0.70, 1.43, 2.16]) {
      box(4.50, height, -3.07, 1.38, 0.075, 0.61, oak);
      box(4.03, height + 0.18, -3.14, 0.19, 0.30, 0.24, teal);
      box(4.27, height + 0.22, -3.14, 0.16, 0.38, 0.24, 0xc09d66);
      box(4.46, height + 0.20, -3.14, 0.12, 0.34, 0.24, 0xe1d5b9);
    }
    box(3.83, 1.44, -3.18, 0.065, 2.82, 0.07, 0x66706a);
    box(5.17, 1.44, -3.18, 0.065, 2.82, 0.07, 0x66706a);
    plant(-4.70, 2.10, 0.34);
    plant(4.65, -0.43, 0.31);

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(meshPositions, 3));
    geometry.setAttribute('normal', new THREE.Float32BufferAttribute(meshNormals, 3));
    geometry.setAttribute('color', new THREE.Float32BufferAttribute(meshColors, 3));
    geometry.computeBoundingSphere();
    const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.86, metalness: 0 });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.name = 'Synthetic architecture · merged opaque surfaces';
    meshGroup.add(mesh);
    resources.push(geometry, material);
    metrics.triangles = meshPositions.length / 9;
    meshPositions.length = meshNormals.length = meshColors.length = 0;

    addFloor();
    // Thin emissive strips are illumination cues, not expensive shadow lights.
    const stripGeometry = new THREE.BoxGeometry(4.62, 0.024, 0.025);
    const stripMaterial = new THREE.MeshBasicMaterial({ color: 0xffd8a2 });
    const strip = new THREE.Mesh(stripGeometry, stripMaterial);
    strip.position.set(2.56, 3.25, -3.91);
    meshGroup.add(strip);
    resources.push(stripGeometry, stripMaterial);
    metrics.triangles += 12;
  }

  function plant(x, z, radius) {
    shape(new THREE.CylinderGeometry(radius, radius * 0.77, 0.54, 20), x, 0.28, z, 0xd5c9b3);
    shape(new THREE.CylinderGeometry(0.034, 0.048, 1.28, 10), x, 1.11, z, 0x767044);
    for (let i = 0; i < 11; i++) {
      const angle = i * 2.3999632297;
      const px = x + Math.cos(angle) * (0.20 + (i % 3) * 0.095);
      const pz = z + Math.sin(angle) * (0.20 + (i % 3) * 0.095);
      const leaf = new THREE.SphereGeometry(1, 9, 6);
      leaf.scale(0.28, 0.15, 0.12);
      leaf.rotateZ(0.45 + (i % 3) * 0.27);
      shape(leaf, px, 0.92 + i * 0.067, pz, i % 2 ? 0x53816a : 0x356c58, angle);
    }
  }

  function addFloor() {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 512;
    const paint = canvas.getContext('2d');
    const pixels = paint.createImageData(512, 512);
    let seed = 7819;
    for (let i = 0; i < pixels.data.length; i += 4) {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      const grain = (seed >>> 26) * 0.11;
      pixels.data[i] = 184 + grain;
      pixels.data[i + 1] = 182 + grain;
      pixels.data[i + 2] = 170 + grain;
      pixels.data[i + 3] = 255;
    }
    paint.putImageData(pixels, 0, 0);
    paint.strokeStyle = 'rgba(105,108,99,.20)';
    paint.lineWidth = 1;
    for (let i = 0; i < 512; i += 128) {
      paint.beginPath(); paint.moveTo(i, 0); paint.lineTo(i, 512); paint.stroke();
      paint.beginPath(); paint.moveTo(0, i); paint.lineTo(512, i); paint.stroke();
    }
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(2.85, 2.1);
    texture.anisotropy = 4;
    const geometry = new THREE.PlaneGeometry(11.4, 8.4);
    geometry.rotateX(-Math.PI / 2);
    const material = new THREE.MeshStandardMaterial({ map: texture, roughness: 0.96 });
    const floor = new THREE.Mesh(geometry, material);
    floor.position.y = 0.002;
    meshGroup.add(floor);
    resources.push(texture, geometry, material);
    metrics.triangles += 2;
    // Plane samples use the same concrete palette and tile seam positions.
    const a = [-5.7, 0.002, 4.2], b = [5.7, 0.002, 4.2];
    const c = [5.7, 0.002, -4.2], d = [-5.7, 0.002, -4.2], n = [0, 1, 0];
    const color = rgb(0xb8b6aa);
    triangle(a, b, c, n, n, n, color, true);
    triangle(a, c, d, n, n, n, color, true);
    meshPositions.length = meshNormals.length = meshColors.length = 0;
  }

  async function rebuild(count) {
    const started = performance.now();
    metrics.rebuilding = true;
    const grows = capacity < count;
    if (grows) {
      capacity = count;
      metrics.buffer_bytes = capacity * 15;
      positionAttribute = new THREE.BufferAttribute(new Float32Array(capacity * 3), 3).setUsage(THREE.DynamicDrawUsage);
      colorAttribute = new THREE.BufferAttribute(new Uint8Array(capacity * 3), 3, true).setUsage(THREE.DynamicDrawUsage);
      colorAttribute.onUpload(() => {
        metrics.upload_ms = performance.now() - uploadStarted;
        metrics.geometry_uploads += 1;
        metrics.uploaded_points = metrics.points;
        pendingUpload = false;
      });
    }
    const positions = positionAttribute.array, colors = colorAttribute.array;
    const pointSize = count >= 1_000_000 ? 0.014 : count >= 250_000 ? 0.023 : 0.039;
    pointMaterial.uniforms.pointSize.value = pointSize;
    let seed = 0x19ac0401;
    // Yield during large builds; the retained GPU scene remains visible.
    for (let start = 0; start < count; start += 131_072) {
      const end = Math.min(count, start + 131_072);
      for (let i = start; i < end; i++) {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        const target = seed / 4294967296 * totalArea;
        let lo = 0, hi = samples.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >>> 1;
          if (target < samples[mid].end) hi = mid; else lo = mid + 1;
        }
        const s = samples[lo];
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        const u = Math.sqrt(seed / 4294967296);
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        const v = seed / 4294967296;
        const wa = 1 - u, wb = u * (1 - v), wc = u * v;
        const nx = s.na[0] * wa + s.nb[0] * wb + s.nc[0] * wc;
        const ny = s.na[1] * wa + s.nb[1] * wb + s.nc[1] * wc;
        const nz = s.na[2] * wa + s.nb[2] * wb + s.nc[2] * wc;
        const x = s.a[0] * wa + s.b[0] * wb + s.c[0] * wc;
        const y = s.a[1] * wa + s.b[1] * wb + s.c[1] * wc;
        const z = s.a[2] * wa + s.b[2] * wb + s.c[2] * wc;
        const k = i * 3;
        positions[k] = x + nx * 0.003;
        positions[k + 1] = y + ny * 0.003;
        positions[k + 2] = z + nz * 0.003;
        // Static directional shading gives points mode readable surface form.
        const shade = 0.69 + 0.26 * Math.max(0, nx * 0.37 + ny * 0.81 + nz * 0.45)
          + 0.06 * ((seed >>> 24) / 255);
        let tile = 1;
        if (s.floor) {
          const tx = (x + 5.7) % 1, tz = (z + 4.2) % 1;
          tile = tx < 0.013 || tz < 0.013 ? 0.80 : 1;
        }
        colors[k] = (s.color[0] * shade * tile * 255 + 0.5) | 0;
        colors[k + 1] = (s.color[1] * shade * tile * 255 + 0.5) | 0;
        colors[k + 2] = (s.color[2] * shade * tile * 255 + 0.5) | 0;
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    if (grows) {
      pointGeometry.dispose();
      pointGeometry.setAttribute('position', positionAttribute);
      pointGeometry.setAttribute('color', colorAttribute);
    }
    positionAttribute.clearUpdateRanges();
    colorAttribute.clearUpdateRanges();
    positionAttribute.addUpdateRange(0, count * 3);
    colorAttribute.addUpdateRange(0, count * 3);
    positionAttribute.needsUpdate = true;
    colorAttribute.needsUpdate = true;
    pointGeometry.setDrawRange(0, count);
    metrics.points = count;
    metrics.generate_ms = performance.now() - started;
    metrics.rebuilding = false;
    pendingUpload = true;
    ctx.requestFrame();
    return metrics;
  }

  return layer;
}
