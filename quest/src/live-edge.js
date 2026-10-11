// Live-edge policy for MSE playback of the rover/Mac H.264 streams.
// Seeking MSE playback flushes the decoder: repeated currentTime writes left Chrome
// "seeking" ~80% of the time at ~4 Hz (Sai, native Metal, issue #24 6103028791).
// So latency is trimmed by playing slightly fast; a seek is reserved for a real
// backlog or a gap the player would stall at, never while a seek is in flight,
// and at most once per cooldown.
// The newest frame is on screen while playback is anywhere inside it, so a lead of
// one frame duration is the live edge itself. Rate trimming starts just past that,
// rather than at a fixed 200 ms that left up to a frame of untrimmed lag at 10 fps.

export const LIVE_EDGE = {
  /** Lead (s) treated as a real backlog worth one seek. Normal fragment buffering stays below it. */
  seekAbove: 0.9,
  /** Frame duration (s) assumed when the stream's is unknown. */
  frame: 0.1,
  /** Minimum time between seeks (ms). */
  cooldownMs: 1000,
  /** Lag (s) beyond the newest frame above which playback runs at `fastRate`, and above `rateAbove` at `gentleRate`. */
  fastAbove: 0.1,
  rateAbove: 0.03,
  fastRate: 1.15,
  gentleRate: 1.05,
};

/**
 * How long until a decoded frame is actually on screen, or null if its expected
 * display time is implausibly far ahead. requestVideoFrameCallback reports the
 * next vsync, so a frame read in the same tick is up to one refresh early: count
 * that wait into its age instead of reporting no frame at all.
 * @param {number} expectedAt expected display time (ms, performance clock)
 * @param {number} now
 */
export function presentationWait(expectedAt, now) {
  if (!Number.isFinite(expectedAt) || !Number.isFinite(now)) return null;
  const wait = expectedAt - now;
  return wait > 50 ? null : Math.max(0, wait);
}

/**
 * Decide how to chase the live edge.
 * @param {number} currentTime playback position (s)
 * @param {{start: number, end: number} | null} last newest buffered range
 * @param {{now: number, lastSeekAt: number, seeking: boolean, frame?: number}} state monotonic ms clock;
 *   `frame` is the newest fragment's frame duration (s)
 * @returns {{seekTo: number | null, rate: number, lead: number}}
 */
export function chaseLiveEdge(currentTime, last, { now = 0, lastSeekAt = -Infinity, seeking = false, frame = LIVE_EDGE.frame } = {}) {
  if (!last || !Number.isFinite(currentTime) || !(last.end > last.start)) return { seekTo: null, rate: 1, lead: 0 };
  const lead = Math.max(0, last.end - currentTime);
  // Before the newest range (a skipped-fragment gap) or past its end: playback is not on decodable data.
  const gap = currentTime < last.start || currentTime > last.end + 0.1;
  const mayseek = !seeking && now - lastSeekAt >= LIVE_EDGE.cooldownMs;
  // One frame, bounded so a malformed duration can neither skip ahead nor hide real lag.
  const edge = Number.isFinite(frame) ? Math.min(Math.max(frame, 0.01), 0.2) : LIVE_EDGE.frame;
  if (mayseek && (gap || lead > LIVE_EDGE.seekAbove)) {
    // Land at the start of the newest frame.
    return { seekTo: Math.max(last.start, last.end - edge), rate: 1, lead };
  }
  const late = lead - edge;
  const rate = late > LIVE_EDGE.fastAbove ? LIVE_EDGE.fastRate : late > LIVE_EDGE.rateAbove ? LIVE_EDGE.gentleRate : 1;
  return { seekTo: null, rate, lead };
}
