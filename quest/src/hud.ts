import type {
  ControlReply,
  DetectionBox,
  DetectionPacket,
} from "./roverProtocol";
import type { DrillSnapshot } from "./drill";

export interface HudState {
  detections: DetectionPacket | null;
  detectionsAt: number | null;
  reply: ControlReply | null;
  replyAt: number | null;
  now: number;
  videoLive: boolean;
  drill?: DrillSnapshot;
}

export interface HudSummary {
  link: "live" | "stale" | "unknown";
  linkText: string;
  rangeText: string;
  estop: "active" | "clear" | "unknown";
  estopText: string;
  detectorFpsText: string;
  linkMsText: string;
  activeBoxes: DetectionBox[];
}

function object(value: unknown): Record<string, unknown> | null {
  if (typeof value === "string") {
    if (value.length > 65_536) return null;
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const nonnegative = (value: unknown): value is number =>
  finite(value) && value >= 0;
const identifier = (value: unknown): value is number =>
  nonnegative(value) && Number.isSafeInteger(value);
const unit = (value: unknown): value is number =>
  finite(value) && value >= 0 && value <= 1;

/** Reject the whole packet if any box violates the normalized image contract. */
export function parseDetections(value: unknown): DetectionPacket | null {
  const packet = object(value);
  if (
    !packet ||
    !nonnegative(packet.ts) ||
    !identifier(packet.frame) ||
    !nonnegative(packet.fps) ||
    !Array.isArray(packet.boxes) ||
    packet.boxes.length > 256
  )
    return null;
  const boxes: DetectionBox[] = [];
  const ids = new Set<number>();
  for (const candidate of packet.boxes) {
    const box = typeof candidate === "string" ? null : object(candidate);
    if (
      !box ||
      !identifier(box.id) ||
      ids.has(box.id) ||
      typeof box.cls !== "string" ||
      box.cls.length === 0 ||
      box.cls.length > 64 ||
      !unit(box.conf) ||
      !unit(box.x) ||
      !unit(box.y) ||
      !unit(box.w) ||
      !unit(box.h) ||
      box.w === 0 ||
      box.h === 0 ||
      box.x + box.w > 1 + 1e-9 ||
      box.y + box.h > 1 + 1e-9
    )
      return null;
    ids.add(box.id);
    boxes.push({
      id: box.id,
      cls: box.cls,
      conf: box.conf,
      x: box.x,
      y: box.y,
      w: box.w,
      h: box.h,
    });
  }
  return { ts: packet.ts, frame: packet.frame, fps: packet.fps, boxes };
}

export function parseControlReply(value: unknown): ControlReply | null {
  const reply = object(value);
  if (
    !reply ||
    !identifier(reply.seq) ||
    !nonnegative(reply.range_cm) ||
    typeof reply.estop !== "boolean" ||
    (reply.mode !== "teleop" && reply.mode !== "seek") ||
    !nonnegative(reply.link_ms)
  )
    return null;
  return {
    seq: reply.seq,
    range_cm: reply.range_cm,
    estop: reply.estop,
    mode: reply.mode,
    link_ms: reply.link_ms,
  };
}

function fresh(at: number | null, now: number): boolean {
  return (
    at !== null &&
    Number.isFinite(at) &&
    Number.isFinite(now) &&
    now >= at &&
    now - at <= 500
  );
}

export function summarizeHud(state: HudState): HudSummary {
  const replyFresh = state.reply !== null && fresh(state.replyAt, state.now);
  const detectionsFresh =
    state.videoLive &&
    state.detections !== null &&
    fresh(state.detectionsAt, state.now);
  const link = replyFresh
    ? "live"
    : state.replyAt === null
      ? "unknown"
      : "stale";
  const estop = replyFresh
    ? state.reply!.estop
      ? "active"
      : "clear"
    : "unknown";
  return {
    link,
    linkText:
      link === "live"
        ? "LINK LIVE"
        : link === "stale"
          ? "LINK LOST"
          : "LINK UNKNOWN",
    rangeText: replyFresh
      ? `${Math.round(state.reply!.range_cm)} cm`
      : "unknown",
    estop,
    estopText: `E-STOP ${estop.toUpperCase()}`,
    detectorFpsText: detectionsFresh
      ? `${state.detections!.fps.toFixed(1)} fps`
      : "unknown",
    linkMsText: replyFresh
      ? `${Math.round(state.reply!.link_ms)} ms`
      : "unknown",
    activeBoxes: detectionsFresh ? state.detections!.boxes : [],
  };
}

/** Draw over the image's full pixel rectangle, before that rectangle is curved in XR. */
export function drawHud(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  state: HudState,
): void {
  if (!finite(width) || !finite(height) || width <= 0 || height <= 0) return;
  const summary = summarizeHud(state);
  const scale = width / 640;
  const red = "#ff4d5e";
  const green = "#62f5bd";
  ctx.save();
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.font = `600 ${Math.max(12, 17 * scale)}px system-ui, sans-serif`;
  ctx.lineWidth = Math.max(2, 2 * scale);
  for (const box of summary.activeBoxes) {
    const x = box.x * width;
    const y = box.y * height;
    const label = `${box.cls} #${box.id} ${Math.round(box.conf * 100)}%`;
    ctx.strokeStyle = green;
    ctx.strokeRect(x, y, box.w * width, box.h * height);
    const labelY = Math.max(0, y - 22 * scale);
    ctx.fillStyle = "rgba(5, 14, 21, 0.8)";
    ctx.fillRect(
      x,
      labelY,
      ctx.measureText(label).width + 8 * scale,
      22 * scale,
    );
    ctx.fillStyle = green;
    ctx.fillText(label, x + 4 * scale, labelY + 2 * scale);
  }
  ctx.fillStyle = "rgba(5, 14, 21, 0.86)";
  ctx.fillRect(0, 0, width, 68 * scale);
  ctx.font = `700 ${Math.max(14, 21 * scale)}px system-ui, sans-serif`;
  ctx.fillStyle = "#e6eff6";
  ctx.fillText(`RANGE ${summary.rangeText}`, 12 * scale, 10 * scale);
  ctx.fillStyle = summary.estop === "clear" ? green : red;
  ctx.fillText(summary.estopText, 265 * scale, 10 * scale);
  ctx.font = `600 ${Math.max(12, 16 * scale)}px system-ui, sans-serif`;
  ctx.fillStyle = summary.link === "live" ? green : red;
  ctx.fillText(summary.linkText, 12 * scale, 43 * scale);
  ctx.fillStyle = "#e6eff6";
  ctx.fillText(`DETECTOR ${summary.detectorFpsText}`, 180 * scale, 43 * scale);
  ctx.fillText(`LINK ${summary.linkMsText}`, 430 * scale, 43 * scale);
  if (state.drill && state.drill.status !== "idle") {
    const drill = state.drill;
    const y = height - 106 * scale;
    const time =
      drill.status === "finished"
        ? `${(drill.elapsedMs / 1000).toFixed(1)} s`
        : `${Math.ceil(drill.remainingMs / 1000)} s`;
    ctx.fillStyle = "rgba(5, 14, 21, 0.86)";
    ctx.fillRect(0, y, width, 80 * scale);
    ctx.fillStyle = "#e6eff6";
    ctx.font = `700 ${Math.max(12, 17 * scale)}px system-ui, sans-serif`;
    ctx.fillText(
      `DRILL ${drill.status.toUpperCase()} ${time}  |  FACES ${drill.foundCount}/${drill.expectedTotal ?? "unknown"}`,
      12 * scale,
      y + 7 * scale,
    );
    ctx.font = `600 ${Math.max(11, 14 * scale)}px system-ui, sans-serif`;
    ctx.fillText(
      `SAFETY STOPS ${drill.safetyStops}`,
      12 * scale,
      y + 32 * scale,
    );
    ctx.fillStyle = drill.faceSupport === "ready" ? green : red;
    ctx.fillText(
      drill.faceSupport === "ready"
        ? "FACE CLASS READY: reported stable IDs only"
        : "FACE CLASS PENDING: face detection unavailable",
      12 * scale,
      y + 56 * scale,
    );
  }
  ctx.fillStyle = "rgba(5, 14, 21, 0.86)";
  ctx.fillRect(0, height - 26 * scale, width, 26 * scale);
  ctx.font = `${Math.max(11, 13 * scale)}px system-ui, sans-serif`;
  ctx.fillStyle = "#e6eff6";
  ctx.textAlign = "center";
  ctx.fillText("Boxes may lag the image", width / 2, height - 21 * scale);
  ctx.restore();
}
