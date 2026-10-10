/** Capture order of two camera.jpg responses from their X-Frame-Source and X-Frame-Id headers.
 * Request start/arrival order is not capture order, and conservative age bounds differ with each
 * request's delay, so within one source the frame ordinal decides. A large backwards jump is a
 * capture restart (the counter reset), which only the capture-age bound can rank. */

/** Backwards jumps larger than this are treated as a restarted capture counter. */
export const RESTART_GAP = 30;

/**
 * @param {string} source candidate X-Frame-Source
 * @param {string | null} id candidate X-Frame-Id
 * @param {string} shownSource displayed frame's source
 * @param {string} shownId displayed frame's id
 * @returns {1 | -1 | 0} 1 newer, -1 older or duplicate (keep the displayed frame), 0 unknown (compare capture age)
 */
export function frameOrder(source, id, shownSource, shownId) {
  if (source !== shownSource) return 0;
  if (id === shownId) return -1;
  const next = Number(id), shown = Number(shownId);
  if (!id || !shownId || !Number.isSafeInteger(next) || !Number.isSafeInteger(shown) || next < 0 || shown < 0) return 0;
  if (next > shown) return 1;
  return shown - next > RESTART_GAP ? 0 : -1;
}
