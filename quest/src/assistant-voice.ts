type Reply = { question?: string; answer?: string; audio_url?: string | null };

/** Capture starts only from a user click; all transcription stays on the rover. */
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
  private answer = "";
  private audioURL: string | null = null;
  private audio: HTMLAudioElement | null = null;
  private disposed = false;
  private ownsTextBusy = false;
  private removeCloseListener: () => void;

  constructor(
    parent: HTMLElement,
    private settings: HTMLDialogElement,
    private textButton: HTMLButtonElement,
    private onStart: () => void,
    private onAnswer: (reply: Reply) => void,
  ) {
    const block = document.createElement("div"); block.className = "voice-assistant";
    block.innerHTML = '<div class="button-row"><button type="button" id="voice-record">Ask by voice</button><button type="button" id="voice-cancel" disabled>Cancel</button><button type="button" id="voice-play" disabled>Play answer</button></div><p role="status">Optional voice: records up to 10 seconds, transcribes on the rover.</p>';
    parent.append(block);
    this.recordButton = block.querySelector("#voice-record")!;
    this.cancelButton = block.querySelector("#voice-cancel")!;
    this.playButton = block.querySelector("#voice-play")!;
    this.status = block.querySelector("p")!;
    this.recordButton.onclick = () => {
      if (this.recorder?.state === "recording") this.finish();
      else if (!this.textButton.disabled) void this.record();
    };
    this.cancelButton.onclick = () => this.cancelCapture("Voice request cancelled");
    this.playButton.onclick = () => void this.play();
    const close = () => this.cancelCapture();
    this.settings.addEventListener("close", close);
    this.removeCloseListener = () => this.settings.removeEventListener("close", close);
    document.addEventListener("visibilitychange", this.visibility);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      this.recordButton.disabled = true;
      this.status.textContent = "Voice recording is unavailable here; use the text question.";
    }
  }

  private visibility = () => { if (document.hidden) { this.cancelCapture(); this.stopPlayback(); } };
  private releaseTracks() { this.stream?.getTracks().forEach(track => track.stop()); this.stream = null; }
  private finish() {
    window.clearTimeout(this.timer);
    if (this.recorder?.state === "recording") {
      this.recordButton.disabled = true; this.status.textContent = "Sending your question to the rover…";
      this.recorder.stop(); this.releaseTracks();
    }
  }
  private async record() {
    if (this.disposed || !this.settings.open || document.hidden) return;
    this.onStart(); this.stopPlayback();
    const generation = ++this.generation;
    this.recordButton.disabled = true; this.cancelButton.disabled = false;
    this.ownsTextBusy = true; this.textButton.disabled = true; this.status.textContent = "Allow the microphone to ask by voice, or cancel and use text.";
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      if (this.disposed || generation !== this.generation || document.hidden || !this.settings.open) {
        stream.getTracks().forEach(track => track.stop()); return;
      }
      this.stream = stream; this.chunks = [];
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      this.recorder = recorder;
      recorder.ondataavailable = event => { if (event.data.size && generation === this.generation) this.chunks.push(event.data); };
      recorder.onerror = () => this.cancelCapture("Recording failed; use the text question.");
      recorder.onstop = () => {
        stream.getTracks().forEach(track => track.stop());
        if (this.stream === stream) this.stream = null;
        if (this.recorder === recorder) this.recorder = null;
        if (generation !== this.generation || this.disposed || !this.settings.open || document.hidden) return;
        const audio = new Blob(this.chunks, { type: recorder.mimeType || "audio/webm" }); this.chunks = [];
        void this.submit(audio, generation);
      };
      recorder.start(250);
      this.recordButton.disabled = false; this.recordButton.textContent = "Stop and ask";
      this.status.textContent = "Recording your question (10 seconds maximum). Stop and ask when finished.";
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
    this.status.textContent = "Transcribing and answering locally on the rover…";
    try {
      const response = await fetch("/ask", { method: "POST", body, signal: abort.signal });
      if (!response.ok) {
        const message = await response.text(); throw new Error(`Rover ${response.status}: ${message.slice(0, 250)}`);
      }
      const reply = await response.json() as Reply;
      if (generation !== this.generation || this.disposed || !this.settings.open || document.hidden) return;
      this.onAnswer(reply); this.setReply(reply);
      this.status.textContent = "Rover answered. Select Play answer to hear it.";
    } catch (error) {
      if (generation === this.generation && !this.disposed) this.status.textContent = `Voice unavailable: ${String(error)}. The text question still works.`;
    } finally {
      window.clearTimeout(timeout);
      if (generation === this.generation) {
        this.request = null; this.ownsTextBusy = false; this.textButton.disabled = false; this.recordButton.disabled = false;
        this.recordButton.textContent = "Ask by voice"; this.cancelButton.disabled = true;
      }
    }
  }

  setReply(reply: Reply) {
    this.stopPlayback(); this.answer = String(reply.answer ?? ""); this.audioURL = null;
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
    this.onStart(); this.stopPlayback();
    if (this.audioURL) {
      const audio = new Audio(this.audioURL); this.audio = audio;
      this.status.textContent = "Playing audio generated on the rover.";
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
    this.stopPlayback();
    this.generation++; window.clearTimeout(this.timer); this.request?.abort(); this.request = null;
    if (this.recorder?.state === "recording") this.recorder.stop();
    this.recorder = null; this.releaseTracks(); this.chunks = [];
    if (this.ownsTextBusy) this.textButton.disabled = false;
    this.ownsTextBusy = false; this.cancelButton.disabled = true;
    this.recordButton.textContent = "Ask by voice";
    this.recordButton.disabled = !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined";
    if (message) this.status.textContent = message;
  }
  dispose() { this.disposed = true; this.cancelCapture(); this.stopPlayback(); this.removeCloseListener(); document.removeEventListener("visibilitychange", this.visibility); }
}
