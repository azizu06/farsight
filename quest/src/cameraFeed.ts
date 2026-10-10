import { receiveMjpeg } from "./stream";

export interface CameraEvidence {
  frameId: string;
  ageMs: number;
  source: string;
}

/** Server receipt age plus full request duration; no wall-clock synchronization. */
export function cameraFrameEvidence(
  headers: Headers,
  startedAt: number,
  completedAt: number,
): CameraEvidence | null {
  const frameId = headers.get("X-Frame-Id");
  const rawAge = headers.get("X-Frame-Age-Ms");
  const source = headers.get("X-Frame-Source");
  if (
    !frameId ||
    frameId.length > 128 ||
    !/^[\x21-\x7e]+$/.test(frameId) ||
    !rawAge ||
    !/^\d+(?:\.\d+)?$/.test(rawAge) ||
    !source ||
    source.length > 128 ||
    !Number.isFinite(startedAt) ||
    !Number.isFinite(completedAt) ||
    completedAt < startedAt
  )
    return null;
  const ageMs = Number(rawAge) + completedAt - startedAt;
  return Number.isFinite(ageMs) ? { frameId, ageMs, source } : null;
}

/** Freshness-aware JPEGs when supported; legacy MJPEG remains a view-only source. */
export async function receiveCamera(
  signal: AbortSignal,
  onFrame: (
    jpeg: Blob,
    receivedAt: number,
    evidence: CameraEvidence | null,
  ) => void,
): Promise<void> {
  let selected = false;
  let lastFrameKey: string | null = null;
  for (;;) {
    if (signal.aborted) throw signal.reason ?? new Error("Camera cancelled.");
    const controller = new AbortController();
    const abort = () => controller.abort(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(
      () => controller.abort(new Error("Camera request timed out.")),
      750,
    );
    const startedAt = performance.now();
    let legacy = false;
    try {
      const response = await fetch("/camera.jpg", {
        signal: controller.signal,
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!selected && response.status === 404) legacy = true;
      else {
        if (!response.ok)
          throw new Error(`Camera unavailable (${response.status}).`);
        if (
          !response.headers
            .get("Content-Type")
            ?.toLowerCase()
            .startsWith("image/jpeg")
        )
          throw new Error("Camera response is not JPEG.");
        const reader = response.body?.getReader();
        if (!reader) throw new Error("Camera response has no image.");
        const chunks: Uint8Array<ArrayBuffer>[] = [];
        let size = 0;
        try {
          for (;;) {
            const next = await reader.read();
            if (next.done) break;
            size += next.value.length;
            if (size > 2_000_000)
              throw new Error("Camera JPEG exceeds its limit.");
            chunks.push(next.value as Uint8Array<ArrayBuffer>);
          }
        } finally {
          await reader.cancel().catch(() => undefined);
          reader.releaseLock();
        }
        if (signal.aborted || controller.signal.aborted)
          throw signal.reason ?? new Error("Camera cancelled.");
        const now = performance.now();
        const evidence = cameraFrameEvidence(response.headers, startedAt, now);
        if (!selected && !evidence) legacy = true;
        else {
          if (!evidence)
            throw new Error("Camera freshness metadata disappeared.");
          selected = true;
          if (evidence.ageMs >= 2000)
            throw new Error("Camera source frame is stale; old image cleared.");
          const key = `${evidence.source}:${evidence.frameId}`;
          if (key !== lastFrameKey) {
            lastFrameKey = key;
            onFrame(new Blob(chunks, { type: "image/jpeg" }), now, evidence);
          }
        }
      }
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
      controller.abort();
    }
    if (legacy)
      return receiveMjpeg("/stream.mjpg", signal, (blob, at) =>
        onFrame(blob, at, null),
      );
    // One request in flight; abort also clears the cadence timer/listener.
    const delay = Math.max(0, 100 - (performance.now() - startedAt));
    await new Promise<void>((resolve, reject) => {
      const abort = () => {
        clearTimeout(timer);
        reject(signal.reason ?? new Error("Camera cancelled."));
      };
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", abort);
        resolve();
      }, delay);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) {
        signal.removeEventListener("abort", abort);
        abort();
      }
    });
  }
}
