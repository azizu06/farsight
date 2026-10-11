export type Action = { type: "seek"; target_id: number } | { type: "scan" };
type Reply = { question?: string; answer?: string; audio_url?: string | null; action?: unknown };
export type VoicePhase = "idle" | "permission" | "listening" | "processing" | "answer" | "error";
const CONFIRM_WINDOW_MS = 20000;

function validAction(action: unknown): Action | null {
  const value = action as { type?: unknown; target_id?: unknown } | null;
  if (value?.type === "scan") return { type: "scan" };
  if (value?.type === "seek" && Number.isInteger(value.target_id)) return { type: "seek", target_id: value.target_id as number };
  return null;
}

/** Capture starts only from a deliberate click or held push-to-talk; all transcription stays on the rover. */
export class AssistantVoice {
  private recorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private request: AbortController | null = null;
  private generation = 0;
  private timer = 0;
  private chunks: Blob[] = [];
  private recordButton: HTMLButtonElement;
  private cancelButton: HTMLButtonElement;
  private playButton: HTMLButtonElement;
  private status: HTMLElement;
  private actionPrompt: HTMLElement;
  private confirmButton: HTMLButtonElement;
  private dismissButton: HTMLButtonElement;
  private action: Action | null = null;
  private actionAt = 0;
  private answer = "";
  private question = "";
  private audioURL: string | null = null;
  private audio: HTMLAudioElement | null = null;
  private disposed = false;
  private ownsTextBusy = false;
  private removeCloseListener: () => void;
  private ptt = false;
  private enableButton: HTMLButtonElement;
  private available = true;
  /** Settings/deep-link preference, default on in the demo. Capture still needs a held trigger. */
  vrEnabled = false;
  /** Called once the microphone permission for in-VR voice is granted. */
  onVREnabled: () => void = () => {};
  /** Microphone opened in 2D when in-VR voice was enabled. The Quest Browser re-prompts each
   * getUserMedia and cannot show that prompt in immersive VR, so push-to-talk records from this. */
  private vrStream: MediaStream | null = null;
  phase: VoicePhase = "idle";

  constructor(
    parent: HTMLElement,
    private settings: HTMLDialogElement,
    private textButton: HTMLButtonElement,
    private onStart: () => void,
    private onAnswer: (reply: Reply) => void,
    private onConfirm: (action: Action) => void = () => {},
    private readonly vrPushToTalk = true,
  ) {
    const block = document.createElement("div"); block.className = "voice-assistant";
    block.innerHTML = '<div class="button-row"><button type="button" id="voice-record">Ask by voice</button><button type="button" id="voice-cancel" disabled>Cancel</button><button type="button" id="voice-play" disabled>Play answer</button></div><p role="status">Optional voice: records up to 10 seconds, transcribes on the rover.</p><button type="button" id="voice-vr-enable">Enable in-VR voice (hold left trigger)</button><p id="voice-action" hidden></p><div class="button-row"><button type="button" id="voice-confirm" disabled>Confirm</button><button type="button" id="voice-dismiss" disabled>Dismiss</button></div>';
    parent.append(block);
    this.recordButton = block.querySelector("#voice-record")!;
    this.cancelButton = block.querySelector("#voice-cancel")!;
    this.playButton = block.querySelector("#voice-play")!;
    this.status = block.querySelector("p")!;
    // The rover only proposes motion; nothing happens until the operator confirms here.
    this.actionPrompt = block.querySelector("#voice-action")!;
    this.confirmButton = block.querySelector("#voice-confirm")!;
    this.dismissButton = block.querySelector("#voice-dismiss")!;
    this.enableButton = block.querySelector("#voice-vr-enable")!;
    this.enableButton.hidden = !this.vrPushToTalk;
    this.confirmButton.onclick = () => this.confirmAction();
    this.dismissButton.onclick = () => this.dismissAction();
    this.recordButton.onclick = () => {
      if (this.recorder?.state === "recording") this.finish();
      else if (!this.textButton.disabled) void this.record(false);
    };
    this.enableButton.onclick = () => void this.enableVR();
    this.cancelButton.onclick = () => this.cancelCapture("Voice request cancelled");
    this.playButton.onclick = () => void this.play();
    const close = () => this.cancelCapture();
    this.settings.addEventListener("close", close);
    this.removeCloseListener = () => this.settings.removeEventListener("close", close);
    document.addEventListener("visibilitychange", this.visibility);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      this.recordButton.disabled = true; this.enableButton.disabled = true;
      this.status.textContent = "Voice recording is unavailable here; use the text question.";
    }
  }

  private say(text: string, phase: VoicePhase = this.phase) { this.status.textContent = text; this.phase = phase; }
  get statusText() { return this.status.textContent ?? ""; }
  get answerText() { return this.answer; }
  get questionText() { return this.question; }
  get busy() { return this.capturing || this.phase === "processing"; }
  get pendingAction() { return this.action && Date.now() - this.actionAt <= CONFIRM_WINDOW_MS ? this.action : null; }
  /** Microphone requested or open: the rover must hold motion. */
  get capturing() { return this.phase === "permission" || this.phase === "listening"; }
  private contextOpen() { return !document.hidden && (this.ptt || this.settings.open); }
  confirmAction() {
    const action = this.action;
    const fresh = action && Date.now() - this.actionAt <= CONFIRM_WINDOW_MS;
    this.clearAction(fresh ? undefined : "Proposal expired; ask again.");
    if (fresh) this.onConfirm(action);
    return Boolean(fresh);
  }
  dismissAction() { this.clearAction("Proposal dismissed."); }
  /** False when the rover has no speech recognizer: voice controls hide and the left trigger never starts a question. */
  get voiceAvailable() { return this.available; }
  setAvailable(available: boolean) {
    if (available === this.available) return;
    this.available = available;
    if (!available) this.cancelCapture();
    for (const control of [this.recordButton, this.cancelButton, this.enableButton]) control.hidden = !available;
    this.enableButton.hidden = !available || !this.vrPushToTalk;
    this.say(available ? "Optional voice: records up to 10 seconds, transcribes on the rover."
      : "Voice questions are off: this rover has no speech recognizer. The text question works.", "idle");
  }
  /** Re-enable in-VR voice saved from an earlier visit; no-op where recording is unavailable. */
  restoreVR() {
    if (!this.vrPushToTalk || !this.available || this.enableButton.disabled || this.disposed) return;
    // Arm the preference without a permission probe on page load. The held trigger requests the mic.
    this.vrEnabled = true;
    this.enableButton.textContent = "In-VR voice enabled (hold left trigger)";
  }
  private async enableVR() {
    if (!this.vrPushToTalk || this.disposed || this.vrEnabled) return;
    this.enableButton.disabled = true;
    try {
      // One deliberate permission request; the probe tracks stop immediately.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      if (this.disposed) { stream.getTracks().forEach(track => track.stop()); return; }
      this.vrStream = stream;
      this.vrEnabled = true; this.enableButton.textContent = "In-VR voice enabled (hold left trigger)";
      this.say("In-VR voice enabled: in the headset, hold the left trigger to ask and release to send.", "idle");
      this.onVREnabled();
    } catch (error) {
      this.enableButton.disabled = false;
      this.say(`Microphone permission not granted: ${String(error)}. In-VR voice stays off.`, "error");
    }
  }
  /** Left-trigger press in VR. Returns false when nothing started. */
  pushStart() {
    if (!this.vrPushToTalk || this.disposed || document.hidden || !this.available) return false;
    if (!this.vrEnabled) { this.say("In-VR voice is off: enable it in Settings before entering VR.", "error"); return false; }
    if (this.textButton.disabled || this.recorder || this.request || this.capturing) return false;
    void this.record(true);
    return true;
  }
  /** Left-trigger release: send what was recorded, or abandon a pending permission. */
  pushEnd() {
    if (!this.ptt) return;
    if (this.recorder?.state === "recording") this.finish();
    else if (this.phase === "permission") this.cancelCapture("Released before the microphone opened; nothing was sent.");
  }
  /** Tracking loss, session exit or headset blur: stop the microphone, keep any in-flight answer. */
  stopListening(reason: string) { if (this.capturing) this.cancelCapture(reason); }
  private visibility = () => { if (document.hidden) { this.cancelCapture(); this.stopPlayback(); } };
  private releaseTracks() { if (this.stream !== this.vrStream) this.stream?.getTracks().forEach(track => track.stop()); this.stream = null; }
  private liveVRStream() { return this.vrStream?.getTracks().some(track => track.readyState === "live") ? this.vrStream : null; }
  private finish() {
    window.clearTimeout(this.timer);
    if (this.recorder?.state === "recording") {
      this.recordButton.disabled = true; this.say("Sending your question to the rover…", "processing");
      this.recorder.stop(); this.releaseTracks();
    }
  }
  private async record(ptt: boolean) {
    if (this.disposed || document.hidden || (!ptt && !this.settings.open)) return;
    this.onStart(); this.stopPlayback(); this.clearAction(); this.answer = ""; this.question = "";
    const generation = ++this.generation;
    this.ptt = ptt;
    this.recordButton.disabled = true; this.cancelButton.disabled = false;
    this.ownsTextBusy = true; this.textButton.disabled = true;
    this.say(ptt ? "Opening microphone…" : "Allow the microphone to ask by voice, or cancel and use text.", "permission");
    try {
      const stream = (ptt ? this.liveVRStream() : null) ?? await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      if (this.disposed || generation !== this.generation || !this.contextOpen()) {
        if (stream !== this.vrStream) stream.getTracks().forEach(track => track.stop());
        return;
      }
      this.stream = stream; this.chunks = [];
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      this.recorder = recorder;
      recorder.ondataavailable = event => { if (event.data.size && generation === this.generation) this.chunks.push(event.data); };
      recorder.onerror = () => this.cancelCapture("Recording failed; use the text question.");
      recorder.onstop = () => {
        if (stream !== this.vrStream) stream.getTracks().forEach(track => track.stop());
        if (this.stream === stream) this.stream = null;
        if (this.recorder === recorder) this.recorder = null;
        if (generation !== this.generation || this.disposed || !this.contextOpen()) return;
        const audio = new Blob(this.chunks, { type: recorder.mimeType || "audio/webm" }); this.chunks = [];
        void this.submit(audio, generation);
      };
      recorder.start(250);
      this.recordButton.disabled = false; this.recordButton.textContent = "Stop and ask";
      this.say(ptt ? "Listening (10 seconds maximum). Release the left trigger to send." :
        "Recording your question (10 seconds maximum). Stop and ask when finished.", "listening");
      this.timer = window.setTimeout(() => { if (generation === this.generation) this.finish(); }, 10000);
    } catch (error) {
      if (generation !== this.generation || this.disposed) return;
      this.cancelCapture(`Microphone unavailable: ${String(error)}. Use the text question.`);
    }
  }
  private async submit(audio: Blob, generation: number) {
    if (!audio.size || audio.size > 8 * 1024 * 1024) { this.cancelCapture("Recording is empty or too large; use text."); return; }
    const abort = new AbortController(); this.request = abort;
    const timeout = window.setTimeout(() => abort.abort(), 45000);
    const extension = audio.type.includes("mp4") ? "m4a" : audio.type.includes("ogg") ? "ogg" : "webm";
    const body = new FormData(); body.append("audio", audio, `question.${extension}`);
    this.say("Transcribing and answering locally on the rover…", "processing");
    try {
      const response = await fetch("/ask", { method: "POST", body, signal: abort.signal });
      if (!response.ok) {
        const message = await response.text(); throw new Error(`Rover ${response.status}: ${message.slice(0, 250)}`);
      }
      const reply = await response.json() as Reply;
      if (generation !== this.generation || this.disposed || !this.contextOpen()) return;
      this.onAnswer(reply); this.setReply(reply);
      this.say(this.ptt ? "Rover answered." : "Rover answered. Select Play answer to hear it.", "answer");
      if (this.ptt && this.audioURL) void this.play();
    } catch (error) {
      if (generation === this.generation && !this.disposed) this.say(`Voice unavailable: ${String(error)}. The text question still works.`, "error");
    } finally {
      window.clearTimeout(timeout);
      if (generation === this.generation) {
        this.request = null; this.ownsTextBusy = false; this.ptt = false; this.textButton.disabled = false; this.recordButton.disabled = false;
        this.recordButton.textContent = "Ask by voice"; this.cancelButton.disabled = true;
      }
    }
  }

  private clearAction(message?: string) {
    this.action = null; this.actionPrompt.hidden = true; this.actionPrompt.textContent = "";
    this.confirmButton.disabled = true; this.dismissButton.disabled = true;
    if (message) this.status.textContent = message;
  }
  setReply(reply: Reply) {
    this.stopPlayback(); this.answer = String(reply.answer ?? ""); this.question = String(reply.question ?? ""); this.audioURL = null;
    this.clearAction();
    const action = validAction(reply.action);
    if (action) {
      this.action = action; this.actionAt = Date.now();
      this.actionPrompt.textContent = action.type === "seek" ? `Seek person #${action.target_id}?` : "Scan the room?";
      this.actionPrompt.hidden = false; this.confirmButton.disabled = false; this.dismissButton.disabled = false;
    }
    if (typeof reply.audio_url === "string") {
      try {
        const url = new URL(reply.audio_url, location.origin);
        if (url.origin === location.origin && url.pathname.startsWith("/tts/")) this.audioURL = url.href;
      } catch { /* A malformed audio URL never hides the textual answer. */ }
    }
    this.playButton.disabled = !this.answer;
    this.playButton.textContent = this.audioURL ? "Play rover audio" : "Read answer locally";
  }
  private async play() {
    // In-VR replies play without stopping motion: the operator never pressed a settings control.
    if (!this.ptt) this.onStart();
    this.stopPlayback();
    if (this.audioURL) {
      const audio = new Audio(this.audioURL); this.audio = audio;
      if (!this.ptt) this.status.textContent = "Playing audio generated on the rover.";
      try { await audio.play(); } catch { this.status.textContent = "Rover audio could not play; the answer remains available as text."; }
      return;
    }
    if (!("speechSynthesis" in window) || !this.answer) { this.status.textContent = "Local spoken playback is unavailable; read the answer above."; return; }
    const voice = speechSynthesis.getVoices().find(candidate => candidate.localService && candidate.lang.startsWith("en")) ?? speechSynthesis.getVoices().find(candidate => candidate.localService);
    if (!voice) { this.status.textContent = "No local speech voice is available here; read the answer above."; return; }
    const utterance = new SpeechSynthesisUtterance(this.answer); utterance.voice = voice;
    speechSynthesis.speak(utterance); this.status.textContent = "Reading with the browser's local speech voice.";
  }
  private stopPlayback() {
    this.audio?.pause(); this.audio = null;
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }
  cancelCapture(message?: string) {
    this.stopPlayback(); this.clearAction();
    this.generation++; this.ptt = false; this.phase = message ? "error" : "idle"; window.clearTimeout(this.timer); this.request?.abort(); this.request = null;
    if (this.recorder?.state === "recording") this.recorder.stop();
    this.recorder = null; this.releaseTracks(); this.chunks = [];
    if (this.ownsTextBusy) this.textButton.disabled = false;
    this.ownsTextBusy = false; this.cancelButton.disabled = true;
    this.recordButton.textContent = "Ask by voice";
    this.recordButton.disabled = !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined";
    if (message) this.say(message, "error");
  }
  dispose() {
    this.disposed = true; this.cancelCapture(); this.stopPlayback();
    this.vrStream?.getTracks().forEach(track => track.stop()); this.vrStream = null; this.removeCloseListener(); document.removeEventListener("visibilitychange", this.visibility); }
}
