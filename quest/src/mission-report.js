/** Text lines for the mission report dialog from GET /mission/report.
 * `summary`, `timeline` and per-encounter `first_range_cm` are optional:
 * older servers render the base report without them. */

const LOST_LABELS = { rover_link: "Motor link lost", controller: "Controller lost", camera: "Camera lost" };
const FLAG_LABELS = {
  estop: "E-stop", stop: "Stop", detection: "Seen",
  rover_link_lost: "Motor link lost", rover_link_restored: "Motor link back",
  controller_lost: "Controller lost", controller_restored: "Controller back",
  camera_lost: "Camera lost", camera_restored: "Camera back",
};

const finite = value => typeof value === "number" && Number.isFinite(value);

/** Mission clock offset, e.g. "+1:05". */
export function missionTime(ts, startedAt) {
  if (!finite(ts) || !finite(startedAt)) return "+?";
  const seconds = Math.max(0, Math.round(ts - startedAt));
  return `+${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

const range = cm => (finite(cm) ? `${Math.round(cm)} cm` : "no range");

/** Headline evidence lines: min range, e-stops, stop reasons, losses, classes. */
export function summaryLines(report) {
  const summary = report?.summary;
  if (!summary || typeof summary !== "object") return [];
  const start = report.started_at;
  const lines = [];
  lines.push(finite(summary.min_range_cm)
    ? `Min range ${Math.round(summary.min_range_cm)} cm at ${missionTime(summary.min_range_at, start)}`
    : "Min range: no ultrasonic reading");
  lines.push(`E-stops ${Number(summary.estop_count) || 0}`);
  const reasons = Object.entries(summary.stop_reasons ?? {})
    .filter(([, entry]) => entry && finite(entry.count))
    .sort((a, b) => b[1].count - a[1].count).slice(0, 5);
  if (reasons.length) {
    lines.push(`Stops: ${reasons.map(([reason, entry]) =>
      `${reason} ×${entry.count} (${missionTime(entry.first_ts, start)}–${missionTime(entry.last_ts, start)})`).join(", ")}`);
  }
  for (const [kind, label] of Object.entries(LOST_LABELS)) {
    const lost = summary.lost?.[kind];
    if (lost && Number(lost.count) > 0) {
      const open = Array.isArray(lost.intervals) && lost.intervals.some(entry => entry?.end === null);
      lines.push(`${label} ${lost.count}× · ${Number(lost.total_s) || 0}s total${open ? " · still lost" : ""}`);
    }
  }
  const classes = Object.entries(summary.detections_by_class ?? {}).filter(([, count]) => finite(count));
  if (classes.length) lines.push(`Detections: ${classes.map(([cls, count]) => `${cls} ${count}`).join(", ")}`);
  return lines;
}

/** One track line with time first seen, duration in view and range at first sight. */
export function encounterLine(entry, startedAt) {
  const parts = [`${String(entry?.cls ?? "object")} · track ${entry?.id ?? "?"}`,
    `${Math.round((Number(entry?.conf) || 0) * 100)}%`, String(entry?.source ?? "unknown source")];
  if (finite(entry?.first_seen)) {
    const seen = finite(entry.last_seen) ? Math.max(0, Math.round(entry.last_seen - entry.first_seen)) : 0;
    parts.push(`seen ${missionTime(entry.first_seen, startedAt)} for ${seen}s`);
  }
  if ("first_range_cm" in (entry ?? {})) parts.push(`range ${range(entry.first_range_cm)} at first sight`);
  return parts.join(" · ");
}

/** Most recent flagged timeline events, newest first. */
export function timelineLines(report, limit = 12) {
  const timeline = Array.isArray(report?.timeline) ? report.timeline : [];
  return timeline.slice(-limit).reverse().map(event => {
    const label = FLAG_LABELS[event?.kind] ?? String(event?.kind ?? "event");
    const detail = event?.kind === "stop" ? ` ${event.reason}`
      : event?.kind === "detection" ? ` ${event.cls}` : "";
    const where = "range_cm" in (event ?? {}) ? ` · ${range(event.range_cm)}` : "";
    const lost = finite(event?.lost_s) ? ` after ${event.lost_s}s` : "";
    return `${missionTime(event?.ts, report.started_at)} ${label}${detail}${where}${lost}`;
  });
}
