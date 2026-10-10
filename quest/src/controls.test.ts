import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ControlLink } from "./controls";
import type { ControlCommand, ControlReply } from "./roverProtocol";

class Socket extends EventTarget {
  readyState = 0;
  bufferedAmount = 0;
  sessionId = "session-one";
  permitIndex = 0;
  sent: ControlCommand[] = [];
  send(data: string): void {
    if (this.readyState !== 1) throw new Error("closed socket");
    this.sent.push(JSON.parse(data) as ControlCommand);
  }
  open(): void {
    this.readyState = 1;
    this.dispatchEvent(new Event("open"));
  }
  close(): void {
    this.readyState = 3;
    this.dispatchEvent(new Event("close"));
  }
  status(overrides: Record<string, unknown> = {}) {
    return {
      type: "control_status",
      session_id: this.sessionId,
      permit: `permit-${++this.permitIndex}`,
      permit_ms: 250,
      seq: this.sent.at(-1)?.seq ?? -1,
      range_cm: this.sent.length ? 64 : null,
      estop: !this.sent.length,
      mode: "teleop",
      link_ms: this.sent.length ? 38 : null,
      rover: true,
      simulated: true,
      requires_release: !this.sent.length,
      reason: this.sent.length ? "deadman_released" : "release_required",
      left_pwm: 0,
      right_pwm: 0,
      pan_deg: 90,
      max_pwm: 100,
      ...overrides,
    };
  }
  reply(value: unknown = this.status()): void {
    this.dispatchEvent(
      new MessageEvent("message", { data: JSON.stringify(value) }),
    );
  }
}

function setup(
  open = true,
  now: () => number = () => Date.now(),
  video = true,
) {
  const sockets: Socket[] = [];
  const commands: { command: ControlCommand; at: number }[] = [];
  const replies: ControlReply[] = [];
  const link = new ControlLink(
    "wss://example.test/ws/control",
    {
      onCommand: (command, at) => commands.push({ command, at }),
      onReply: (reply) => replies.push(reply),
    },
    {
      createSocket: () => {
        const socket = new Socket();
        sockets.push(socket);
        return socket;
      },
      now,
    },
  );
  link.connect();
  if (video) {
    link.setVideoFrame("frame-initial", 0, now());
  }
  const socket = sockets[0];
  if (open) {
    socket.open();
    socket.reply();
  }
  return { link, socket, sockets, commands, replies };
}

function armKeyboard(link: ControlLink, socket: Socket): void {
  socket.reply();
  link.setKeyboard(new Set());
}

type MutableButton = {
  -readonly [Key in keyof GamepadButton]: GamepadButton[Key];
};
type MutablePad = Omit<
  Gamepad,
  "axes" | "buttons" | "connected" | "mapping"
> & {
  axes: number[];
  buttons: MutableButton[];
  connected: boolean;
  mapping: GamepadMappingType;
};
type MutableSource = Omit<XRInputSource, "gamepad"> & { gamepad: MutablePad };

function xrSource(
  axes: number[] = [0, 0, 0, 0],
  held = false,
  mapping = "xr-standard",
): MutableSource {
  return {
    handedness: "right",
    targetRayMode: "tracked-pointer",
    gripSpace: {},
    profiles: ["oculus-touch-v3"],
    gamepad: {
      id: "",
      index: -1,
      timestamp: 0,
      connected: true,
      mapping,
      axes,
      buttons: [{ pressed: held, touched: held, value: held ? 1 : 0 }],
      hapticActuators: [],
      vibrationActuator: null,
    },
  } as unknown as MutableSource;
}

class Session extends EventTarget {
  inputSources: MutableSource[];
  visibilityState = "visible";
  constructor(source = xrSource()) {
    super();
    this.inputSources = [source];
  }
  get session(): XRSession {
    return this as unknown as XRSession;
  }
}

describe("ControlLink", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
  });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("waits for an authorized server session before sending even a neutral handshake", () => {
    const { link, socket } = setup(false);
    socket.open();
    expect(socket.sent).toEqual([]);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(200);
    expect(socket.sent.every((command) => !command.deadman)).toBe(true);
    expect(link.state).toBe("waiting");
    socket.reply();
    expect(socket.sent.at(-1)).toMatchObject({
      seq: 1,
      deadman: false,
      v: 0,
      w: 0,
      session_id: "session-one",
    });
  });

  it("pumps forward and right keyboard drive at 20 Hz with increasing sequence numbers", () => {
    const { link, socket, commands } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW", "KeyD"]));
    vi.advanceTimersByTime(49);
    expect(socket.sent).toHaveLength(1);
    vi.advanceTimersByTime(51);
    socket.reply();
    vi.advanceTimersByTime(100);
    expect(socket.sent.slice(1)).toMatchObject([
      { seq: 2, deadman: true, v: 1, w: 1, pan: 0, mode: "teleop" },
      { seq: 3, deadman: true, v: 1, w: 1, pan: 0, mode: "teleop" },
      { seq: 4, deadman: true, v: 1, w: 1, pan: 0, mode: "teleop" },
      { seq: 5, deadman: true, v: 1, w: 1, pan: 0, mode: "teleop" },
    ]);
    expect(commands.slice(1).map(({ at }) => at)).toEqual([50, 100, 150, 200]);
  });

  it("keeps each nominal 50 ms drive tick when the fractional clock has submillisecond scheduler jitter", () => {
    let now = 0;
    const { link, socket, commands } = setup(true, () => now);
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    now = 25;
    link.tick();
    expect(socket.sent).toHaveLength(1);
    const sampledTimes = [49.8, 100.1, 149.9, 200, 249.8, 300.1];
    for (const at of sampledTimes) {
      now = at;
      vi.advanceTimersByTime(50);
      socket.reply();
    }
    expect(socket.sent).toHaveLength(7);
    expect(commands.slice(1).map(({ at }) => at)).toEqual(sampledTimes);
    expect(
      socket.sent.slice(1).every(({ deadman, v }) => deadman && v === 1),
    ).toBe(true);
  });

  it("sends zero immediately when Space is released between ticks", () => {
    const { link, socket, commands } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(75);
    link.setKeyboard(new Set(["KeyW"]));
    expect(socket.sent.at(-1)).toMatchObject({
      seq: 3,
      deadman: false,
      v: 0,
      w: 0,
    });
    expect(commands.at(-1)?.at).toBe(75);
    vi.advanceTimersByTime(100);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
  });

  it("disconnects at the next pump when any outbound bytes remain, even with progressing old acknowledgements", () => {
    const { link, socket, sockets } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(150);
    expect(socket.sent).toHaveLength(4);
    socket.bufferedAmount = 1;
    socket.reply({
      seq: 2,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    socket.reply({
      seq: 3,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    vi.advanceTimersByTime(50);
    expect(link.state).toBe("disconnected");
    expect(link.inputStatus).toBe("release-to-arm");
    expect(socket.readyState).toBe(3);
    expect(socket.sent).toHaveLength(4); // Neither motion nor a stop is appended behind the queue.
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(1);
    expect(socket.sent).toHaveLength(4);
    link.connect();
    const next = sockets[1];
    next.open();
    next.reply();
    next.reply();
    link.setVideoFrame("after-backpressure", 0, Date.now());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(next.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(next.sent.at(-1)).toMatchObject({ deadman: true, v: 1 });
  });

  it("closes a backed-up socket on deadman release instead of queuing a stop behind old motion", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(75);
    expect(socket.sent).toHaveLength(2);
    socket.bufferedAmount = 4096;
    link.setKeyboard(new Set());
    expect(link.state).toBe("disconnected");
    expect(link.inputStatus).toBe("release-to-arm");
    expect(socket.readyState).toBe(3);
    expect(socket.sent).toHaveLength(2);
  });

  it("allows handshake bytes to drain for 50 ms when socket open falls just before a pump deadline", () => {
    const { link, socket } = setup(false);
    vi.advanceTimersByTime(49);
    socket.open();
    socket.reply();
    socket.bufferedAmount = 1;
    vi.advanceTimersByTime(1);
    expect(link.state).toBe("healthy");
    expect(socket.readyState).toBe(1);
    expect(socket.sent).toHaveLength(1);
    socket.bufferedAmount = 0;
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, v: 1 });
  });

  it("allows release bytes to drain before the next command is due", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(99);
    link.setKeyboard(new Set(["KeyW"]));
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    expect(socket.sent).toHaveLength(3);
    socket.bufferedAmount = 1;
    vi.advanceTimersByTime(1);
    expect(link.state).toBe("healthy");
    expect(socket.readyState).toBe(1);
    expect(socket.sent).toHaveLength(3);
    socket.bufferedAmount = 0;
    socket.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, v: 1 });
  });

  it("still disconnects for pending handshake bytes when the first command is due", () => {
    const { link, socket } = setup(false);
    vi.advanceTimersByTime(49);
    socket.open();
    socket.reply();
    socket.bufferedAmount = 1;
    vi.advanceTimersByTime(51);
    expect(link.state).toBe("disconnected");
    expect(socket.sent).toHaveLength(1);
  });

  it("checks permit expiry immediately even when a drive command was just sent", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.setSystemTime(199);
    link.tick();
    expect(socket.sent.at(-1)?.deadman).toBe(true);
    vi.setSystemTime(200);
    link.tick();
    expect(link.inputStatus).toBe("release-to-arm");
    expect(socket.sent.at(-1)).toMatchObject({ deadman: false, v: 0, w: 0 });
  });

  it("checks XR tracking expiry immediately even when a drive command was just sent", () => {
    const { link, socket } = setup();
    socket.reply();
    const source = xrSource();
    const session = new Session(source);
    link.sampleXR(session.session, 0);
    source.gamepad.buttons[0].pressed = true;
    source.gamepad.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.setSystemTime(149);
    link.tick();
    expect(socket.sent.at(-1)?.deadman).toBe(true);
    vi.setSystemTime(150);
    link.tick();
    expect(link.inputStatus).toBe("release-to-arm");
    expect(socket.sent.at(-1)).toMatchObject({ deadman: false, v: 0, w: 0 });
  });

  it("keeps the link disconnected if a late acknowledgement triggers a stale stop on a backed-up socket", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    socket.bufferedAmount = 1;
    vi.setSystemTime(500); // Simulate a blocked browser before its next timer runs.
    socket.reply();
    expect(link.state).toBe("disconnected");
    expect(link.inputStatus).toBe("release-to-arm");
    expect(socket.readyState).toBe(3);
    expect(socket.sent).toHaveLength(2);
  });

  it("neutralizes drive at 500 ms without fresh replies and requires a neutral rearm", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(500);
    expect(link.state).toBe("stale");
    expect(socket.sent.at(-1)).toMatchObject({ deadman: false, v: 0, w: 0 });
    socket.reply();
    link.setVideoFrame("after-link-loss", 0, Date.now());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set(["KeyW"])); // Direction still held is not neutral.
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, v: 1 });
  });

  it("does not let malformed, future or repeated old drive replies hide link loss", () => {
    const { link, socket, replies } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(100);
    socket.reply({
      seq: 2,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    vi.advanceTimersByTime(300);
    socket.reply({
      seq: 2,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    socket.reply({
      seq: 9999,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    socket.reply({
      seq: 8,
      range_cm: "64",
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    vi.advanceTimersByTime(200);
    expect(replies).toHaveLength(2);
    expect(link.state).toBe("stale");
    expect(socket.sent.at(-1)?.deadman).toBe(false);
  });

  it("rejects an old drive acknowledgement after a stale stop instead of treating it as recovery", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    const oldReply = {
      seq: 2,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    };
    socket.reply(oldReply);
    vi.advanceTimersByTime(500);
    expect(link.state).toBe("stale");
    socket.reply(oldReply);
    expect(link.state).toBe("stale");
    socket.reply();
    expect(link.state).toBe("healthy");
  });

  it("never resumes held keys after an explicit socket reconnect", () => {
    const { link, socket, sockets } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    socket.close();
    expect(link.state).toBe("disconnected");
    vi.advanceTimersByTime(100);
    expect(sockets).toHaveLength(1);
    link.connect();
    const next = sockets[1];
    next.open();
    next.reply();
    next.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(next.sent.every((command) => !command.deadman)).toBe(true);
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyS", "KeyA"]));
    vi.advanceTimersByTime(50);
    expect(next.sent.at(-1)).toMatchObject({ deadman: true, v: -1, w: -1 });
  });

  it("makes stop clear local intent until a neutral input is observed", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    link.stop("blur");
    expect(socket.sent.at(-1)).toMatchObject({ deadman: false, v: 0, w: 0 });
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
  });

  it("reads the right xr-standard thumbstick and trigger with a deadzone", () => {
    const { link, socket } = setup();
    socket.reply();
    const session = new Session();
    link.sampleXR(session.session, 0);
    session.inputSources[0].gamepad!.axes[2] = 0.1;
    session.inputSources[0].gamepad!.axes[3] = -0.15;
    session.inputSources[0].gamepad!.buttons[0].pressed = true;
    session.inputSources[0].gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, v: 0, w: 0 });
    session.inputSources[0].gamepad!.axes[0] = -1; // A touchpad cannot override the stick.
    session.inputSources[0].gamepad!.axes[1] = 1;
    session.inputSources[0].gamepad!.axes[2] = 1;
    session.inputSources[0].gamepad!.axes[3] = -1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, v: 1, w: 1 });
  });

  it("ignores unknown mappings and missing thumbstick axes rather than guessing", () => {
    const { link, socket } = setup();
    socket.reply();
    const session = new Session(xrSource([1, -1], true, ""));
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.every((command) => !command.deadman)).toBe(true);
    session.inputSources = [xrSource([1, -1], true)];
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.every((command) => !command.deadman)).toBe(true);
    const source = xrSource();
    session.inputSources = [source];
    link.sampleXR(session.session, 0);
    source.gamepad.axes[3] = -1;
    source.gamepad.buttons[0].pressed = true;
    source.gamepad.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, v: 1 });
  });

  it("stops for nonfinite axes, disconnected controllers and XR focus loss", () => {
    const { link, socket } = setup();
    socket.reply();
    const source = xrSource();
    const session = new Session(source);
    link.sampleXR(session.session, 0);
    source.gamepad.buttons[0].pressed = true;
    source.gamepad.buttons[0].value = 1;
    source.gamepad.axes[3] = -1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    source.gamepad.axes[3] = NaN;
    link.sampleXR(session.session, 0);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    source.gamepad.axes[3] = 0;
    source.gamepad.buttons[0].pressed = false;
    source.gamepad.buttons[0].value = 0;
    source.gamepad.connected = false;
    link.sampleXR(session.session, 0);
    source.gamepad.connected = true;
    source.gamepad.buttons[0].pressed = true;
    source.gamepad.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false); // A vanished controller requires neutral first.
    source.gamepad.buttons[0].pressed = false;
    source.gamepad.buttons[0].value = 0;
    link.sampleXR(session.session, 0);
    source.gamepad.buttons[0].pressed = true;
    source.gamepad.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(true);
    session.visibilityState = "visible-blurred";
    session.dispatchEvent(new Event("visibilitychange"));
    expect(socket.sent.at(-1)?.deadman).toBe(false);
  });

  it("can establish the initial yaw after tracking was unavailable on the first XR sample", () => {
    const { link, socket } = setup();
    socket.reply();
    const source = xrSource();
    const session = new Session(source);
    link.sampleXR(session.session, NaN);
    link.sampleXR(session.session, 1);
    source.gamepad.buttons[0].pressed = true;
    source.gamepad.buttons[0].value = 1;
    link.sampleXR(session.session, 1);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: true, pan: 0 });
  });

  it("clears drive intent when send fails and ignores late events from that socket", () => {
    const { link, socket, sockets } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    socket.send = () => {
      throw new Error("network failure");
    };
    vi.advanceTimersByTime(50);
    expect(link.state).toBe("disconnected");
    expect(link.inputStatus).toBe("release-to-arm");
    socket.reply();
    expect(link.state).toBe("disconnected");
    link.connect();
    const next = sockets[1];
    next.open();
    next.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(next.sent.at(-1)?.deadman).toBe(false);
  });

  it("stops immediately when the XR trigger is released or the right controller disappears", () => {
    const { link, socket } = setup();
    socket.reply();
    const source = xrSource();
    const session = new Session(source);
    link.sampleXR(session.session, 0);
    source.gamepad!.axes[3] = -1;
    source.gamepad!.buttons[0].pressed = true;
    source.gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    source.gamepad!.buttons[0].pressed = false;
    source.gamepad!.buttons[0].value = 0;
    link.sampleXR(session.session, 0);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    source.gamepad!.buttons[0].pressed = true;
    source.gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(true);
    session.inputSources = [];
    link.sampleXR(session.session, 0);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
  });

  it("stops when XR frames cease or the XR session ends", () => {
    const { link, socket } = setup();
    socket.reply();
    const session = new Session();
    link.sampleXR(session.session, 0);
    session.inputSources[0].gamepad!.axes[3] = -1;
    session.inputSources[0].gamepad!.buttons[0].pressed = true;
    session.inputSources[0].gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(200);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    socket.reply();
    session.inputSources[0].gamepad!.axes[3] = 0;
    session.inputSources[0].gamepad!.buttons[0].pressed = false;
    session.inputSources[0].gamepad!.buttons[0].value = 0;
    link.sampleXR(session.session, 0);
    session.inputSources[0].gamepad!.buttons[0].pressed = true;
    session.inputSources[0].gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(true);
    session.dispatchEvent(new Event("end"));
    expect(socket.sent.at(-1)?.deadman).toBe(false);
  });

  it("centres pan on session facing, smooths head turns, and clamps camera yaw", () => {
    const { link, socket } = setup();
    socket.reply();
    const session = new Session();
    link.sampleXR(session.session, 1);
    vi.advanceTimersByTime(50);
    session.inputSources[0].gamepad!.buttons[0].pressed = true;
    session.inputSources[0].gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, 0);
    vi.advanceTimersByTime(50);
    const firstPan = socket.sent.at(-1)!.pan;
    expect(firstPan).toBeGreaterThan(0);
    expect(firstPan).toBeLessThan(57.3);
    for (let step = 0; step < 12; step++) {
      socket.reply();
      link.setVideoFrame(`pan-${step}`, 0, Date.now());
      link.sampleXR(session.session, -2);
      vi.advanceTimersByTime(50);
    }
    expect(socket.sent.at(-1)!.pan).toBeGreaterThan(79);
    expect(socket.sent.every(({ pan }) => Math.abs(pan) <= 80)).toBe(true);
  });

  it("uses the shortest relative yaw across the plus/minus pi boundary", () => {
    const { link, socket } = setup();
    socket.reply();
    const session = new Session();
    link.sampleXR(session.session, Math.PI - 0.05);
    vi.advanceTimersByTime(50);
    session.inputSources[0].gamepad!.buttons[0].pressed = true;
    session.inputSources[0].gamepad!.buttons[0].value = 1;
    link.sampleXR(session.session, -Math.PI + 0.05);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)!.pan).toBeLessThan(0);
    expect(socket.sent.at(-1)!.pan).toBeGreaterThan(-6);
  });

  it("sends a final neutral and removes the pump when disposed", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    link.dispose();
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    expect(link.state).toBe("disposed");
    const count = socket.sent.length;
    vi.advanceTimersByTime(1000);
    link.connect();
    expect(socket.sent).toHaveLength(count);
  });

  it("accepts initial seq minus one and nullable telemetry, then echoes the latest session and permit", () => {
    const { link, socket } = setup(false);
    socket.open();
    socket.reply(socket.status({ range_cm: null, link_ms: null }));
    expect(link.reply).toMatchObject({
      seq: -1,
      range_cm: null,
      link_ms: null,
      rover: true,
    });
    expect(link.state).toBe("healthy");
    armKeyboard(link, socket);
    const next = socket.status();
    socket.reply(next);
    link.setKeyboard(new Set(["Space", "KeyS"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({
      session_id: "session-one",
      permit: next.permit,
      deadman: true,
      v: -1,
      video_fresh: true,
      video_age_ms: 50,
    });
  });

  it("does not authorize legacy replies or missing displayed-frame metadata", () => {
    const { link, socket } = setup(false, () => Date.now(), false);
    socket.open();
    socket.reply({
      seq: 1,
      range_cm: 64,
      estop: false,
      mode: "teleop",
      link_ms: 38,
    });
    expect(link.state).toBe("waiting");
    expect(socket.sent).toHaveLength(0);
    armKeyboard(link, socket);
    socket.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.every((command) => !command.deadman)).toBe(true);
    link.setVideoFrame("displayed-first", 20, Date.now());
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({
      deadman: true,
      video_age_ms: 70,
    });
  });

  it("expires permits at 200 ms and never resumes held intent when fresh permits recover", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(200);
    expect(socket.sent.at(-1)).toMatchObject({ deadman: false, v: 0, w: 0 });
    expect(link.stopReason).toContain("permit");
    socket.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(true);
  });

  it("accepts new permits with an unchanged acknowledgement but rejects replayed permit freshness", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    const fresh = socket.status();
    vi.advanceTimersByTime(150);
    socket.reply(fresh);
    expect(link.replyReceivedAt).toBe(150);
    vi.advanceTimersByTime(150);
    socket.reply(fresh);
    expect(link.replyReceivedAt).toBe(150);
    vi.advanceTimersByTime(150);
    expect(link.state).toBe("stale");
  });

  it("stops when server rover readiness or release-required status changes", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    socket.reply(
      socket.status({
        rover: false,
        requires_release: true,
        range_cm: null,
        reason: "serial_disconnected",
      }),
    );
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    socket.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set());
    socket.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(true);
    socket.reply(
      socket.status({ requires_release: true, reason: "lease_expired" }),
    );
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    expect(link.inputStatus).toBe("release-to-arm");
  });

  it("requires neutral input after a new server session and uses no previous permit", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    socket.sessionId = "session-two";
    socket.reply(
      socket.status({
        seq: -1,
        requires_release: true,
        reason: "release_required",
      }),
    );
    expect(socket.sent.at(-1)).toMatchObject({
      deadman: false,
      session_id: "session-two",
    });
    socket.reply();
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(false);
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({
      deadman: true,
      session_id: "session-two",
    });
  });

  it("counts server capture age and rejects duplicate frames as freshness renewal", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setVideoFrame("aged-displayed", 400, 0);
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({
      deadman: true,
      video_age_ms: 450,
    });
    link.setVideoFrame("aged-displayed", 0, 50);
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)).toMatchObject({
      deadman: false,
      v: 0,
      w: 0,
      video_fresh: false,
    });
    expect(link.stopReason).toContain("video");
  });

  it("permits neutral release after the motion permit expires", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    vi.setSystemTime(250);
    link.stop("operator release");
    expect(socket.sent.at(-1)).toMatchObject({
      deadman: false,
      session_id: "session-one",
      v: 0,
      w: 0,
    });
  });

  it("sends another ordered neutral after a serial reconnect even when it was already idle", () => {
    const { link, socket } = setup();
    armKeyboard(link, socket);
    socket.reply(
      socket.status({
        rover: false,
        requires_release: true,
        reason: "serial_disconnected",
      }),
    );
    socket.reply(
      socket.status({
        rover: true,
        requires_release: true,
        reason: "release_required",
      }),
    );
    link.setKeyboard(new Set());
    expect(socket.sent.at(-1)).toMatchObject({
      seq: 2,
      deadman: false,
      session_id: "session-one",
    });
    socket.reply();
    link.setKeyboard(new Set());
    link.setKeyboard(new Set(["Space", "KeyW"]));
    vi.advanceTimersByTime(50);
    expect(socket.sent.at(-1)?.deadman).toBe(true);
  });
});
