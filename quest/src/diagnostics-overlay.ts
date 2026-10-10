import * as THREE from "three";
import { FrameStats, budgetTone, diagnosticsLines } from "./diagnostics.js";

type RoverReadout = Parameters<typeof diagnosticsLines>[1];
const TONES = { ok: "#a6edc4", warn: "#ffe08a", bad: "#ffb3a8" };

/**
 * Off-by-default diagnostics: a desktop corner readout and a head-locked headset
 * panel. Toggle with the settings checkbox, the I key, or `?diag=1`. While off it
 * only records frame times; nothing is drawn or uploaded.
 */
export class DiagnosticsOverlay {
  readonly stats = new FrameStats();
  readonly panel: THREE.Mesh;
  private enabled = false;
  private drawnAt = -Infinity;
  private element = document.createElement("pre");
  private canvas = document.createElement("canvas");
  private texture = new THREE.CanvasTexture(this.canvas);
  private toggle: HTMLInputElement;

  constructor(private readout: () => RoverReadout) {
    this.element.className = "diagnostics"; this.element.hidden = true;
    this.element.setAttribute("aria-label", "Frame and link diagnostics");
    document.querySelector("main")!.append(this.element);
    this.canvas.width = 1024; this.canvas.height = 300;
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.panel = new THREE.Mesh(new THREE.PlaneGeometry(1.2, 0.35),
      new THREE.MeshBasicMaterial({ map: this.texture, transparent: true, toneMapped: false, depthTest: false }));
    this.panel.position.set(2.15, -0.95, -2.1); this.panel.rotation.y = -0.35; this.panel.renderOrder = 14; this.panel.visible = false;
    const option = document.createElement("label"); option.className = "check";
    option.innerHTML = '<input type="checkbox"/> Diagnostics overlay (I)';
    document.querySelector("#settings")!.append(option);
    this.toggle = option.querySelector("input")!;
    this.toggle.onchange = () => this.setEnabled(this.toggle.checked);
    window.addEventListener("keydown", this.key);
    if (new URLSearchParams(location.search).get("diag") === "1") this.setEnabled(true);
  }

  private key = (event: KeyboardEvent) => {
    if (event.code !== "KeyI" || event.repeat || event.metaKey || event.ctrlKey || event.altKey ||
        /INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement)?.tagName ?? "")) return;
    this.setEnabled(!this.enabled);
  };

  setEnabled(enabled: boolean) {
    this.enabled = enabled; this.toggle.checked = enabled;
    this.element.hidden = !enabled; this.panel.visible = false; this.drawnAt = -Infinity;
  }

  /** Call once per animation frame with its start time and JS work time. */
  frame(now: number, workMs: number, xr: boolean) {
    this.stats.record(now, workMs);
    if (!this.enabled || now - this.drawnAt < 500) return;
    this.drawnAt = now;
    const frame = this.stats.summary(), lines = diagnosticsLines(frame, this.readout()), tone = budgetTone(frame.frameP95);
    this.element.textContent = lines.join("\n"); this.element.dataset.tone = tone;
    this.panel.visible = xr;
    if (!xr) return;
    const c = this.canvas.getContext("2d")!;
    c.clearRect(0, 0, 1024, 300); c.fillStyle = "#111b25e6"; c.fillRect(0, 0, 1024, 300);
    c.font = "bold 28px ui-monospace, monospace"; c.fillStyle = TONES[tone]; c.fillText(lines[0], 24, 52);
    c.font = "24px ui-monospace, monospace"; c.fillStyle = "#dce5ee";
    lines.slice(1).forEach((line, index) => c.fillText(line, 24, 120 + index * 60));
    this.texture.needsUpdate = true;
  }

  dispose() {
    window.removeEventListener("keydown", this.key);
    this.texture.dispose(); this.element.remove();
  }
}
