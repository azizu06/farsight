import type { ControlCommand, ControlReply, DetectionPacket } from "./protocol";

export interface CommandBreadcrumb {
  forward: number;
  turn: number;
  at: number;
}
export interface DrillSnapshot {
  status: "idle" | "running" | "finished";
  elapsedMs: number;
  remainingMs: number;
  foundCount: number;
  foundIds: number[];
  expectedTotal: number | null;
  safetyStops: number;
  faceSupport: "pending-contract" | "ready";
  finishReason: "manual" | "timeout" | null;
  breadcrumbs: CommandBreadcrumb[];
  returnTrail: CommandBreadcrumb[];
  routeUnits: "command-seconds";
  routeLabel: string;
  trainingNote: string;
}
export interface DrillOptions {
  faceClassApproved?: boolean;
  expectedTotal?: number | null;
  now?: () => number;
}

const DURATION_MS = 90_000;
const DETECTION_FRESH_MS = 500;
const FACE_DWELL_MS = 1000;
const COMMAND_LEASE_MS = 250;
const MAX_BREADCRUMBS = 2048;

function validTime(at: number): number {
  if (!Number.isFinite(at) || at < 0)
    throw new RangeError(
      "Drill time must be a finite nonnegative monotonic timestamp",
    );
  return at;
}

/** Scores observed faces and records command intent; it never estimates a measured rover position. */
export class RescueDrill {
  private readonly clock: () => number;
  private readonly faceClassApproved: boolean;
  private status: DrillSnapshot["status"] = "idle";
  private startedAt: number | null = null;
  private endedAt: number | null = null;
  private advancedAt = 0;
  private expectedTotal: number | null = null;
  private finishReason: DrillSnapshot["finishReason"] = null;
  private readonly foundIds = new Set<number>();
  private readonly visibleFaces = new Map<
    number,
    { since: number; seenAt: number }
  >();
  private detectionFrame: number | null = null;
  private safetyStops = 0;
  private estopWasActive = false;
  private activeCommand: { v: number; w: number; at: number } | null = null;
  private forward = 0;
  private turn = 0;
  private readonly breadcrumbs: CommandBreadcrumb[] = [];

  constructor(options: DrillOptions = {}) {
    this.clock = options.now ?? (() => performance.now());
    this.faceClassApproved = options.faceClassApproved === true;
    this.setExpectedTotal(options.expectedTotal ?? null);
  }

  start(now = this.clock()): DrillSnapshot {
    validTime(now);
    this.reset();
    this.status = "running";
    this.startedAt = now;
    this.advancedAt = now;
    this.breadcrumbs.push({ forward: 0, turn: 0, at: now });
    return this.result();
  }

  tick(now = this.clock()): DrillSnapshot {
    this.advance(now);
    return this.result();
  }

  updateDetections(packet: DetectionPacket | null, at = this.clock()): void {
    if (validTime(at) < this.advancedAt) return;
    this.advance(at);
    if (this.status !== "running") return;
    if (!packet) {
      this.visibleFaces.clear();
      this.detectionFrame = null;
      return;
    }
    // Replaying a frame cannot establish an additional second of visibility.
    if (this.detectionFrame !== null && packet.frame <= this.detectionFrame)
      return;
    this.detectionFrame = packet.frame;
    if (!this.faceClassApproved) return;
    const faces = new Set(
      packet.boxes.filter((box) => box.cls === "face").map((box) => box.id),
    );
    for (const id of this.visibleFaces.keys()) {
      if (!faces.has(id)) this.visibleFaces.delete(id);
    }
    for (const id of faces) {
      if (this.foundIds.has(id)) continue;
      const dwell = this.visibleFaces.get(id) ?? { since: at, seenAt: at };
      dwell.seenAt = at;
      this.visibleFaces.set(id, dwell);
      // Require another positive observation after the dwell, rather than counting on an idle timer.
      if (at - dwell.since >= FACE_DWELL_MS) {
        this.foundIds.add(id);
        this.visibleFaces.delete(id);
      }
    }
  }

  updateReply(reply: ControlReply | null, at = this.clock()): void {
    if (validTime(at) < this.advancedAt) return;
    this.advance(at);
    if (this.status !== "running" || !reply) return;
    if (reply.estop && !this.estopWasActive) this.safetyStops++;
    this.estopWasActive = reply.estop;
  }

  recordCommand(command: ControlCommand, at = this.clock()): void {
    if (validTime(at) < this.advancedAt) return;
    this.advance(at);
    if (this.status !== "running") return;
    if (
      !command.deadman ||
      !Number.isFinite(command.v) ||
      !Number.isFinite(command.w)
    ) {
      this.activeCommand = null;
      return;
    }
    this.activeCommand = {
      v: Math.max(-1, Math.min(1, command.v)),
      w: Math.max(-1, Math.min(1, command.w)),
      at,
    };
  }

  setExpectedTotal(total: number | null): void {
    if (total !== null && (!Number.isSafeInteger(total) || total < 0)) {
      throw new RangeError(
        "Expected face total must be an operator-provided nonnegative integer or unknown",
      );
    }
    this.expectedTotal = total;
  }

  reset(): DrillSnapshot {
    this.status = "idle";
    this.startedAt = null;
    this.endedAt = null;
    this.advancedAt = 0;
    this.finishReason = null;
    this.foundIds.clear();
    this.visibleFaces.clear();
    this.detectionFrame = null;
    this.safetyStops = 0;
    this.estopWasActive = false;
    this.activeCommand = null;
    this.forward = 0;
    this.turn = 0;
    this.breadcrumbs.length = 0;
    return this.result();
  }

  finish(now = this.clock()): DrillSnapshot {
    this.advance(now);
    if (this.status === "running") {
      this.status = "finished";
      this.endedAt = this.advancedAt;
      this.finishReason = "manual";
      this.activeCommand = null;
      this.visibleFaces.clear();
    }
    return this.result();
  }

  snapshot(now = this.clock()): DrillSnapshot {
    this.advance(now);
    return this.result();
  }

  private advance(now: number): void {
    validTime(now);
    if (this.status !== "running" || now < this.advancedAt) return;
    const deadline = this.startedAt! + DURATION_MS;
    const to = Math.min(now, deadline);
    if (this.activeCommand) {
      const heldUntil = Math.min(to, this.activeCommand.at + COMMAND_LEASE_MS);
      const seconds = Math.max(0, heldUntil - this.advancedAt) / 1000;
      this.forward += this.activeCommand.v * seconds;
      this.turn += this.activeCommand.w * seconds;
    }
    this.advancedAt = to;
    const last = this.breadcrumbs.at(-1);
    if (
      last &&
      to - last.at >= 100 &&
      (last.forward !== this.forward || last.turn !== this.turn)
    ) {
      this.breadcrumbs.push({ forward: this.forward, turn: this.turn, at: to });
      if (this.breadcrumbs.length >= MAX_BREADCRUMBS)
        this.breadcrumbs.splice(1, 1);
    }
    for (const [id, dwell] of this.visibleFaces) {
      if (to - dwell.seenAt > DETECTION_FRESH_MS) this.visibleFaces.delete(id);
    }
    if (now >= deadline) {
      this.status = "finished";
      this.endedAt = deadline;
      this.finishReason = "timeout";
      this.activeCommand = null;
      this.visibleFaces.clear();
    }
  }

  private result(): DrillSnapshot {
    const elapsedMs =
      this.startedAt === null
        ? 0
        : (this.endedAt ?? this.advancedAt) - this.startedAt;
    const breadcrumbs = this.breadcrumbs.map((point) => ({ ...point }));
    const last = breadcrumbs.at(-1);
    if (last && (last.forward !== this.forward || last.turn !== this.turn)) {
      breadcrumbs.push({
        forward: this.forward,
        turn: this.turn,
        at: this.advancedAt,
      });
    }
    return {
      status: this.status,
      elapsedMs,
      remainingMs: Math.max(0, DURATION_MS - elapsedMs),
      foundCount: this.foundIds.size,
      foundIds: [...this.foundIds].sort((a, b) => a - b),
      expectedTotal: this.expectedTotal,
      safetyStops: this.safetyStops,
      faceSupport: this.faceClassApproved ? "ready" : "pending-contract",
      finishReason: this.finishReason,
      breadcrumbs,
      returnTrail: breadcrumbs
        .slice()
        .reverse()
        .map((point) => ({ ...point })),
      routeUnits: "command-seconds",
      routeLabel:
        "Approximate commanded trail; command-seconds, not measured position",
      trainingNote:
        "Responders practise a timed room search by finding every reported face with the rover.",
    };
  }
}
