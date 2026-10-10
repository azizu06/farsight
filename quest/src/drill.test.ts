import { describe, expect, it } from "vitest";
import { RescueDrill } from "./drill";
import type { ControlCommand, DetectionPacket } from "./roverProtocol";

function fixture(
  options: { faceClassApproved?: boolean; expectedTotal?: number } = {
    faceClassApproved: true,
  },
) {
  let now = 0;
  let frame = 0;
  const drill = new RescueDrill({ ...options, now: () => now });
  const at = (time: number) => {
    now = time;
  };
  const see = (ids: number[], cls = "face") => {
    const packet: DetectionPacket = {
      ts: now / 1000,
      frame: ++frame,
      fps: 20,
      boxes: ids.map((id) => ({
        id,
        cls,
        conf: 0.9,
        x: 0.1,
        y: 0.2,
        w: 0.2,
        h: 0.3,
      })),
    };
    drill.updateDetections(packet);
  };
  const reply = (estop: boolean) =>
    drill.updateReply({
      seq: 1,
      estop,
      range_cm: estop ? 10 : 60,
      mode: "teleop",
      link_ms: 20,
    });
  return { drill, at, see, reply };
}

const command = (v: number, w = 0, deadman = true): ControlCommand => ({
  seq: 1,
  deadman,
  v,
  w,
  pan: 0,
  mode: "teleop",
  session_id: "drill-fixture",
  permit: "drill-permit",
  video_fresh: true,
  video_age_ms: 10,
});

describe("rescue drill timer and scoring readiness", () => {
  it("runs for exactly 90 seconds and freezes the end result", () => {
    const { drill, at } = fixture();
    drill.start();
    at(89_999);
    expect(drill.tick().status).toBe("running");
    expect(drill.snapshot().remainingMs).toBe(1);
    at(90_000);
    expect(drill.tick()).toMatchObject({
      status: "finished",
      elapsedMs: 90_000,
      remainingMs: 0,
      finishReason: "timeout",
    });
    at(150_000);
    expect(drill.snapshot().elapsedMs).toBe(90_000);
  });

  it("shows unknown total and pending face support until the operator and owner provide them", () => {
    const { drill, at, see } = fixture({});
    drill.start();
    see([1]);
    at(500);
    see([1]);
    at(1000);
    see([1]);
    expect(drill.snapshot()).toMatchObject({
      expectedTotal: null,
      faceSupport: "pending-contract",
      foundCount: 0,
    });
    drill.setExpectedTotal(4);
    expect(drill.snapshot().expectedTotal).toBe(4);
  });

  it.each([-1, 1.5, NaN, Infinity])(
    "rejects an invented or invalid expected total %s",
    (total) => {
      expect(() => new RescueDrill({ expectedTotal: total })).toThrow(
        RangeError,
      );
    },
  );

  it("freezes a manually finished drill and resets all session state", () => {
    const { drill, at, see, reply } = fixture({
      faceClassApproved: true,
      expectedTotal: 2,
    });
    drill.start();
    see([1]);
    reply(true);
    at(500);
    see([1]);
    at(1000);
    see([1]);
    expect(drill.finish()).toMatchObject({
      status: "finished",
      elapsedMs: 1000,
      foundCount: 1,
      safetyStops: 1,
      finishReason: "manual",
    });
    at(3000);
    see([2]);
    reply(false);
    reply(true);
    expect(drill.snapshot()).toMatchObject({
      elapsedMs: 1000,
      foundCount: 1,
      safetyStops: 1,
    });
    expect(drill.reset()).toMatchObject({
      status: "idle",
      foundCount: 0,
      safetyStops: 0,
      remainingMs: 90_000,
      expectedTotal: 2,
    });
    drill.start();
    expect(drill.snapshot().breadcrumbs).toEqual([
      { forward: 0, turn: 0, at: 3000 },
    ]);
  });
});

describe("continuous face observations", () => {
  it("counts a stable face only after one second of fresh observations and counts it once", () => {
    const { drill, at, see } = fixture();
    drill.start();
    see([7]);
    at(499);
    see([7]);
    at(999);
    see([7]);
    expect(drill.snapshot().foundCount).toBe(0);
    at(1000);
    see([7]);
    expect(drill.snapshot().foundIds).toEqual([7]);
    at(1100);
    see([]);
    at(1200);
    see([7]);
    at(1700);
    see([7]);
    at(2200);
    see([7]);
    expect(drill.snapshot().foundCount).toBe(1);
  });

  it("restarts dwell when the face disappears", () => {
    const { drill, at, see } = fixture();
    drill.start();
    see([1]);
    at(500);
    see([1]);
    at(700);
    see([]);
    at(800);
    see([1]);
    at(1300);
    see([1]);
    at(1700);
    see([1]);
    expect(drill.snapshot().foundCount).toBe(0);
    at(1800);
    see([1]);
    expect(drill.snapshot().foundCount).toBe(1);
  });

  it("does not count a single stale face while the clock advances", () => {
    const { drill, at, see } = fixture();
    drill.start();
    see([1]);
    at(1000);
    drill.tick();
    expect(drill.snapshot().foundCount).toBe(0);
    see([1]);
    at(1500);
    see([1]);
    at(1999);
    see([1]);
    expect(drill.snapshot().foundCount).toBe(0);
    at(2000);
    see([1]);
    expect(drill.snapshot().foundCount).toBe(1);
  });

  it("does not infer faces from person detections", () => {
    const { drill, at, see } = fixture();
    drill.start();
    see([1], "person");
    at(500);
    see([1], "person");
    at(1000);
    see([1], "person");
    expect(drill.snapshot().foundCount).toBe(0);
  });

  it("does not accumulate dwell across a feed loss", () => {
    const { drill, at, see } = fixture();
    drill.start();
    see([1]);
    at(500);
    see([1]);
    drill.updateDetections(null);
    at(800);
    see([1]);
    at(1300);
    see([1]);
    expect(drill.snapshot().foundCount).toBe(0);
    at(1800);
    see([1]);
    expect(drill.snapshot().foundCount).toBe(1);
  });

  it("does not extend visibility by replaying the same detection frame", () => {
    const { drill, at } = fixture();
    const packet: DetectionPacket = {
      ts: 0,
      frame: 1,
      fps: 20,
      boxes: [{ id: 1, cls: "face", conf: 0.9, x: 0, y: 0, w: 0.2, h: 0.2 }],
    };
    drill.start();
    drill.updateDetections(packet);
    at(500);
    drill.updateDetections(packet);
    at(1000);
    drill.updateDetections(packet);
    expect(drill.snapshot().foundCount).toBe(0);
  });
});

describe("safety stops and command breadcrumbs", () => {
  it("counts each observed e-stop activation without double counting repeated true replies", () => {
    const { drill, reply } = fixture();
    drill.start();
    reply(true);
    reply(true);
    reply(false);
    reply(true);
    expect(drill.snapshot().safetyStops).toBe(2);
    drill.updateReply(null);
    reply(true);
    expect(drill.snapshot().safetyStops).toBe(2);
  });

  it("integrates normalized command-time and returns a reversed approximate trail", () => {
    const { drill, at } = fixture();
    drill.start();
    drill.recordCommand(command(0.4, -0.2));
    at(200);
    drill.recordCommand(command(0.4, -0.2));
    at(400);
    drill.recordCommand(command(0, 0, false));
    const result = drill.snapshot();
    expect(result.breadcrumbs[0]).toEqual({ forward: 0, turn: 0, at: 0 });
    expect(result.breadcrumbs.at(-1)!.forward).toBeCloseTo(0.16);
    expect(result.breadcrumbs.at(-1)!.turn).toBeCloseTo(-0.08);
    expect(result.returnTrail[0]).toEqual(result.breadcrumbs.at(-1));
    expect(result.returnTrail.at(-1)).toEqual({ forward: 0, turn: 0, at: 0 });
    expect(result.routeUnits).toBe("command-seconds");
    expect(result.routeLabel).toMatch(/approximate/i);
    expect(result.routeLabel).toMatch(/not measured/i);
  });

  it("stops integrating after the 250 ms command lease expires", () => {
    const { drill, at } = fixture();
    drill.start();
    drill.recordCommand(command(1));
    at(5000);
    drill.tick();
    expect(drill.snapshot().breadcrumbs.at(-1)!.forward).toBeCloseTo(0.25);
  });

  it("does not integrate released or stopped commands", () => {
    const { drill, at } = fixture();
    drill.start();
    drill.recordCommand(command(1, 1, false));
    at(200);
    drill.tick();
    expect(drill.snapshot().breadcrumbs).toHaveLength(1);
  });

  it("keeps breadcrumb memory bounded while preserving the starting point", () => {
    const { drill, at } = fixture();
    drill.start();
    for (let time = 0; time < 90_000; time += 10) {
      at(time);
      drill.recordCommand(command(1));
    }
    const points = drill.snapshot().breadcrumbs;
    expect(points.length).toBeLessThanOrEqual(2048);
    expect(points[0]).toEqual({ forward: 0, turn: 0, at: 0 });
    expect(points.at(-1)!.forward).toBeCloseTo(89.99);
  });
});
