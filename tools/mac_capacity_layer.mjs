/** SYNTHETIC capacity load. Never mounted by the live demo. */
export async function install(count) {
  const { DenseCloudState, DenseGpuChunks } = await import('/render/dense-cloud.mjs');
  const { StereoPass } = await import('/render/stereo-pass.mjs');
  let context;
  window.__macRegisterLayer({ init(ctx) { context = ctx; } });
  const { THREE, root, camera } = context;
  const gl = context.renderer.getContext(), debug = gl.getExtension('WEBGL_debug_renderer_info');
  const rendererName = debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  const state = new DenseCloudState({ capacity: count });
  const started = performance.now();
  for (let offset = 0; offset < count; offset += 8192) {
    const size = Math.min(8192, count - offset), rows = new Int32Array(size * 7);
    for (let i = 0; i < size; i++) {
      const n = offset + i, at = i * 7;
      // Distinct centimetre cells in a ten-metre wall/volume, all in view.
      rows.set([(n % 1000) * 10, Math.floor(n / 1000) % 1000 * 10,
        Math.floor(n / 1_000_000) * 20, 100 + n % 156, 160, 210, 1], at);
    }
    state.apply(rows);
    if (offset % 131072 === 0) await new Promise(resolve => setTimeout(resolve, 0));
  }
  const textureCanvas = document.createElement('canvas');
  textureCanvas.width = textureCanvas.height = 256;
  const paint = textureCanvas.getContext('2d');
  for (let y = 0; y < 16; y++) for (let x = 0; x < 16; x++) {
    paint.fillStyle = (x + y) % 2 ? '#76899a' : '#a18d72';
    paint.fillRect(x * 16, y * 16, 16, 16);
  }
  const texture = new THREE.CanvasTexture(textureCanvas);
  const meshGeometry = new THREE.PlaneGeometry(10, 10, 255, 191);
  const mesh = new THREE.Mesh(meshGeometry, new THREE.MeshBasicMaterial({ map: texture, side: THREE.DoubleSide }));
  mesh.position.set(5, 5, -.02); root.add(mesh);
  const material = root.children.find(child => child.isPoints).material.clone();
  material.size = .012;
  const gpu = new DenseGpuChunks({ state, THREE, material, parent: root });
  camera.position.set(5, 5, 14); camera.lookAt(5, 5, 0);
  const fillMs = performance.now() - started;
  window.__macRegisterLayer({
    pre() { gpu.flush(); gpu.beginFrame(); },
    post() {
      gpu.endFrame();
      window.__mac.benchmark = { synthetic: true, cells: state.size, requested: count,
        fill_ms: fillMs, textured_triangles: meshGeometry.index.count / 3,
        renderer: rendererName,
        ...gpu.metrics() };
    },
    dispose() { gpu.dispose(); meshGeometry.dispose(); texture.dispose(); material.dispose(); },
  });
  context.requestFrame();
  window.__macBenchRaster = (frames = 60) => {
    const pass = new StereoPass({ THREE, renderer: context.renderer });
    const eyes = window.__mac.poseEcho.eyes, samples = [];
    for (let i = 0; i < frames + 10; i++) {
      const start = performance.now();
      pass.render(context.scene, eyes, { width: 1920, height: 1080 });
      gl.finish();
      if (i >= 10) samples.push(performance.now() - start);
    }
    return samples;
  };
  return { cells: state.size, fill_ms: fillMs };
}
