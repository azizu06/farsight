/** Frame and link diagnostics for the operator overlay. Display only: nothing here
 * feeds the control gate, so a wrong reading can never enable motion. */

/** Quest 2 refresh budgets in milliseconds. */
export const QUEST2_BUDGET_MS = { 72: 1000 / 72, 90: 1000 / 90 };

/** Fixed-size ring of frame intervals and per-frame JS work; no allocation per frame. */
export class FrameStats {
  constructor(size = 256) {
    this.intervals = new Float64Array(size);
    this.work = new Float64Array(size);
    this.count = 0;
    this.next = 0;
    this.lastAt = -Infinity;
  }

  /** @param {number} now frame start (performance.now) @param {number} workMs JS time spent in the frame */
  record(now, workMs) {
    // A gap over 1 s is a pause (tab hidden, headset off), not a frame time.
    const interval = now - this.lastAt;
    this.lastAt = now;
    if (!(interval > 0 && interval < 1000)) return;
    this.intervals[this.next] = interval;
    this.work[this.next] = workMs;
    this.next = (this.next + 1) % this.intervals.length;
    this.count = Math.min(this.count + 1, this.intervals.length);
  }

  /** @returns {{ fps: number, frameP95: number, workP95: number, frames: number }} */
  summary() {
    if (!this.count) return { fps: 0, frameP95: 0, workP95: 0, frames: 0 };
    const intervals = Array.from(this.intervals.subarray(0, this.count));
    const mean = intervals.reduce((sum, value) => sum + value, 0) / this.count;
    return { fps: 1000 / mean, frameP95: percentile(intervals, 0.95),
      workP95: percentile(Array.from(this.work.subarray(0, this.count)), 0.95), frames: this.count };
  }
}

/** @param {number[]} values @param {number} q */
export function percentile(values, q) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
}

/** "ok" within the 72 Hz budget, "warn" within 1.5 frames, otherwise "bad". */
export function budgetTone(frameP95, budgetMs = QUEST2_BUDGET_MS[72]) {
  if (!(frameP95 > 0)) return "warn";
  return frameP95 <= budgetMs * 1.1 ? "ok" : frameP95 <= budgetMs * 1.5 ? "warn" : "bad";
}

const ms = (value, digits = 0) => Number.isFinite(value) ? `${value.toFixed(digits)} ms` : "n/a";

/**
 * @param {{ fps: number, frameP95: number, workP95: number, frames: number }} frame
 * @param {{ source: string, video: string, videoAgeMs: number, decodeMs: number | null, ackMs: number | null, linkMs: number | null | undefined, xr: boolean }} rover
 * @returns {string[]}
 */
export function diagnosticsLines(frame, rover) {
  return [
    `${frame.frames ? frame.fps.toFixed(1) : "--"} fps · frame p95 ${ms(frame.frameP95, 1)} · JS p95 ${ms(frame.workP95, 1)}`,
    `${rover.video} age ${ms(rover.videoAgeMs)} · decode ${ms(rover.decodeMs, 1)}`,
    `control RTT ${ms(rover.ackMs)} · server link ${ms(rover.linkMs ?? null)}`,
    `${rover.source} · ${rover.xr ? "immersive VR" : "desktop"} · budget 72 Hz ${QUEST2_BUDGET_MS[72].toFixed(1)} ms`,
  ];
}
