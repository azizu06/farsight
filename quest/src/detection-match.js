/** Which detector packet to draw over the displayed camera frame.
 * The detector usually runs slower than the camera (--demo: ~3 fps vs ~15 Hz;
 * rig: 10-15 fps vs 30 Hz), so most displayed frames have no packet of their
 * own. Draw the nearest packet from the same source within MAX_LAG_FRAMES,
 * preferring the exact frame, faded by how far its frame is from the shown one.
 * Boxes never cross camera sources and never outlive PACKET_FRESH_MS. */
export const MAX_LAG_FRAMES = 8; // ~270 ms at 30 Hz, ~530 ms at 15 Hz
export const PACKET_FRESH_MS = 500; // same window as the "detections stale" HUD guard
const MIN_ALPHA = 0.35;
const CAPACITY = 40;

const frameNumber = frame => {
  const value = typeof frame === "number" ? frame : typeof frame === "string" && /^\d+$/.test(frame) ? Number(frame) : NaN;
  return Number.isSafeInteger(value) ? value : null;
};

export class DetectionCache {
  /** @type {Map<string, { packet: any, at: number }>} */
  entries = new Map();

  get size() { return this.entries.size; }

  /** @param {{ frame: number | string, source?: string }} packet @param {number} now */
  add(packet, now) {
    const key = `${packet.source ?? ""}:${packet.frame}`;
    this.entries.delete(key);
    this.entries.set(key, { packet, at: now });
    while (this.entries.size > CAPACITY) this.entries.delete(this.entries.keys().next().value);
  }

  clear() { this.entries.clear(); }

  /** @returns {{ packet: any, lag: number, alpha: number } | null} lag > 0: packet is older than the displayed frame. */
  match(source, frameId, now) {
    if (!frameId) return null;
    const fresh = entry => entry && now - entry.at < PACKET_FRESH_MS;
    const exact = this.entries.get(`${source}:${frameId}`) ?? this.entries.get(`:${frameId}`);
    if (fresh(exact)) return { packet: exact.packet, lag: 0, alpha: 1 };
    const shown = frameNumber(frameId);
    if (shown === null) return null;
    let best = null;
    for (const entry of this.entries.values()) {
      // Legacy sourceless packets match only their exact frame above.
      if (entry.packet.source !== source || !fresh(entry)) continue;
      const frame = frameNumber(entry.packet.frame);
      if (frame === null) continue;
      const lag = shown - frame;
      if (Math.abs(lag) > MAX_LAG_FRAMES) continue;
      if (!best || Math.abs(lag) < Math.abs(best.lag) || (Math.abs(lag) === Math.abs(best.lag) && lag > best.lag)) best = { packet: entry.packet, lag };
    }
    if (!best) return null;
    return { ...best, alpha: 1 - (1 - MIN_ALPHA) * Math.abs(best.lag) / MAX_LAG_FRAMES };
  }
}
