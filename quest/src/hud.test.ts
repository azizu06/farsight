import { describe, expect, it } from "vitest";
import {
  drawHud,
  parseControlReply,
  parseDetections,
  summarizeHud,
  type HudState,
} from "./hud";
import type { DetectionPacket } from "./roverProtocol";
import { RescueDrill } from "./drill";

const detections: DetectionPacket = {
  ts: 1760090000.123,
  frame: 1042,
  fps: 24.8,
  boxes: [
    { id: 3, cls: "person", conf: 0.91, x: 0.41, y: 0.22, w: 0.18, h: 0.55 },
  ],
};
const reply = {
  seq: 812,
  range_cm: 64,
  estop: false,
  mode: "teleop",
  link_ms: 38,
};
// The server sends this before a command or range reading has been accepted.
const idleStatus = {
  type: "control_status" as const,
  session_id: "controller-session",
  permit: "server-opaque-token",
  permit_ms: 250,
  seq: -1,
  range_cm: null,
  estop: true,
  mode: "teleop",
  link_ms: null,
  rover: false,
  simulated: false,
  requires_release: true,
  reason: "release_required",
  left_pwm: 0,
  right_pwm: 0,
  pan_deg: 90,
  max_pwm: 100,
};
const liveState = (): HudState => ({
  detections,
  detectionsAt: 1000,
  reply,
  replyAt: 1000,
  now: 1100,
  videoLive: true,
});

describe("HUD packet validation", () => {
  it("accepts the documented packets as objects or JSON", () => {
    expect(parseDetections(detections)).toEqual(detections);
    expect(parseDetections(JSON.stringify(detections))).toEqual(detections);
    expect(parseControlReply(reply)).toEqual(reply);
  });

  it("accepts and preserves the backend's initial status with unknown measurements", () => {
    expect(parseControlReply(JSON.stringify(idleStatus))).toEqual(idleStatus);
  });

  it.each([
    { ...reply, session_id: [] },
    { ...reply, permit: "x".repeat(129) },
    { ...reply, permit_ms: Infinity },
    { ...reply, requires_release: "false" },
    { ...reply, rover: 1 },
    { ...reply, reason: "x".repeat(257) },
    { ...reply, left_pwm: 256 },
    { ...reply, pan_deg: 171 },
  ])("rejects malformed optional control status metadata", (packet) => {
    expect(parseControlReply(packet)).toBeNull();
  });

  it.each([
    { ...detections, fps: Infinity },
    { ...detections, frame: -1 },
    { ...detections, boxes: [{ ...detections.boxes[0], x: NaN }] },
    { ...detections, boxes: [{ ...detections.boxes[0], x: -0.01 }] },
    { ...detections, boxes: [{ ...detections.boxes[0], w: 0 }] },
    { ...detections, boxes: [{ ...detections.boxes[0], x: 0.9, w: 0.2 }] },
    { ...detections, boxes: [{ ...detections.boxes[0], conf: 1.01 }] },
    { ...detections, boxes: [{ ...detections.boxes[0], id: 3.5 }] },
    {
      ...detections,
      boxes: Array.from({ length: 257 }, (_, id) => ({
        ...detections.boxes[0],
        id,
      })),
    },
    "{",
    "x".repeat(65_537),
    null,
  ])("rejects malformed or unbounded detections", (packet) => {
    expect(parseDetections(packet)).toBeNull();
  });

  it("rejects a nested JSON string in place of a canonical box object", () => {
    expect(
      parseDetections({
        ...detections,
        boxes: [JSON.stringify(detections.boxes[0])],
      }),
    ).toBeNull();
  });

  it.each([
    { ...reply, range_cm: NaN },
    { ...reply, range_cm: -1 },
    { ...reply, estop: "false" },
    { ...reply, link_ms: Infinity },
    { ...reply, link_ms: -1 },
    { ...reply, seq: -2 },
    { ...reply, mode: "x".repeat(65) },
    "not JSON",
  ])(
    "rejects invalid control telemetry without inventing a range",
    (packet) => {
      expect(parseControlReply(packet)).toBeNull();
    },
  );
});

describe("HUD freshness", () => {
  it("shows a live idle socket with unknown range and link timing, without inventing zero", () => {
    const summary = summarizeHud({ ...liveState(), reply: idleStatus });
    expect(summary.link).toBe("live");
    expect(summary.rangeText).toBe("unknown");
    expect(summary.linkMsText).toBe("unknown");
    expect(summary.estop).toBe("active");
    expect(summary.roverText).toBe("ROVER DISCONNECTED");
    expect(summary.controlReasonText).toBe("release_required");
  });

  it("shows received measurements and boxes at the 500 ms boundary", () => {
    const summary = summarizeHud({ ...liveState(), now: 1500 });
    expect(summary.link).toBe("live");
    expect(summary.rangeText).toBe("64 cm");
    expect(summary.estop).toBe("clear");
    expect(summary.detectorFpsText).toBe("24.8 fps");
    expect(summary.linkMsText).toBe("38 ms");
    expect(summary.activeBoxes).toEqual(detections.boxes);
  });

  it("turns the link red and hides obsolete measurements after 500 ms", () => {
    const summary = summarizeHud({ ...liveState(), now: 1501 });
    expect(summary.link).toBe("stale");
    expect(summary.rangeText).toBe("unknown");
    expect(summary.estop).toBe("unknown");
    expect(summary.linkMsText).toBe("unknown");
    expect(summary.detectorFpsText).toBe("unknown");
    expect(summary.activeBoxes).toEqual([]);
  });

  it("clears boxes when the video disappears even with recent detections", () => {
    expect(
      summarizeHud({ ...liveState(), videoLive: false }).activeBoxes,
    ).toEqual([]);
  });

  it("shows unknown link, range and safety state before any reply", () => {
    const summary = summarizeHud({
      ...liveState(),
      reply: null,
      replyAt: null,
    });
    expect(summary.link).toBe("unknown");
    expect(summary.rangeText).toBe("unknown");
    expect(summary.estopText).toContain("UNKNOWN");
  });

  it("does not hide a recent e-stop behind valid detector data", () => {
    expect(
      summarizeHud({ ...liveState(), reply: { ...reply, estop: true } })
        .estopText,
    ).toContain("ACTIVE");
  });

  it("does not treat a future receive timestamp as live telemetry", () => {
    expect(summarizeHud({ ...liveState(), replyAt: 1200 }).link).toBe("stale");
  });
});

describe("HUD rendering", () => {
  // Canvas is the output boundary; record geometry and paint to check image alignment and warning visibility.
  function canvasCapture() {
    const boxes: number[][] = [];
    const text: { value: string; color: string }[] = [];
    const context = {
      fillStyle: "",
      strokeStyle: "",
      font: "",
      lineWidth: 0,
      textBaseline: "",
      textAlign: "",
      save() {},
      restore() {},
      clearRect() {},
      fillRect() {},
      strokeRect(...coordinates: number[]) {
        boxes.push(coordinates);
      },
      fillText(value: string) {
        text.push({ value, color: String(this.fillStyle) });
      },
      measureText(value: string) {
        return { width: value.length * 8 };
      },
    };
    return { ctx: context as unknown as CanvasRenderingContext2D, boxes, text };
  }

  it("maps top-left normalized detections onto the same image dimensions", () => {
    const capture = canvasCapture();
    drawHud(capture.ctx, 640, 480, liveState());
    expect(capture.boxes).toHaveLength(1);
    expect(capture.boxes[0][0]).toBeCloseTo(262.4);
    expect(capture.boxes[0][1]).toBeCloseTo(105.6);
    expect(capture.boxes[0][2]).toBeCloseTo(115.2);
    expect(capture.boxes[0][3]).toBeCloseTo(264);
    expect(capture.text.some(({ value }) => /24.8 fps/.test(value))).toBe(true);
    expect(capture.text.some(({ value }) => /38 ms/.test(value))).toBe(true);
  });

  it("paints active e-stop and stale link warnings in red with readable words", () => {
    const capture = canvasCapture();
    drawHud(capture.ctx, 640, 480, {
      ...liveState(),
      reply: { ...reply, estop: true },
    });
    expect(
      capture.text.some(
        ({ value, color }) =>
          value.includes("E-STOP ACTIVE") && color === "#ff4d5e",
      ),
    ).toBe(true);
    const stale = canvasCapture();
    drawHud(stale.ctx, 640, 480, { ...liveState(), now: 1501 });
    expect(stale.boxes).toHaveLength(0);
    expect(
      stale.text.some(
        ({ value, color }) =>
          value.includes("LINK LOST") && color === "#ff4d5e",
      ),
    ).toBe(true);
  });

  it("shows the running drill timer, unknown total, and pending face approval", () => {
    const drill = new RescueDrill({ now: () => 0 });
    drill.start();
    const capture = canvasCapture();
    drawHud(capture.ctx, 640, 480, {
      ...liveState(),
      drill: drill.snapshot(1000),
    });
    expect(
      capture.text.some(
        ({ value }) => /89 s/.test(value) && /0\/unknown/.test(value),
      ),
    ).toBe(true);
    expect(
      capture.text.some(
        ({ value, color }) =>
          /FACE CLASS PENDING/.test(value) && color === "#ff4d5e",
      ),
    ).toBe(true);
  });

  it("shows finished elapsed time and safety stops without calling them collisions avoided", () => {
    const drill = new RescueDrill({
      now: () => 0,
      faceClassApproved: true,
      expectedTotal: 2,
    });
    drill.start();
    drill.updateReply({ ...reply, estop: true });
    const capture = canvasCapture();
    drawHud(capture.ctx, 640, 480, {
      ...liveState(),
      drill: drill.finish(1000),
    });
    expect(
      capture.text.some(
        ({ value }) =>
          /FINISHED/.test(value) && /1.0 s/.test(value) && /0\/2/.test(value),
      ),
    ).toBe(true);
    expect(capture.text.some(({ value }) => /SAFETY STOPS 1/.test(value))).toBe(
      true,
    );
    expect(capture.text.some(({ value }) => /collisions/i.test(value))).toBe(
      false,
    );
  });
});
