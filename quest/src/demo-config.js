/** One reversible switch for the judged demo (captain's decision, 2026-10-10): default ON, `?demo=0`
 * restores every client guard. It only relaxes client-side checks while immersive; permits, leases,
 * link loss and B / STOP are unchanged, `video_fresh` stays truthful, and the rover's own camera checks
 * follow its separate `--demo-mode` (FARSIGHT_DEMO_MODE).
 *
 * Off in demo mode (XR only):
 * - the client 500 ms camera-stale motion block (a small VIDEO STALE warning shows instead);
 * - stops on a right-controller change or a tracking blip shorter than DEMO_BLIP_MS;
 * - the forced trigger re-press after a transient stop (the client sends one release itself, then resumes).
 * Added in demo mode: AUTO is hands-free (A starts Explore heartbeats without the trigger; A or B ends it). */

/** @param {string} [search] */
export function demoMode(search = globalThis.location?.search ?? "") {
  return new URLSearchParams(search).get("demo") !== "0";
}
export const DEMO_MODE = demoMode();
/** A right controller missing or untracked for longer than this still stops. */
export const DEMO_BLIP_MS = 1000;
