/** Where an /ask answer came from, so judges can tell the offline summary from the local vision model. */
const SOURCES = {
  "local-observation-summary": "Offline observation summary",
  "local-vision-model": "Local vision model",
};

export function askLabel(reply) {
  const name = SOURCES[reply?.model] ?? "Rover assistant";
  const parts = [name];
  const ms = Number(reply?.ms);
  if (Number.isFinite(ms) && ms >= 0) parts.push(`${Math.round(ms)} ms`);
  parts.push("on rover, no internet");
  if (reply?.simulated === true) parts.push("simulated view");
  return parts.join(" · ");
}

/** Answer text followed by its source label, plus the rover's warning when it fell back. */
export function askText(reply) {
  const lines = [String(reply?.answer ?? "No answer returned")];
  if (typeof reply?.assistant_warning === "string") lines.push(reply.assistant_warning);
  lines.push(`(${askLabel(reply)})`);
  return lines.join("\n");
}
