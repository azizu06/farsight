import { describe, expect, it, vi } from "vitest";
import { cameraFrameEvidence, receiveCamera } from "./cameraFeed";

const headers = (changes: Record<string, string> = {}) =>
  new Headers({
    "X-Frame-Id": "12",
    "X-Frame-Age-Ms": "20",
    "X-Frame-Source": "SIMULATED",
    ...changes,
  });

describe("camera receipt evidence", () => {
  it("includes the entire HTTP transfer time without clock synchronization", () => {
    expect(cameraFrameEvidence(headers(), 100, 180)).toEqual({
      frameId: "12",
      ageMs: 100,
      source: "SIMULATED",
    });
  });
  it("does not invent evidence for legacy camera responses", () => {
    expect(cameraFrameEvidence(new Headers(), 100, 180)).toBeNull();
    const partial = headers();
    partial.delete("X-Frame-Source");
    expect(cameraFrameEvidence(partial, 100, 180)).toBeNull();
  });
  it.each(["", " ", "NaN", "Infinity", "-1", "1e3"])(
    "rejects malformed camera age %j",
    (age) => {
      expect(
        cameraFrameEvidence(headers({ "X-Frame-Age-Ms": age }), 100, 180),
      ).toBeNull();
    },
  );
  it("rejects unbounded identifiers and inconsistent monotonic time", () => {
    expect(
      cameraFrameEvidence(headers({ "X-Frame-Id": "x".repeat(129) }), 100, 180),
    ).toBeNull();
    expect(cameraFrameEvidence(headers(), 180, 100)).toBeNull();
    expect(cameraFrameEvidence(headers(), 100, NaN)).toBeNull();
  });
  it("retains stale age instead of certifying old video as fresh", () => {
    expect(cameraFrameEvidence(headers(), 100, 650)?.ageMs).toBe(570);
  });
  it("does not deliver the same source frame repeatedly as new video", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(new Uint8Array([255, 216, 255, 217]), {
          headers: {
            "Content-Type": "image/jpeg",
            "X-Frame-Id": "12",
            "X-Frame-Age-Ms": "10",
            "X-Frame-Source": "SIMULATED",
          },
        }),
    );
    const controller = new AbortController();
    const stop = setTimeout(
      () => controller.abort(new Error("Probe complete")),
      240,
    );
    const delivered: string[] = [];
    try {
      await expect(
        receiveCamera(controller.signal, (_blob, _at, evidence) => {
          delivered.push(evidence!.frameId);
        }),
      ).rejects.toThrow("Probe complete");
      expect(delivered).toEqual(["12"]);
    } finally {
      clearTimeout(stop);
      controller.abort();
      vi.unstubAllGlobals();
    }
  });
});
