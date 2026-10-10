/** Server-reported footage truth for the desktop and headset banner.
 * LIVE needs fresh /health proof of a real camera and a real rover. SIMULATED
 * and REPLAY are sticky: a failed poll or disconnect never clears them. */
export const REAL_CAMERA_SOURCES = new Set(["USB CAMERA", "PHONE RGB", "IPHONE LIDAR"]);
const HEALTH_FRESH_MS = 5000;

export class SourceTruth {
  replay = false;
  simulated = false;
  health = null;
  healthAt = -Infinity;
  busy = false;

  /** @param {typeof fetch} fetcher @param {number} [now] Explicit receipt time for deterministic callers. */
  async refresh(fetcher = fetch, now) {
    if (this.busy) return;
    this.busy = true;
    try {
      const response = await fetcher("/health", { cache: "no-store", signal: AbortSignal.timeout(2000) });
      if (!response.ok) return;
      const health = await response.json();
      if (!health || typeof health !== "object") return;
      this.accept(health, now ?? performance.now());
    } catch { /* Retain the last confirmed REPLAY/SIMULATED flags while disconnected. */ }
    finally { this.busy = false; }
  }

  accept(health, now) {
    if (typeof health.replay === "boolean") this.replay = health.replay;
    const simulatedEvidence = health.control?.simulated === true || health.camera_source === "SIMULATED" ||
      health.source === "SIMULATED";
    // Only an explicit, complete "not simulated" report clears the sticky flag.
    if (simulatedEvidence) this.simulated = true;
    else if (health.control?.simulated === false && typeof health.camera_source === "string") this.simulated = false;
    this.health = health;
    this.healthAt = now;
  }

  /** Control status and drawn frames are additional evidence; they can only add SIMULATED/REPLAY.
   * @param {string} [frameSource] @param {{ simulated?: boolean } | null} [controlStatus] */
  observe(frameSource = "", controlStatus = null) {
    if (frameSource.startsWith("REPLAY:")) this.replay = true;
    if (frameSource === "SIMULATED" || controlStatus?.simulated === true) this.simulated = true;
  }

  /**
   * @param {{ now: number, frameSource?: string, frameId?: string, videoAgeMs?: number }} view
   * @returns {{ kind: "REPLAY" | "SIMULATED" | "LIVE" | "UNVERIFIED", title: string, detail: string }}
   */
  state({ now, frameSource = "", frameId = "", videoAgeMs = Infinity }) {
    const age = Number.isFinite(videoAgeMs) ? `${Math.ceil(videoAgeMs)} ms` : "age unknown";
    const camera = `${frameSource || "no camera"}${frameId ? ` · frame ${frameId}` : ""} · ${age}`;
    if (this.replay || frameSource.startsWith("REPLAY:"))
      return { kind: "REPLAY", title: "REPLAY · RECORDED MISSION", detail: `Not live · motors disabled · ${camera}` };
    if (this.simulated || frameSource === "SIMULATED")
      return { kind: "SIMULATED", title: "SIMULATED", detail: `Not a physical rover or camera · ${camera}` };
    const health = this.health, fresh = now - this.healthAt < HEALTH_FRESH_MS;
    const missing = !health || !fresh ? "rover health unavailable" :
      health.replay !== false ? "replay state unreported" :
      health.control?.simulated !== false ? "simulation state unreported" :
      health.rover !== true ? "rover not connected" :
      health.camera !== true ? "camera not fresh" :
      !REAL_CAMERA_SOURCES.has(health.camera_source) ? `camera source ${String(health.camera_source ?? "unreported")}` :
      !REAL_CAMERA_SOURCES.has(frameSource) ? "no verified frame displayed" :
      !(videoAgeMs < 500) ? "displayed frame stale" : null;
    if (missing) return { kind: "UNVERIFIED", title: "NOT VERIFIED LIVE", detail: `${missing} · ${camera}` };
    const drive = health.camera_drive_ready === false ? "View only · driving camera not qualified · " : "";
    return { kind: "LIVE", title: "LIVE", detail: drive + camera };
  }
}
