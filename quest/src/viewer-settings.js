/** Viewer settings that survive reloads and can be deep-linked into the headset:
 * `?mount=fixed|servo&h264=0|1&voice=0|1&diag=0|1`. An explicit query value wins
 * over the saved choice, which wins over the default. Query values are saved, so
 * one deep link sets the headset up for later plain loads. The rover's /health
 * `camera_pan` only picks the default mount; it never overrides a choice.
 * Display and convenience only: nothing here gates motion. */
import { demoMode } from "./demo-config.js";
export const SETTINGS_KEY = "farsight.viewer";

const bit = value => value === "1" || value === true ? true : value === "0" || value === false ? false : undefined;
const PARSERS = {
  mount: value => value === "fixed" || value === "servo" ? value : undefined,
  h264: bit, voice: bit, diag: bit,
};
/** @type {ViewerSettings} */
export const DEFAULTS = { mount: "servo", h264: false, voice: true, diag: false };

/** @param {Pick<Storage, "getItem"> | null | undefined} storage */
export function readSaved(storage) {
  try {
    const saved = JSON.parse(storage?.getItem(SETTINGS_KEY) ?? "null");
    if (!saved || typeof saved !== "object") return {};
    return Object.fromEntries(Object.keys(PARSERS)
      .map(name => [name, PARSERS[name](saved[name])]).filter(([, value]) => value !== undefined));
  } catch { return {}; }
}

/** @typedef {{ mount: "fixed" | "servo", h264: boolean, voice: boolean, diag: boolean }} ViewerSettings */
/** "rover" marks a mount or video mode chosen from /health; it is never saved. */
/** "fallback" marks an auto-selected H.264 that produced no frame and reverted to JPEG. */
/** @typedef {Record<keyof ViewerSettings, "query" | "saved" | "default" | "rover" | "fallback">} SettingOrigins */

/** @param {string} search location.search @param {Record<string, unknown>} saved from readSaved
 * @returns {{ values: ViewerSettings, origin: SettingOrigins, explicit: Partial<ViewerSettings> }} */
export function resolveSettings(search, saved = {}) {
  const query = new URLSearchParams(search);
  /** @type {any} */ const values = { ...DEFAULTS, voice: demoMode(search) }, origin = {}, explicit = {};
  for (const name of Object.keys(PARSERS)) {
    const fromQuery = PARSERS[name](query.get(name)), fromSaved = PARSERS[name](saved[name]);
    if (fromQuery !== undefined) { values[name] = explicit[name] = fromQuery; origin[name] = "query"; }
    else if (fromSaved !== undefined) { values[name] = fromSaved; origin[name] = "saved"; }
    else origin[name] = "default";
  }
  return { values, origin, explicit };
}

/** Mount to use when nothing was chosen: fixed unless /health says a pan servo exists.
 * Older servers without `camera_pan` keep the servo default. */
export function defaultMount(health) {
  return health?.camera_pan === false ? "fixed" : health?.camera_pan === true ? "servo" : null;
}

/** H.264 when nothing was chosen: on when the rover's /health offers enabled H.264 video
 * (JPEG stays the fallback and an explicit choice). Unknown health keeps the default. */
export function defaultH264(health) {
  const video = health?.video;
  return video?.enabled === true ? true : video?.enabled === false ? false : null;
}

/** @param {Pick<Storage, "getItem" | "setItem"> | null | undefined} storage @returns {boolean} saved */
export function saveSettings(storage, changes) {
  try {
    if (!storage) return false;
    storage.setItem(SETTINGS_KEY, JSON.stringify({ ...readSaved(storage), ...changes }));
    return true;
  } catch { return false; }
}

/** Page storage, or null where reading it throws (private mode, blocked site data). */
export function pageStorage() {
  try { return window.localStorage; } catch { return null; }
}

/** Settings for this page load; query choices are saved for the next one. */
export function loadSettings(search = location.search, storage = pageStorage()) {
  const resolved = resolveSettings(search, readSaved(storage));
  if (Object.keys(resolved.explicit).length) saveSettings(storage, resolved.explicit);
  return resolved;
}
