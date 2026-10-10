import * as THREE from "three";
import { VoiceAssistant, actionLabel, pickMimeType } from "./assistant.js";

type Action = { type: "seek"; target_id: number } | { type: "scan" };

/**
 * "Ask the rover" UI: hold left X (VR), T (keyboard) or the on-screen button to talk.
 * A proposed action waits for left Y / Y key / Confirm; pressing talk again dismisses it.
 */
export class VoicePanel {
  readonly assistant: VoiceAssistant;
  private element = document.createElement("section");
  private canvas = document.createElement("canvas");
  private texture: THREE.CanvasTexture;
  private mesh: THREE.Mesh;
  private audio = new Audio();
  private keys = { talk: false, confirm: false, dismiss: false };
  private pointer = false;
  private changedAt = -Infinity;

  constructor(private renderer: THREE.WebGLRenderer, cockpit: THREE.Group, onConfirm: (action: Action) => string | void) {
    const mime = typeof MediaRecorder === "undefined" ? "" : pickMimeType((type: string) => MediaRecorder.isTypeSupported(type));
    this.assistant = new VoiceAssistant({
      fetch: window.fetch.bind(window),
      getMicrophone: () => navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }),
      createRecorder: (stream: MediaStream) => new MediaRecorder(stream, mime ? { mimeType: mime } : undefined),
      play: (url: string) => { this.audio.src = url; return this.audio.play(); },
      speak: (text: string) => {
        if (!("speechSynthesis" in window)) return;
        speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text));
      },
      onChange: () => this.render(),
      onConfirm: (action: Action) => {
        window.dispatchEvent(new CustomEvent("farsight-assistant-action", { detail: action }));
        return onConfirm(action);
      },
    });
    this.element.className = "ask-rover";
    this.element.innerHTML = `<p class="voice-status" role="status"></p><p class="voice-answer" aria-live="polite"></p>
      <div class="voice-buttons"><button type="button" class="voice-talk">Hold to ask (T)</button>
      <button type="button" class="voice-confirm" hidden>Confirm (Y)</button><button type="button" class="voice-dismiss" hidden>Dismiss (N)</button></div>`;
    document.body.append(this.element);
    const talk = this.element.querySelector<HTMLButtonElement>(".voice-talk")!;
    talk.onpointerdown = event => { talk.setPointerCapture(event.pointerId); this.pointer = true; this.poll(); };
    talk.onpointerup = talk.onpointercancel = talk.onlostpointercapture = () => { this.pointer = false; this.poll(); };
    this.element.querySelector<HTMLButtonElement>(".voice-confirm")!.onclick = () => this.assistant.confirm();
    this.element.querySelector<HTMLButtonElement>(".voice-dismiss")!.onclick = () => this.assistant.dismiss();
    window.addEventListener("keydown", this.keyDown);
    window.addEventListener("keyup", this.keyUp);
    window.addEventListener("blur", this.blur);

    this.canvas.width = 1024; this.canvas.height = 200;
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(2.8, 0.55),
      new THREE.MeshBasicMaterial({ map: this.texture, transparent: true, toneMapped: false, depthTest: false }));
    this.mesh.position.set(0, 1.55, -2.30); this.mesh.renderOrder = 12; this.mesh.visible = false;
    cockpit.add(this.mesh);
    this.render();
  }

  askText(text: string) { return this.assistant.askText(text); }

  private keyDown = (event: KeyboardEvent) => {
    if (event.repeat || event.metaKey || event.ctrlKey || event.altKey) return;
    if (/INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement)?.tagName ?? "")) return;
    const name = ({ KeyT: "talk", KeyY: "confirm", KeyN: "dismiss" } as const)[event.code as "KeyT" | "KeyY" | "KeyN"];
    if (name) { this.keys[name] = true; this.poll(); }
  };
  private keyUp = (event: KeyboardEvent) => {
    const name = ({ KeyT: "talk", KeyY: "confirm", KeyN: "dismiss" } as const)[event.code as "KeyT" | "KeyY" | "KeyN"];
    if (name) { this.keys[name] = false; this.poll(); }
  };
  private blur = () => { this.keys = { talk: false, confirm: false, dismiss: false }; this.pointer = false; this.poll(); };

  /** Call every frame: reads the left Touch controller (X = buttons[4], Y = buttons[5]). */
  update() { this.poll(); }

  private poll() {
    const session = this.renderer.xr.isPresenting ? this.renderer.xr.getSession() : null;
    const left = session ? [...session.inputSources].find(source => source.handedness === "left" && source.gamepad) : undefined;
    const buttons = left?.gamepad?.buttons ?? [];
    void this.assistant.input(this.keys.talk || this.pointer || !!buttons[4]?.pressed,
      this.keys.confirm || !!buttons[5]?.pressed, this.keys.dismiss);
  }

  private render() {
    const { state, answer, question, action } = this.assistant;
    const status = { idle: "Hold X / T to ask the rover", listening: "Listening… release to send",
      thinking: "Rover is thinking…", answer: question ? `“${question}”` : "Answer", error: "Assistant problem" }[state as string] ?? state;
    const prompt = action ? `${actionLabel(action)}? Y: confirm · X: dismiss` : "";
    this.changedAt = performance.now();
    this.element.dataset.state = state;
    this.element.querySelector(".voice-status")!.textContent = status;
    this.element.querySelector(".voice-answer")!.textContent = [answer, prompt].filter(Boolean).join(" ");
    this.element.querySelector<HTMLButtonElement>(".voice-confirm")!.hidden = !action;
    this.element.querySelector<HTMLButtonElement>(".voice-dismiss")!.hidden = !action;
    const c = this.canvas.getContext("2d")!;
    c.clearRect(0, 0, 1024, 200); c.fillStyle = "#111b25ee"; c.fillRect(0, 0, 1024, 200);
    c.fillStyle = state === "error" ? "#ffb3a8" : state === "listening" ? "#a6edc4" : "#dce5ee";
    c.font = "bold 30px system-ui"; c.fillText(status.slice(0, 60), 24, 44);
    c.font = "26px system-ui"; c.fillStyle = "#dce5ee";
    const words = answer.split(/\s+/); let line = "", y = 88;
    for (const word of words) {
      if (c.measureText(`${line} ${word}`).width > 970 && y < 150) { c.fillText(line, 24, y); line = word; y += 34; }
      else line = line ? `${line} ${word}` : word;
    }
    if (line) c.fillText(line, 24, y);
    if (prompt) { c.fillStyle = "#ffc779"; c.font = "bold 26px system-ui"; c.fillText(prompt, 24, 186); }
    this.texture.needsUpdate = true;
    this.mesh.visible = state !== "idle" || !!answer;
  }

  dispose() {
    window.removeEventListener("keydown", this.keyDown); window.removeEventListener("keyup", this.keyUp);
    window.removeEventListener("blur", this.blur);
    this.audio.pause(); this.element.remove(); this.texture.dispose();
  }
}
