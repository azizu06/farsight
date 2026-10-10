import * as THREE from "three";

export type VRAction = "explore" | "scan" | "drill" | "close";
type UV = { x: number; y: number };

const WIDTH = 768;
const HEIGHT = 900;
const LEFT = 44;
const RIGHT = WIDTH - LEFT;
const ROW_HEIGHT = 128;
const ROWS: { action: VRAction; label: string; top: number }[] = [
  { action: "explore", label: "Explore", top: 152 },
  { action: "scan", label: "Scan", top: 308 },
  { action: "drill", label: "Rescue drill", top: 464 },
  { action: "close", label: "Close", top: 620 },
];

/** three.js UV starts at bottom-left; only the four visible cards are targets. */
export function vrActionHit(uv: UV | undefined): VRAction | null {
  if (!uv || !Number.isFinite(uv.x) || !Number.isFinite(uv.y)
      || uv.x < 0 || uv.x > 1 || uv.y < 0 || uv.y > 1) return null;
  const x = uv.x * WIDTH;
  const y = (1 - uv.y) * HEIGHT;
  if (x < LEFT || x > RIGHT) return null;
  return ROWS.find(row => y >= row.top && y <= row.top + ROW_HEIGHT)?.action ?? null;
}

/** Informational, pointable VR menu. The owner handles input and all actions. */
export class VRActionMenu {
  readonly panel: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  private readonly canvas = document.createElement("canvas");
  private readonly context: CanvasRenderingContext2D;
  private readonly texture: THREE.CanvasTexture;
  private selected: VRAction | null = null;

  constructor() {
    this.canvas.width = WIDTH;
    this.canvas.height = HEIGHT;
    const context = this.canvas.getContext("2d");
    if (!context) throw new Error("VR action menu requires a canvas context");
    this.context = context;
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.panel = new THREE.Mesh(new THREE.PlaneGeometry(1.15, 1.35), new THREE.MeshBasicMaterial({
      map: this.texture, toneMapped: false, depthTest: false, depthWrite: false,
    }));
    this.panel.name = "vr-action-menu";
    this.panel.position.set(0, 0, -2.2);
    this.panel.renderOrder = 20;
    this.panel.visible = false;
    this.draw();
  }

  get visible() { return this.panel.visible; }
  set visible(value: boolean) {
    if (value === this.panel.visible) return;
    this.panel.visible = value;
    if (value) this.draw();
    else this.selected = null;
  }

  hit(uv: UV | undefined): VRAction | null {
    return this.visible ? vrActionHit(uv) : null;
  }

  hover(action: VRAction | null) {
    const next = ROWS.some(row => row.action === action) ? action : null;
    if (next === this.selected) return;
    this.selected = next;
    if (this.visible) this.draw();
  }

  private draw() {
    const c = this.context;
    c.clearRect(0, 0, WIDTH, HEIGHT);
    c.fillStyle = "#07151e";
    c.beginPath(); c.roundRect(0, 0, WIDTH, HEIGHT, 30); c.fill();
    c.textAlign = "center";
    c.textBaseline = "middle";
    c.fillStyle = "#f3f9fc";
    c.font = "bold 48px system-ui";
    c.fillText("Rover actions", WIDTH / 2, 80);
    for (const row of ROWS) {
      const hovered = this.selected === row.action;
      c.fillStyle = hovered ? "#123b46" : "#152936";
      c.strokeStyle = hovered ? "#70f2ff" : "#4a6575";
      c.lineWidth = hovered ? 5 : 2;
      c.beginPath(); c.roundRect(LEFT, row.top, RIGHT - LEFT, ROW_HEIGHT, 20); c.fill(); c.stroke();
      c.fillStyle = hovered ? "#bdfaff" : "#ffffff";
      c.font = "bold 46px system-ui";
      c.fillText(row.label, WIDTH / 2, row.top + ROW_HEIGHT / 2);
    }
    c.fillStyle = "#d5e6ee";
    c.font = "40px system-ui";
    c.fillText("Point + right grip to select", WIDTH / 2, 808);
    c.fillText("Y closes", WIDTH / 2, 861);
    this.texture.needsUpdate = true;
  }

  dispose() {
    this.panel.geometry.dispose();
    this.panel.material.dispose();
    this.texture.dispose();
  }
}
