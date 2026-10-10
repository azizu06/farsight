/** Rescue drill HUD: timer, victims counter, end screen and arrow trail home.
 * The rover scores the drill (GET/POST /mission/drill); this module only renders it. */

export const DRILL_TRAINS = "Trains responders practising room search: find every victim fast, avoid collisions, and know the way back out.";

const finite = value => typeof value === "number" && Number.isFinite(value);
const points = value => Array.isArray(value)
  ? value.filter(p => p && finite(p.x) && finite(p.y)).slice(-600).map(p => ({ x: p.x, y: p.y }))
  : [];

export function parseDrill(json) {
  if (!json || typeof json.active !== "boolean" || !(json.remaining_s === null || finite(json.remaining_s))
      || (json.found !== undefined && !finite(json.found))) throw new Error("Invalid drill status");
  const summary = json.summary && finite(json.summary.found) ? json.summary : null;
  return {
    active: json.active, remaining_s: json.remaining_s,
    face_detector: typeof json.face_detector === "boolean" ? json.face_detector : null,
    face_error: typeof json.face_error === "string" ? json.face_error : null,
    found: json.found ?? 0, total: finite(json.total) ? json.total : null, estops: finite(json.estops) ? json.estops : 0,
    route_home: points(json.route_home), breadcrumb: points(json.breadcrumb),
    summary: summary && {
      time_s: finite(summary.time_s) ? summary.time_s : 0, found: summary.found,
      total: finite(summary.total) ? summary.total : null,
      estops_avoided: finite(summary.estops_avoided) ? summary.estops_avoided : 0,
    },
  };
}

export const NO_FACE_MODEL = "No face model: victims cannot be found";
/** Warning only when the rover explicitly reports the face pass missing; older rovers omit the field. */
export const faceWarning = drill => (drill?.face_detector === false ? NO_FACE_MODEL : "");

const victims = (found, total) => `${found}${total === null ? "" : "/" + total}`;
const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}`;

/** One-line status; `ageMs` is the time since the server status arrived. */
export function drillLine(drill, ageMs) {
  if (!drill || drill.remaining_s === null) return faceWarning(drill);
  if (ageMs > 3000 && drill.active) return "Rescue drill status stale";
  const left = Math.max(0, drill.remaining_s - ageMs / 1000);
  const head = drill.active && left > 0 ? `Rescue drill ${Math.ceil(left)}s` : "Rescue drill complete";
  return [`${head} · victims ${victims(drill.found, drill.total)} · e-stops ${drill.estops}`, faceWarning(drill)].filter(Boolean).join(" · ");
}

export function endLines(drill) {
  const s = drill?.summary;
  if (!s) return null;
  return [`Time ${clock(s.time_s)}`, `Victims found ${victims(s.found, s.total)}`,
    `Collisions avoided (e-stops) ${s.estops_avoided}`, "Follow the arrows home · route is approximate (dead reckoning)"];
}

/** Rover frame (x forward, y left, metres) to screen, forward up, fitted into `box`. */
function projector(route, box) {
  const xs = route.map(p => p.x), ys = route.map(p => p.y);
  const margin = Math.min(box.w, box.h) * .12;
  const spanX = Math.max(1, Math.max(...xs) - Math.min(...xs)), spanY = Math.max(1, Math.max(...ys) - Math.min(...ys));
  const scale = Math.min((box.h - 2 * margin) / spanX, (box.w - 2 * margin) / spanY);
  const midX = (Math.max(...xs) + Math.min(...xs)) / 2, midY = (Math.max(...ys) + Math.min(...ys)) / 2;
  return p => ({ x: box.x + box.w / 2 - (p.y - midY) * scale, y: box.y + box.h / 2 - (p.x - midX) * scale });
}

/** Arrow heads every `spacing` px along route_home (rover first, start last), pointing toward the start. */
export function routeArrows(route, box, spacing = 28) {
  if (route.length < 2) return [];
  const project = projector(route, box), screen = route.map(project), arrows = [];
  let carry = spacing / 2;
  for (let i = 1; i < screen.length; i++) {
    const a = screen[i - 1], b = screen[i], length = Math.hypot(b.x - a.x, b.y - a.y);
    if (!length) continue;
    const angle = Math.atan2(b.y - a.y, b.x - a.x);
    for (; carry <= length; carry += spacing) {
      arrows.push({ x: a.x + (b.x - a.x) * carry / length, y: a.y + (b.y - a.y) * carry / length, angle });
    }
    carry -= length;
  }
  return arrows;
}

function wrap(text, width) {
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    if (line && (line + " " + word).length > width) { lines.push(line); line = word; }
    else line = line ? line + " " + word : word;
  }
  return line ? [...lines, line] : lines;
}

/** Draws the drill panel (live counter or end screen, plus the route home) on a 2D canvas. */
export function drawDrill(c, drill, ageMs, width, height) {
  c.clearRect(0, 0, width, height);
  c.fillStyle = "#111b25ee"; c.fillRect(0, 0, width, height);
  const end = endLines(drill), u = width / 512;
  c.fillStyle = "#ffc779"; c.font = `bold ${26 * u}px system-ui`;
  c.fillText(end ? "RESCUE DRILL COMPLETE" : "RESCUE DRILL", 20 * u, 38 * u);
  c.fillStyle = "#dce5ee";
  if (end) {
    c.font = `${22 * u}px system-ui`;
    end.slice(0, 3).forEach((line, i) => c.fillText(line, 20 * u, (74 + i * 30) * u));
  } else {
    const left = drill.remaining_s === null ? 0 : Math.max(0, Math.ceil(drill.remaining_s - ageMs / 1000));
    c.font = `bold ${44 * u}px system-ui`; c.fillText(`${left}s`, 20 * u, 96 * u);
    c.font = `bold ${30 * u}px system-ui`; c.fillText(`Victims ${victims(drill.found, drill.total)}`, 190 * u, 92 * u);
    c.font = `${20 * u}px system-ui`; c.fillText(`E-stops ${drill.estops}${ageMs > 3000 ? " · status stale" : ""}`, 20 * u, 130 * u);
  }
  if (faceWarning(drill)) { c.fillStyle = "#ff8a80"; c.font = `bold ${17 * u}px system-ui`; c.fillText(NO_FACE_MODEL, 20 * u, 156 * u); }
  const box = { x: 20 * u, y: 170 * u, w: width - 40 * u, h: 250 * u };
  c.strokeStyle = "#3a4b5c"; c.lineWidth = 1; c.beginPath();
  c.moveTo(box.x, box.y); c.lineTo(box.x + box.w, box.y); c.lineTo(box.x + box.w, box.y + box.h);
  c.lineTo(box.x, box.y + box.h); c.closePath(); c.stroke();
  c.fillStyle = "#a6edc4"; c.font = `${18 * u}px system-ui`;
  c.fillText("Route home (approximate)", box.x + 8 * u, box.y + 22 * u);
  const route = drill.route_home;
  if (route.length >= 2) {
    const project = projector(route, box), screen = route.map(project);
    c.save(); c.setLineDash([6 * u, 6 * u]); c.strokeStyle = "#5f7a93"; c.lineWidth = 2 * u; c.beginPath();
    screen.forEach((p, i) => (i ? c.lineTo(p.x, p.y) : c.moveTo(p.x, p.y))); c.stroke(); c.restore();
    c.fillStyle = "#ffc779";
    for (const arrow of routeArrows(route, box, 28 * u)) {
      c.save(); c.translate(arrow.x, arrow.y); c.rotate(arrow.angle); c.beginPath();
      c.moveTo(9 * u, 0); c.lineTo(-6 * u, -6 * u); c.lineTo(-6 * u, 6 * u); c.closePath(); c.fill(); c.restore();
    }
    const start = screen.at(-1), rover = screen[0];
    c.fillStyle = "#a6edc4"; c.beginPath(); c.arc(start.x, start.y, 7 * u, 0, Math.PI * 2); c.fill();
    c.fillText("START", start.x + 10 * u, start.y + 6 * u);
    c.fillStyle = "#8dc8ff"; c.beginPath(); c.arc(rover.x, rover.y, 6 * u, 0, Math.PI * 2); c.fill();
  } else {
    c.fillStyle = "#bdcbd9"; c.fillText("At start: no commanded motion yet", box.x + 8 * u, box.y + box.h / 2);
  }
  c.fillStyle = "#bdcbd9"; c.font = `${16 * u}px system-ui`;
  wrap(DRILL_TRAINS, 52).forEach((line, i) => c.fillText(line, 20 * u, (448 + i * 22) * u));
}

export class DrillHud {
  constructor({ fetchImpl = (...args) => fetch(...args), now = () => performance.now(), canvas = document.createElement("canvas") } = {}) {
    this.fetchImpl = fetchImpl; this.now = now; this.canvas = canvas;
    this.drill = null; this.at = -Infinity; this.busy = false;
    if (canvas) { canvas.width = 512; canvas.height = 512; canvas.className = "drill-hud"; }
  }
  async request(init) {
    if (this.busy) return "Drill request pending";
    this.busy = true;
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 2000);
    try {
      const response = await this.fetchImpl("/mission/drill", { cache: "no-store", ...init, signal: abort.signal });
      if (!response.ok) throw new Error(`Drill HTTP ${response.status}`);
      this.drill = parseDrill(await response.json()); this.at = this.now();
      return this.drill.remaining_s === null ? ["Ready to start", faceWarning(this.drill)].filter(Boolean).join(" · ") : drillLine(this.drill, 0);
    } catch (error) {
      return `Drill unavailable: ${String(error)}`;
    } finally { clearTimeout(timer); this.busy = false; }
  }
  start(duration = 90, victims = 3) {
    return this.request({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ duration_s: duration, victims }) });
  }
  refresh() { return this.request({}); }
  line(now) { return drillLine(this.drill, now - this.at); }
  /** Running drills and their end screen are shown; a never-started drill is hidden. */
  visible() { return !!this.drill && this.drill.remaining_s !== null && (this.drill.active || !!this.drill.summary); }
  draw(now) {
    if (!this.canvas || !this.visible()) return false;
    drawDrill(this.canvas.getContext("2d"), this.drill, now - this.at, this.canvas.width, this.canvas.height);
    return true;
  }
}
