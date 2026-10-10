/** Minimal headset HUD: a tiny mode/source chip, a 1-3 word fault cue only when something needs
 * the operator, and an icon controller diagram on entry. Display only; no safety decision here.
 * Long telemetry stays on the 2D page. */

/** Truthful source labels, shortest form that keeps the meaning. */
export const SOURCE_CHIP = {
  LIVE: { text: "LIVE", fill: "#14532d", ink: "#bbf7d0" },
  UNVERIFIED: { text: "NOT VERIFIED", fill: "#5b1f1f", ink: "#ffd2ca" },
  SIMULATED: { text: "SIMULATED", fill: "#7c4a03", ink: "#ffe3a3" },
  REPLAY: { text: "REPLAY", fill: "#4c1d95", ink: "#e9d5ff" },
};
export const TONE_COLOR = { go: "#3ddc84", wait: "#ffc233", stop: "#ff4d4d" };

/** [pattern on the stop code or cause, icon, words]; first match wins. */
const CUES = [
  [/^Waiting for fresh control permit$/, "wait", "WAIT"],
  [/^(forward_blocked|estop)$/, "blocked", "BLOCKED"],
  [/^target_(missing|lost)$/, "target", "TARGET LOST"],
  [/^detector_stale$/, "eye", "DETECTOR SLOW"],
  [/^scan_finished$/, "check", "SCAN DONE"],
  [/scan/i, "scan", "SCAN STOPPED"],
  [/voice/i, "mic", "LISTENING"],
  [/camera|video|frame/i, "camera", "NO VIDEO"],
  [/XR |tracking|controller input|Controller changed|controller_disconnected/i, "controller", "SHOW CONTROLLER"],
  [/Rover unavailable|serial|motor|uno|rover_disabled|shutdown/i, "motor", "ROVER OFF"],
  [/permit|\blease|sequence|link|disconnect|status stale|queue|session/i, "link", "LINK LOST"],
  [/Headset|Display|Window|Browser|Viewer|Input or display/i, "pause", "PAUSED"],
];

/**
 * Turn explainStop() output into a headset cue, or null when nothing needs explaining.
 * A stop whose only remaining action is "release, then hold" says so, keeping the cause icon.
 * @param {{ code: string, cause: string, recovery: string, tone: "stop" | "warn" } | null} stop
 * @param {boolean} unsafe a live readiness condition currently blocks motion
 * @returns {{ tone: "wait" | "stop", icon: string, words: string } | null}
 */
export function cueFor(stop, unsafe = false) {
  if (!stop) return null;
  const hit = CUES.find(([pattern]) => pattern.test(stop.code) || pattern.test(stop.cause));
  const release = /^Release, then hold/.test(stop.recovery);
  const icon = hit?.[1] ?? (release ? "trigger" : "stop");
  if (release && !unsafe) return { tone: "wait", icon, words: "RELEASE TRIGGER" };
  if (hit?.[1] === "wait") return { tone: "wait", icon, words: hit[2] };
  return { tone: unsafe || stop.tone === "stop" ? "stop" : "wait", icon, words: hit?.[2] ?? "STOPPED" };
}

/** Green moving, amber waiting on the operator or a fresh permit, red when something blocks motion. */
export function motionTone({ moving, unsafe, cue }) {
  if (moving && !cue) return "go";
  if (unsafe || cue?.tone === "stop") return "stop";
  return "wait";
}

function round(c, x, y, w, h, r) { c.beginPath(); c.roundRect(x, y, w, h, r); }

/** Simple stroked pictograms in a box of radius r around (x, y). */
export function drawIcon(c, name, x, y, r, color) {
  c.save();
  c.strokeStyle = c.fillStyle = color; c.lineWidth = Math.max(3, r / 5); c.lineCap = c.lineJoin = "round";
  const line = (...points) => { c.beginPath(); c.moveTo(points[0], points[1]); for (let i = 2; i < points.length; i += 2) c.lineTo(points[i], points[i + 1]); c.stroke(); };
  const circle = (cx, cy, radius, fill = false) => { c.beginPath(); c.arc(cx, cy, radius, 0, Math.PI * 2); fill ? c.fill() : c.stroke(); };
  switch (name) {
    case "manual": // steering wheel
      circle(x, y, r * .85); circle(x, y, r * .22, true);
      line(x - r * .85, y, x - r * .22, y); line(x + r * .22, y, x + r * .85, y); line(x, y + r * .22, x, y + r * .85); break;
    case "auto": // compass needle
      circle(x, y, r * .85);
      c.beginPath(); c.moveTo(x, y - r * .65); c.lineTo(x + r * .25, y); c.lineTo(x, y + r * .65); c.lineTo(x - r * .25, y); c.closePath(); c.fill(); break;
    case "camera":
      round(c, x - r * .85, y - r * .5, r * 1.2, r, r * .15); c.stroke();
      line(x + r * .35, y - r * .2, x + r * .85, y - r * .5, x + r * .85, y + r * .5, x + r * .35, y + r * .2);
      line(x - r * .9, y + r * .7, x + r * .9, y - r * .7); break;
    case "link": // signal bars with a slash
      for (let i = 0; i < 3; i++) { const h = r * (.5 + .4 * i); c.fillRect(x - r * .8 + i * r * .6, y + r * .7 - h, r * .35, h); }
      c.strokeStyle = "#000"; c.lineWidth *= 2.2; line(x - r * .9, y + r * .8, x + r * .9, y - r * .8);
      c.strokeStyle = color; c.lineWidth /= 2.2; line(x - r * .9, y + r * .8, x + r * .9, y - r * .8); break;
    case "controller":
      round(c, x - r * .35, y - r * .85, r * .7, r * 1.7, r * .35); c.stroke(); circle(x, y - r * .35, r * .18, true);
      c.beginPath(); c.arc(x, y - r * .4, r * .9, Math.PI * 1.15, Math.PI * 1.85); c.stroke(); break;
    case "motor": // bolt
      c.beginPath(); c.moveTo(x + r * .2, y - r * .9); c.lineTo(x - r * .5, y + r * .1); c.lineTo(x, y + r * .1);
      c.lineTo(x - r * .2, y + r * .9); c.lineTo(x + r * .5, y - r * .1); c.lineTo(x, y - r * .1); c.closePath(); c.fill(); break;
    case "blocked": // wall with a stop bar
      c.fillRect(x + r * .45, y - r * .85, r * .3, r * 1.7); line(x - r * .85, y, x + r * .15, y); line(x - r * .2, y - r * .35, x + r * .15, y, x - r * .2, y + r * .35); break;
    case "target":
      circle(x, y, r * .7); circle(x, y, r * .2, true); line(x, y - r * .95, x, y - r * .5); line(x, y + r * .5, x, y + r * .95);
      line(x - r * .95, y, x - r * .5, y); line(x + r * .5, y, x + r * .95, y); break;
    case "eye":
      c.beginPath(); c.moveTo(x - r * .9, y); c.quadraticCurveTo(x, y - r * .9, x + r * .9, y); c.quadraticCurveTo(x, y + r * .9, x - r * .9, y); c.stroke();
      circle(x, y, r * .25, true); break;
    case "scan": // circular arrow
      c.beginPath(); c.arc(x, y, r * .7, Math.PI * .2, Math.PI * 1.8); c.stroke();
      line(x + r * .25, y - r * .55, x + r * .6, y - r * .4, x + r * .55, y - r * .05); break;
    case "check": line(x - r * .7, y, x - r * .15, y + r * .55, x + r * .75, y - r * .6); break;
    case "mic":
      round(c, x - r * .3, y - r * .85, r * .6, r * 1.1, r * .3); c.fill();
      c.beginPath(); c.arc(x, y - r * .1, r * .55, 0, Math.PI); c.stroke(); line(x, y + r * .45, x, y + r * .85); break;
    case "pause": c.fillRect(x - r * .55, y - r * .7, r * .38, r * 1.4); c.fillRect(x + r * .17, y - r * .7, r * .38, r * 1.4); break;
    case "wait": // hourglass
      line(x - r * .6, y - r * .85, x + r * .6, y - r * .85, x - r * .6, y + r * .85, x + r * .6, y + r * .85, x - r * .6, y - r * .85); break;
    case "trigger": // finger releasing a trigger: arrow up off a curved trigger
      c.beginPath(); c.arc(x - r * .1, y + r * .55, r * .5, Math.PI, Math.PI * 1.9); c.stroke();
      line(x + r * .45, y + r * .3, x + r * .45, y - r * .85); line(x + r * .15, y - r * .55, x + r * .45, y - r * .85, x + r * .75, y - r * .55); break;
    default: // stop octagon
      c.beginPath();
      for (let i = 0; i < 8; i++) { const a = Math.PI / 8 + i * Math.PI / 4; c.lineTo(x + Math.cos(a) * r * .85, y + Math.sin(a) * r * .85); }
      c.closePath(); c.fill();
  }
  c.restore();
}

/** Tiny mode/source chip: tone dot + MANUAL/AUTO, then a truthful source tag. */
export function drawChip(c, width, height, { mode, source, tone }) {
  const chip = SOURCE_CHIP[source] ?? SOURCE_CHIP.UNVERIFIED;
  c.clearRect(0, 0, width, height);
  c.textBaseline = "middle"; c.textAlign = "left";
  c.font = "bold 44px system-ui";
  const modeText = mode === "auto" ? "AUTO" : "MANUAL";
  const sourceWidth = c.measureText(chip.text).width + 44;
  const modeWidth = height + 16 + c.measureText(modeText).width + 28;
  const x = Math.max(0, (width - modeWidth - sourceWidth - 12) / 2), y = height / 2;
  // AUTO gets a cyan pill so "the rover is driving itself" reads at a glance.
  c.fillStyle = mode === "auto" ? "#0e5566f0" : "#0b141ccc"; round(c, x, 4, modeWidth, height - 8, (height - 8) / 2); c.fill();
  if (mode === "auto") { c.strokeStyle = "#70f2ff"; c.lineWidth = 4; c.stroke(); }
  c.fillStyle = TONE_COLOR[tone] ?? TONE_COLOR.wait;
  c.beginPath(); c.arc(x + height / 2, y, height * .22, 0, Math.PI * 2); c.fill();
  drawIcon(c, mode === "auto" ? "auto" : "manual", x + height + 6, y, height * .3, "#f3f9fc");
  c.fillStyle = "#f3f9fc"; c.fillText(modeText, x + height + 32, y + 2);
  const sx = x + modeWidth + 12;
  c.fillStyle = chip.fill; round(c, sx, 4, sourceWidth, height - 8, 18); c.fill();
  c.strokeStyle = chip.ink; c.lineWidth = 3; c.stroke();
  c.fillStyle = chip.ink; c.fillText(chip.text, sx + 22, y + 2);
}

/** One icon + 1-3 words, coloured by severity. Clears the canvas when there is no cue. */
export function drawCue(c, width, height, cue) {
  c.clearRect(0, 0, width, height);
  if (!cue) return;
  const color = TONE_COLOR[cue.tone];
  c.textBaseline = "middle"; c.textAlign = "left"; c.font = "bold 52px system-ui";
  const w = Math.min(width - 8, height + 24 + c.measureText(cue.words).width + 36), x = (width - w) / 2;
  c.fillStyle = cue.tone === "stop" ? "#3b0d0df0" : "#3a2a05f0"; round(c, x, 4, w, height - 8, 24); c.fill();
  c.strokeStyle = color; c.lineWidth = 5; c.stroke();
  drawIcon(c, cue.icon, x + height / 2 + 8, height / 2, height * .34, color);
  c.fillStyle = "#ffffff"; c.fillText(cue.words, x + height + 20, height / 2 + 3, w - height - 40);
}

/** Right-controller diagram: each button gets an icon-sized callout of one or two words. */
export function controllerHintLabels(mode) {
  return [
    { part: "trigger", words: "HOLD = GO" },
    { part: "stick", words: mode === "auto" ? "FLY" : "DRIVE" },
    { part: "a", words: mode === "auto" ? "A = MANUAL" : "A = AUTO" },
    { part: "b", words: "STOP" },
    { part: "grip", words: mode === "auto" ? "SET GOAL" : "PICK PERSON" },
    { part: "hold-a", words: "HOLD A = MENU" },
  ];
}

export function drawControllerHint(c, width, height, { mode }) {
  c.clearRect(0, 0, width, height);
  c.fillStyle = "#07151ee8"; round(c, 0, 0, width, height, 40); c.fill();
  const cx = width * .5, cy = height * .52, s = height / 640;
  c.save(); c.lineCap = c.lineJoin = "round";
  // Controller body (front view, Quest right): ring on top, grip body below.
  c.strokeStyle = "#9fb3c2"; c.lineWidth = 10 * s;
  c.beginPath(); c.ellipse(cx, cy - 150 * s, 150 * s, 110 * s, 0, 0, Math.PI * 2); c.stroke();
  c.fillStyle = "#1d2a35"; round(c, cx - 95 * s, cy - 130 * s, 190 * s, 330 * s, 70 * s); c.fill(); c.stroke();
  const parts = {
    stick: [cx - 30 * s, cy - 70 * s, 34 * s, "#cfe8f5"],
    a: [cx + 38 * s, cy - 10 * s, 24 * s, "#70f2ff"],
    b: [cx + 50 * s, cy - 78 * s, 24 * s, "#ff6b6b"],
    trigger: [cx - 92 * s, cy - 20 * s, 22 * s, "#3ddc84"],
    grip: [cx - 92 * s, cy + 110 * s, 22 * s, "#ffc233"],
  };
  for (const [name, [x, y, r, color]] of Object.entries(parts)) {
    c.fillStyle = color; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
    if (name === "a" || name === "b") {
      c.fillStyle = "#07151e"; c.font = `bold ${Math.round(r * 1.3)}px system-ui`; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(name.toUpperCase(), x, y + 2 * s);
    }
  }
  const callouts = {
    trigger: [parts.trigger, cx - 470 * s, cy - 120 * s, "right"],
    grip: [parts.grip, cx - 470 * s, cy + 140 * s, "right"],
    stick: [parts.stick, cx - 470 * s, cy - 250 * s, "right"],
    b: [parts.b, cx + 470 * s, cy - 230 * s, "left"],
    a: [parts.a, cx + 470 * s, cy - 40 * s, "left"],
    "hold-a": [parts.a, cx + 470 * s, cy + 150 * s, "left"],
  };
  c.font = `bold ${Math.round(46 * s)}px system-ui`; c.textBaseline = "middle";
  for (const { part, words } of controllerHintLabels(mode)) {
    const [[px, py, , color], tx, ty, side] = callouts[part];
    c.strokeStyle = color; c.lineWidth = 4 * s;
    const end = side === "right" ? tx + 16 * s : tx - 16 * s;
    c.beginPath(); c.moveTo(px, py); c.lineTo(end, ty); c.stroke();
    c.fillStyle = color; c.textAlign = side; c.fillText(words, tx, ty);
  }
  c.restore();
}
