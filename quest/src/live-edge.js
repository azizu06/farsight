// Live-edge policy for MSE playback of the rover/Mac H.264 streams.
// Seeking MSE playback flushes the decoder: repeated currentTime writes left Chrome
// "seeking" ~80% of the time at ~4 Hz (Sai, native Metal, issue #24 6103028791).
// So latency is trimmed by playing slightly fast; a seek is reserved for a real
// backlog or a gap the player would stall at, never while a seek is in flight,
// and at most once per cooldown.

export const LIVE_EDGE = {
  /** Lead (s) treated as a real backlog worth one seek. Normal fragment buffering stays below it. */
  seekAbove: 0.9,
  /** Where a seek lands, this far behind the newest buffered time (s). */
  target: 0.15,
  /** Minimum time between seeks (ms). */
  cooldownMs: 1000,
  /** Lead (s) above which playback runs at `fastRate`, and above `rateAbove` at `gentleRate`. */
  fastAbove: 0.35,
  rateAbove: 0.2,
  fastRate: 1.15,
  gentleRate: 1.05,
};

/**
 * Decide how to chase the live edge.
 * @param {number} currentTime playback position (s)
 * @param {{start: number, end: number} | null} last newest buffered range
 * @param {{now: number, lastSeekAt: number, seeking: boolean}} state monotonic ms clock
 * @returns {{seekTo: number | null, rate: number, lead: number}}
 */
export function chaseLiveEdge(currentTime, last, { now = 0, lastSeekAt = -Infinity, seeking = false } = {}) {
  if (!last || !Number.isFinite(currentTime) || !(last.end > last.start)) return { seekTo: null, rate: 1, lead: 0 };
  const lead = Math.max(0, last.end - currentTime);
  const gap = currentTime < last.start;
  const mayseek = !seeking && now - lastSeekAt >= LIVE_EDGE.cooldownMs;
  if (mayseek && (gap || lead > LIVE_EDGE.seekAbove)) {
    return { seekTo: Math.max(last.start, last.end - LIVE_EDGE.target), rate: 1, lead };
  }
  const rate = lead > LIVE_EDGE.fastAbove ? LIVE_EDGE.fastRate : lead > LIVE_EDGE.rateAbove ? LIVE_EDGE.gentleRate : 1;
  return { seekTo: null, rate, lead };
}
