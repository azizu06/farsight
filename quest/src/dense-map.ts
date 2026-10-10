import * as THREE from "three";
import { decodeDenseChunk, DENSE_CHUNK_POINTS, DENSE_VIEW_CHUNKS } from "./dense-protocol.js";
import { floorHeight } from "./map-walk.js";

type ChunkRef = { id: string; revision: number; lod: number };
type Slot = { points: THREE.Points; key: string; count: number; floor: number };

/** Bounded Quest view of a much larger Mac cloud. Call update from the existing render loop. */
export class DenseMapView {
  readonly group = new THREE.Group();
  readonly material = new THREE.PointsMaterial({ size: .012, vertexColors: true, toneMapped: false });
  private slots = new Map<string, Slot>();
  private epoch = "";
  private busy = false;
  private lastPull = -Infinity;
  private lastGood = 0;
  private ttlMs = 30000;
  private request: AbortController | null = null;
  private viewer = new THREE.Vector3();
  private viewport = new THREE.Vector4();
  private height = { value: 1 };
  private frames = 0;
  private sampleAt = 0;
  private lastMetrics = 0;
  private fps = 0;
  private stopped = false;
  private sourceKind = "rgbd";
  private resumeUpload: (() => void) | null = null;
  error: string | null = null;

  constructor(private root: THREE.Group, private renderer: THREE.WebGLRenderer,
              private onBounds?: (bounds: THREE.Box3, floor: number) => void) {
    this.group.name = "Measured dense cloud";
    root.add(this.group);
    this.material.onBeforeCompile = shader => {
      shader.uniforms.denseViewportHeight = this.height;
      shader.vertexShader = "uniform float denseViewportHeight;\n" + shader.vertexShader;
      shader.vertexShader = shader.vertexShader.replace("#include <logdepthbuf_vertex>", `
        gl_PointSize = clamp(0.012 * length(modelMatrix[0].xyz) * abs(projectionMatrix[1][1])
          * denseViewportHeight * 0.5 / max(0.01, -mvPosition.z), 1.25, 16.0);
        #include <logdepthbuf_vertex>`);
      shader.fragmentShader = shader.fragmentShader.replace("#include <clipping_planes_fragment>",
        "#include <clipping_planes_fragment>\nif (distance(gl_PointCoord, vec2(0.5)) > 0.5) discard;");
    };
  }

  get points() { return [...this.slots.values()].reduce((n, slot) => n + slot.count, 0); }
  get active() { return this.group.visible && this.points > 0; }

  update(now: number, camera: THREE.Camera, enabled: boolean) {
    if (this.stopped) return;
    this.resumeUpload?.();
    this.group.visible = enabled;
    this.frames++;
    if (now - this.sampleAt >= 1000) {
      this.fps = this.frames * 1000 / Math.max(1, now - this.sampleAt);
      this.frames = 0; this.sampleAt = now;
    }
    if (this.lastGood && now - this.lastGood > this.ttlMs) this.clear();
    if (now - this.lastMetrics > 2000) {
      this.lastMetrics = now;
      if (this.epoch) void fetch("/dense/metrics", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ points: this.active ? this.renderer.info.render.points : 0,
          uploaded_points: this.points, fps: this.fps, xr: this.renderer.xr.isPresenting }) }).catch(() => {});
    }
    if (!enabled) { this.request?.abort(); return; }
    if (!this.busy && now - this.lastPull > 750) {
      this.root.updateWorldMatrix(true, false);
      camera.getWorldPosition(this.viewer);
      this.root.worldToLocal(this.viewer);
      void this.pull(now);
    }
  }

  private async pull(now: number) {
    this.busy = true; this.lastPull = now;
    const abort = new AbortController(); this.request = abort;
    const timeout = window.setTimeout(() => abort.abort(), 5000);
    try {
      const query = new URLSearchParams({ x: String(this.viewer.x), y: String(this.viewer.y), z: String(this.viewer.z) });
      const response = await fetch(`/dense/manifest?${query}`, { signal: abort.signal, cache: "no-store" });
      if (!response.ok) throw new Error(`Dense manifest ${response.status}`);
      const manifest = await response.json();
      if (typeof manifest.epoch !== "string" || !Array.isArray(manifest.chunks)
          || manifest.chunks.length > DENSE_VIEW_CHUNKS) throw new Error("Invalid dense manifest");
      if (manifest.epoch !== this.epoch) { this.clear(); this.epoch = manifest.epoch; }
      this.sourceKind = manifest.source_kind ?? "rgbd";
      this.ttlMs = Math.min(30000, Math.max(1000, Number(manifest.ttl_s ?? 30) * 1000));
      const wanted = new Set<string>(manifest.chunks.map((chunk: ChunkRef) => chunk.id));
      for (const [id, slot] of this.slots) if (!wanted.has(id)) this.remove(id, slot);
      for (const chunk of manifest.chunks as ChunkRef[]) {
        if (!/^-?\d+_-?\d+_-?\d+$/.test(chunk.id) || !Number.isSafeInteger(chunk.revision)
            || !Number.isInteger(chunk.lod) || chunk.lod < 0 || chunk.lod > 4) throw new Error("Invalid dense chunk reference");
        const key = `${chunk.revision}:${chunk.lod}`;
        if (this.slots.get(chunk.id)?.key === key) continue;
        const params = new URLSearchParams({ id: chunk.id, lod: String(chunk.lod), source: this.sourceKind });
        const reply = await fetch(`/dense/chunk.bin?${params}`, { signal: abort.signal, cache: "no-store" });
        if (!reply.ok) throw new Error(`Dense chunk ${reply.status}`);
        const data = decodeDenseChunk(await reply.arrayBuffer());
        if (abort.signal.aborted) return;
        if (data.metadata.epoch !== this.epoch || data.metadata.revision < chunk.revision
            || data.metadata.chunk?.join("_") !== chunk.id || data.metadata.lod !== chunk.lod) continue;
        let slot = this.slots.get(chunk.id);
        if (!slot) {
          const geometry = new THREE.BufferGeometry();
          for (const name of ["position", "color"]) geometry.setAttribute(name,
            new THREE.BufferAttribute(new Float32Array(DENSE_CHUNK_POINTS * 3), 3).setUsage(THREE.DynamicDrawUsage));
          const points = new THREE.Points(geometry, this.material);
          points.onBeforeRender = renderer => { this.height.value = renderer.getCurrentViewport(this.viewport).w; };
          this.group.add(points);
          slot = { points, key: "", count: 0, floor: NaN }; this.slots.set(chunk.id, slot);
        }
        for (const [name, values] of [["position", data.positions], ["color", data.colors]] as const) {
          const attribute = slot.points.geometry.getAttribute(name) as THREE.BufferAttribute;
          (attribute.array as Float32Array).set(values);
          attribute.clearUpdateRanges(); attribute.addUpdateRange(0, values.length); attribute.needsUpdate = true;
        }
        const geometry = slot.points.geometry;
        geometry.setDrawRange(0, data.count);
        // Bounds use only populated samples, never the unused zero-filled capacity.
        geometry.boundingBox = new THREE.Box3().setFromArray(data.positions);
        geometry.boundingSphere = geometry.boundingBox.getBoundingSphere(new THREE.Sphere());
        slot.key = `${data.metadata.revision}:${chunk.lod}`; slot.count = data.count;
        this.lastGood = performance.now();
        slot.floor = floorHeight(data.positions);
        const bounds = new THREE.Box3();
        for (const other of this.slots.values()) if (other.points.geometry.boundingBox)
          bounds.union(other.points.geometry.boundingBox);
        const floors = [...this.slots.values()].map(other => other.floor).filter(Number.isFinite);
        this.onBounds?.(bounds, floors.length ? Math.min(...floors) : NaN);
        // Yield between bounded uploads so the render loop can run.
        // Window rAF can pause in immersive XR, so resume from update() instead.
        await new Promise<void>(resolve => {
          const resume = () => { this.resumeUpload = null; abort.signal.removeEventListener("abort", resume); resolve(); };
          this.resumeUpload = resume;
          abort.signal.addEventListener("abort", resume, { once: true });
          if (abort.signal.aborted) resume();
        });
      }
      this.lastGood = performance.now(); this.error = null;
    } catch (error) {
      if (!abort.signal.aborted) { this.error = String(error); this.lastPull = performance.now() + 4250; }
    } finally {
      window.clearTimeout(timeout); this.busy = false;
      if (this.request === abort) this.request = null;
    }
  }

  private remove(id: string, slot: Slot) {
    this.group.remove(slot.points); slot.points.geometry.dispose(); this.slots.delete(id);
  }
  clear() { for (const [id, slot] of this.slots) this.remove(id, slot); }
  dispose() { this.stopped = true; this.request?.abort(); this.clear(); this.root.remove(this.group); this.material.dispose(); }
}
