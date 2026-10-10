import * as THREE from "three";

export function curvedPanelGeometry(
  radius = 2.5,
  arc = 1.65,
  aspect = 4 / 3,
): THREE.BufferGeometry {
  const segments = 48;
  const height = (radius * arc) / aspect;
  const positions: number[] = [],
    uv: number[] = [],
    indices: number[] = [];
  for (let i = 0; i <= segments; i++) {
    const angle = (i / segments - 0.5) * arc;
    for (const vertical of [0, 1]) {
      positions.push(
        Math.sin(angle) * radius,
        (vertical - 0.5) * height,
        -Math.cos(angle) * radius,
      );
      uv.push(i / segments, vertical);
    }
    if (i < segments) {
      const a = i * 2;
      indices.push(a, a + 2, a + 1, a + 1, a + 2, a + 3);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(positions, 3),
  );
  geometry.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}

export interface SceneCallbacks {
  onFrame: (now: number, session: XRSession | null, yaw: number) => void;
  onAction: (action: "drill" | "stop") => void;
  onSessionEnd: () => void;
}

export class RoverScene {
  readonly renderer: THREE.WebGLRenderer;
  readonly imageCanvas = document.createElement("canvas");
  private ctx: CanvasRenderingContext2D;
  private camera = new THREE.PerspectiveCamera(65, 1, 0.05, 30);
  private scene = new THREE.Scene();
  private content = new THREE.Group();
  private texture: THREE.CanvasTexture;
  private material: THREE.MeshBasicMaterial;
  private panel: THREE.Mesh;
  private bitmap: ImageBitmap | null = null;
  private menu: THREE.Mesh[] = [];
  private menuTextures: THREE.CanvasTexture[] = [];
  private raycaster = new THREE.Raycaster();
  private quaternion = new THREE.Quaternion();
  private euler = new THREE.Euler(0, 0, 0, "YXZ");
  private needRecenter = false;
  private disposed = false;
  private resizeObserver: ResizeObserver;
  private ended = () => {
    this.camera.position.set(0, 1.6, 0);
    this.camera.quaternion.identity();
    this.camera.fov = 65;
    this.camera.zoom = 1;
    this.camera.near = 0.05;
    this.camera.far = 30;
    this.content.position.set(0, 1.6, 0);
    this.content.rotation.set(0, 0, 0);
    this.needRecenter = false;
    this.resize();
    this.callbacks.onSessionEnd();
  };

  constructor(
    private canvas: HTMLCanvasElement,
    private callbacks: SceneCallbacks,
  ) {
    const context = this.imageCanvas.getContext("2d");
    if (!context) throw new Error("Camera canvas is unavailable.");
    this.ctx = context;
    this.imageCanvas.width = 640;
    this.imageCanvas.height = 480;
    this.renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: false,
    });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.xr.enabled = true;
    this.renderer.xr.addEventListener("sessionend", this.ended);
    this.renderer.xr.setReferenceSpaceType("local-floor");
    this.scene.background = new THREE.Color("#071317");
    this.camera.position.set(0, 1.6, 0);
    this.content.position.y = 1.6;
    this.scene.add(this.content);
    this.texture = this.createVideoTexture();
    this.material = new THREE.MeshBasicMaterial({
      map: this.texture,
      toneMapped: false,
    });
    this.panel = new THREE.Mesh(curvedPanelGeometry(), this.material);
    this.content.add(this.panel);
    this.makeMenu("Start 90 s drill", "STOP / end drill");
    for (let i = 0; i < 2; i++) {
      const controller = this.renderer.xr.getController(i);
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(),
          new THREE.Vector3(0, 0, -5),
        ]),
        new THREE.LineBasicMaterial({ color: "#62f5bd" }),
      );
      controller.add(line);
      this.scene.add(controller);
      controller.addEventListener("select", (event) => {
        const input = event.data as XRInputSource;
        // Right trigger is reserved for deliberate drive, never a menu click.
        if (input?.handedness !== "left") return;
        controller.updateWorldMatrix(true, false);
        const origin = new THREE.Vector3().setFromMatrixPosition(
          controller.matrixWorld,
        );
        const direction = new THREE.Vector3(0, 0, -1).transformDirection(
          controller.matrixWorld,
        );
        this.raycaster.set(origin, direction);
        const hit = this.raycaster.intersectObjects(this.menu)[0];
        if (hit) this.callbacks.onAction(hit.object.userData.action);
      });
    }
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas);
    this.resize();
    this.clearFrame("Waiting for rover camera");
    this.renderer.setAnimationLoop((time, frame) => {
      const session = this.renderer.xr.getSession();
      const camera = session ? this.renderer.xr.getCamera() : this.camera;
      const reference = this.renderer.xr.getReferenceSpace();
      const viewerPose =
        session && frame && reference ? frame.getViewerPose(reference) : null;
      if (this.needRecenter && viewerPose) {
        this.placeContent(camera);
        this.needRecenter = false;
      }
      camera.getWorldQuaternion(this.quaternion);
      this.euler.setFromQuaternion(this.quaternion, "YXZ");
      const right =
        session &&
        Array.from(session.inputSources).find(
          (input) => input.handedness === "right",
        );
      const tracked =
        !session ||
        Boolean(
          viewerPose &&
          frame &&
          reference &&
          right?.gripSpace &&
          frame.getPose(right.gripSpace, reference),
        );
      this.callbacks.onFrame(time, session, tracked ? this.euler.y : NaN);
      this.renderer.render(this.scene, this.camera);
    });
  }

  private makeMenu(first: string, second: string) {
    for (const [index, text] of [first, second].entries()) {
      const canvas = document.createElement("canvas");
      canvas.width = 512;
      canvas.height = 96;
      const ctx = canvas.getContext("2d")!;
      ctx.fillStyle = index ? "#561d2a" : "#173d35";
      ctx.fillRect(0, 0, 512, 96);
      ctx.strokeStyle = index ? "#ff667c" : "#62f5bd";
      ctx.lineWidth = 5;
      ctx.strokeRect(3, 3, 506, 90);
      ctx.fillStyle = "#f1fff8";
      ctx.font = "bold 27px system-ui";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, 256, 48);
      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      const mesh = new THREE.Mesh(
        new THREE.PlaneGeometry(1.7, 0.32),
        new THREE.MeshBasicMaterial({ map: texture, toneMapped: false }),
      );
      mesh.position.set(index ? 0.92 : -0.92, -1.78, -2.3);
      mesh.userData.action = index ? "stop" : "drill";
      this.content.add(mesh);
      this.menu.push(mesh);
      this.menuTextures.push(texture);
    }
  }

  setFrame(bitmap: ImageBitmap) {
    this.bitmap?.close();
    this.bitmap = bitmap;
    const scale = Math.min(
      1280 / bitmap.width,
      1280 / bitmap.height,
      Math.max(1, 640 / bitmap.width, 480 / bitmap.height),
    );
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    if (
      this.imageCanvas.width !== width ||
      this.imageCanvas.height !== height
    ) {
      this.imageCanvas.width = width;
      this.imageCanvas.height = height;
      this.panel.scale.y = 4 / 3 / (bitmap.width / bitmap.height);
      // WebGL uses immutable texture storage: a resized canvas needs a fresh
      // texture allocation, otherwise only a corner updates or uploads fail.
      this.texture.dispose();
      this.texture = this.createVideoTexture();
      this.material.map = this.texture;
      this.material.needsUpdate = true;
      this.resize();
    }
  }

  private createVideoTexture() {
    const texture = new THREE.CanvasTexture(this.imageCanvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.minFilter = THREE.LinearFilter;
    texture.generateMipmaps = false;
    return texture;
  }

  draw(
    overlay: (
      ctx: CanvasRenderingContext2D,
      width: number,
      height: number,
    ) => void,
  ) {
    const { width, height } = this.imageCanvas;
    if (this.bitmap) this.ctx.drawImage(this.bitmap, 0, 0, width, height);
    else {
      this.ctx.fillStyle = "#101e24";
      this.ctx.fillRect(0, 0, width, height);
    }
    overlay(this.ctx, width, height);
    this.texture.needsUpdate = true;
  }

  clearFrame(message = "Camera unavailable") {
    this.bitmap?.close();
    this.bitmap = null;
    this.ctx.fillStyle = "#101e24";
    this.ctx.fillRect(0, 0, this.imageCanvas.width, this.imageCanvas.height);
    this.ctx.fillStyle = "#c8d5df";
    this.ctx.font = "24px system-ui";
    this.ctx.textAlign = "center";
    this.ctx.fillText(
      message,
      this.imageCanvas.width / 2,
      this.imageCanvas.height / 2,
    );
    this.texture.needsUpdate = true;
  }

  private placeContent(camera: THREE.Camera) {
    this.content.position.copy(camera.getWorldPosition(new THREE.Vector3()));
    camera.getWorldQuaternion(this.quaternion);
    this.euler.setFromQuaternion(this.quaternion, "YXZ");
    this.content.rotation.set(0, this.euler.y, 0);
  }
  recenter() {
    if (this.renderer.xr.isPresenting) this.needRecenter = true;
    else {
      this.content.position.set(0, 1.6, 0);
      this.content.rotation.set(0, 0, 0);
      this.camera.quaternion.identity();
      this.resize();
    }
  }
  async enterVR() {
    if (this.disposed) throw new Error("Viewer has closed.");
    if (!navigator.xr || !window.isSecureContext)
      throw new Error(
        "Open this page over trusted HTTPS in the Quest browser to enter VR.",
      );
    const session = await navigator.xr.requestSession("immersive-vr", {
      optionalFeatures: ["local-floor"],
    });
    if (this.disposed) {
      await session.end().catch(() => undefined);
      throw new Error("Viewer closed while VR was starting.");
    }
    this.camera.position.set(0, 1.6, 0);
    try {
      await this.renderer.xr.setSession(session);
      if (this.disposed)
        throw new Error("Viewer closed while VR was starting.");
      this.needRecenter = true;
    } catch (error) {
      await session.end().catch(() => undefined);
      throw error;
    }
  }
  async exitVR() {
    await this.renderer.xr.getSession()?.end();
  }
  private resize() {
    if (this.renderer.xr.isPresenting) return;
    const width = Math.max(1, this.canvas.clientWidth),
      height = Math.max(1, this.canvas.clientHeight);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    // Fit the full curved image and menu in a tall desktop preview without
    // changing the fixed world-space viewing distance in an XR session.
    this.panel.geometry.computeBoundingBox();
    const bounds = this.panel.geometry.boundingBox!;
    const tangent = Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2));
    const halfWidth = Math.max(
      Math.abs(bounds.min.x),
      Math.abs(bounds.max.x),
      1.77,
    );
    const halfHeight = Math.max(
      Math.abs(bounds.min.y) * this.panel.scale.y,
      Math.abs(bounds.max.y) * this.panel.scale.y,
      1.94,
    );
    const nearestDepth = Math.max(0.1, -bounds.max.z);
    const distance =
      Math.max(
        0,
        halfWidth / (tangent * this.camera.aspect) - nearestDepth,
        halfHeight / tangent - nearestDepth,
      ) + 0.35;
    this.camera.position.set(0, 1.6, distance);
    this.camera.updateProjectionMatrix();
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.resizeObserver.disconnect();
    this.renderer.setAnimationLoop(null);
    this.renderer.xr.removeEventListener("sessionend", this.ended);
    const session = this.renderer.xr.getSession();
    void session?.end().catch(() => undefined);
    this.bitmap?.close();
    this.bitmap = null;
    this.scene.traverse((object) => {
      if (object instanceof THREE.Mesh || object instanceof THREE.Line) {
        object.geometry.dispose();
        for (const material of Array.isArray(object.material)
          ? object.material
          : [object.material])
          material.dispose();
      }
    });
    this.texture.dispose();
    this.menuTextures.forEach((texture) => texture.dispose());
    this.renderer.dispose();
  }
}
