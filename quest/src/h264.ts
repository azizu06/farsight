type Segment = {
  request_id: number; media_start_s: number; media_end_s: number;
  frame: number | string; age_ms: number; source: string;
  receivedAt: number; boundedAge: number;
};
/** TCP fMP4 with one requested segment in flight and capture-to-decoder freshness. */
export class H264Viewer {
  readonly video = document.createElement("video");
  source = "H.264 camera";
  state = "Starting H.264 preview";
  private socket: WebSocket | null = null;
  private media: MediaSource | null = null;
  private buffer: SourceBuffer | null = null;
  private queue: ArrayBuffer[] = [];
  private objectURL = "";
  private stopped = true;
  private retry = 0;
  private mime = "";
  private connecting = false;
  private initReceived = false;
  private requestId = 0;
  private requestedAt = -Infinity;
  private pending: Omit<Segment, "receivedAt" | "boundedAge"> | null = null;
  private segments: Segment[] = [];
  private decodedMediaTime = -1;
  private decodedAt = -Infinity;
  private decodedFrames = 0;
  private frameCallback = 0;
  private callbackSupported = "requestVideoFrameCallback" in this.video;

  constructor(private onFrame: () => void, private onReset: () => void) {
    this.video.muted = true; this.video.autoplay = true; this.video.playsInline = true;
    this.video.onerror = () => this.reset("Decoder error: restarting H.264");
  }
  start() { this.stopped = false; this.connect(); this.watchDecodedFrame(); }
  private watchDecodedFrame() {
    if (this.stopped || !this.callbackSupported) return;
    this.frameCallback = this.video.requestVideoFrameCallback((_now, metadata) => {
      this.decodedMediaTime = metadata.mediaTime;
      this.decodedAt = Math.max(performance.now(), metadata.expectedDisplayTime);
      this.decodedFrames++; this.onFrame(); this.watchDecodedFrame();
    });
  }
  get displayedFrame() {
    if (!this.callbackSupported || this.decodedAt > performance.now() || performance.now() - this.decodedAt >= 250) return null;
    const segment = [...this.segments].reverse().find(item =>
      this.decodedMediaTime >= item.media_start_s - 0.0001 && this.decodedMediaTime < item.media_end_s);
    if (!segment) return null;
    return {
      id: `h264:${segment.request_id}:${this.decodedFrames}`,
      age: segment.boundedAge + performance.now() - segment.receivedAt,
      source: segment.source,
      frame: segment.frame,
    };
  }
  private connect() {
    if (this.stopped || this.connecting) return;
    if (!("MediaSource" in window)) { this.state = "H.264 unavailable: MediaSource unsupported"; this.stopped = true; return; }
    this.connecting = true;
    const socket = new WebSocket(new URL("/ws/video", location.origin).href.replace(/^http/, "ws"));
    this.socket = socket; socket.binaryType = "arraybuffer";
    socket.onmessage = event => {
      if (typeof event.data === "string") {
        try {
          const message = JSON.parse(event.data);
          if (message.type === "video_init") {
            if (typeof message.mime !== "string" || !MediaSource.isTypeSupported(message.mime)) {
              this.state = "H.264 codec unsupported: use JPEG"; this.stop(false); return;
            }
            this.source = String(message.source ?? "H.264 camera"); this.initialize(message.mime);
          } else if (message.type === "video_segment") {
            if (message.request_id !== this.requestId || this.requestedAt === -Infinity ||
                ![message.age_ms, message.media_start_s, message.media_end_s].every(Number.isFinite) ||
                message.age_ms < 0 || message.media_end_s <= message.media_start_s) {
              this.reset("H.264 freshness metadata rejected"); return;
            }
            this.pending = message;
          } else if (message.type === "video_error") {
            this.state = `H.264 unavailable: ${String(message.reason ?? message.error ?? "encoder disabled")}`;
          }
        } catch { this.reset("Invalid H.264 metadata"); }
      } else {
        if (!this.media || event.data.byteLength > 4_000_000 || this.queue.length >= 8) {
          this.reset("H.264 queue stalled: restarting from initialization"); return;
        }
        const now = performance.now();
        if (this.initReceived) {
          if (!this.pending) { this.reset("H.264 fragment missing freshness metadata"); return; }
          this.source = this.pending.source;
          this.segments.push({ ...this.pending, receivedAt: now,
            boundedAge: this.pending.age_ms + now - this.requestedAt });
          this.segments = this.segments.slice(-64);
          this.pending = null; this.requestedAt = -Infinity;
        } else this.initReceived = true;
        this.queue.push(event.data); this.append();
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      this.connecting = false; this.onReset();
      if (!this.stopped) this.retry = window.setTimeout(() => { this.cleanMedia(); this.connect(); }, 1000);
    };
  }
  private initialize(mime: string) {
    this.cleanMedia(); this.mime = mime;
    const media = new MediaSource(); this.media = media;
    this.objectURL = URL.createObjectURL(media); this.video.src = this.objectURL;
    media.addEventListener("sourceopen", () => {
      if (this.media !== media) return;
      try {
        this.buffer = media.addSourceBuffer(this.mime);
        this.buffer.addEventListener("updateend", () => this.append());
        this.buffer.addEventListener("error", () => this.reset("H.264 buffer rejected"));
        this.append();
        void this.video.play().catch(() => { this.state = "H.264 playback blocked: select preview again"; });
      } catch { this.reset("H.264 buffer initialization failed"); }
    });
  }
  private append() {
    if (!this.buffer || this.buffer.updating || this.media?.readyState !== "open") return;
    try {
      const ranges = this.buffer.buffered;
      if (ranges.length) {
        const end = ranges.end(ranges.length - 1);
        if (end - this.video.currentTime > 0.25) this.video.currentTime = Math.max(ranges.start(ranges.length - 1), end - 0.05);
        if (this.video.currentTime > 3 && ranges.start(0) < this.video.currentTime - 3) {
          this.buffer.remove(ranges.start(0), this.video.currentTime - 2); return;
        }
      }
      const chunk = this.queue.shift();
      if (chunk) this.buffer.appendBuffer(chunk);
      else this.requestNext();
    } catch { this.reset("H.264 append failed: restarting stream"); }
  }
  private requestNext() {
    if (!this.initReceived || this.requestedAt !== -Infinity || this.socket?.readyState !== WebSocket.OPEN) return;
    if (this.socket.bufferedAmount > 0) { this.reset("H.264 request queue stalled"); return; }
    this.requestId++; this.requestedAt = performance.now();
    this.socket.send(JSON.stringify({ type: "next", request_id: this.requestId }));
  }
  update() {
    if (this.stopped) return;
    if (!this.callbackSupported && this.video.readyState >= 2) this.onFrame();
    if (this.requestedAt !== -Infinity && performance.now() - this.requestedAt > 1000) {
      this.reset("H.264 segment stalled: restarting");
    }
    this.state = this.displayedFrame ? `${this.source} · H.264 decoded frame verified` : `${this.source} · waiting for verified decoded frame (motion disabled)`;
  }
  private reset(reason: string) {
    if (this.stopped) return;
    this.state = reason; this.onReset(); this.socket?.close(); this.cleanMedia();
  }
  private cleanMedia() {
    this.queue = []; this.buffer = null; this.media = null; this.segments = [];
    this.initReceived = false; this.pending = null; this.requestedAt = -Infinity;
    this.decodedMediaTime = -1; this.decodedAt = -Infinity;
    this.video.pause(); this.video.removeAttribute("src"); this.video.load();
    if (this.objectURL) URL.revokeObjectURL(this.objectURL);
    this.objectURL = "";
  }
  stop(clearState = true) {
    this.stopped = true; this.connecting = false; window.clearTimeout(this.retry);
    if (this.callbackSupported) this.video.cancelVideoFrameCallback(this.frameCallback);
    if (this.socket) { this.socket.onclose = null; this.socket.close(); }
    this.socket = null; this.cleanMedia();
    if (clearState) this.state = "H.264 preview stopped";
  }
}
