/**
 * Measured-surface layer: replaces the fixed-size dense discs with
 * spacing-sized samples plus a density mesh, rebuilt in a worker at ~1-2 Hz.
 * See measured-surface.worker.mjs for the geometry rules.
 *
 * URL options: ?msurf=0 disables the layer (original dense discs);
 * ?mcell= patch size in metres; ?mmin= samples per meshed cell;
 * ?mcovered=1 also keeps samples under faces.
 */
import { DEFAULTS } from "./measured-surface.worker.mjs";

const MIN_INTERVAL_MS = 500;

function numberParam(params, name, fallback, low, high) {
  const value = Number(params.get(name));
  return params.has(name) && Number.isFinite(value) && value >= low && value <= high ? value : fallback;
}

export function measuredSurfaceOptions(params) {
  return {
    cell: numberParam(params, "mcell", DEFAULTS.cell, 0.01, 0.2),
    minPoints: numberParam(params, "mmin", DEFAULTS.minPoints, 3, 200),
    minNeighbors: numberParam(params, "mneighbors", DEFAULTS.minNeighbors, 1, 200),
    coveredPoints: params.get("mcovered") === "1",
  };
}

export function measuredSurfaceLayer({ THREE, state, denseGroup, options = {} }) {
  const view = { enabled: true, triangles: 0, points: 0, builds: 0, error: null, stats: null };
  let worker = null, busy = false, lastStart = -Infinity, interval = MIN_INTERVAL_MS;
  let seen = null, pending = null, nextId = 0, viewportHeight = 1080;

  // World-radius discs: size = 2r projected, clamped in pixels.
  const pointMaterial = new THREE.ShaderMaterial({
    uniforms: { viewportHeight: { value: viewportHeight }, minPx: { value: 1.5 }, maxPx: { value: 18 } },
    vertexShader: `
      attribute float radius;
      attribute vec3 color;
      uniform float viewportHeight, minPx, maxPx;
      varying vec3 vColor;
      void main() {
        vColor = color;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = clamp(2.0 * radius * projectionMatrix[1][1] * 0.5 * viewportHeight / max(-mv.z, 1e-3), minPx, maxPx);
      }`,
    fragmentShader: `
      varying vec3 vColor;
      void main() {
        vec2 d = gl_PointCoord - 0.5;
        if (dot(d, d) > 0.25) discard;
        gl_FragColor = vec4(vColor, 1.0);
        #include <colorspace_fragment>
      }`,
  });
  // Native camera colours, like the dense discs: no scene lighting or tone mapping.
  const meshMaterial = new THREE.MeshBasicMaterial({
    vertexColors: true, side: THREE.DoubleSide, toneMapped: false,
    polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4,
  });
  const pointGeometry = new THREE.BufferGeometry();
  const meshGeometry = new THREE.BufferGeometry();
  const huge = new THREE.Sphere(new THREE.Vector3(), 1e9);
  pointGeometry.boundingSphere = huge; meshGeometry.boundingSphere = huge;
  const points = new THREE.Points(pointGeometry, pointMaterial);
  const mesh = new THREE.Mesh(meshGeometry, meshMaterial);
  points.frustumCulled = mesh.frustumCulled = false;
  points.renderOrder = 2; mesh.renderOrder = 1;
  points.visible = mesh.visible = false;

  function snapshot() {
    const count = state.keys.length;
    const positions = new Float32Array(count * 3), colors = new Float32Array(count * 3);
    let offset = 0;
    for (const chunk of state.chunks) {
      if (!chunk || !chunk.count) continue;
      positions.set(chunk.positions.subarray(0, chunk.count * 3), offset);
      colors.set(chunk.colors.subarray(0, chunk.count * 3), offset);
      offset += chunk.count * 3;
    }
    return { positions, colors, count: offset / 3 };
  }

  function replace(geometry, name, array, itemSize) {
    geometry.setAttribute(name, new THREE.BufferAttribute(array, itemSize));
  }

  function apply(result) {
    replace(pointGeometry, "position", result.points.positions, 3);
    replace(pointGeometry, "color", result.points.colors, 3);
    replace(pointGeometry, "radius", result.points.radius, 1);
    pointGeometry.setDrawRange(0, result.points.count);
    replace(meshGeometry, "position", result.mesh.positions, 3);
    replace(meshGeometry, "color", result.mesh.colors, 3);
    replace(meshGeometry, "normal", result.mesh.normals, 3);
    meshGeometry.setIndex(new THREE.BufferAttribute(result.mesh.indices, 1));
    meshGeometry.setDrawRange(0, result.mesh.indices.length);
    view.triangles = result.mesh.triangles;
    view.points = result.points.count;
    view.stats = result.stats;
    view.builds++;
    const active = view.enabled && result.stats.input_points > 0;
    points.visible = mesh.visible = active;
    denseGroup.visible = !active;
  }

  function changeMark() {
    return `${state.resetVersion}:${state.receivedUpserts}:${state.receivedDeletes}:${state.localEvictions}`;
  }

  return {
    view,
    init(ctx) {
      ctx.root.add(mesh, points);
      viewportHeight = ctx.height;
      pointMaterial.uniforms.viewportHeight.value = viewportHeight;
      try {
        worker = new Worker(new URL("./measured-surface.worker.mjs", import.meta.url), { type: "module" });
        worker.onmessage = ({ data }) => {
          busy = false;
          if (data.error) { view.error = data.error; return; }
          view.error = null;
          // Rebuild cadence follows cost: at most ~1/3 of wall time in the worker.
          interval = Math.max(MIN_INTERVAL_MS, 3 * data.stats.build_ms);
          pending = data;
        };
        worker.onerror = (event) => { busy = false; view.error = String(event.message || "worker error"); };
      } catch (error) {
        view.error = String(error);
      }
    },
    poll(ctx, now) {
      let changed = false;
      if (pending) { apply(pending); pending = null; changed = true; }
      if (!state.size && (points.visible || mesh.visible)) {
        points.visible = mesh.visible = false; denseGroup.visible = true; view.triangles = view.points = 0; changed = true;
      }
      const mark = changeMark();
      if (worker && !busy && view.enabled && state.size && mark !== seen && now - lastStart >= interval) {
        seen = mark; lastStart = now; busy = true;
        const { positions, colors, count } = snapshot();
        worker.postMessage({ id: nextId++, positions, colors, count, options }, [positions.buffer, colors.buffer]);
      }
      return changed;
    },
    quality(ctx, lod) {
      pointMaterial.uniforms.viewportHeight.value = viewportHeight * (lod?.pixelRatio || 1);
    },
    setEnabled(value) {
      view.enabled = Boolean(value);
      const active = view.enabled && view.builds > 0 && state.size > 0;
      points.visible = mesh.visible = active;
      denseGroup.visible = !active;
      seen = null;
    },
    dispose() {
      worker?.terminate();
      mesh.parent?.remove(mesh); points.parent?.remove(points);
      pointGeometry.dispose(); meshGeometry.dispose(); pointMaterial.dispose(); meshMaterial.dispose();
      denseGroup.visible = true;
    },
  };
}
