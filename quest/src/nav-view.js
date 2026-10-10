/** Viewer side of the optional `nav` status object (docs/interfaces.md, "Navigation"). Pure math, no three.js. */
export const NAV_STATES = new Set(["idle", "planning", "driving", "recovering", "arrived", "failed", "unavailable"]);
export const NAV_MODES = new Set(["explore", "seek", "goto"]);
export const MAX_PATH = 200;
export const MAX_FRONTIERS = 50;
// Voxel-map coordinates beyond this are corrupt, not a building-scale map.
const MAX_COORD = 10000;
/** Farthest floor point a controller ray may pick, metres from the ray origin. */
export const MAX_GOAL_RANGE = 40;

function xz(point) {
  if (!Array.isArray(point) || point.length < 2) return null;
  const [x, z] = point;
  return Number.isFinite(x) && Number.isFinite(z) && Math.abs(x) <= MAX_COORD && Math.abs(z) <= MAX_COORD ? [x, z] : null;
}

/** Validated copy of a status `nav` object, or null when absent or malformed. A path with any bad point is dropped whole: never draw a route the planner did not send. */
export function parseNav(raw) {
  if (!raw || typeof raw !== "object" || !NAV_STATES.has(raw.state)) return null;
  const goal = raw.goal && typeof raw.goal === "object" ? xz([raw.goal.x, raw.goal.z]) : null;
  const rawPath = Array.isArray(raw.path) ? raw.path.slice(0, MAX_PATH) : [];
  const path = rawPath.map(xz);
  return {
    state: raw.state,
    mode: NAV_MODES.has(raw.mode) ? raw.mode : null,
    goal: goal ? { x: goal[0], z: goal[1] } : null,
    path: path.every(Boolean) ? path : [],
    frontiers: (Array.isArray(raw.frontiers) ? raw.frontiers.slice(0, MAX_FRONTIERS) : []).map(xz).filter(Boolean),
    backend: raw.backend === "nav2" || raw.backend === "fallback" ? raw.backend : null,
    message: typeof raw.message === "string" ? raw.message.slice(0, 120) : "",
  };
}

/** The planner is working toward a goal; the drawn path is live. */
export function navActive(nav) {
  return !!nav && (nav.state === "planning" || nav.state === "driving" || nav.state === "recovering");
}

export function pathLength(path) {
  let total = 0;
  for (let i = 1; i < path.length; i++) total += Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]);
  return total;
}

/**
 * Flat ribbon on the floor plane for a voxel-map [x, z] path: two vertices per point,
 * mitred at corners (capped at twice the half-width so hairpins cannot spike).
 * `side` is -1/+1 across the width and `along` the distance from the start, for the glow shader.
 * Returns null when fewer than two distinct points remain.
 */
export function ribbon(path, halfWidth, y = 0) {
  const points = [];
  for (const point of path) {
    const last = points[points.length - 1];
    if (!last || Math.hypot(point[0] - last[0], point[1] - last[1]) > 1e-4) points.push(point);
  }
  if (points.length < 2) return null;
  const n = points.length;
  const positions = new Float32Array(n * 6), side = new Float32Array(n * 2), along = new Float32Array(n * 2);
  const indices = new Uint16Array((n - 1) * 6);
  const direction = (a, b) => { const dx = b[0] - a[0], dz = b[1] - a[1], length = Math.hypot(dx, dz); return [dx / length, dz / length]; };
  let distance = 0;
  for (let i = 0; i < n; i++) {
    const before = i > 0 ? direction(points[i - 1], points[i]) : null;
    const after = i < n - 1 ? direction(points[i], points[i + 1]) : null;
    let tx = (before?.[0] ?? 0) + (after?.[0] ?? 0), tz = (before?.[1] ?? 0) + (after?.[1] ?? 0);
    let length = Math.hypot(tx, tz);
    // A full reversal has no average tangent; fall back to the incoming segment.
    if (length < 1e-6) { [tx, tz] = before ?? after; length = 1; }
    tx /= length; tz /= length;
    // Left normal of the tangent in the x-z plane; the miter stretches it to keep segment width.
    const cos = after ? tx * after[0] + tz * after[1] : before ? tx * before[0] + tz * before[1] : 1;
    const miter = Math.min(halfWidth / Math.max(cos, 1e-6), halfWidth * 2);
    const nx = -tz * miter, nz = tx * miter;
    if (i > 0) distance += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    positions.set([points[i][0] + nx, y, points[i][1] + nz, points[i][0] - nx, y, points[i][1] - nz], i * 6);
    side.set([1, -1], i * 2);
    along.set([distance, distance], i * 2);
    if (i < n - 1) { const v = i * 2; indices.set([v, v + 1, v + 2, v + 1, v + 3, v + 2], i * 6); }
  }
  return { positions, side, along, indices, length: distance, points: n };
}

/** Where a ray meets the horizontal floor plane, as voxel-map {x, z}; null when it points level/up or lands out of range. */
export function floorHit(origin, direction, floorY = 0, maxRange = MAX_GOAL_RANGE) {
  if (![origin?.x, origin?.y, origin?.z, direction?.x, direction?.y, direction?.z, floorY].every(Number.isFinite)) return null;
  if (direction.y > -1e-3) return null;
  const t = (floorY - origin.y) / direction.y;
  if (!(t > 0)) return null;
  const x = origin.x + direction.x * t, z = origin.z + direction.z * t;
  return Math.hypot(x - origin.x, z - origin.z) <= maxRange ? { x, z } : null;
}

/** POST /nav/goal body: exactly {x, z} in voxel-map metres, rounded to millimetres. */
export function goalBody(point) {
  const mm = (value) => Math.round(value * 1000) / 1000 || 0; // never -0
  return { x: mm(point.x), z: mm(point.z) };
}

const BACKENDS = { nav2: "Nav2", fallback: "Fallback planner" };
const MODES = { explore: "Explore", seek: "Seek", goto: "Go-to" };

/** One-line HUD text for the nav object; empty when the server sends none. */
export function navLabel(nav) {
  if (!nav) return "";
  const parts = [`${BACKENDS[nav.backend] ?? "Nav"} · ${nav.mode ? MODES[nav.mode] + " " : ""}${nav.state}`];
  if (nav.path.length > 1 && navActive(nav)) parts.push(`${pathLength(nav.path).toFixed(1)} m path`);
  if (nav.frontiers.length) parts.push(`${nav.frontiers.length} frontier${nav.frontiers.length === 1 ? "" : "s"}`);
  if (nav.message) parts.push(nav.message);
  return parts.join(" · ");
}

/** Path colour by planner state: live states glow cyan/blue, recovery amber, outcomes green/red. */
export function navColor(state) {
  return { planning: "#8fb4ff", driving: "#43e8ff", recovering: "#ffc14d", arrived: "#6dff9c", failed: "#ff6b6b" }[state] ?? "#8a96a3";
}

/** POST a nav request. 202/200 carry the nav object; 409 and errors come back as text, never thrown. */
export async function postNav(path, body, fetchImpl = fetch, timeoutMs = 2000) {
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const response = await fetchImpl(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined, signal: abort.signal });
    let data = null;
    try { data = await response.json(); } catch { /* Non-JSON error bodies keep the status text. */ }
    if (!response.ok) {
      const detail = data?.message ?? data?.nav?.message ?? data?.detail;
      return { ok: false, status: response.status, nav: parseNav(data?.nav ?? data), error: typeof detail === "string" ? detail.slice(0, 120) : `HTTP ${response.status}` };
    }
    return { ok: true, status: response.status, nav: parseNav(data?.nav ?? data), error: null };
  } catch (error) {
    return { ok: false, status: 0, nav: null, error: abort.signal.aborted ? "timed out" : String(error?.message ?? error) };
  } finally { clearTimeout(timer); }
}
