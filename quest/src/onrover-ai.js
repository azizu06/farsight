// "On-rover AI" HUD line from GET /health `models` (jetson/models.py): the local models
// behind voice questions and their latest measured stage latency on the Jetson.
const STAGES = [["stt", model => model.name], ["vision", model => model.name], ["tts", () => "Piper"]];
const seconds = ms => (Number.isFinite(ms) ? `${(ms / 1000).toFixed(1)} s` : "–");

/** @param {any} models @returns {string} */
export function onRoverAiLine(models) {
  if (!Array.isArray(models)) return "";
  const parts = STAGES.map(([role, label]) => {
    const model = models.find(item => item?.role === role && item.loaded === true);
    return model ? `${label(model)} ${seconds(model.last_ms)}` : "";
  }).filter(Boolean);
  return parts.length ? `On-rover AI: ${parts.join(" · ")}` : "";
}
