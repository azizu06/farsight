const encoder = new TextEncoder();
const decoder = new TextDecoder();
const HEADER_LIMIT = 8192;
const FRAME_LIMIT = 4 * 1024 * 1024;

export function multipartBoundary(contentType: string): string {
  const match =
    /^multipart\/x-mixed-replace\s*;.*?\bboundary=(?:"([^"\r\n]+)"|([^;\s]+))/i.exec(
      contentType,
    );
  const boundary = match?.[1] ?? match?.[2];
  if (!boundary || !/^[\w'()+,\-./:=? ]{1,70}$/.test(boundary))
    throw new Error("Camera did not return a valid MJPEG multipart boundary.");
  return boundary;
}

function find(bytes: Uint8Array, needle: Uint8Array): number {
  outer: for (let i = 0; i <= bytes.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++)
      if (bytes[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

/** Incremental, bounded multipart parser. Does not require network reads to align to frames. */
export class MjpegParser {
  private buffer: Uint8Array = new Uint8Array();
  private state: "boundary" | "headers" | "body" = "boundary";
  private length: number | null = null;
  private marker: Uint8Array;
  private nextMarker: Uint8Array;
  constructor(
    boundary: string,
    private maxFrame = FRAME_LIMIT,
  ) {
    this.marker = encoder.encode(`--${boundary}`);
    this.nextMarker = encoder.encode(`\r\n--${boundary}`);
  }
  push(chunk: Uint8Array): Uint8Array[] {
    if (
      this.buffer.length + chunk.length >
      this.maxFrame + HEADER_LIMIT + 65536
    )
      throw new Error("Camera multipart buffer exceeds its limit.");
    const joined = new Uint8Array(this.buffer.length + chunk.length);
    joined.set(this.buffer);
    joined.set(chunk, this.buffer.length);
    this.buffer = joined;
    const frames: Uint8Array[] = [];
    for (;;) {
      if (this.state === "boundary") {
        const index = find(this.buffer, this.marker);
        if (index < 0) {
          if (this.buffer.length > HEADER_LIMIT)
            throw new Error("Camera multipart preamble is too large.");
          break;
        }
        const end = index + this.marker.length;
        if (this.buffer.length < end + 2) break;
        if (this.buffer[end] === 45 && this.buffer[end + 1] === 45) {
          this.buffer = new Uint8Array();
          break;
        }
        if (this.buffer[end] !== 13 || this.buffer[end + 1] !== 10)
          throw new Error("Invalid camera boundary.");
        this.buffer = this.buffer.slice(end + 2);
        this.state = "headers";
      }
      if (this.state === "headers") {
        const end = find(this.buffer, encoder.encode("\r\n\r\n"));
        if (end < 0) {
          if (this.buffer.length > HEADER_LIMIT)
            throw new Error("Camera headers exceed their limit.");
          break;
        }
        if (end > HEADER_LIMIT)
          throw new Error("Camera headers exceed their limit.");
        const headers = decoder.decode(this.buffer.slice(0, end));
        const type = /^content-type:\s*([^\r\n]+)/im.exec(headers)?.[1];
        if (type && type.trim().toLowerCase() !== "image/jpeg")
          throw new Error("Camera part is not JPEG.");
        const length = /^content-length:\s*([^\r\n]+)/im.exec(headers)?.[1];
        this.length = null;
        if (length !== undefined) {
          if (!/^\d+$/.test(length.trim()))
            throw new Error("Invalid JPEG Content-Length.");
          this.length = Number(length);
          if (
            !Number.isSafeInteger(this.length) ||
            this.length < 4 ||
            this.length > this.maxFrame
          )
            throw new Error(
              "JPEG frame is too large or has an invalid length.",
            );
        }
        this.buffer = this.buffer.slice(end + 4);
        this.state = "body";
      }
      if (this.state === "body") {
        const end = this.length ?? find(this.buffer, this.nextMarker);
        if (end < 0 || this.buffer.length < end) {
          if (this.buffer.length > this.maxFrame)
            throw new Error("JPEG frame exceeds its limit.");
          break;
        }
        const frame = this.buffer.slice(0, end);
        if (
          frame.length > this.maxFrame ||
          frame[0] !== 255 ||
          frame[1] !== 216 ||
          frame.at(-2) !== 255 ||
          frame.at(-1) !== 217
        )
          throw new Error("Camera returned an invalid JPEG frame.");
        frames.push(frame);
        this.buffer = this.buffer.slice(end);
        this.state = "boundary";
      }
    }
    return frames;
  }
}

export async function receiveMjpeg(
  url: string,
  signal: AbortSignal,
  onFrame: (jpeg: Blob, receivedAt: number) => void,
): Promise<void> {
  const controller = new AbortController();
  const abort = () => controller.abort(signal.reason);
  if (signal.aborted) abort();
  signal.addEventListener("abort", abort, { once: true });
  let timer: ReturnType<typeof setTimeout>;
  const refreshTimeout = () => {
    clearTimeout(timer);
    timer = setTimeout(
      () => controller.abort(new Error("No new camera frame for two seconds.")),
      2000,
    );
  };
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  refreshTimeout();
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok || !response.body)
      throw new Error(`Camera stream unavailable (${response.status}).`);
    const parser = new MjpegParser(
      multipartBoundary(response.headers.get("Content-Type") ?? ""),
    );
    reader = response.body.getReader();
    for (;;) {
      const read = await reader.read();
      if (read.done) throw new Error("Camera stream ended.");
      for (const frame of parser.push(read.value)) {
        refreshTimeout();
        onFrame(
          new Blob([frame as Uint8Array<ArrayBuffer>], { type: "image/jpeg" }),
          performance.now(),
        );
      }
    }
  } finally {
    clearTimeout(timer!);
    signal.removeEventListener("abort", abort);
    await reader?.cancel().catch(() => undefined);
    reader?.releaseLock();
    controller.abort();
  }
}
