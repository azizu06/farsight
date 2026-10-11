export const INTERCOM_RATE = 16_000;
export const INTERCOM_SAMPLES = 320;
export const INTERCOM_FRAME_BYTES = INTERCOM_SAMPLES * 2;
export const INTERCOM_BUFFER_LIMIT = INTERCOM_FRAME_BYTES * 4;
const SOCKET_OPEN = 1;

export type IntercomControl =
  | { type: "talk"; active: boolean }
  | { type: "peer"; connected: boolean };

export type IntercomState = {
  enabled: boolean;
  phoneConnected: boolean;
  status: string;
};

export function parseIntercomControl(text: string): IntercomControl | null {
  try {
    const packet: unknown = JSON.parse(text);
    if (!packet || typeof packet !== "object") return null;
    const value = packet as Record<string, unknown>;
    if (value.type === "talk" && typeof value.active === "boolean") return { type: "talk", active: value.active };
    if (value.type === "peer" && typeof value.connected === "boolean") return { type: "peer", connected: value.connected };
  } catch { /* malformed peer control */ }
  return null;
}

export function intercomSocketURL(origin: string): string {
  const url = new URL("/ws/intercom", origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.search = "?role=quest";
  return url.href;
}

export function canSendIntercomAudio(socket: Pick<WebSocket, "readyState" | "bufferedAmount">): boolean {
  return socket.readyState === SOCKET_OPEN && socket.bufferedAmount < INTERCOM_BUFFER_LIMIT;
}

/** Converts browser input callbacks into fixed 20 ms PCM16LE mono frames. */
export class PCMFrameEncoder {
  private phase = 0;
  private pending: number[] = [];

  constructor(private readonly sourceRate: number) {
    if (!Number.isFinite(sourceRate) || sourceRate < INTERCOM_RATE) throw new RangeError("Audio sample rate must be at least 16 kHz");
  }

  push(input: Float32Array): ArrayBuffer[] {
    const step = this.sourceRate / INTERCOM_RATE;
    let position = this.phase;
    for (; position < input.length; position += step) {
      this.pending.push(input[Math.min(input.length - 1, Math.floor(position))] ?? 0);
    }
    this.phase = position - input.length;
    const frames: ArrayBuffer[] = [];
    while (this.pending.length >= INTERCOM_SAMPLES) {
      const packet = new ArrayBuffer(INTERCOM_FRAME_BYTES);
      const view = new DataView(packet);
      for (let i = 0; i < INTERCOM_SAMPLES; i++) {
        const sample = Math.max(-1, Math.min(1, this.pending[i]));
        view.setInt16(i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
      }
      this.pending.splice(0, INTERCOM_SAMPLES);
      frames.push(packet);
    }
    return frames;
  }

  reset() { this.phase = 0; this.pending = []; }
}

export class QuestIntercom {
  private socket: WebSocket | null = null;
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private silence: GainNode | null = null;
  private encoder: PCMFrameEncoder | null = null;
  private speakerNodes = new Set<AudioBufferSourceNode>();
  private nextPlaybackAt = 0;
  private reconnectTimer = 0;
  private openResolve: (() => void) | null = null;
  private openReject: ((reason: Error) => void) | null = null;
  private requested = false;
  private serverActive = false;
  private phoneConnected = false;
  private connectionWanted = false;
  private disposed = false;
  private status = "Connecting to iPhone…";

  constructor(private readonly onState: (state: IntercomState) => void) {
    this.status = "Enter VR or connect iPhone capture";
    this.publish();
  }

  get enabled() { return this.requested; }
  get connected() { return this.phoneConnected; }

  connectIfNeeded() {
    if (this.disposed || this.socket || this.connectionWanted) return;
    this.connectionWanted = true;
    this.status = "Connecting to iPhone…";
    this.publish();
    this.connect();
  }

  async toggle() {
    if (this.requested) { this.disable(true); return; }
    if (!this.socket) { this.connectIfNeeded(); return; }
    if (!this.phoneConnected) {
      this.status = this.socket.readyState === 0 ? "Connecting to Jetson…" : "Start iPhone capture first";
      this.publish(); return;
    }
    this.requested = true;
    this.status = "Requesting headset microphone…";
    this.publish();
    let context: AudioContext | null = null;
    try {
      // Resume synchronously from the controller/button gesture before the permission prompt awaits.
      context = new AudioContext();
      this.context = context;
      void context.resume();
      const permission = navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
      });
      const stream = await permission;
      if (!this.requested || this.disposed) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      this.encoder = new PCMFrameEncoder(context.sampleRate);
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(256, 1, 1);
      const silence = context.createGain(); silence.gain.value = 0;
      processor.onaudioprocess = event => {
        if (!this.requested || !this.serverActive || !this.socket || !this.encoder) return;
        for (const packet of this.encoder.push(event.inputBuffer.getChannelData(0))) {
          if (canSendIntercomAudio(this.socket)) this.socket.send(packet);
        }
      };
      source.connect(processor); processor.connect(silence); silence.connect(context.destination);
      this.source = source; this.processor = processor; this.silence = silence;
      const socket = this.socket ?? this.connect();
      await this.waitForOpen(socket);
      if (!this.requested || this.disposed || socket !== this.socket) return;
      socket.send(JSON.stringify({ type: "talk", active: true }));
      this.status = "Connecting two-way audio…";
      this.publish();
    } catch (error) {
      if (!this.disposed) {
        this.status = error instanceof DOMException && error.name === "NotAllowedError"
          ? "Microphone permission denied" : `Talk unavailable: ${error instanceof Error ? error.message : String(error)}`;
        this.disable(false);
        this.status = error instanceof DOMException && error.name === "NotAllowedError"
          ? "Microphone permission denied" : this.status;
        this.publish();
      }
    }
  }

  disable(notifyServer: boolean) {
    if (notifyServer && this.socket?.readyState === SOCKET_OPEN) {
      this.socket.send(JSON.stringify({ type: "talk", active: false }));
    }
    this.requested = false;
    this.serverActive = false;
    this.flushPlayback();
    this.processor && (this.processor.onaudioprocess = null);
    this.source?.disconnect(); this.processor?.disconnect(); this.silence?.disconnect();
    this.stream?.getTracks().forEach(track => track.stop());
    if (this.context && this.context.state !== "closed") void this.context.close();
    this.stream = null; this.context = null; this.source = null; this.processor = null; this.silence = null;
    this.encoder?.reset(); this.encoder = null;
    if (!this.status.includes("denied") && !this.status.includes("unavailable")) this.status = "Talk off";
    this.publish();
  }

  dispose() {
    this.disposed = true;
    this.disconnect();
  }

  disconnect() {
    if (this.requested) this.disable(true);
    this.connectionWanted = false;
    window.clearTimeout(this.reconnectTimer);
    const socket = this.socket; this.socket = null;
    if (socket) { socket.onclose = null; socket.onerror = null; socket.close(); }
    this.phoneConnected = false; this.serverActive = false;
    this.status = "Intercom disconnected"; this.publish();
  }

  private connect(): WebSocket {
    const socket = new WebSocket(intercomSocketURL(location.origin));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.onopen = () => {
      this.openResolve?.(); this.openResolve = null; this.openReject = null;
      this.status = this.phoneConnected ? (this.requested ? "Connecting two-way audio…" : "iPhone connected · Talk off") : "Waiting for iPhone capture";
      this.publish();
    };
    socket.onmessage = event => {
      if (typeof event.data === "string") this.control(event.data);
      else if (event.data instanceof ArrayBuffer) this.play(event.data);
    };
    socket.onerror = () => { this.openReject?.(new Error("Jetson intercom socket failed")); };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.openReject?.(new Error("Jetson intercom socket closed")); this.openResolve = null; this.openReject = null;
      this.serverActive = false; this.phoneConnected = false;
      this.disable(false);
      this.status = "Intercom disconnected · reconnecting"; this.publish();
      if (!this.disposed && this.connectionWanted) this.reconnectTimer = window.setTimeout(() => this.connect(), 750);
    };
    return socket;
  }

  private waitForOpen(socket: WebSocket): Promise<void> {
    if (socket.readyState === SOCKET_OPEN) return Promise.resolve();
    return new Promise((resolve, reject) => {
      this.openResolve = resolve; this.openReject = reject;
      window.setTimeout(() => {
        if (this.openResolve === resolve) { this.openResolve = null; this.openReject = null; reject(new Error("Jetson intercom connection timed out")); }
      }, 5000);
    });
  }

  private control(text: string) {
    const message = parseIntercomControl(text);
    if (!message) return;
    if (message.type === "peer") {
      this.phoneConnected = message.connected;
      if (!message.connected && this.requested) this.disable(false);
      else if (message.connected && this.requested && this.socket?.readyState === SOCKET_OPEN) {
        this.socket.send(JSON.stringify({ type: "talk", active: true }));
      }
      this.status = message.connected ? (this.requested ? "Connecting two-way audio…" : "iPhone connected · Talk off") : "Waiting for iPhone capture";
    } else {
      this.serverActive = message.active;
      this.nextPlaybackAt = 0;
      if (!message.active) this.flushPlayback();
      this.status = message.active ? "Two-way audio active" : "Talk off";
    }
    this.publish();
  }

  private play(packet: ArrayBuffer) {
    const context = this.context;
    if (!this.requested || !this.serverActive || !context || packet.byteLength !== INTERCOM_FRAME_BYTES) return;
    const audio = context.createBuffer(1, INTERCOM_SAMPLES, INTERCOM_RATE);
    const output = audio.getChannelData(0), view = new DataView(packet);
    for (let i = 0; i < INTERCOM_SAMPLES; i++) output[i] = view.getInt16(i * 2, true) / 32768;
    const now = context.currentTime;
    let startAt = Math.max(this.nextPlaybackAt, now + 0.04);
    if (startAt > now + 0.12) startAt = now + 0.04;
    const node = context.createBufferSource(); node.buffer = audio; node.connect(context.destination);
    this.speakerNodes.add(node); node.onended = () => this.speakerNodes.delete(node);
    node.start(startAt); this.nextPlaybackAt = startAt + INTERCOM_SAMPLES / INTERCOM_RATE;
  }

  private flushPlayback() {
    for (const node of this.speakerNodes) { try { node.stop(); } catch { /* already ended */ } }
    this.speakerNodes.clear(); this.nextPlaybackAt = 0;
  }

  private publish() { this.onState({ enabled: this.requested, phoneConnected: this.phoneConnected, status: this.status }); }
}
