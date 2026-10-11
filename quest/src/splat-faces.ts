import * as THREE from "three";
import { SplatNavigation } from "./splat-navigation";

type FaceSpot = { id: number; pos: number[]; view: number[]; seen: number;
  frame: string; frame_img: string; thumb: string };

/** Offline face sidecars from tools/postprocess/faces/face_pins.py. */
export class SplatFaces {
  readonly stats = { count: 0, selected: null as number | null, panel: false, error: null as string | null };
  private faces: FaceSpot[] = [];
  private markers = new THREE.Group();
  private dots: THREE.Mesh[] = [];
  private raycaster = new THREE.Raycaster();
  private rayMatrix = new THREE.Matrix4();
  private laser = new THREE.Line(new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(), new THREE.Vector3(0, 0, -1),
  ]), new THREE.LineBasicMaterial({ color: 0xff4d6d, depthTest: false }));
  private canvas = document.createElement("canvas");
  private texture: THREE.CanvasTexture;
  private panel: THREE.Mesh;
  private list = document.getElementById("splat-faces")!;
  private shot = document.getElementById("splat-shot")!;
  private shotImage = document.getElementById("splat-shot-image") as HTMLImageElement;
  private caption = document.getElementById("splat-shot-caption")!;
  private manifest = new URL(location.href);
  private index = -1;
  private flipped = false;
  private triggerHeld = false;
  private nextHeld = false;
  private imageVersion = 0;

  constructor(private renderer: THREE.WebGLRenderer, private rig: THREE.Group,
    camera: THREE.PerspectiveCamera, scene: THREE.Scene, private navigation: SplatNavigation) {
    this.canvas.width = 1024; this.canvas.height = 768;
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.panel = new THREE.Mesh(new THREE.PlaneGeometry(1.2, .9),
      new THREE.MeshBasicMaterial({ map: this.texture, depthTest: false, depthWrite: false }));
    this.panel.position.set(0, 0, -1.35); this.panel.renderOrder = 1001; this.panel.visible = false;
    camera.add(this.panel);
    this.laser.visible = false; this.laser.renderOrder = 1000; this.laser.matrixAutoUpdate = false;
    scene.add(this.markers, this.laser);
    document.getElementById("splat-shot-close")!.onclick = () => this.close();
    addEventListener("keydown", event => { if (event.key === "Escape") this.close(); });
    renderer.xr.addEventListener("sessionstart", () => this.close());
    renderer.xr.addEventListener("sessionend", () => { this.close(); this.triggerHeld = this.nextHeld = false; });
  }

  async load(url: string) {
    this.manifest = new URL(url, location.href);
    try {
      const response = await fetch(this.manifest);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.faces = (await response.json()).faces ?? [];
      this.stats.count = this.faces.length;
      this.list.hidden = !this.faces.length;
      const title = document.createElement("strong");
      title.textContent = `Faces seen · ${this.faces.length} spots`;
      this.list.replaceChildren(title);
      for (const [index, face] of this.faces.entries()) {
        const dot = new THREE.Mesh(new THREE.SphereGeometry(.075, 12, 8),
          new THREE.MeshBasicMaterial({ color: 0xff4d6d, depthTest: false, depthWrite: false }));
        dot.position.fromArray(face.pos); dot.renderOrder = 999; dot.userData.faceIndex = index;
        this.markers.add(dot); this.dots.push(dot);
        const button = document.createElement("button"); button.type = "button"; button.dataset.id = String(face.id);
        const image = document.createElement("img"); image.src = new URL(face.thumb, this.manifest).href; image.alt = `Face ${face.id}`;
        const text = document.createElement("span"); text.textContent = `Face ${face.id}`;
        const detail = document.createElement("small"); detail.textContent = `seen in ${face.seen} frames`;
        text.appendChild(detail); button.append(image, text); button.onclick = () => this.select(index);
        this.list.appendChild(button);
      }
    } catch (error) {
      this.stats.error = String(error); this.list.hidden = false;
      this.list.textContent = `Face spots unavailable: ${this.stats.error}`;
    }
  }

  setFlipped(flipped: boolean) { this.flipped = flipped; this.markers.rotation.x = flipped ? Math.PI : 0; }

  select(index: number) {
    const face = this.faces[index];
    if (!face) return;
    this.index = index; this.stats.selected = face.id;
    const eye = face.view.slice(0, 3), target = face.pos.slice();
    if (this.flipped) { eye[1] *= -1; eye[2] *= -1; target[1] *= -1; target[2] *= -1; }
    this.navigation.teleport({ eye, yaw: Math.atan2(-(target[0] - eye[0]), -(target[2] - eye[2])) });
    this.dots.forEach((dot, i) => {
      (dot.material as THREE.MeshBasicMaterial).color.setHex(i === index ? 0xffd23f : 0xff4d6d);
      dot.scale.setScalar(i === index ? 1.35 : 1);
    });
    this.list.querySelectorAll("button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.id === String(face.id))));
    this.caption.textContent = `Face ${face.id} · camera photo ${face.frame} · seen in ${face.seen} frames`;
    this.stats.panel = true;
    this.shot.hidden = this.renderer.xr.isPresenting;
    this.panel.visible = this.renderer.xr.isPresenting;
    this.drawPanel();
    const version = ++this.imageVersion;
    const image = new Image();
    image.onload = () => { if (version === this.imageVersion) this.drawPanel(image); };
    image.src = new URL(face.frame_img, this.manifest).href;
    this.shotImage.src = image.src;
  }

  close() {
    this.stats.panel = false; this.panel.visible = false; this.shot.hidden = true;
  }

  private drawPanel(image?: HTMLImageElement) {
    const ctx = this.canvas.getContext("2d")!;
    ctx.fillStyle = "#080f16"; ctx.fillRect(0, 0, 1024, 768);
    if (image) {
      const scale = Math.min(1024 / image.width, 650 / image.height);
      ctx.drawImage(image, (1024 - image.width * scale) / 2, (650 - image.height * scale) / 2, image.width * scale, image.height * scale);
    }
    ctx.fillStyle = "#d7e2ea"; ctx.font = "25px system-ui";
    ctx.fillText(this.caption.textContent ?? "", 24, 694, 976);
    ctx.fillText("B / Y or trigger: close · B / Y again: next face", 24, 740, 976);
    this.texture.needsUpdate = true;
  }

  update(frame?: XRFrame) {
    const session = this.renderer.xr.getSession();
    this.laser.visible = false;
    if (!session || !frame) return;
    const right = [...session.inputSources].find(source => source.handedness === "right");
    const left = [...session.inputSources].find(source => source.handedness === "left");
    const trigger = !!right?.gamepad?.buttons[0]?.pressed;
    const next = !!right?.gamepad?.buttons[5]?.pressed || !!left?.gamepad?.buttons[5]?.pressed;
    let hit: THREE.Intersection | undefined;
    const reference = this.renderer.xr.getReferenceSpace();
    const pose = right && reference && frame.getPose(right.targetRaySpace, reference);
    if (pose && !this.stats.panel) {
      this.rig.updateMatrixWorld(true); this.markers.updateMatrixWorld(true);
      this.rayMatrix.fromArray(pose.transform.matrix).premultiply(this.rig.matrixWorld);
      this.raycaster.ray.origin.setFromMatrixPosition(this.rayMatrix);
      this.raycaster.ray.direction.set(0, 0, -1).transformDirection(this.rayMatrix);
      hit = this.raycaster.intersectObjects(this.dots, false)[0];
      this.laser.matrix.copy(this.rayMatrix).scale(new THREE.Vector3(1, 1, hit?.distance ?? 3));
      this.laser.visible = this.faces.length > 0;
    }
    if (trigger && !this.triggerHeld) {
      if (this.stats.panel) this.close();
      else if (hit) this.select(hit.object.userData.faceIndex);
    } else if (next && !this.nextHeld && this.faces.length) {
      if (this.stats.panel) this.close();
      else this.select((this.index + 1) % this.faces.length);
    }
    this.triggerHeld = trigger; this.nextHeld = next;
  }
}
