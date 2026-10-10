/** Operator intent for Explore/Seek. The rover still needs a held deadman for every heartbeat. */
export class AutonomyIntent {
  constructor() { this.clear(); }
  clear() { this.mode = "teleop"; this.targetId = null; }
  select(id) {
    if (!Number.isSafeInteger(id) || id < 0) return false;
    this.mode = "seek"; this.targetId = id;
    return true;
  }
  /** Go-to after the server accepted a POST /nav/goal; the server owns the goal itself. */
  goto() { this.mode = "goto"; this.targetId = null; }
  /** A stop or release disarms Explore/Seek, but an accepted go-to goal stays armed so the operator can set it in the map view and then hold the deadman in the live view. Cancel, a stick override or another mode clears it. */
  release() { if (this.mode !== "goto") this.clear(); }
  toggleExplore() {
    if (this.mode === "explore") this.clear();
    else { this.mode = "explore"; this.targetId = null; }
  }
  /** Any stick input takes back control, matching the server's override latch. */
  stick(v, w) {
    if (!v && !w) return false;
    const active = this.mode !== "teleop";
    this.clear();
    return active;
  }
  /** Mode fields for one control message; a release never carries autonomy. */
  fields(deadman) {
    if (!deadman || this.mode === "teleop") return { mode: "teleop" };
    if (this.mode === "seek") return { mode: "seek", target_id: this.targetId };
    return { mode: this.mode };
  }
}

/** Smallest tracked person box containing a canvas-normalized point. */
export function pickTarget(boxes, x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  let best = null;
  for (const box of boxes) {
    if (box.cls !== "person" || !Number.isSafeInteger(box.id)) continue;
    if (x < box.x || x > box.x + box.w || y < box.y || y > box.y + box.h) continue;
    if (!best || box.w * box.h < best.w * best.h) best = box;
  }
  return best;
}

/** Client point to normalized canvas coordinates under `object-fit: contain`. */
export function containPoint(clientX, clientY, rect, width, height) {
  const scale = Math.min(rect.width / width, rect.height / height);
  if (!Number.isFinite(scale) || scale <= 0) return null;
  const left = rect.left + (rect.width - width * scale) / 2;
  const top = rect.top + (rect.height - height * scale) / 2;
  const x = (clientX - left) / (width * scale), y = (clientY - top) / (height * scale);
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}

/** three.js plane uv (origin bottom-left) to canvas coordinates (origin top-left). */
export function rayPanelPoint(uv) {
  if (!uv || !(uv.x >= 0 && uv.x <= 1 && uv.y >= 0 && uv.y <= 1)) return null;
  return { x: uv.x, y: 1 - uv.y };
}

/** Short operator text for the server's optional autonomy status fields. */
export function autonomyLabel(status) {
  const state = status?.autonomy_state, target = status?.target_id;
  const who = Number.isSafeInteger(target) ? ` #${target}` : "";
  switch (state) {
    case "forward": return "Exploring";
    case "turning": return "Exploring · turning";
    case "tracking": return `Seeking${who}`;
    case "target_missing": return `Target${who} not visible`;
    case "target_lost": return `Target${who} lost`;
    case "arrived": return `Arrived at${who}`;
    case "estop": return "Autonomy stopped: e-stop";
    case "stick_override": return "Stick override: teleop";
    default: return "";
  }
}
