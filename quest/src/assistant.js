/** Hold-to-talk client for POST /ask. Proposals never move the rover: they wait for a confirmation. */
export const TALK_MIN_MS = 300;
export const CONFIRM_WINDOW_MS = 20000;

export function pickMimeType(isTypeSupported) {
  return ["audio/webm;codecs=opus", "audio/webm"].find(type => isTypeSupported(type)) ?? "";
}

function validAction(action) {
  if (action?.type === "scan") return { type: "scan" };
  if (action?.type === "seek" && Number.isInteger(action.target_id)) return { type: "seek", target_id: action.target_id };
  return null;
}

export function parseAskReply(raw) {
  if (!raw || typeof raw.answer !== "string" || typeof raw.question !== "string") throw new Error("Invalid assistant reply");
  const audio = typeof raw.audio_url === "string" && /^\/tts\/[\w.-]+\.wav$/.test(raw.audio_url) ? raw.audio_url : null;
  return { question: raw.question, answer: raw.answer, action: validAction(raw.action), audioUrl: audio,
    model: String(raw.model ?? ""), ms: Number.isFinite(raw.ms) ? raw.ms : null };
}

export function actionLabel(action) {
  return action.type === "seek" ? `Seek person #${action.target_id}` : "Scan the room";
}

export class VoiceAssistant {
  /** @param {{ fetch: Function, getMicrophone: Function, createRecorder: Function, play: Function, speak: Function,
   *   onChange?: Function, onConfirm?: Function, now?: () => number, timeoutMs?: number }} options */
  constructor({ fetch, getMicrophone, createRecorder, play, speak, onChange = () => {}, onConfirm = () => {},
    now = () => performance.now(), timeoutMs = 30000 }) {
    Object.assign(this, { fetchImpl: fetch, getMicrophone, createRecorder, play, speak, onChange, onConfirm, now, timeoutMs });
    this.state = "idle"; this.question = ""; this.answer = ""; this.action = null; this.actionAt = -Infinity;
    this.recorder = null; this.chunks = []; this.startedAt = 0; this.microphone = null;
    this.held = { talk: false, confirm: false, dismiss: false };
  }
  set(state, fields = {}) {
    Object.assign(this, fields, { state });
    this.onChange(this);
  }
  async press() {
    if (this.state === "listening" || this.state === "thinking" || this.opening) return;
    this.action = null; this.releaseQueued = false; this.opening = true;
    try {
      this.microphone ??= await this.getMicrophone();
      const recorder = this.createRecorder(this.microphone);
      this.chunks = [];
      recorder.ondataavailable = event => { if (event.data?.size) this.chunks.push(event.data); };
      this.recorder = recorder; this.startedAt = this.now();
      recorder.start();
      this.set("listening", { question: "", answer: "" });
    } catch (error) {
      this.set("error", { answer: `Microphone unavailable: ${error?.message ?? error}` });
    } finally { this.opening = false; }
    if (this.releaseQueued) await this.release();
  }
  async release() {
    if (this.opening) { this.releaseQueued = true; return; }
    const recorder = this.recorder;
    if (!recorder || this.state !== "listening") return;
    this.recorder = null;
    const stopped = new Promise(resolve => { recorder.onstop = resolve; });
    recorder.stop();
    await stopped;
    if (this.now() - this.startedAt < TALK_MIN_MS) return this.set("idle", { answer: "Hold the button while you speak" });
    const type = recorder.mimeType || "audio/webm";
    const form = new FormData();
    form.append("audio", new Blob(this.chunks, { type }), type.includes("wav") ? "question.wav" : "question.webm");
    await this.send({ method: "POST", body: form });
  }
  async askText(text) {
    if (!text.trim() || this.state === "thinking") return;
    this.action = null;
    await this.send({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: text.trim() }) });
  }
  async send(init) {
    this.set("thinking", { answer: "" });
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), this.timeoutMs);
    let reply;
    try {
      const response = await this.fetchImpl("/ask", { ...init, signal: abort.signal });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Rover returned ${response.status}`);
      reply = parseAskReply(body);
    } catch (error) {
      return this.set("error", { answer: `Assistant unavailable: ${error?.message ?? error}` });
    } finally { clearTimeout(timer); }
    this.set("answer", { question: reply.question, answer: reply.answer, action: reply.action, actionAt: this.now() });
    try {
      if (!reply.audioUrl) throw new Error("No rover audio");
      await this.play(reply.audioUrl);
    } catch { this.speak(reply.answer); }
  }
  confirm() {
    const action = this.action;
    this.action = null;
    if (!action || this.now() - this.actionAt > CONFIRM_WINDOW_MS) return this.onChange(this);
    const note = this.onConfirm(action);
    this.set("idle", { answer: typeof note === "string" ? note : `Confirmed: ${actionLabel(action)}` });
  }
  dismiss() {
    if (!this.action) return;
    this.action = null;
    this.set("idle", { answer: "Dismissed" });
  }
  /** Edge-detect held buttons or keys; call every frame. */
  async input(talk, confirm, dismiss) {
    const before = this.held;
    this.held = { talk, confirm, dismiss };
    if (confirm && !before.confirm) this.confirm();
    if (dismiss && !before.dismiss) this.dismiss();
    if (talk && !before.talk) await this.press();
    if (!talk && before.talk) await this.release();
  }
}
